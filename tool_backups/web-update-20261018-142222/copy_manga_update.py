"""Update explicitly mapped Komga series from CopyManga's public web pages."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import queue
import re
import shutil
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


BASE = 'https://www.copy20.com'
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
SLUG = re.compile(r'^[a-z0-9_-]+$', re.I)
MAX_HTML = 4 * 1024 * 1024
MAX_CATALOG = 12 * 1024 * 1024
MAX_IMAGE = 100 * 1024 * 1024
MANIFEST = '.copy-manga-update.json'


class AccessProblem(RuntimeError):
    """A site response is restricted, incomplete, or inconsistent."""


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


@dataclass(frozen=True)
class Chapter:
    number: Decimal
    id: str
    title: str
    group: str


class Client:
    def __init__(self, delay=1.5, opener=urlopen):
        self.delay = delay
        self.opener = opener
        self.last_request = 0.0

    def fetch(self, url: str, limit: int, referer: str, headers=None) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username:
            raise AccessProblem('网站返回了不安全的下载地址')
        if parsed.hostname not in {'www.copy20.com'} and not parsed.hostname.endswith('.mangafunb.fun'):
            raise AccessProblem('网站返回了非预期域名的图片地址')
        remaining = self.delay - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)
        request = Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141.0 Safari/537.36',
            'Referer': referer,
            'Accept-Language': 'zh-CN,zh;q=0.9',
            **(headers or {}),
        })
        self.last_request = time.monotonic()
        with self.opener(request, timeout=30) as response:
            final = urlparse(response.geturl())
            if final.scheme != 'https' or final.hostname != parsed.hostname:
                raise AccessProblem('请求被重定向到非预期页面')
            content = response.read(limit + 1)
            if len(content) > limit:
                raise AccessProblem('网站响应超出预期大小')
            return content

    def catalog(self, slug: str) -> list[Chapter]:
        slug = parse_slug(slug)
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
                raise AccessProblem('章节目录数量不完整，可能触发了网站限制')
            for item in entries:
                ident = item.get('id', '')
                if not UUID.fullmatch(ident) or ident in ids:
                    raise AccessProblem('章节目录包含无效或重复的 ID')
                ids.add(ident)
                name = item.get('name', '')
                if item.get('type') != 1 or not isinstance(name, str):
                    continue  # Special stories need manual mapping; avoid numeric false positives.
                match = re.search(r'(?<!\d)(\d+(?:\.\d+)?)(?!\d)', name)
                if not match:
                    continue
                number = Decimal(match[1])
                chapters.append(Chapter(number, ident, chapter_title(name, number), group_name))
        if not chapters:
            raise AccessProblem('目录没有可识别的正篇话数，停止更新')
        numbered = [c.number for c in chapters]
        if len(numbered) != len(set(numbered)):
            raise AccessProblem('网站存在同话数不同章节，需手工确认后更新')
        return sorted(chapters, key=lambda c: c.number)

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
        result = decrypt_web(script_value(html, 'contentKey'), script_value(html, 'cct'))
        if not isinstance(result, list) or not result or len(result) > 1000:
            raise AccessProblem('章节图片列表为空或异常')
        urls = [item.get('url') if isinstance(item, dict) else None for item in result]
        if any(not isinstance(u, str) or not u.startswith('https://') for u in urls):
            raise AccessProblem('章节包含无效图片地址')
        return urls


def inspect_local(series: Path) -> dict[Decimal, Path]:
    if not series.is_dir() or series.is_symlink():
        raise ValueError('本地漫画目录不存在或是链接目录')
    local = {}
    for entry in series.iterdir():
        if entry.is_symlink():
            raise ValueError('漫画目录包含链接，暂停自动更新')
        number = chapter_number(entry.stem if entry.is_file() and entry.suffix.lower() in {'.cbz', '.zip'} else entry.name)
        if number is not None and (entry.is_dir() or entry.suffix.lower() in {'.cbz', '.zip'}):
            local.setdefault(number, entry)
    return local


def plan_update(series: Path, catalog: list[Chapter]):
    local = inspect_local(series)
    latest = max(local) if local else None
    remote = max(c.number for c in catalog)
    if latest is not None and latest > remote:
        raise AccessProblem(f'本地最新第{latest}话比网站第{remote}话还新，可能目录受限或对应错误')
    missing = [c for c in catalog if c.number not in local]
    return {'local_latest': latest, 'remote_latest': remote, 'missing': missing, 'local_count': len(local)}


def save_chapter(client: Client, slug: str, series: Path, chapter: Chapter,
                 cancelled=lambda: False, progress=lambda _: None):
    local = inspect_local(series)
    if chapter.number in local:
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
            progress(f'第{chapter.number}话：{index}/{len(urls)} 页')
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
            'chapter_number': str(chapter.number), 'page_count': len(urls), 'pages': hashes,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        if target.exists() or chapter.number in inspect_local(series):
            raise FileExistsError('写入期间目标章节已出现')
        stage.rename(target)
        stage = None
        return '已完成'
    finally:
        if stage is not None:
            shutil.rmtree(stage)


def run_update(series: Path, slug: str, client=None, cancelled=lambda: False, progress=lambda _: None,
               write=False):
    client = client or Client()
    slug = parse_slug(slug)
    series = Path(series).resolve()
    catalog = client.catalog(slug)
    plan = plan_update(series, catalog)
    if not write:
        return plan
    results = []
    # Catch up from the local latest first; fill historical gaps afterward.
    latest = plan['local_latest']
    ordered = ([c for c in plan['missing'] if latest is None or c.number > latest]
               + [c for c in plan['missing'] if latest is not None and c.number <= latest])
    for chapter in ordered:
        if cancelled():
            break
        try:
            status = save_chapter(client, slug, series, chapter, cancelled, progress)
        except InterruptedError:
            break
        except Exception as exc:
            results.append({'number': str(chapter.number), 'status': '失败', 'error': str(exc)})
            break  # Network/content errors can indicate a site restriction; never cascade failures.
        else:
            results.append({'number': str(chapter.number), 'status': status})
            progress(f'第{chapter.number}话：{status}')
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
    state = {'busy': False, 'plans': {}, 'snapshot': None}
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

    table = ttk.Treeview(frame, columns=('series', 'local', 'remote', 'missing', 'status'), show='headings', selectmode='browse')
    for key, label, width in [('series', '漫画', 390), ('local', '本地最新', 90), ('remote', '网站最新', 90),
                              ('missing', '缺失话数', 95), ('status', '状态', 180)]:
        table.heading(key, text=label)
        table.column(key, width=width)
    table.pack(fill='both', expand=True, pady=5)
    log = tk.Text(frame, height=10, wrap='word')
    log.pack(fill='both', expand=True)

    def render():
        table.delete(*table.get_children())
        for index, (path, slug) in enumerate(mappings.items()):
            plan = state['plans'].get(path)
            table.insert('', 'end', iid=str(index), values=(Path(path).name,
                str(plan['local_latest']) if plan else '—', str(plan['remote_latest']) if plan else '—',
                len(plan['missing']) if plan else '—', '检查完成' if plan else '待检查'))

    def selected_path():
        chosen = table.selection()
        return list(mappings)[int(chosen[0])] if chosen else None

    def selected_row(_event=None):
        path = selected_path()
        if path:
            series_var.set(path)
            source_var.set(f'{BASE}/comic/{mappings[path]}')

    table.bind('<<TreeviewSelect>>', selected_row)

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
            render()

    def start(write=False):
        if state['busy']:
            return
        if not mappings:
            messagebox.showinfo('先添加漫画', '请先保存漫画目录和对应的网站详情页。')
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
        state['plans'] = {} if not write else state['plans']
        state['snapshot'] = None if not write else snapshot
        check_btn.configure(state='disabled')
        update_btn.configure(state='disabled')

        def worker():
            client = Client()
            try:
                for path, slug in snapshot:
                    if stop.is_set():
                        break
                    events.put(('log', f'检查：{Path(path).name}'))
                    try:
                        if write:
                            plan, results = run_update(Path(path), slug, client, stop.is_set,
                                lambda msg: events.put(('progress', msg)), write=True)
                            events.put(('log', f'完成：{Path(path).name}；成功 {sum(r["status"] == "已完成" for r in results)} / 计划 {len(plan["missing"])}'))
                            failures = [r for r in results if r['status'] == '失败']
                            for failure in failures:
                                events.put(('log', f'失败：第{failure["number"]}话 {failure["error"]}'))
                            if failures:
                                break
                        else:
                            plan = run_update(Path(path), slug, client, stop.is_set)
                            events.put(('plan', path, plan))
                            events.put(('log', f'{Path(path).name}：本地第{plan["local_latest"]}话，网站第{plan["remote_latest"]}话，缺 {len(plan["missing"])} 话'))
                    except Exception as exc:
                        events.put(('log', f'停止：{Path(path).name}：{exc}'))
                        break
            finally:
                events.put(('done', snapshot, write))

        threading.Thread(target=worker, daemon=True).start()

    actions = ttk.Frame(frame)
    actions.pack(fill='x', pady=5, before=table)
    ttk.Button(actions, text='保存对应关系', command=save_mapping).pack(side='left', padx=4)
    ttk.Button(actions, text='移除选中对应关系', command=remove_mapping).pack(side='left', padx=4)
    check_btn = ttk.Button(actions, text='1. 检查全部更新', command=start)
    check_btn.pack(side='left', padx=4)
    update_btn = ttk.Button(actions, text='2. 补齐到最新', command=lambda: start(True))
    update_btn.pack(side='left', padx=4)
    ttk.Button(actions, text='停止', command=stop.set).pack(side='left', padx=4)
    ttk.Label(frame, textvariable=status_var).pack(fill='x')
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
            elif event[0] == 'plan':
                state['plans'][event[1]] = event[2]
                render()
            elif event[0] == 'done':
                state['busy'] = False
                state['snapshot'] = event[1] if not event[2] and len(state['plans']) == len(event[1]) else None
                check_btn.configure(state='normal')
                update_btn.configure(state='normal')
                status_var.set('已停止' if stop.is_set() else '任务结束；详情见日志')
        parent.after(100, poll)

    parent.after(100, poll)
    return state, stop
