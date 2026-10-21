"""Discover Aidoku CopyManga downloads, import only new series, then update them."""
from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

from aidoku_to_komga import Resolver, convert, scan
from copy_manga_update import BASE, Client, inspect_local, plan_update, run_update, UUID, SLUG
from progress_ui import TaskProgress


MAX_XML = 2 * 1024 * 1024


def canonical_name(value: str) -> str:
    return unicodedata.normalize('NFKC', value).strip().casefold()


def archive_metadata(archive: Path) -> dict:
    """Read and strictly cross-check the Aidoku source keys in one archive."""
    archive = Path(archive)
    slug_from_path = archive.parent.name
    chapter_from_path = archive.stem
    if not SLUG.fullmatch(slug_from_path) or not UUID.fullmatch(chapter_from_path):
        raise ValueError('文件夹漫画标识或 CBZ 章节 UUID 格式无效')
    with zipfile.ZipFile(archive) as z:
        infos = [i for i in z.infolist()
                 if PurePosixPath(i.filename.replace('\\', '/')).name.casefold() == 'comicinfo.xml']
        if len(infos) != 1:
            raise ValueError('CBZ 必须且只能包含一个 ComicInfo.xml')
        if infos[0].file_size > MAX_XML:
            raise ValueError('ComicInfo.xml 过大')
        raw = z.read(infos[0])
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('不支持包含实体声明的 XML')
    root = ET.fromstring(raw)
    fields = {element.tag.split('}')[-1]: (element.text or '').strip() for element in root}
    series = fields.get('Series', '')
    notes = fields.get('Notes', '')
    if not series:
        raise ValueError('ComicInfo.xml 缺少漫画名')
    if '!AIDOKUDATA' not in notes:
        raise ValueError('ComicInfo.xml 缺少 Aidoku 来源数据')
    try:
        data, _ = json.JSONDecoder().raw_decode(notes.split('!AIDOKUDATA', 1)[1].lstrip())
    except (ValueError, TypeError) as exc:
        raise ValueError('Aidoku 来源数据无法解析') from exc
    slug, chapter_id = data.get('mangaKey'), data.get('chapterKey')
    if slug != slug_from_path or chapter_id != chapter_from_path:
        raise ValueError('CBZ 内的来源标识与文件路径不一致')
    return {'series': series, 'slug': slug, 'chapter_id': chapter_id,
            'archive': str(archive), 'url': f'{BASE}/comic/{slug}'}


def load_mapping(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                          for k, v in value.items()):
        raise ValueError(f'{path.name} 格式无效')
    return value


def load_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError(f'{path.name} 格式无效')
    return value


def existing_series(series: str, slug: str, library: Path, mappings: dict, children=None):
    slug_paths = [Path(path) for path, mapped_slug in mappings.items() if mapped_slug == slug]
    existing_mapped = [path for path in slug_paths if path.is_dir()]
    if existing_mapped:
        return existing_mapped[0], '网站标识已在更新列表中'
    if slug_paths:
        raise ValueError('该网站标识已有对应关系，但原漫画目录不存在；请先修复旧对应关系')

    wanted = canonical_name(series)
    children = children if children is not None else [
        path for path in library.iterdir() if path.is_dir() and not path.is_symlink()]
    exact = [path for path in children if canonical_name(path.name) == wanted]
    authored = [path for path in children if canonical_name(path.name).startswith(wanted + '_')]
    matches = exact or authored
    if len(matches) > 1:
        raise ValueError('书库中有多个同名候选目录，无法安全判断是否已存在')
    return (matches[0], '漫画名已存在于书库') if matches else (None, '')


def discover(source: Path, library: Path, mapping_file: Path, progress=lambda _: None,
             cancelled=lambda: False) -> list[dict]:
    source, library = Path(source).resolve(), Path(library).resolve()
    if not source.is_dir():
        raise ValueError('Aidoku 下载目录不存在')
    if not library.is_dir() or library.is_symlink():
        raise ValueError('Komga 漫画目录不存在或是链接目录')
    mappings = load_mapping(mapping_file)
    library_children = [path for path in library.iterdir() if path.is_dir() and not path.is_symlink()]
    archives = sorted((p for p in source.rglob('*.cbz') if p.is_file()),
                      key=lambda p: str(p).casefold())
    rows, seen = [], {}
    for index, archive in enumerate(archives, 1):
        if cancelled():
            break
        row = {'archive': str(archive), 'status': '错误'}
        try:
            row.update(archive_metadata(archive))
            if row['slug'] in seen:
                previous = seen[row['slug']]
                previous.update(status='错误', error='同一漫画发现多个下载文件；请只保留一话后重新扫描',
                                detail='同一漫画发现多个下载文件；请只保留一话后重新扫描')
                raise ValueError('同一漫画发现多个下载文件；请只保留一话后重新扫描')
            seen[row['slug']] = row
            found, reason = existing_series(row['series'], row['slug'], library, mappings,
                                            library_children)
            if found:
                row.update(status='已跳过', existing=str(found), detail=reason)
            else:
                row.update(status='待导入', detail='已解析网址，等待网站验证')
        except Exception as exc:
            row['error'] = str(exc)
            row['detail'] = str(exc)
        rows.append(row)
        progress(f'扫描 {index}/{len(archives)}：{row.get("series", archive.parent.name)}｜{row["status"]}')
    return rows


def write_json_atomic(path: Path, value):
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(min(0.1 * 2 ** attempt, 1))
    finally:
        temporary.unlink(missing_ok=True)


def preflight_record(slug: str, plan: dict, overlap: int, preferred_groups=()) -> dict:
    return {'slug': slug, 'status': 'ok',
            'local_latest': str(plan['local_latest']) if plan['local_latest'] is not None else None,
            'remote_latest': str(plan['remote_latest']) if plan['remote_latest'] is not None else None,
            'missing': [str(chapter.number) for chapter in plan['missing']], 'overlap': overlap,
            'preferred_groups': [
                {'kind': kind, 'group': group} for kind, group in sorted(preferred_groups)]}


def import_and_update(row: dict, library: Path, mapping_file: Path, preflight_file: Path,
                      client: Client, cancelled=lambda: False, progress=lambda _: None) -> dict:
    """Validate source, convert the seed chapter, persist mapping, and fill all missing chapters."""
    if row.get('status') != '待导入':
        return dict(row)
    library = Path(library).resolve()
    # Re-check the current library and mapping immediately before writing.
    mappings = load_mapping(mapping_file)
    found, reason = existing_series(row['series'], row['slug'], library, mappings)
    if found:
        return dict(row, status='已跳过', existing=str(found), detail=reason)

    progress(f'{row["series"]}：验证网站目录与下载章节')
    catalog = client.catalog(row['slug'], preferred_ids={row['chapter_id']})
    matching = [chapter for chapter in catalog if chapter.id == row['chapter_id']]
    if len(matching) != 1:
        raise ValueError('下载的这一话未在网站可识别目录中找到，未创建漫画目录')
    chapter = matching[0]
    preferred_groups = {(chapter.kind, chapter.group)}

    seed_rows = scan(Path(row['archive']).parent, library,
                     Resolver(BASE, online=False, sortable=True), lambda _: None, cancelled)
    if len(seed_rows) != 1 or seed_rows[0].get('status') == '失败':
        reason = seed_rows[0].get('error') if seed_rows else '没有找到待转换 CBZ'
        raise ValueError('下载话检查失败：' + reason)
    seed = seed_rows[0]
    seed['chapter'] = chapter.title
    seed['target'] = str(library / seed['series'] / chapter.title)
    converted = convert(seed, cancelled, progress)
    if converted['status'] not in {'已完成', '已跳过（校验一致）'}:
        raise RuntimeError('下载话转换失败：' + converted.get('error', converted['status']))

    series_path = Path(converted['target']).parent.resolve()
    local = inspect_local(series_path)
    overlap = sum((item.kind, item.number) in local for item in catalog)
    if overlap < 1:
        raise RuntimeError('转换后仍无法确认本地章节与网站重合，未保存更新关系')
    plan = plan_update(series_path, catalog)

    mappings = load_mapping(mapping_file)
    conflicting = [path for path, slug in mappings.items()
                   if slug == row['slug'] and Path(path).resolve() != series_path]
    if conflicting:
        raise RuntimeError('更新关系在处理期间发生冲突，已停止')
    mappings[str(series_path)] = row['slug']
    write_json_atomic(mapping_file, mappings)
    preflight = load_object(preflight_file)
    preflight[str(series_path)] = preflight_record(row['slug'], plan, overlap, preferred_groups)
    write_json_atomic(preflight_file, preflight)

    progress(f'{row["series"]}：已导入下载话，开始补齐 {len(plan["missing"])} 项')
    _, results = run_update(series_path, row['slug'], client, cancelled, progress,
                            write=True, catalog=catalog)
    failures = [result for result in results if result['status'] == '失败']
    # Refresh resumable state even when a later chapter failed.
    refreshed = plan_update(series_path, catalog)
    local = inspect_local(series_path)
    overlap = sum((item.kind, item.number) in local for item in catalog)
    preflight = load_object(preflight_file)
    preflight[str(series_path)] = preflight_record(row['slug'], refreshed, overlap, preferred_groups)
    write_json_atomic(preflight_file, preflight)
    if failures:
        raise RuntimeError('已导入并保存续传关系，但自动更新停止：' + failures[0]['error'])
    return dict(row, status='已完成', target=str(series_path),
                detail=f'已导入 1 话，并新增 {sum(r["status"] == "已完成" for r in results)} 项')


def gui(parent):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    here = Path(__file__).resolve().parent
    frame = ttk.Frame(parent, padding=10)
    frame.pack(fill='both', expand=True)
    source_var = tk.StringVar(value=r'Z:\未命名文件夹\Downloads 2')
    library_var = tk.StringVar(value=r'Z:\Manga')
    status_var = tk.StringVar(value='先扫描；已有漫画整部跳过，新漫画会验证网址后导入并更新。')
    events, stop = queue.Queue(), threading.Event()
    state = {'busy': False, 'rows': [], 'snapshot': None}

    def path_row(label, variable):
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text=label, width=18).pack(side='left')
        ttk.Entry(row, textvariable=variable).pack(side='left', fill='x', expand=True, padx=6)
        def choose():
            selected = filedialog.askdirectory(initialdir=variable.get())
            if selected:
                variable.set(selected)
        ttk.Button(row, text='选择目录', command=choose).pack(side='left')
    path_row('Aidoku 下载目录', source_var)
    path_row('Komga 漫画目录', library_var)
    ttk.Label(frame, text='网址来自 CBZ 内的 Aidoku mangaKey，并用章节 UUID 反查网站目录；不会靠书名猜链接，也不会覆盖已有漫画。').pack(anchor='w', pady=5)
    task_progress = TaskProgress(frame, '当前任务进度')
    task_progress.pack(fill='x', pady=5)

    columns = [('series', '漫画', 360), ('slug', 'CopyManga 标识', 260),
               ('url', '详情页', 360), ('status', '处理状态', 260)]
    table_box = ttk.Frame(frame)
    table = ttk.Treeview(table_box, columns=tuple(c[0] for c in columns), show='headings')
    for key, label, width in columns:
        table.heading(key, text=label)
        table.column(key, width=width)
    table_box.pack(fill='both', expand=True, pady=6)
    table_scroll = ttk.Scrollbar(table_box, orient='vertical', command=table.yview)
    table.configure(yscrollcommand=table_scroll.set)
    table_scroll.pack(side='right', fill='y')
    table.pack(side='left', fill='both', expand=True)
    log = tk.Text(frame, height=9, wrap='word')
    log.pack(fill='both', expand=True)

    def render():
        table.delete(*table.get_children())
        for index, row in enumerate(state['rows']):
            detail = row['status'] + ('：' + row.get('detail', '') if row.get('detail') else '')
            table.insert('', 'end', iid=str(index), values=(row.get('series', Path(row['archive']).parent.name),
                row.get('slug', '—'), row.get('url', '—'), detail))

    def set_busy(value):
        state['busy'] = value
        scan_btn.configure(state='disabled' if value else 'normal')
        run_btn.configure(state='disabled' if value else 'normal')

    def start(write=False):
        if state['busy']:
            return
        source, library = source_var.get(), library_var.get()
        snapshot = (str(Path(source)), str(Path(library)))
        if write:
            if not state['rows'] or state['snapshot'] != snapshot:
                messagebox.showinfo('需要扫描', '请先按当前目录扫描预览。')
                return
            pending = [row for row in state['rows'] if row['status'] == '待导入']
            if not pending:
                messagebox.showinfo('没有新漫画', '扫描结果中没有需要导入的新漫画。')
                return
            skipped = sum(row['status'] == '已跳过' for row in state['rows'])
            if not messagebox.askyesno('导入并更新新漫画',
                    f'将导入并更新 {len(pending)} 部新漫画；已存在的 {skipped} 部保持不变。\n'
                    '下载源 CBZ 保留，已有章节不覆盖。是否开始？'):
                return
        stop.clear()
        set_busy(True)
        task_progress.start('准备导入并更新新漫画' if write else '正在统计并解析 Aidoku 下载',
                            sum(row['status'] == '待导入' for row in state['rows']) if write else None)

        def worker():
            try:
                if not write:
                    rows = discover(Path(source), Path(library), here / 'copy_sources.json',
                                    lambda msg: events.put(('progress', msg)), stop.is_set)
                    events.put(('scan', rows, snapshot))
                else:
                    client = Client(on_retry=lambda msg: events.put(('progress', msg)), cancelled=stop.is_set)
                    pending = [(index, row) for index, row in enumerate(list(state['rows']))
                               if row['status'] == '待导入']
                    for position, (index, row) in enumerate(pending, 1):
                        if stop.is_set():
                            break
                        events.put(('progress', f'处理 {position}/{len(pending)}：{row["series"]}'))
                        try:
                            result = import_and_update(row, Path(library), here / 'copy_sources.json',
                                                       here / 'source_preflight.json', client,
                                                       stop.is_set, lambda msg: events.put(('progress', msg)))
                        except Exception as exc:
                            result = dict(row, status='失败', detail=str(exc), error=str(exc))
                        events.put(('row', index, result))
                        events.put(('log', f'{result["status"]}：{row["series"]}｜{result.get("detail", "")}'))
            except Exception as exc:
                events.put(('error', str(exc)))
            finally:
                events.put(('done', write))
        threading.Thread(target=worker, daemon=True).start()

    actions = ttk.Frame(frame)
    actions.pack(fill='x', pady=5, before=table_box)
    scan_btn = ttk.Button(actions, text='1. 扫描并解析网址', command=start)
    scan_btn.pack(side='left', padx=4)
    run_btn = ttk.Button(actions, text='2. 导入新漫画并更新到最新', command=lambda: start(True))
    run_btn.pack(side='left', padx=4)
    ttk.Button(actions, text='停止', command=stop.set).pack(side='left', padx=4)
    ttk.Label(frame, textvariable=status_var).pack(fill='x')

    def poll():
        for _ in range(200):
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'progress':
                status_var.set(event[1])
                task_progress.update(event[1])
            elif event[0] == 'scan':
                state['rows'], state['snapshot'] = event[1], event[2]
                render()
                new = sum(row['status'] == '待导入' for row in state['rows'])
                skipped = sum(row['status'] == '已跳过' for row in state['rows'])
                errors = sum(row['status'] == '错误' for row in state['rows'])
                log.insert('end', f'扫描完成：新漫画 {new}，已存在跳过 {skipped}，错误 {errors}。\n')
                log.see('end')
            elif event[0] == 'row':
                state['rows'][event[1]] = event[2]
                render()
            elif event[0] == 'log':
                log.insert('end', event[1] + '\n')
                log.see('end')
            elif event[0] == 'error':
                messagebox.showerror('操作未完成', event[1])
            elif event[0] == 'done':
                set_busy(False)
                task_progress.finish('已停止' if stop.is_set() else '任务结束')
                status_var.set('已停止；已完成内容保留，可再次扫描后续跑。' if stop.is_set()
                               else '任务结束；未创建的失败项可重试，已登记作品可在“网站补缺与更新”页续跑。')
        parent.after(100, poll)

    parent.after(100, poll)
    return state, stop
