"""Aidoku CBZ -> Komga image folders. Originals are always retained."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import tempfile
import threading
import time
from html.parser import HTMLParser
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
import warnings
import xml.etree.ElementTree as ET
import zipfile

from PIL import Image, ImageOps
from library_audit import sortable_name, chapter_number, PATTERN
from progress_ui import TaskProgress

IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.avif'}
MANIFEST = '.aidoku-conversion.json'
MAX_PAGE = 256 * 1024 * 1024
MAX_ARCHIVE = 8 * 1024 ** 3


def natural(value):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r'(\d+)', value)]


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value.strip()).rstrip(' .')
    if not value or value in {'.', '..'}:
        raise ValueError('漫画名或章节名为空')
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
        value = '_' + value
    if len(value) > 150:
        raise ValueError('目录名过长，请通过名称映射缩短')
    return value


def chapter_name(value):
    value = value.strip()
    volume = re.fullmatch(r'第\s*0*(\d+)\s*卷', value)
    if volume:
        number = int(volume[1])
        if number < 1000:
            return f'{number:03d}'
    # Only normalize a numeric chapter prefix, retaining preview/special labels.
    return safe_name(re.sub(r'^第\s*(\d+)(\.\d+)?\s*[话話]',
                           lambda m: f'第{int(m[1]):02d}{m[2] or ""}话', value))


class HeaderParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'h4' and 'header' in dict(attrs).get('class', '').split():
            self.active = True

    def handle_endtag(self, tag):
        if tag == 'h4':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


class Resolver:
    def __init__(self, base_url='https://www.copy20.com', online=True, mappings=None, sortable=False):
        parsed = urlparse(base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username:
            raise ValueError('网站地址必须为 HTTPS 地址')
        self.base = base_url.rstrip('/')
        self.online = online
        self.mappings = mappings or {}
        self.last_request = 0
        self.sortable = sortable

    def resolve(self, archive, z):
        series = title = ''
        slug, chapter_id = archive.parent.name, archive.stem
        source = 'ComicInfo.xml'
        infos = [i for i in z.infolist() if PurePosixPath(i.filename).name.lower() == 'comicinfo.xml']
        if len(infos) > 1:
            raise ValueError('存在多个 ComicInfo.xml，无法确定章节')
        if infos:
            if infos[0].file_size > 2 * 1024 * 1024:
                raise ValueError('ComicInfo.xml 过大')
            raw = z.read(infos[0])
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise ValueError('不支持包含实体声明的 XML')
            root = ET.fromstring(raw)
            fields = {e.tag.split('}')[-1]: (e.text or '').strip() for e in root}
            series, title = fields.get('Series', ''), fields.get('Title', '')
            if not title and fields.get('Number'):
                title = '第' + fields['Number'] + '话'
            notes = fields.get('Notes', '')
            if '!AIDOKUDATA' in notes:
                data, _ = json.JSONDecoder().raw_decode(notes.split('!AIDOKUDATA', 1)[1].lstrip())
                slug = data.get('mangaKey') or slug
                chapter_id = data.get('chapterKey') or chapter_id
        if not series or not title:
            if not self.online:
                raise ValueError('缺少漫画名/章节名，离线模式无法补全')
            if not re.fullmatch(r'[A-Za-z0-9_-]+', slug) or not re.fullmatch(r'[A-Za-z0-9_-]+', chapter_id):
                raise ValueError('无法确定网页章节标识')
            url = f'{self.base}/comic/{quote(slug)}/chapter/{quote(chapter_id)}'
            time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            req = Request(url, headers={'User-Agent': 'Mozilla/5.0 ComicConverter/1.0'})
            with urlopen(req, timeout=20) as response:
                html = response.read(4 * 1024 * 1024 + 1)
            if len(html) > 4 * 1024 * 1024:
                raise ValueError('网页响应过大')
            parser = HeaderParser()
            parser.feed(html.decode('utf-8'))
            header = ''.join(parser.parts).strip()
            if '/' not in header:
                raise ValueError('网页没有漫画/章节标题，可能是验证页或网站结构变化')
            web_series, web_title = header.rsplit('/', 1)
            series, title = series or web_series, title or web_title
            source = '网页补全'
        mapped = self.mappings.get(slug, self.mappings.get(series, series))
        title = chapter_name(title)
        return safe_name(mapped), sortable_name(title) if self.sortable else title, source


def archive_images(z):
    infos = z.infolist()
    if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_ARCHIVE:
        raise ValueError('压缩包超过安全大小限制')
    images = []
    for i in infos:
        path = PurePosixPath(i.filename.replace('\\', '/'))
        if path.is_absolute() or '..' in path.parts or ':' in i.filename:
            raise ValueError('压缩包包含不安全路径')
        if i.is_dir() or '__MACOSX' in path.parts or path.name.startswith('.'):
            continue
        if path.suffix.lower() in IMAGES:
            if i.file_size > MAX_PAGE:
                raise ValueError('单页图片过大')
            images.append(i)
    if not images:
        raise ValueError('没有可转换的图片')
    names = [i.filename.casefold() for i in images]
    if len(set(names)) != len(names):
        raise ValueError('存在重复图片文件名')
    return sorted(images, key=lambda i: natural(i.filename))


def digest(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def scan(source, output, resolver, log=print, cancelled=lambda: False):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_dir():
        raise ValueError('输入目录不存在')
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('输入与输出目录必须分开，不能互相包含')
    rows, targets = [], set()
    archives = sorted((p for p in source.rglob('*') if p.suffix.lower() in {'.cbz', '.zip'} and p.is_file()), key=lambda p: natural(str(p)))
    for p in archives:
        if cancelled():
            break
        row = {'source': str(p), 'status': '待转换'}
        log(f'扫描 {len(rows) + 1}/{len(archives)}：正在读取 {p.name}')
        try:
            if p.is_symlink() or not p.resolve().is_relative_to(source):
                raise ValueError('不支持源文件符号链接')
            with zipfile.ZipFile(p) as z:
                images = archive_images(z)
                series, title, origin = resolver.resolve(p, z)
            target = output / series / title
            # Reuse an existing equivalent chapter rather than create a second copy
            # when switching between legacy and fixed-width chapter naming.
            if (output / series).is_dir() and chapter_number(title) is not None:
                matches = [p for p in (output / series).iterdir() if p.is_dir()
                           and chapter_number(p.name) == chapter_number(title)
                           and PATTERN.fullmatch(p.name.strip())[2] == PATTERN.fullmatch(title)[2]]
                if len(matches) > 1:
                    raise ValueError('已有多个同话数同后缀目录，请先处理重复章节')
                if matches:
                    target = matches[0]
            if not target.resolve().is_relative_to(output):
                raise ValueError('目标路径超出输出目录')
            key = str(target).casefold()
            if key in targets:
                raise ValueError('多个源文件对应同一章节，请先处理重复下载')
            targets.add(key)
            row.update(target=str(target), series=series, chapter=title, pages=len(images), metadata=origin,
                       sha256=digest(p))
            if target.exists():
                row['status'] = '已存在（转换时校验）'
        except Exception as exc:
            row.update(status='失败', error=str(exc))
        rows.append(row)
        log(f'扫描 {len(rows)}/{len(archives)}：{row["status"]}｜{p.name} → '
            f'{row.get("series", "")} / {row.get("chapter", "")} {row.get("error", "")}')
    return rows


def convert(row, cancelled=lambda: False, progress=lambda _: None):
    if row['status'] == '失败':
        return row
    target, source = Path(row['target']), Path(row['source'])
    stage = None
    try:
        if cancelled():
            return dict(row, status='已取消')
        if digest(source) != row['sha256']:
            raise ValueError('源文件在预览后已改变，请重新预览')
        if target.exists():
            manifest = target / MANIFEST
            if manifest.is_file():
                data = json.loads(manifest.read_text(encoding='utf-8'))
                pages = data.get('pages', {})
                if data.get('sha256') == row['sha256'] and len(pages) == row['pages'] and all(
                    re.fullmatch(r'\d+\.png', name) and (target / name).is_file() and digest(target / name) == h
                    for name, h in pages.items()
                ):
                    return dict(row, status='已跳过（校验一致）')
            raise ValueError('目标章节已存在且不能确认一致，未覆盖；请检查或选择其他输出目录')
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.converting-', dir=target.parent))
        hashes = {}
        with zipfile.ZipFile(source) as z:
            images = archive_images(z)
            if len(images) != row['pages']:
                raise ValueError('图片数量已改变')
            for index, info in enumerate(images, 1):
                if cancelled():
                    raise InterruptedError('已取消，当前章节未写入')
                progress(f'转换页面 {index}/{len(images)}：{row.get("series", "")} / {row.get("chapter", source.name)}')
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(z.read(info))) as image:
                        if getattr(image, 'n_frames', 1) > 1:
                            raise ValueError('发现多帧图片，无法安全转换为单张 PNG')
                        image.load()
                        normalized = ImageOps.exif_transpose(image)
                        if normalized.mode not in ('RGB', 'RGBA', 'L', 'LA', 'P', '1', 'I;16'):
                            normalized = normalized.convert('RGB')
                        name = f'{index:0{max(3, len(str(len(images))))}d}.png'
                        page = stage / name
                        normalized.save(page, format='PNG')
                with Image.open(page) as check:
                    check.verify()
                hashes[name] = digest(page)
        (stage / MANIFEST).write_text(json.dumps({'sha256': row['sha256'], 'pages': hashes,
            'source': str(source), 'series': row['series'], 'chapter': row['chapter']}, ensure_ascii=False, indent=2), encoding='utf-8')
        # Windows rename refuses an existing destination; never merge partial chapters.
        if target.exists():
            raise ValueError('目标目录被其他进程创建，已停止')
        stage.rename(target)
        stage = None
        return dict(row, status='已完成')
    except Exception as exc:
        return dict(row, status='已取消' if isinstance(exc, InterruptedError) else '失败', error=str(exc))
    finally:
        if stage is not None:
            shutil.rmtree(stage)


def report(rows, output):
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'conversion-report-{time.strftime("%Y%m%d-%H%M%S")}-{time.time_ns() % 1000000}.json'
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def gui(parent=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    root = parent if parent is not None else tk.Tk()
    if parent is None:
        root.title('Aidoku → Komga 自动转换器')
        root.geometry('1000x680')
    inputs = {}
    for label, default in [('输入文件夹', ''), ('输出文件夹', r'Z:\Manga'), ('补全网站', 'https://www.copy20.com'), ('名称映射 JSON（可选）', '')]:
        frame = ttk.Frame(root, padding=5)
        frame.pack(fill='x')
        ttk.Label(frame, text=label, width=24).pack(side='left')
        value = tk.StringVar(value=default)
        inputs[label] = value
        ttk.Entry(frame, textvariable=value).pack(side='left', fill='x', expand=True)
        if '文件夹' in label or 'JSON' in label:
            def choose(v=value, mapping='JSON' in label):
                selected = filedialog.askopenfilename(filetypes=[('JSON', '*.json')]) if mapping else filedialog.askdirectory()
                if selected:
                    v.set(selected)
            ttk.Button(frame, text='选择…', command=choose).pack(side='left')
    online = tk.BooleanVar(value=True)
    sorting = tk.BooleanVar(value=True)
    ttk.Checkbutton(root, text='仅在 CBZ 缺少标题时联网补全（保留“试看”等原章节标记）', variable=online).pack(anchor='w', padx=10)
    ttk.Checkbutton(root, text='新章节采用排序兼容命名：第0011.000话 / 第0011.500话；已有同话数目录沿用原名', variable=sorting).pack(anchor='w', padx=10)
    ttk.Label(root, text='输出：漫画名 / 第01话 / 001.png　　保留源文件；已有章节仅校验，不覆盖。').pack(anchor='w', padx=10)
    actions = ttk.Frame(root, padding=8)
    actions.pack(fill='x')
    task_progress = TaskProgress(root, '当前任务进度')
    task_progress.pack(fill='x', padx=10, pady=(0, 6))
    text = tk.Text(root, wrap='word')
    text.pack(fill='both', expand=True, padx=10, pady=8)
    events, stop = queue.Queue(), threading.Event()
    state = {'rows': [], 'busy': False, 'settings': None}
    def settings():
        return tuple(v.get() for v in inputs.values()) + (online.get(), sorting.get())
    def start(writing=False):
        if state['busy']:
            return
        config = settings()
        if not config[0] or not config[1]:
            messagebox.showerror('请选择目录', '请选择输入和输出文件夹')
            return
        if writing and (not state['rows'] or config != state['settings']):
            messagebox.showinfo('需要预览', '请先按当前设置扫描预览')
            return
        state['busy'] = True
        stop.clear()
        task_progress.start('准备转换压缩包' if writing else '正在统计并扫描压缩包',
                            len(state['rows']) if writing else None)
        scan_btn.config(state='disabled')
        run_btn.config(state='disabled')
        def worker():
            try:
                if not writing:
                    mappings = json.loads(Path(config[3]).read_text(encoding='utf-8-sig')) if config[3] else {}
                    if not isinstance(mappings, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mappings.items()):
                        raise ValueError('名称映射必须是字符串到字符串的 JSON 对象')
                    rows = scan(config[0], config[1], Resolver(config[2], config[4], mappings, config[5]), events.put, stop.is_set)
                    events.put(('scan', rows, config))
                else:
                    results = []
                    for index, row in enumerate(state['rows'], 1):
                        events.put(f'转换 {index}/{len(state["rows"])}：{row.get("series", "")} / {row.get("chapter", Path(row["source"]).name)}')
                        result = (dict(row, status='已取消') if stop.is_set() else
                                  convert(row, stop.is_set, events.put))
                        results.append(result)
                        events.put(f'{result["status"]}: {result.get("target", result["source"])} {result.get("error", "")}')
                    events.put(f'报告：{report(results, config[1])}')
            except Exception as exc:
                events.put(f'错误：{exc}')
            finally:
                events.put(('done',))
        threading.Thread(target=worker, daemon=True).start()
    scan_btn = ttk.Button(actions, text='1. 扫描预览', command=start)
    scan_btn.pack(side='left', padx=5)
    run_btn = ttk.Button(actions, text='2. 开始自动转换', command=lambda: start(True))
    run_btn.pack(side='left', padx=5)
    ttk.Button(actions, text='停止', command=stop.set).pack(side='left', padx=5)
    def poll():
        while not events.empty():
            event = events.get_nowait()
            if isinstance(event, tuple):
                if event[0] == 'scan':
                    state['rows'], state['settings'] = event[1:]
                    text.insert('end', f'扫描结束：{len(state["rows"])} 个压缩包。\n')
                else:
                    state['busy'] = False
                    scan_btn.config(state='normal')
                    run_btn.config(state='normal')
                    task_progress.finish('已停止' if stop.is_set() else '任务结束')
            else:
                text.insert('end', event + '\n')
                task_progress.update(event)
            text.see('end')
        root.after(100, poll)
    def close():
        if state['busy']:
            stop.set()
            messagebox.showinfo('正在停止', '已请求停止，请等待当前页面处理结束后关闭窗口。')
        else:
            root.destroy()
    if parent is None:
        root.protocol('WM_DELETE_WINDOW', close)
    poll()
    if parent is None:
        root.mainloop()
    return state, stop


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path, default=Path(r'Z:\Manga'))
    parser.add_argument('--base-url', default='https://www.copy20.com')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--sortable', action='store_true', help='新章节采用固定宽度小数话数命名')
    parser.add_argument('--mapping', type=Path)
    parser.add_argument('--convert', action='store_true', help='执行转换；默认仅预览')
    args = parser.parse_args()
    if args.input is None:
        gui()
        return 0
    mapping = json.loads(args.mapping.read_text(encoding='utf-8-sig')) if args.mapping else {}
    rows = scan(args.input, args.output, Resolver(args.base_url, not args.offline, mapping, args.sortable))
    if args.convert:
        rows = [convert(row) for row in rows]
        print(f'报告：{report(rows, args.output)}')
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 1 if any(row['status'] == '失败' for row in rows) else 0


if __name__ == '__main__':
    raise SystemExit(main())
