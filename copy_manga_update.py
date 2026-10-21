"""Update explicitly mapped Komga series from CopyManga's public web pages."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import queue
import re
import shutil
from urllib.error import HTTPError, URLError
import tempfile
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import warnings

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from PIL import Image, ImageOps

from library_audit import chapter_number, sortable_name
from progress_ui import TaskProgress, estimate_remaining, format_duration


BASE = 'https://www.copy20.com'
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
SLUG = re.compile(r'^[a-z0-9_-]+$', re.I)
MAX_HTML = 4 * 1024 * 1024
MAX_CATALOG = 12 * 1024 * 1024
MAX_IMAGE = 100 * 1024 * 1024
MANIFEST = '.copy-manga-update.json'
VOLUME = re.compile(r'^第\s*0*(\d+)\s*卷$', re.I)
NUMERIC_VOLUME = re.compile(r'^0*(\d+)$')


class AccessProblem(RuntimeError):
    """A site response is restricted, incomplete, or inconsistent."""


class TemporaryNetworkProblem(RuntimeError):
    """A transient HTTP/network failure remained after bounded retries."""


class IncompleteCatalog(AccessProblem):
    """A catalog response declared more entries than it actually contained."""


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = False
        self.header = False
        self.header_parts = []
        self.dnt = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'span' and attrs.get('id') == 'dnt':
            self.dnt = attrs.get('value')
        if tag == 'h4' and 'header' in attrs.get('class', '').split():
            self.header = True

    def handle_endtag(self, tag):
        if tag == 'h4':
            self.header = False

    def handle_data(self, data):
        if self.header:
            self.header_parts.append(data)


def parse_slug(value: str) -> str:
    value = value.strip()
    if value.startswith('https://'):
        parsed = urlparse(value)
        if parsed.hostname != 'www.copy20.com' or parsed.username or parsed.password:
            raise ValueError('只接受 www.copy20.com 漫画详情页链接')
        match = re.fullmatch(r'/comic/([a-z0-9_-]+)/?', parsed.path, re.I)
        if not match:
            raise ValueError('请输入漫画详情页链接，不是单话链接')
        value = match[1]
    if not SLUG.fullmatch(value):
        raise ValueError('漫画标识只允许字母、数字、下划线和连字符')
    return value


def decrypt_web(value: str, key: str):
    if not isinstance(value, str) or len(value) < 48 or not re.fullmatch(r'[0-9a-f]+', value[16:], re.I):
        raise AccessProblem('网站返回的加密数据格式异常')
    try:
        plain = unpad(AES.new(key.encode('utf-8'), AES.MODE_CBC, value[:16].encode('utf-8'))
                      .decrypt(bytes.fromhex(value[16:])), 16)
        return json.loads(plain)
    except (ValueError, KeyError, UnicodeDecodeError) as exc:
        raise AccessProblem('网站目录/页面无法解读，可能触发了访问限制') from exc


def script_value(html: str, name: str) -> str:
    found = re.search(r'\b' + re.escape(name) + r'\s*=\s*[\'"]([^\'"]+)', html)
    if not found:
        raise AccessProblem(f'网页缺少 {name}，可能是验证页或页面结构变化')
    return found[1]


def chapter_title(raw: str, number: Decimal) -> str:
    # The site sometimes serves mojibake; retain only titles which parse cleanly.
    if '�' not in raw and chapter_number(raw) == number:
        return sortable_name(raw)
    return sortable_name(f'第{number}话')


def volume_title(number: Decimal) -> str:
    if number != number.to_integral_value() or number < 0 or number >= 1000:
        raise AccessProblem(f'卷号 {number} 无法安全转换为三位数字目录名')
    return f'{int(number):03d}'


def web_volume_number(name: str):
    """Recognize only a complete, ordinary Arabic-number volume title."""
    match = re.fullmatch(r'\s*第?\s*0*(\d+)\s*卷\s*', name)
    if not match:
        return None
    number = Decimal(match[1])
    return number if number < 1000 else None


def local_volume_number(name: str):
    match = VOLUME.fullmatch(name.strip()) or NUMERIC_VOLUME.fullmatch(name.strip())
    if not match:
        return None
    number = Decimal(match[1])
    return number if number < 1000 else None


@dataclass(frozen=True)
class Chapter:
    number: Decimal
    id: str
    title: str
    group: str
    kind: str = 'chapter'


def parse_preferred_groups(value=()):
    """Normalize persisted or in-memory (kind, group) source preferences."""
    result = set()
    for item in value or ():
        if isinstance(item, dict):
            kind, group = item.get('kind'), item.get('group')
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            kind, group = item
        else:
            raise ValueError('来源分组偏好格式无效')
        if kind not in {'chapter', 'volume'} or not isinstance(group, str) or not group:
            raise ValueError('来源分组偏好内容无效')
        result.add((kind, group))
    return frozenset(result)


def deduplicate_catalog(chapters: list[Chapter], preferred_ids=(), preferred_groups=()) -> list[Chapter]:
    preferred_ids = frozenset(preferred_ids)
    affinity = set(parse_preferred_groups(preferred_groups))
    # A downloaded Aidoku chapter is authoritative evidence for its release
    # group. Apply that group only to duplicates of the same content kind.
    affinity.update((row.kind, row.group) for row in chapters if row.id in preferred_ids)
    grouped = {}
    for chapter in chapters:
        grouped.setdefault((chapter.kind, chapter.number), []).append(chapter)
    selected = []
    for (kind, number), variants in grouped.items():
        if len(variants) == 1:
            selected.append(variants[0])
            continue
        preferred = [row for row in variants if row.id in preferred_ids]
        if len(preferred) == 1:
            selected.append(preferred[0])
            continue
        same_release = [row for row in variants if (row.kind, row.group) in affinity]
        if len(same_release) == 1:
            selected.append(same_release[0])
            continue
        if kind == 'volume':
            tankobon = [row for row in variants if row.group.casefold() == 'tankobon']
            if len(tankobon) == 1:
                selected.append(tankobon[0])
                continue
        groups = '、'.join(sorted({row.group for row in variants}))
        unit = '卷' if kind == 'volume' else '话'
        raise AccessProblem(f'网站第{number}{unit}存在无法自动选择的重复内容（分组：{groups}）')
    return selected


class Client:
    def __init__(self, delay=1.5, opener=urlopen, on_retry=lambda _: None,
                 cancelled=lambda: False, retry_delays=(20, 60, 120),
                 catalog_retry_delays=(5, 15)):
        self.delay = delay
        self.opener = opener
        self.last_request = 0.0
        self.on_retry = on_retry
        self.cancelled = cancelled
        self.retry_delays = retry_delays
        self.catalog_retry_delays = catalog_retry_delays
        self.preferred_catalog_ids = frozenset()
        self.preferred_catalog_groups = frozenset()

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            if self.cancelled():
                raise InterruptedError('用户已停止')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

    def fetch(self, url: str, limit: int, referer: str, headers=None) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username:
            raise AccessProblem('网站返回了不安全的下载地址')
        if parsed.hostname not in {'www.copy20.com'} and not parsed.hostname.endswith('.mangafunb.fun'):
            raise AccessProblem('网站返回了非预期域名的图片地址')
        request = Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141.0 Safari/537.36',
            'Referer': referer,
            'Accept-Language': 'zh-CN,zh;q=0.9',
            **(headers or {}),
        })
        for attempt in range(len(self.retry_delays) + 1):
            if self.cancelled():
                raise InterruptedError('用户已停止')
            self.wait(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                with self.opener(request, timeout=30) as response:
                    final = urlparse(response.geturl())
                    if final.scheme != 'https' or final.hostname != parsed.hostname:
                        raise AccessProblem('请求被重定向到非预期页面')
                    content = response.read(limit + 1)
                    if len(content) > limit:
                        raise AccessProblem('网站响应超出预期大小')
                    return content
            except HTTPError as exc:
                exc.close()
                if exc.code not in {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}:
                    raise AccessProblem(f'网站返回 HTTP {exc.code}，停止自动请求') from exc
                reason = f'HTTP {exc.code}'
                retry_after = exc.headers.get('Retry-After') if exc.headers else None
                extra_delay = min(120, int(retry_after)) if retry_after and retry_after.isdecimal() else 0
            except (URLError, TimeoutError, ConnectionError) as exc:
                reason = f'网络连接异常：{exc}'
                extra_delay = 0
            if attempt == len(self.retry_delays):
                raise TemporaryNetworkProblem(f'{reason}；已重试 {attempt} 次，仍无法获取内容')
            seconds = max(self.retry_delays[attempt], extra_delay)
            self.on_retry(f'{reason}；{seconds} 秒后重试（{attempt + 1}/{len(self.retry_delays)}）')
            self.wait(seconds)

    def catalog(self, slug: str, preferred_ids=(), preferred_groups=()) -> list[Chapter]:
        slug = parse_slug(slug)
        previous_ids = self.preferred_catalog_ids
        previous_groups = self.preferred_catalog_groups
        self.preferred_catalog_ids = frozenset(preferred_ids)
        self.preferred_catalog_groups = parse_preferred_groups(preferred_groups)
        try:
            for attempt in range(len(self.catalog_retry_delays) + 1):
                try:
                    return self._catalog_once(slug)
                except IncompleteCatalog as exc:
                    if attempt == len(self.catalog_retry_delays):
                        raise IncompleteCatalog(
                            f'{exc}；完整目录已重新请求 {attempt} 次，仍不一致') from exc
                    seconds = self.catalog_retry_delays[attempt]
                    self.on_retry(f'{exc}；{seconds} 秒后重新读取完整目录'
                                  f'（{attempt + 1}/{len(self.catalog_retry_delays)}）')
                    self.wait(seconds)
        finally:
            self.preferred_catalog_ids = previous_ids
            self.preferred_catalog_groups = previous_groups

    def _catalog_once(self, slug: str) -> list[Chapter]:
        detail_url = f'{BASE}/comic/{slug}'
        html = self.fetch(detail_url, MAX_HTML, BASE + '/').decode('utf-8-sig')
        parser = PageParser()
        parser.feed(html)
        if not parser.dnt or not parser.dnt.isdecimal():
            raise AccessProblem('详情页缺少目录标识，可能触发了网站限制')
        key = script_value(html, 'ccz')
        raw = self.fetch(f'{BASE}/comicdetail/{slug}/chapters', MAX_CATALOG,
                         detail_url, {'dnts': parser.dnt})
        envelope = json.loads(raw)
        if envelope.get('code') != 200 or not isinstance(envelope.get('results'), str):
            raise AccessProblem('目录接口未成功：' + str(envelope.get('message', '未知错误')))
        data = decrypt_web(envelope['results'], key)
        if data.get('build', {}).get('path_word') != slug or not isinstance(data.get('groups'), dict):
            raise AccessProblem('目录所属漫画不一致')
        chapters = []
        ids = set()
        for group_name, group in data['groups'].items():
            entries = group.get('chapters')
            if not isinstance(entries, list) or group.get('count') != len(entries):
                declared = group.get('count')
                actual = len(entries) if isinstance(entries, list) else '无列表'
                raise IncompleteCatalog(
                    f'章节目录数量不完整（{group_name}：声明 {declared}，实际 {actual}）')
            for item in entries:
                ident = item.get('id', '')
                if not UUID.fullmatch(ident) or ident in ids:
                    raise AccessProblem('章节目录包含无效或重复的 ID')
                ids.add(ident)
                name = item.get('name', '')
                item_type = item.get('type')
                if item_type not in {1, 2} or not isinstance(name, str):
                    continue  # Other special stories need manual mapping; avoid numeric false positives.
                kind = 'volume' if item_type == 2 else 'chapter'
                if kind == 'volume':
                    number = web_volume_number(name)
                    if number is None:
                        continue
                else:
                    match = re.search(r'(?<!\d)(\d+(?:\.\d+)?)(?!\d)', name)
                    if not match:
                        continue
                    number = Decimal(match[1])
                title = volume_title(number) if kind == 'volume' else chapter_title(name, number)
                chapters.append(Chapter(number, ident, title, group_name, kind))
        if not chapters:
            raise AccessProblem('目录没有可识别的正篇话数，停止更新')
        chapters = deduplicate_catalog(chapters, self.preferred_catalog_ids,
                                       self.preferred_catalog_groups)
        return sorted(chapters, key=lambda c: (c.kind != 'volume', c.number))

    def pages(self, slug: str, chapter: Chapter) -> list[str]:
        url = f'{BASE}/comic/{slug}/chapter/{chapter.id}'
        html = self.fetch(url, MAX_HTML, f'{BASE}/comic/{slug}').decode('utf-8-sig')
        parser = PageParser()
        parser.feed(html)
        header = ''.join(parser.header_parts)
        web_chapter = header.rsplit('/', 1)[-1]
        number = re.search(r'(?<!\d)\d+(?:\.\d+)?(?!\d)', web_chapter)
        if number is None or Decimal(number[0]) != chapter.number:
            raise AccessProblem(f'章节 {chapter.number} 的网页标题不匹配')
        if chapter.kind == 'volume' and '卷' not in web_chapter:
            raise AccessProblem(f'卷 {chapter.number} 的网页标题类型不匹配')
        result = decrypt_web(script_value(html, 'contentKey'), script_value(html, 'cct'))
        if not isinstance(result, list) or not result or len(result) > 1000:
            raise AccessProblem('章节图片列表为空或异常')
        urls = [item.get('url') if isinstance(item, dict) else None for item in result]
        if any(not isinstance(u, str) or not u.startswith('https://') for u in urls):
            raise AccessProblem('章节包含无效图片地址')
        return urls


def inspect_local(series: Path) -> dict[tuple[str, Decimal], Path]:
    if not series.is_dir() or series.is_symlink():
        raise ValueError('本地漫画目录不存在或是链接目录')
    local = {}
    for entry in series.iterdir():
        if entry.is_symlink():
            raise ValueError('漫画目录包含链接，暂停自动更新')
        name = entry.stem if entry.is_file() and entry.suffix.lower() in {'.cbz', '.zip'} else entry.name
        number = chapter_number(name)
        kind = 'chapter'
        if number is None:
            number = local_volume_number(name)
            kind = 'volume'
        if number is not None and (entry.is_dir() or entry.suffix.lower() in {'.cbz', '.zip'}):
            local.setdefault((kind, number), entry)
    return local


def plan_update(series: Path, catalog: list[Chapter]):
    local = inspect_local(series)
    local_chapters = [number for (kind, number) in local if kind == 'chapter']
    remote_chapters = [c.number for c in catalog if c.kind == 'chapter']
    latest = max(local_chapters) if local_chapters else None
    remote = max(remote_chapters) if remote_chapters else None
    if latest is not None and remote is not None and latest > remote:
        raise AccessProblem(f'本地最新第{latest}话比网站第{remote}话还新，可能目录受限或对应错误')
    missing = [c for c in catalog if (c.kind, c.number) not in local]
    return {'local_latest': latest, 'remote_latest': remote, 'missing': missing, 'local_count': len(local)}


def sorted_mapping_paths(mappings, plans, column, descending=False, errors=None):
    """Return table paths sorted by one visible column, with unknown values last."""
    paths = list(mappings)
    errors = errors or {}

    def value(path):
        plan = plans.get(path)
        if column == 'series':
            return Path(path).name.casefold()
        if column == 'local':
            return plan['local_latest'] if plan else None
        if column == 'remote':
            return plan['remote_latest'] if plan else None
        if column == 'missing':
            return len(plan['missing']) if plan else None
        if column == 'status':
            return ('检查失败' if path in errors else '检查完成' if plan else '待检查').casefold()
        raise ValueError(f'未知排序列：{column}')

    known = [path for path in paths if value(path) is not None]
    unknown = [path for path in paths if value(path) is None]
    known.sort(key=lambda path: (value(path), Path(path).name.casefold()), reverse=descending)
    unknown.sort(key=lambda path: Path(path).name.casefold())
    return known + unknown


def plan_volume_renames(library: Path):
    """Plan exact volume-folder normalization without touching chapter folders."""
    library = Path(library).resolve()
    if not library.is_dir() or library.is_symlink():
        raise ValueError('漫画书库不存在或是链接目录')
    rows = []
    for series in sorted(library.iterdir(), key=lambda path: path.name.casefold()):
        if not series.is_dir() or series.is_symlink():
            continue
        for source in sorted(series.iterdir(), key=lambda path: path.name.casefold()):
            if not source.is_dir() or source.is_symlink():
                continue
            number = local_volume_number(source.name)
            if number is None:
                continue
            target = source.with_name(volume_title(number))
            if target == source:
                continue
            rows.append({'source': source, 'target': target, 'number': str(number), 'error': ''})
    targets = {}
    for row in rows:
        targets.setdefault(row['target'], []).append(row)
    for target, grouped in targets.items():
        if target.exists() or len(grouped) > 1:
            message = f'目标已存在或有多个目录将改为同一名称：{target}'
            for row in grouped:
                row['error'] = message
    return rows


def apply_volume_renames(rows, log_dir: Path):
    if any(row['error'] for row in rows):
        raise ValueError('预览中存在命名冲突，未执行任何重命名')
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / ('volume-renames-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    journal = [{'source': str(row['source']), 'target': str(row['target']), 'status': '待处理'}
               for row in rows]
    log_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding='utf-8')
    for item, row in zip(journal, rows):
        source, target = row['source'], row['target']
        if not source.is_dir() or source.is_symlink() or target.exists():
            item['status'] = '失败'
            item['error'] = '源目录状态变化或目标已经存在'
            log_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding='utf-8')
            raise RuntimeError(item['error'] + '：' + str(source))
        source.rename(target)
        item['status'] = '已完成'
        log_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding='utf-8')
    return log_path


def save_chapter(client: Client, slug: str, series: Path, chapter: Chapter,
                 cancelled=lambda: False, progress=lambda _: None):
    local = inspect_local(series)
    if (chapter.kind, chapter.number) in local:
        return '已存在'
    target = series / chapter.title
    if target.exists():
        raise FileExistsError(str(target))
    urls = client.pages(slug, chapter)
    stage = Path(tempfile.mkdtemp(prefix='.copy-updating-', dir=series))
    try:
        hashes = {}
        for index, url in enumerate(urls, 1):
            if cancelled():
                raise InterruptedError('用户已停止')
            label = f'第{chapter.number}卷' if chapter.kind == 'volume' else f'第{chapter.number}话'
            progress(f'{label}：{index}/{len(urls)} 页')
            body = client.fetch(url, MAX_IMAGE, f'{BASE}/comic/{slug}/chapter/{chapter.id}')
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(body)) as image:
                    if getattr(image, 'n_frames', 1) > 1:
                        raise ValueError('网站返回多帧图片')
                    image.load()
                    image = ImageOps.exif_transpose(image)
                    if image.mode not in {'RGB', 'RGBA', 'L', 'LA', 'P', '1', 'I;16'}:
                        image = image.convert('RGB')
                    filename = f'{index:03d}.png'
                    path = stage / filename
                    image.save(path, format='PNG')
            with Image.open(path) as verified:
                verified.verify()
            hashes[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
        (stage / MANIFEST).write_text(json.dumps({
            'site': BASE, 'slug': slug, 'chapter_id': chapter.id,
            'chapter_number': str(chapter.number), 'kind': chapter.kind,
            'page_count': len(urls), 'pages': hashes,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        if target.exists() or (chapter.kind, chapter.number) in inspect_local(series):
            raise FileExistsError('写入期间目标章节已出现')
        stage.rename(target)
        stage = None
        return '已完成'
    finally:
        if stage is not None:
            shutil.rmtree(stage)


def run_update(series: Path, slug: str, client=None, cancelled=lambda: False, progress=lambda _: None,
               write=False, catalog=None, preferred_groups=()):
    client = client or Client()
    slug = parse_slug(slug)
    series = Path(series).resolve()
    catalog = catalog if catalog is not None else client.catalog(
        slug, preferred_groups=preferred_groups)
    plan = plan_update(series, catalog)
    if not write:
        return plan
    results = []
    # Catch up from the local latest first; fill historical gaps afterward.
    latest = plan['local_latest']
    volumes = [c for c in plan['missing'] if c.kind == 'volume']
    chapters = [c for c in plan['missing'] if c.kind == 'chapter']
    ordered = (volumes
               + [c for c in chapters if latest is None or c.number > latest]
               + [c for c in chapters if latest is not None and c.number <= latest])
    for chapter in ordered:
        if cancelled():
            break
        try:
            status = save_chapter(client, slug, series, chapter, cancelled, progress)
        except InterruptedError:
            break
        except Exception as exc:
            results.append({'number': str(chapter.number), 'kind': chapter.kind,
                            'status': '失败', 'error': str(exc)})
            break  # Network/content errors can indicate a site restriction; never cascade failures.
        else:
            results.append({'number': str(chapter.number), 'kind': chapter.kind, 'status': status})
            label = f'第{chapter.number}卷' if chapter.kind == 'volume' else f'第{chapter.number}话'
            progress(f'{label}：{status}')
    return plan, results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--series', type=Path, required=True)
    parser.add_argument('--source', required=True, help='CopyManga 漫画详情页链接或 slug')
    parser.add_argument('--update', action='store_true', help='写入缺失章节；默认为只读预览')
    args = parser.parse_args()
    if args.update:
        plan, results = run_update(args.series, args.source, write=True, progress=print)
        print(json.dumps({'local_latest': str(plan['local_latest']), 'remote_latest': str(plan['remote_latest']),
                          'planned': len(plan['missing']), 'results': results}, ensure_ascii=False, indent=2))
    else:
        plan = run_update(args.series, args.source)
        print(json.dumps({'local_latest': str(plan['local_latest']), 'remote_latest': str(plan['remote_latest']),
                          'missing': [str(c.number) for c in plan['missing']]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()


def gui(parent):
    """Add a source-mapping and update panel to the existing notebook."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    frame = ttk.Frame(parent, padding=10)
    frame.pack(fill='both', expand=True)
    source_file = Path(__file__).with_name('copy_sources.json')
    mappings = json.loads(source_file.read_text(encoding='utf-8-sig')) if source_file.exists() else {}
    if not isinstance(mappings, dict):
        raise ValueError('copy_sources.json 必须是目录到网址的对象')
    events, stop = queue.Queue(), threading.Event()
    state = {'busy': False, 'plans': {}, 'errors': {}, 'snapshot': None, 'row_paths': {},
             'sort_column': 'series', 'sort_descending': False}
    series_var = tk.StringVar()
    source_var = tk.StringVar()
    status_var = tk.StringVar(value='添加漫画目录与对应的网站详情页，然后检查更新。')

    def choose():
        value = filedialog.askdirectory(initialdir=r'Z:\Manga')
        if value:
            series_var.set(value)

    form = ttk.Frame(frame)
    form.pack(fill='x')
    ttk.Label(form, text='漫画目录').grid(row=0, column=0, sticky='w')
    ttk.Entry(form, textvariable=series_var).grid(row=0, column=1, sticky='ew', padx=7)
    ttk.Button(form, text='选择目录', command=choose).grid(row=0, column=2)
    ttk.Label(form, text='CopyManga 详情页').grid(row=1, column=0, sticky='w', pady=6)
    ttk.Entry(form, textvariable=source_var).grid(row=1, column=1, sticky='ew', padx=7)
    form.columnconfigure(1, weight=1)
    ttk.Label(frame, text='只有明确配置了网站链接的漫画才会更新；网站空目录、限制页或章节不匹配会停止。').pack(anchor='w', pady=5)

    columns = [('series', '漫画', 390), ('local', '本地最新', 90), ('remote', '网站最新', 90),
               ('missing', '缺失话数', 95), ('status', '状态', 320)]
    labels = {key: label for key, label, _ in columns}
    table = ttk.Treeview(frame, columns=tuple(labels), show='headings', selectmode='browse')
    for key, label, width in columns:
        table.heading(key, text=label)
        table.column(key, width=width)
    table.pack(fill='both', expand=True, pady=5)
    log = tk.Text(frame, height=10, wrap='word')
    log.pack(fill='both', expand=True)

    def render():
        selected = selected_path()
        table.delete(*table.get_children())
        state['row_paths'] = {}
        ordered = sorted_mapping_paths(mappings, state['plans'], state['sort_column'],
                                       state['sort_descending'], state['errors'])
        for index, path in enumerate(ordered):
            plan = state['plans'].get(path)
            iid = f'row-{index}'
            state['row_paths'][iid] = path
            status = ('错误：' + state['errors'][path]) if path in state['errors'] else ('检查完成' if plan else '待检查')
            table.insert('', 'end', iid=iid, values=(Path(path).name,
                str(plan['local_latest']) if plan else '—', str(plan['remote_latest']) if plan else '—',
                len(plan['missing']) if plan else '—', status))
            if path == selected:
                table.selection_set(iid)
                table.focus(iid)
                table.see(iid)

    def sort_table(column):
        if state['sort_column'] == column:
            state['sort_descending'] = not state['sort_descending']
        else:
            state['sort_column'] = column
            state['sort_descending'] = False
        for key, label, _ in columns:
            arrow = (' ▼' if state['sort_descending'] else ' ▲') if key == column else ''
            table.heading(key, text=label + arrow)
        render()

    def selected_path():
        chosen = table.selection()
        return state['row_paths'].get(chosen[0]) if chosen else None

    def selected_row(_event=None):
        path = selected_path()
        if path:
            series_var.set(path)
            source_var.set(f'{BASE}/comic/{mappings[path]}')

    table.bind('<<TreeviewSelect>>', selected_row)
    for key, label, _ in columns:
        table.heading(key, text=label + (' ▲' if key == state['sort_column'] else ''),
                      command=lambda column=key: sort_table(column))

    def save_mapping():
        if state['busy']:
            return
        try:
            path = Path(series_var.get()).resolve()
            if not path.is_dir() or path.is_symlink() or not path.is_relative_to(Path(r'Z:\Manga').resolve()):
                raise ValueError('请选 Z:\\Manga 下的一部漫画目录')
            slug = parse_slug(source_var.get())
            mappings[str(path)] = slug
            source_file.write_text(json.dumps(mappings, ensure_ascii=False, indent=2), encoding='utf-8')
            state['plans'] = {}
            state['errors'] = {}
            render()
        except Exception as exc:
            messagebox.showerror('保存失败', str(exc))

    def remove_mapping():
        if state['busy']:
            return
        path = selected_path()
        if path and messagebox.askyesno('移除对应关系', '仅移除软件中的网站链接，不删除漫画文件。是否继续？'):
            mappings.pop(path)
            source_file.write_text(json.dumps(mappings, ensure_ascii=False, indent=2), encoding='utf-8')
            state['plans'] = {}
            state['errors'] = {}
            render()

    def start(write=False):
        if state['busy']:
            return
        # Another tab (Aidoku new-series import) may have registered sources
        # after this panel was created. Reload before taking the work snapshot.
        latest_mappings = json.loads(source_file.read_text(encoding='utf-8-sig')) if source_file.exists() else {}
        if not isinstance(latest_mappings, dict):
            messagebox.showerror('配置错误', 'copy_sources.json 必须是目录到网址的对象')
            return
        if latest_mappings != mappings:
            mappings.clear()
            mappings.update(latest_mappings)
            state['plans'] = {}
            state['errors'] = {}
            render()
        if not mappings:
            messagebox.showinfo('先添加漫画', '请先保存漫画目录和对应的网站详情页。')
            return
        preflight_file = Path(__file__).with_name('source_preflight.json')
        try:
            preflight_data = (json.loads(preflight_file.read_text(encoding='utf-8-sig'))
                              if preflight_file.exists() else {})
            if not isinstance(preflight_data, dict):
                raise ValueError('source_preflight.json 必须是对象')
        except Exception as exc:
            messagebox.showerror('配置错误', str(exc))
            return
        snapshot = tuple(mappings.items())
        if write:
            if state['snapshot'] != snapshot or len(state['plans']) != len(snapshot):
                messagebox.showinfo('需要检查', '请先检查所有已配置漫画的更新。')
                return
            total = sum(len(p['missing']) for p in state['plans'].values())
            if not total:
                messagebox.showinfo('无需更新', '当前已配置的漫画没有缺失话数。')
                return
            if not messagebox.askyesno('开始更新', f'将为 {len(snapshot)} 部已配置漫画补齐 {total} 个缺失章节。\n每页校验后才写入章节目录，已有章节不会覆盖。是否开始？'):
                return
        stop.clear()
        state['busy'] = True
        task_progress.start('准备补齐缺失章节' if write else '准备检查全部来源', len(snapshot))
        state['plans'] = {} if not write else state['plans']
        state['errors'] = {} if not write else state['errors']
        state['snapshot'] = None if not write else snapshot
        check_btn.configure(state='disabled')
        update_btn.configure(state='disabled')

        def worker():
            client = Client(on_retry=lambda msg: events.put(('progress', msg)),
                            cancelled=stop.is_set)
            try:
                for index, (path, slug) in enumerate(snapshot, 1):
                    if stop.is_set():
                        break
                    events.put(('overall', f'处理 {index}/{len(snapshot)}：{Path(path).name}',
                                index - 1, len(snapshot)))
                    events.put(('log', f'检查：{Path(path).name}'))
                    try:
                        source_row = preflight_data.get(path, {})
                        preferred_groups = parse_preferred_groups(
                            source_row.get('preferred_groups', ()) if isinstance(source_row, dict) else ())
                        if write:
                            plan, results = run_update(Path(path), slug, client, stop.is_set,
                                lambda msg: events.put(('progress', msg)), write=True,
                                preferred_groups=preferred_groups)
                            events.put(('log', f'完成：{Path(path).name}；成功 {sum(r["status"] == "已完成" for r in results)} / 计划 {len(plan["missing"])}'))
                            failures = [r for r in results if r['status'] == '失败']
                            for failure in failures:
                                unit = '卷' if failure.get('kind') == 'volume' else '话'
                                events.put(('log', f'失败：第{failure["number"]}{unit} {failure["error"]}'))
                            if failures:
                                break
                        else:
                            plan = run_update(Path(path), slug, client, stop.is_set,
                                              preferred_groups=preferred_groups)
                            events.put(('plan', path, plan))
                            events.put(('log', f'{Path(path).name}：本地第{plan["local_latest"]}话，网站第{plan["remote_latest"]}话，缺 {len(plan["missing"])} 话'))
                    except Exception as exc:
                        if write:
                            events.put(('log', f'停止：{Path(path).name}：{exc}'))
                            break
                        events.put(('plan_error', path, str(exc)))
                        events.put(('log', f'跳过：{Path(path).name}：{exc}'))
                    events.put(('overall', f'已处理：{Path(path).name}', index, len(snapshot)))
            finally:
                events.put(('done', snapshot, write))

        threading.Thread(target=worker, daemon=True).start()

    def background_files():
        folder = Path(__file__).with_name('update_logs')
        return folder, folder / 'latest_status.json', folder / 'stop_requested'

    def background_status():
        _, file, _ = background_files()
        if not file.is_file():
            messagebox.showinfo('后台更新', '尚无后台更新记录。')
            return
        try:
            data = json.loads(file.read_text(encoding='utf-8'))
            messagebox.showinfo('后台更新进度',
                f'状态：{"运行中" if data.get("running") else "已结束"}\n'
                f'结果：{data.get("phase") or "-"}\n'
                f'本轮已完成作品：{data.get("completed_series", 0)} / {data.get("total_series", 0)}\n'
                f'本轮已完成章节：{data.get("completed_chapters", 0)} / 预检 {data.get("planned_chapters", 0)}\n'
                f'当前作品：{Path(data.get("current_series") or "-").name}\n'
                f'当前内容：{("第" + str(data.get("current_chapter")) + ("卷" if data.get("current_kind") == "volume" else "话")) if data.get("current_chapter") else "-"}\n'
                f'页面进度：{data.get("current_page") or "-"}\n'
                f'重试：{data.get("retry") or "无"}\n'
                f'错误：{data.get("last_error") or "无"}\n'
                f'最后更新：{data.get("updated_at") or "-"}\n'
                f'日志：{data.get("log") or "-"}')
        except Exception as exc:
            messagebox.showerror('进度读取失败', str(exc))

    def background_start():
        import subprocess
        import sys
        folder, status_file, stop_file = background_files()
        if not Path(__file__).with_name('source_preflight.json').is_file():
            messagebox.showerror('缺少预检', '没有批量来源预检结果。')
            return
        if status_file.is_file():
            status = json.loads(status_file.read_text(encoding='utf-8'))
            if status.get('running'):
                messagebox.showinfo('正在运行', '后台更新已在运行。')
                return
        if not messagebox.askyesno('继续后台更新', '程序会重新检查已配置来源，并仅补齐剩余章节。遇到访问限制或图片异常会停下。是否启动？'):
            return
        folder.mkdir(exist_ok=True)
        if stop_file.exists():
            stop_file.unlink()
        output = (folder / 'background-console.log').open('ab')
        try:
            subprocess.Popen([sys.executable, '-u', str(Path(__file__).with_name('batch_update.py'))],
                cwd=str(Path(__file__).parent), stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        finally:
            output.close()
        messagebox.showinfo('后台更新', '已启动。稍后点击“查看后台进度”查看状态。')

    def background_stop():
        folder, status_file, stop_file = background_files()
        if not status_file.is_file() or not json.loads(status_file.read_text(encoding='utf-8')).get('running'):
            messagebox.showinfo('后台更新', '当前没有运行中的后台任务。')
            return
        folder.mkdir(exist_ok=True)
        stop_file.write_text('stop requested by GUI\n', encoding='utf-8')
        messagebox.showinfo('停止请求已发送', '当前页处理结束后会停止；已完成章节会保留。')

    def normalize_existing_volumes():
        if state['busy']:
            messagebox.showinfo('请稍候', '当前前台任务结束后再规范卷名。')
            return
        _, status_file, _ = background_files()
        if status_file.is_file():
            try:
                if json.loads(status_file.read_text(encoding='utf-8')).get('running'):
                    messagebox.showinfo('后台更新正在运行', '请等待后台更新结束或先停止后台更新，再规范已有卷名。')
                    return
            except (OSError, ValueError):
                messagebox.showerror('状态读取失败', '无法确认后台任务是否运行，未执行重命名。')
                return
        try:
            rows = plan_volume_renames(Path(r'Z:\Manga'))
            if not rows:
                messagebox.showinfo('无需处理', '已有卷目录都符合三位数字命名规范。')
                return
            conflicts = [row for row in rows if row['error']]
            preview = '\n'.join(f'{row["source"].parent.name}：{row["source"].name} → {row["target"].name}'
                                for row in rows[:15])
            if len(rows) > 15:
                preview += f'\n……另有 {len(rows) - 15} 项'
            if conflicts:
                messagebox.showerror('存在命名冲突',
                    f'发现 {len(conflicts)} 项冲突，未执行任何重命名。请先手工处理同卷号重复目录。\n\n{preview}')
                return
            if not messagebox.askyesno('规范已有卷名',
                    f'将把 {len(rows)} 个卷目录统一为三位数字名称（例如第5卷→005）。\n'
                    f'话目录不会修改，图片不会改动。\n\n{preview}\n\n是否应用？'):
                return
            log_path = apply_volume_renames(rows, Path(__file__).with_name('volume_rename_logs'))
            state['plans'] = {}
            state['errors'] = {}
            render()
            messagebox.showinfo('卷名规范完成', f'已重命名 {len(rows)} 个卷目录。\n日志：{log_path}')
        except Exception as exc:
            messagebox.showerror('卷名规范失败', str(exc))

    actions = ttk.Frame(frame)
    actions.pack(fill='x', pady=5, before=table)
    ttk.Button(actions, text='保存对应关系', command=save_mapping).pack(side='left', padx=4)
    ttk.Button(actions, text='移除选中对应关系', command=remove_mapping).pack(side='left', padx=4)
    ttk.Button(actions, text='规范已有卷名', command=normalize_existing_volumes).pack(side='left', padx=4)
    check_btn = ttk.Button(actions, text='1. 检查全部更新', command=start)
    check_btn.pack(side='left', padx=4)
    update_btn = ttk.Button(actions, text='2. 补齐到最新', command=lambda: start(True))
    update_btn.pack(side='left', padx=4)
    ttk.Button(actions, text='停止', command=stop.set).pack(side='left', padx=4)
    ttk.Button(actions, text='启动/续跑后台更新', command=background_start).pack(side='left', padx=4)
    ttk.Button(actions, text='查看后台进度', command=background_status).pack(side='left', padx=4)
    ttk.Button(actions, text='停止后台更新', command=background_stop).pack(side='left', padx=4)
    ttk.Label(frame, textvariable=status_var).pack(fill='x')
    task_progress = TaskProgress(frame, '前台任务进度')
    task_progress.pack(fill='x', pady=6, before=table)
    bg_panel = ttk.LabelFrame(frame, text='后台更新 · 自动刷新', padding=8)
    bg_panel.pack(fill='x', pady=6, before=table)
    bg_state = tk.StringVar(value='等待读取后台进度…')
    bg_detail = tk.StringVar(value='')
    bg_retry = tk.StringVar(value='')
    bg_timing = tk.StringVar(value='已用时间：—｜预计剩余：—｜预计总耗时：—')
    ttk.Label(bg_panel, textvariable=bg_state).pack(fill='x')
    bg_chapters = ttk.Progressbar(bg_panel, mode='determinate', maximum=1)
    bg_chapters.pack(fill='x', pady=(4, 2))
    ttk.Label(bg_panel, textvariable=bg_detail).pack(fill='x')
    bg_pages = ttk.Progressbar(bg_panel, mode='determinate', maximum=1)
    bg_pages.pack(fill='x', pady=(4, 2))
    ttk.Label(bg_panel, textvariable=bg_retry, wraplength=780).pack(fill='x')
    ttk.Label(bg_panel, textvariable=bg_timing).pack(fill='x', pady=(3, 0))

    def poll_background():
        _, file, _ = background_files()
        try:
            data = json.loads(file.read_text(encoding='utf-8')) if file.is_file() else None
            if data:
                done = data.get('completed_chapters', 0)
                planned = data.get('planned_chapters', 0)
                bg_chapters.configure(maximum=max(planned, 1), value=min(done, max(planned, 1)))
                running = data.get('running')
                result = data.get('phase') or ('运行中' if running else '已结束')
                bg_state.set(f'{result}｜本轮作品 {data.get("completed_series", 0)}/{data.get("total_series", 0)}'
                             f'｜章节 {done}/{planned}｜最后更新 {data.get("updated_at") or "-"}')
                page = data.get('current_page') or ''
                match = re.search(r'(\d+)\s*/\s*(\d+)\s*页', page)
                bg_pages.configure(maximum=int(match[2]) if match else 1,
                                   value=int(match[1]) if match else 0)
                current = (f'第{data.get("current_chapter")}卷' if data.get('current_kind') == 'volume'
                           else f'第{data.get("current_chapter")}话') if data.get('current_chapter') else '-'
                bg_detail.set(f'当前：{Path(data.get("current_series") or "-").name}'
                              f' · {current} · {page or "尚未开始下载"}')
                bg_retry.set(data.get('retry') or (f'错误：{data["last_error"]}'
                             if data.get('last_error') else '无错误'))
                try:
                    elapsed = max(0.0, (datetime.now() - datetime.fromisoformat(data['started'])).total_seconds())
                except (KeyError, TypeError, ValueError):
                    elapsed = None
                remaining = (estimate_remaining(elapsed, done, planned)
                             if elapsed is not None and data.get('running') else 0 if not data.get('running') else None)
                total_estimate = elapsed + remaining if elapsed is not None and remaining is not None else None
                bg_timing.set(f'已用时间：{format_duration(elapsed)}｜预计剩余：{format_duration(remaining)}'
                              f'｜预计总耗时：{format_duration(total_estimate)}')
            else:
                bg_state.set('尚无后台更新记录')
        except (OSError, ValueError):
            # The writer may be replacing the status file at this instant.
            pass
        parent.after(1000, poll_background)

    render()

    def poll():
        for _ in range(200):
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'log':
                log.insert('end', event[1] + '\n')
                log.see('end')
            elif event[0] == 'progress':
                status_var.set(event[1])
                task_progress.update(event[1], infer=False)
            elif event[0] == 'overall':
                task_progress.update(event[1], event[2], event[3], infer=False)
            elif event[0] == 'plan':
                state['plans'][event[1]] = event[2]
                state['errors'].pop(event[1], None)
                render()
            elif event[0] == 'plan_error':
                state['plans'].pop(event[1], None)
                state['errors'][event[1]] = event[2]
                render()
            elif event[0] == 'done':
                state['busy'] = False
                state['snapshot'] = event[1] if not event[2] and len(state['plans']) == len(event[1]) else None
                check_btn.configure(state='normal')
                update_btn.configure(state='normal')
                task_progress.finish('已停止' if stop.is_set() else '任务结束')
                if stop.is_set():
                    status_var.set('已停止')
                elif state['errors'] and not event[2]:
                    status_var.set(f'检查结束；{len(state["errors"])} 部失败，其他作品已继续检查')
                else:
                    status_var.set('任务结束；详情见日志')
        parent.after(100, poll)

    parent.after(100, poll)
    parent.after(200, poll_background)
    return state, stop
