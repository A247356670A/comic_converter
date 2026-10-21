"""Read-only library inspection and explicit, journaled chapter renames."""
from collections import Counter
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import time
import warnings
from PIL import Image

EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.avif'}
PATTERN = re.compile(r'^第\s*(\d+(?:\.\d+)?)\s*[话話](.*)$')


def chapter_number(name):
    m = PATTERN.fullmatch(name.strip())
    return Decimal(m[1]) if m else None


def sortable_name(name):
    m = PATTERN.fullmatch(name.strip())
    if not m:
        return name
    number = Decimal(m[1])
    if number >= 10000 or number.as_tuple().exponent < -3:
        raise ValueError('仅自动处理 0–9999 话和最多三位小数，其余请手动命名')
    return f'第{number:08.3f}话{m[2]}'


def gaps(numbers, start=None):
    numbers = sorted(set(numbers))
    ranges = []
    if start is not None and numbers and numbers[0] > start:
        ranges.append(str(start) if numbers[0] == start + 1 else f'{start}–{numbers[0] - 1}')
    ranges.extend(str(a + 1) if b == a + 2 else f'{a + 1}–{b - 1}'
                  for a, b in zip(numbers, numbers[1:]) if b - a > 1)
    # The final chapter/page cannot be inferred without an external catalogue.
    return ranges


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def inspect_chapter(path, deep=False, stop=lambda: False, progress=lambda _: None):
    result = {'name': path.name, 'path': str(path), 'pages': 0, 'issues': [],
              'number': str(chapter_number(path.name)) if chapter_number(path.name) is not None else None,
              'mode': '完整解码' if deep else '快速检查', 'cancelled': False}
    issues = result['issues']
    try:
        entries = list(path.iterdir())
        pages = sorted(p for p in entries if p.suffix.lower() in EXTENSIONS and p.is_file())
        result['pages'] = len(pages)
        if any(p.is_dir() for p in entries):
            issues.append('含子目录：仅检查当前层图片，请核对嵌套结构')
        archives = [p.name for p in entries if p.suffix.lower() in {'.cbz', '.zip', '.cbr', '.pdf'}]
        if archives:
            issues.append('含压缩包/PDF：本页检查针对散图，未验证这些文件')
        if not pages:
            issues.append('空章节或未找到支持的图片')
        numbered = [int(p.stem) for p in pages if p.stem.isdecimal()]
        if len(numbered) == len(pages) and pages:
            missing = gaps(numbered, start=1)
            if missing:
                issues.append('疑似缺页（编号间隙）：' + ', '.join(missing))
            duplicates = [str(n) for n, count in Counter(numbered).items() if count > 1]
            if duplicates:
                issues.append('重复页码：' + ', '.join(duplicates))
        elif pages:
            issues.append('部分页名不是纯数字，无法可靠判断缺号')
        for index, page in enumerate(pages, 1):
            if stop():
                result['cancelled'] = True
                break
            progress(f'{path.parent.name} / {path.name}：图片 {index}/{len(pages)}')
            try:
                if page.is_symlink() or not page.resolve().is_relative_to(path.resolve()):
                    raise ValueError('跳过链接文件')
                if page.stat().st_size == 0:
                    raise ValueError('零字节图片')
                if deep:
                    with warnings.catch_warnings():
                        warnings.simplefilter('error', Image.DecompressionBombWarning)
                        with Image.open(page) as im:
                            im.verify()
                        with Image.open(page) as im:
                            for i in range(getattr(im, 'n_frames', 1)):
                                im.seek(i)
                                im.load()
            except Exception as exc:
                issues.append(f'图片异常 {page.name}：{exc}')
        manifest = path / '.aidoku-conversion.json'
        if manifest.exists() and not result['cancelled']:
            data = json.loads(manifest.read_text(encoding='utf-8'))
            expected = data['pages']
            if not isinstance(expected, dict):
                raise ValueError('转换清单 pages 不是对象')
            for name, checksum in expected.items():
                if stop():
                    result['cancelled'] = True
                    break
                if not re.fullmatch(r'\d+\.png', name):
                    issues.append('清单包含非法页名')
                    continue
                page = path / name
                if not page.is_file():
                    issues.append('确认缺页（转换清单）：' + name)
                elif deep and sha(page) != checksum:
                    issues.append('内容变化/损坏（校验不一致）：' + name)
        try:
            result['proposed'] = sortable_name(path.name)
        except ValueError as exc:
            result['proposed'] = path.name
            issues.append(str(exc))
    except Exception as exc:
        issues.append('读取失败：' + str(exc))
    return result


def inspect_series(path, deep=False, stop=lambda: False, progress=lambda _: None):
    result = {'name': path.name, 'path': str(path), 'chapters': [], 'issues': [], 'latest': '未知', 'cancelled': False}
    try:
        entries = sorted(path.iterdir(), key=lambda p: p.name)
        if any(p.is_file() and p.suffix.lower() in EXTENSIONS | {'.cbz', '.zip', '.cbr', '.pdf'} for p in entries):
            result['issues'].append('漫画根目录含散图/压缩包：未作为章节目录检查')
        for chapter in entries:
            if stop():
                result['cancelled'] = True
                break
            if not chapter.is_dir() or chapter.name.startswith('.'):
                continue
            if chapter.is_symlink() or not chapter.resolve().is_relative_to(path.resolve()):
                result['issues'].append('跳过链接目录：' + chapter.name)
                continue
            progress(f'{path.name} / {chapter.name}')
            result['chapters'].append(inspect_chapter(chapter, deep, stop, progress))
        result['cancelled'] |= any(c['cancelled'] for c in result['chapters'])
        numbered = [c for c in result['chapters'] if c['number'] is not None]
        if numbered:
            result['latest'] = max(numbered, key=lambda c: Decimal(c['number']))['name']
        normal = [Decimal(c['number']) for c in numbered if not PATTERN.fullmatch(c['name'])[2].strip()]
        missing = gaps([int(n) for n in normal if n == int(n)], start=1)
        if missing:
            result['issues'].append('疑似缺话（从第1话起的纯数字正篇编号间隙）：' + ', '.join(missing))
        if not result['chapters']:
            result['issues'].append('没有章节目录')
        unknown = sum(c['number'] is None for c in result['chapters'])
        if unknown:
            result['issues'].append(f'{unknown} 个章节无法识别话数（可能为番外/卷名）')
    except Exception as exc:
        result['issues'].append('读取失败：' + str(exc))
    return result


def scan_library(root, deep=False, stop=lambda: False, on_series=lambda _: None, progress=lambda _: None):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('书库目录不存在')
    results = []
    for series in sorted(root.iterdir(), key=lambda p: p.name):
        if stop():
            break
        if series.is_dir() and not series.name.startswith('.'):
            if series.is_symlink() or not series.resolve().is_relative_to(root):
                continue
            row = inspect_series(series, deep, stop, progress)
            results.append(row)
            on_series(row)
    return {'root': str(root), 'deep': deep, 'cancelled': stop(), 'series': results,
            'note': '缺号仅是线索；无来源页数或清单不能确认首尾缺页。完整解码不能证明内容完整。'}


def rename_plan(series):
    parent = Path(series['path'])
    plan, targets = [], set()
    for chapter in series['chapters']:
        src = parent / chapter['name']
        proposed = sortable_name(chapter['name'])
        if proposed == chapter['name']:
            continue
        dst = parent / proposed
        key = proposed.casefold()
        error = '目标已存在或重复' if dst.exists() or key in targets else ''
        targets.add(key)
        plan.append({'source': str(src), 'target': str(dst), 'error': error})
    return plan


def apply_renames(plan, journal_dir):
    if not plan or any(r['error'] for r in plan):
        raise ValueError('计划为空或包含冲突，请先重新扫描')
    for row in plan:
        src, dst = Path(row['source']), Path(row['target'])
        if src.parent != dst.parent or src.is_symlink() or not src.is_dir() or dst.exists():
            raise ValueError('路径状态已改变，请重新扫描')
    journal_dir = Path(journal_dir)
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal = journal_dir / f'rename-{time.time_ns()}.jsonl'
    # A durable intent precedes each rename; completion follows it. No replacement.
    with journal.open('x', encoding='utf-8') as log:
        for row in plan:
            def record(status, **extra):
                log.write(json.dumps(dict(row, status=status, **extra), ensure_ascii=False) + '\n')
                log.flush()
                os.fsync(log.fileno())
            record('planned')
            try:
                src, dst = Path(row['source']), Path(row['target'])
                if dst.exists():
                    raise FileExistsError(str(dst))
                src.rename(dst)
            except Exception as exc:
                record('failed', message=str(exc))
                raise RuntimeError(f'重命名中断；已完成项保留，恢复记录：{journal}；{exc}') from exc
            record('completed')
    return journal
