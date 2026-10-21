"""Compare loose chapter images with CopyManga page lists, then delete only approved rows."""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from copy_manga_update import (BASE, Client, Chapter, local_volume_number,
                               parse_preferred_groups)
from library_audit import EXTENSIONS, chapter_number


COPY_MANIFEST = '.copy-manga-update.json'
AIDOKU_MANIFEST = '.aidoku-conversion.json'


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    return value


def _numbered_images(folder: Path):
    images = sorted((item for item in folder.iterdir()
                     if item.is_file() and item.suffix.casefold() in EXTENSIONS),
                    key=lambda item: item.name.casefold())
    if not images:
        return images, [], '没有找到散图'
    if any(not image.stem.isdecimal() for image in images):
        return images, [], '存在非纯数字图片名，无法可靠对应网站页序'
    numbers = [int(image.stem) for image in images]
    duplicate = sorted(number for number, count in Counter(numbers).items() if count > 1)
    if duplicate:
        return images, numbers, '存在重复页码：' + format_pages(duplicate)
    return images, numbers, ''


def _folder_identity(folder: Path):
    """Return (chapter id, slug) only when a manifest provides trustworthy evidence."""
    copy_manifest = folder / COPY_MANIFEST
    if copy_manifest.is_file():
        data = _read_json(copy_manifest, {})
        chapter_id, slug = data.get('chapter_id'), data.get('slug')
        if isinstance(chapter_id, str) and isinstance(slug, str):
            return chapter_id, slug
    aidoku_manifest = folder / AIDOKU_MANIFEST
    if aidoku_manifest.is_file():
        data = _read_json(aidoku_manifest, {})
        source = data.get('source')
        if isinstance(source, str):
            archive = Path(source)
            if (re.fullmatch(r'[0-9a-f-]{36}', archive.stem, re.I)
                    and re.fullmatch(r'[a-z0-9_-]+', archive.parent.name, re.I)):
                return archive.stem, archive.parent.name
    return None, None


def _folder_kind_number(folder: Path):
    number = chapter_number(folder.name)
    if number is not None:
        return 'chapter', number
    number = local_volume_number(folder.name)
    if number is not None:
        return 'volume', number
    return None, None


def _snapshot(path: Path):
    rows = []
    for item in sorted(path.iterdir(), key=lambda value: value.name.casefold()):
        stat = item.stat(follow_symlinks=False)
        rows.append((item.name, item.is_dir(), stat.st_size, stat.st_mtime_ns))
    return rows


def _archive_for(folder: Path):
    matches = [item for item in folder.parent.iterdir()
               if item.is_file() and item.suffix.casefold() == '.zip'
               and item.stem.casefold() == folder.name.casefold()]
    if len(matches) > 1:
        raise ValueError('存在多个同名 ZIP，停止自动处理')
    return matches[0] if matches else None


def format_pages(numbers):
    numbers = sorted(set(int(number) for number in numbers))
    ranges = []
    start = previous = None
    for number in numbers + [None]:
        if start is None:
            start = previous = number
        elif number is not None and number == previous + 1:
            previous = number
        else:
            ranges.append(f'{start:03d}' if start == previous else f'{start:03d}–{previous:03d}')
            start = previous = number
    return '、'.join(ranges)


def _chapter_for(folder: Path, catalog: list[Chapter], slug: str):
    chapter_id, manifest_slug = _folder_identity(folder)
    if manifest_slug and manifest_slug != slug:
        return None, '章节清单的网址与漫画配置不一致'
    if chapter_id:
        matches = [chapter for chapter in catalog if chapter.id.casefold() == chapter_id.casefold()]
        if len(matches) == 1:
            return matches[0], ''
        return None, '章节清单 ID 不在当前网站目录中'
    kind, number = _folder_kind_number(folder)
    if number is None:
        return None, '目录名无法识别为话或卷'
    matches = [chapter for chapter in catalog
               if chapter.kind == kind and chapter.number == number]
    if len(matches) != 1:
        return None, '无法用类型和编号唯一对应网站章节'
    return matches[0], ''


def _preferred_groups(preflight: dict, series: Path):
    row = preflight.get(str(series), {})
    if not isinstance(row, dict):
        return ()
    return parse_preferred_groups(row.get('preferred_groups', ()))


def plan_source_page_audit(library: Path, mapping_file: Path, preflight_file: Path | None = None,
                           client=None, cancelled=lambda: False, progress=lambda _: None):
    """Read-only website comparison. Network uncertainty is always a skip, never a candidate."""
    library = Path(library).resolve()
    if not library.is_dir() or library.is_symlink():
        raise ValueError('漫画书库不存在或是链接目录')
    mappings = _read_json(Path(mapping_file), {})
    if not isinstance(mappings, dict):
        raise ValueError('copy_sources.json 必须是目录到网址的对象')
    preflight = _read_json(Path(preflight_file), {}) if preflight_file else {}
    if not isinstance(preflight, dict):
        preflight = {}
    normalized = {str(Path(path).resolve()).casefold(): slug for path, slug in mappings.items()
                  if isinstance(path, str) and isinstance(slug, str)}
    client = client or Client(cancelled=cancelled, on_retry=progress)
    candidates, skipped = [], []
    series_seen = 0
    series_list = [series for series in sorted(library.iterdir(), key=lambda item: item.name.casefold())
                   if series.is_dir() and not series.is_symlink() and not series.name.startswith('.')]
    mapped_folders = sum(
        sum(folder.is_dir() and not folder.is_symlink() and not folder.name.startswith('.')
            for folder in series.iterdir())
        for series in series_list
        if str(series.resolve()).casefold() in normalized)
    checked_folders = 0
    for series in series_list:
        if cancelled():
            break
        slug = normalized.get(str(series.resolve()).casefold())
        folders = [folder for folder in sorted(series.iterdir(), key=lambda item: item.name.casefold())
                   if folder.is_dir() and not folder.is_symlink() and not folder.name.startswith('.')]
        if not slug:
            skipped.append({'series': series.name, 'chapter': '', 'reason': '没有对应网址，整部跳过'})
            continue
        series_seen += 1
        preferred_ids = set()
        for folder in folders:
            chapter_id, manifest_slug = _folder_identity(folder)
            if chapter_id and (not manifest_slug or manifest_slug == slug):
                preferred_ids.add(chapter_id)
        progress(f'读取网站目录：{checked_folders}/{mapped_folders}｜{series.name}')
        try:
            catalog = client.catalog(slug, preferred_ids=preferred_ids,
                                     preferred_groups=_preferred_groups(preflight, series))
        except Exception as exc:
            skipped.append({'series': series.name, 'chapter': '',
                            'reason': '网站目录读取失败，整部跳过：' + str(exc)})
            continue
        for folder in folders:
            if cancelled():
                break
            checked_folders += 1
            progress(f'核对网页页数：{checked_folders}/{mapped_folders}｜{series.name} / {folder.name}')
            try:
                images, numbers, unsafe = _numbered_images(folder)
                if unsafe:
                    raise ValueError(unsafe)
                chapter, unsafe = _chapter_for(folder, catalog, slug)
                if unsafe:
                    raise ValueError(unsafe)
                urls = client.pages(slug, chapter)
                expected = len(urls)
                outside = sorted(number for number in numbers if number < 1 or number > expected)
                if outside:
                    raise ValueError('本地存在超出网站页数的页码：' + format_pages(outside))
                missing = sorted(set(range(1, expected + 1)) - set(numbers))
                if not missing:
                    continue
                archive = _archive_for(folder)
                candidates.append({
                    'selected': False,
                    'series': series.name,
                    'chapter': folder.name,
                    'folder': str(folder),
                    'archive': str(archive) if archive else '',
                    'archive_snapshot': ([archive.stat().st_size, archive.stat().st_mtime_ns]
                                         if archive else None),
                    'slug': slug,
                    'chapter_id': chapter.id,
                    'kind': chapter.kind,
                    'number': str(chapter.number),
                    'url': f'{BASE}/comic/{slug}/chapter/{chapter.id}',
                    'website_pages': expected,
                    'local_pages': len(images),
                    'missing_pages': missing,
                    'missing_text': format_pages(missing),
                    'folder_snapshot': _snapshot(folder),
                })
            except Exception as exc:
                skipped.append({'series': series.name, 'chapter': folder.name,
                                'reason': str(exc)})
    return {'library': str(library), 'cancelled': cancelled(), 'mapped_series': series_seen,
            'candidates': candidates, 'skipped': skipped}


def _write_log(path: Path, value):
    with path.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2))
        stream.flush()
        os.fsync(stream.fileno())


def apply_source_page_deletions(rows: list[dict], library: Path, log_dir: Path,
                                progress=lambda _: None):
    """Permanently delete selected snapshots after strict path and state revalidation."""
    library = Path(library).resolve()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / ('source-page-delete-' + time.strftime('%Y%m%d-%H%M%S')
                          + f'-{time.time_ns() % 1000000:06d}.json')
    journal = [{'series': row['series'], 'chapter': row['chapter'], 'folder': row['folder'],
                'archive': row.get('archive', ''), 'missing_pages': row['missing_pages'],
                'url': row['url'], 'status': '已计划'} for row in rows]
    _write_log(log_path, journal)
    for position, (item, row) in enumerate(zip(journal, rows), 1):
        folder = Path(row['folder'])
        archive = Path(row['archive']) if row.get('archive') else None
        staged_folder = staged_archive = None
        try:
            resolved = folder.resolve()
            if (not folder.is_dir() or folder.is_symlink()
                    or not resolved.is_relative_to(library)
                    or resolved.parent.parent != library):
                raise ValueError('散图目录路径状态变化或不在书库的章节层级')
            if _snapshot(folder) != [tuple(value) for value in row['folder_snapshot']]:
                raise ValueError('检查后散图目录内容已变化，拒绝删除')
            exact_archive = _archive_for(folder)
            if archive:
                if exact_archive is None or exact_archive.resolve() != archive.resolve():
                    raise ValueError('对应 ZIP 路径已变化，拒绝删除')
                stat = archive.stat()
                if [stat.st_size, stat.st_mtime_ns] != row['archive_snapshot']:
                    raise ValueError('检查后对应 ZIP 已变化，拒绝删除')
            elif exact_archive is not None:
                raise ValueError('检查后出现了对应 ZIP，拒绝删除')
            token = uuid.uuid4().hex
            staged_folder = folder.with_name(f'.page-audit-delete-{token}-folder')
            folder.rename(staged_folder)
            if archive:
                staged_archive = archive.with_name(f'.page-audit-delete-{token}.zip')
                try:
                    archive.rename(staged_archive)
                except Exception:
                    staged_folder.rename(folder)
                    staged_folder = None
                    raise
            item['status'] = '已隔离，正在删除'
            _write_log(log_path, journal)
            progress(f'删除 {position}/{len(rows)}：{row["series"]} / {row["chapter"]}')
            if staged_archive:
                staged_archive.unlink()
                staged_archive = None
            shutil.rmtree(staged_folder)
            staged_folder = None
            item['status'] = '已删除'
        except Exception as exc:
            item['status'] = '失败'
            item['error'] = str(exc)
            if staged_folder is not None and staged_folder.exists():
                item['隔离目录'] = str(staged_folder)
            if staged_archive is not None and staged_archive.exists():
                item['隔离ZIP'] = str(staged_archive)
        _write_log(log_path, journal)
    return journal, log_path
