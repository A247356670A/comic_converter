"""Extensible Tkinter desktop: library audit, chapter ordering, conversion."""
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from library_audit import scan_library, inspect_series, rename_plan, apply_renames
import aidoku_to_komga
import aidoku_library_import
import copy_manga_update
from zip_packager import (apply_zero_zip_cleanup, apply_zip_pack,
                          plan_zero_zip_cleanup, plan_zip_pack)
from source_page_audit import (apply_source_page_deletions,
                               plan_source_page_audit)
from progress_ui import TaskProgress


def enable_windows_dpi_awareness():
    """Ask Windows for native-resolution Tk rendering before creating a window."""
    if os.name != 'nt':
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def configure_fonts(root):
    """Use native point scaling and a modern Chinese UI font."""
    dpi = root.winfo_fpixels('1i')
    root.tk.call('tk', 'scaling', dpi / 72.0)
    family = 'Microsoft YaHei UI' if os.name == 'nt' else tkfont.nametofont('TkDefaultFont').cget('family')
    for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont', 'TkCaptionFont', 'TkSmallCaptionFont'):
        font = tkfont.nametofont(name)
        font.configure(family=family, size=10)
    fixed = tkfont.nametofont('TkFixedFont')
    fixed.configure(family='Cascadia Mono' if os.name == 'nt' else fixed.cget('family'), size=10)
    style = ttk.Style(root)
    style.configure('.', font=(family, 10))
    style.configure('Treeview', font=(family, 10), rowheight=max(28, round(28 * dpi / 96)))
    style.configure('Treeview.Heading', font=(family, 10, 'bold'))


class LibraryPanel(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=10)
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.task_total = None
        self.rows = []
        self.result = None
        self.path = tk.StringVar(value=r'Z:\Manga')
        self.search = tk.StringVar()
        self.status = tk.StringVar(value='选择漫画根目录（目录下每个文件夹是一部漫画），开始检查。')
        bar = ttk.Frame(self)
        bar.pack(fill='x')
        ttk.Label(bar, text='Komga 漫画目录').pack(side='left')
        ttk.Entry(bar, textvariable=self.path).pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(bar, text='选择目录', command=self.choose).pack(side='left')
        actions = ttk.Frame(self)
        actions.pack(fill='x', pady=8)
        self.buttons = []
        for name, command in [('快速检查全部', lambda: self.start(False)), ('完整检查全部', lambda: self.start(True)),
                              ('完整检查选中漫画', self.deep_selected), ('话数排序重命名预览', self.preview),
                              ('一键打包章节为 ZIP', self.pack_all_zip),
                              ('清理 0 字节 ZIP', self.clean_zero_zips),
                              ('网站核对缺页并清理', self.audit_source_pages)]:
            button = ttk.Button(actions, text=name, command=command)
            button.pack(side='left', padx=3)
            self.buttons.append(button)
        ttk.Button(actions, text='停止', command=self.stop.set).pack(side='left', padx=3)
        ttk.Button(actions, text='导出报告', command=self.export).pack(side='left', padx=3)
        ttk.Label(self, text='快速：页码间隙、零字节、清单缺页。完整：另逐张解码、校验转换清单，NAS 大书库可能耗时较长。').pack(anchor='w')
        ttk.Label(self, text='注意：本页内容检查针对散图章节目录；不会解包检查 ZIP。“网站核对缺页”是完整检查的联网扩展，删除前必须逐项勾选。').pack(anchor='w')
        ttk.Label(self, text='疑似缺号不等于确认缺失；没有原始页数，无法可靠发现首尾缺页。最新话按数字比较，保留“试看”等标签。').pack(anchor='w')
        self.task_progress = TaskProgress(self, '当前任务进度')
        self.task_progress.pack(fill='x', pady=6)
        searchbar = ttk.Frame(self)
        searchbar.pack(fill='x', pady=6)
        ttk.Label(searchbar, text='搜索漫画').pack(side='left')
        ttk.Entry(searchbar, textvariable=self.search).pack(side='left', fill='x', expand=True, padx=6)
        self.search.trace_add('write', lambda *_: self.render())
        panes = ttk.Panedwindow(self, orient='vertical')
        panes.pack(fill='both', expand=True)
        table = ttk.Frame(panes)
        self.tree = ttk.Treeview(table, columns=('name', 'chapters', 'latest', 'pages', 'issues', 'mode'), show='headings', selectmode='browse')
        for key, title, width in [('name', '漫画', 350), ('chapters', '章节数', 70), ('latest', '最新话（本地）', 170),
                                  ('pages', '图片数', 80), ('issues', '问题/提示', 80), ('mode', '检查状态', 120)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width)
        scroll = ttk.Scrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.details)
        panes.add(table, weight=3)
        self.detail = ScrolledText(panes, height=12, wrap='word')
        panes.add(self.detail, weight=2)
        ttk.Label(self, textvariable=self.status).pack(fill='x', pady=5)
        self.after(100, self.poll)

    def choose(self):
        path = filedialog.askdirectory()
        if path:
            self.path.set(path)

    def selected(self):
        selection = self.tree.selection()
        return self.rows[int(selection[0])] if selection else None

    def run(self, task):
        if self.busy:
            return
        self.busy = True
        self.stop.clear()
        for b in self.buttons:
            b.configure(state='disabled')
        def worker():
            try:
                task()
            except Exception as exc:
                self.events.put(('error', str(exc)))
            finally:
                self.events.put(('done', None))
        threading.Thread(target=worker, daemon=True).start()

    def start(self, deep):
        if self.busy:
            return
        root = self.path.get()
        self.rows = []
        self.result = None
        self.render()
        self.status.set('开始检查…')
        try:
            total = sum(item.is_dir() and not item.name.startswith('.')
                        for item in Path(root).iterdir())
        except OSError:
            total = None
        self.task_progress.start('准备检查漫画书库', total)
        self.task_total = total
        def task():
            result = scan_library(root, deep, self.stop.is_set,
                lambda r: self.events.put(('row', r)), lambda p: self.events.put(('progress', p)))
            self.events.put(('result', result))
        self.run(task)

    def deep_selected(self):
        row = self.selected()
        if row is None:
            messagebox.showinfo('选择漫画', '请先扫描，再选择一部漫画。')
            return
        self.task_progress.start('准备完整检查：' + row['name'], len(row['chapters']))
        self.task_total = len(row['chapters'])
        self.run(lambda: self.events.put(('replace', inspect_series(Path(row['path']), True, self.stop.is_set,
                         lambda p: self.events.put(('progress', p))))))

    def render(self):
        previous = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        term = self.search.get().casefold()
        for i, row in enumerate(self.rows):
            if term not in row['name'].casefold():
                continue
            chapters = row['chapters']
            mode = '完整解码' if chapters and all(c['mode'] == '完整解码' for c in chapters) else '快速检查'
            if row['cancelled']:
                mode = '已中止/部分结果'
            self.tree.insert('', 'end', iid=str(i), values=(row['name'], len(chapters), row['latest'],
                sum(c['pages'] for c in chapters), len(row['issues']) + sum(len(c['issues']) for c in chapters), mode))
        if previous and self.tree.exists(previous[0]):
            self.tree.selection_set(previous[0])

    def details(self, *_):
        row = self.selected()
        if row is None:
            return
        lines = [row['name'], row['path'], *row['issues']]
        for c in row['chapters']:
            lines.append(f'\n{c["name"]} — {c["pages"]} 页 / {c["mode"]}' + (' / 已中止' if c['cancelled'] else ''))
            lines.extend('  • ' + issue for issue in c['issues'])
            if not c['issues']:
                lines.append('  本次检查未发现异常（不代表已确认内容完整）')
        self.detail.delete('1.0', 'end')
        self.detail.insert('end', '\n'.join(lines))

    def poll(self):
        for _ in range(200):
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'row':
                self.rows.append(data)
                self.render()
                self.task_progress.update('已检查：' + data['name'], len(self.rows),
                                          self.task_total, infer=False)
            elif kind == 'replace':
                self.rows = [data if r['path'] == data['path'] else r for r in self.rows]
                self.render()
                self.details()
                self.task_progress.update('完整检查完成：' + data['name'], len(data['chapters']),
                                          len(data['chapters']), infer=False)
            elif kind == 'progress':
                self.status.set('检查中：' + data)
                self.task_progress.update(data)
            elif kind == 'result':
                self.result = data
            elif kind == 'renamed':
                messagebox.showinfo('重命名完成', f'操作日志：{data}\n请重新扫描书库。Komga 中需重新扫描并检查 Sort Number。')
                self.rows = []
                self.result = None
                self.render()
            elif kind == 'zip_plan':
                rows, decision = data
                eligible = [row for row in rows if row['status'] in {'待打包', '待校验'}]
                skipped = [row for row in rows if row['status'] == '跳过']
                total_size = sum(row.get('size', 0) for row in eligible)
                decision['proceed'] = bool(eligible) and messagebox.askyesno(
                    '打包所有散图章节',
                    f'将把 {len(eligible)} 个章节目录打包为旧格式 ZIP（约 {total_size / 1024**3:.2f} GB）。\n'
                    f'另有 {len(skipped)} 个异常目录会跳过。\n\n'
                    'ZIP 会完整校验；散图目录始终保留，已有同名 ZIP 不会覆盖。是否继续？')
                if decision['proceed']:
                    self.task_progress.start('准备打包散图章节', len(eligible))
                decision['event'].set()
            elif kind == 'packed':
                results, log_path = data
                completed = sum(row['status'] in {'已完成', '已存在且校验一致'} for row in results)
                failed = sum(row['status'] == '失败' for row in results)
                messagebox.showinfo('ZIP 打包结束',
                    f'完成 {completed} 个，失败 {failed} 个。\n操作日志：{log_path}\n'
                    '现在可在 Komga 中执行“扫描所有书库”；任务队列处理完后新章节会出现。')
            elif kind == 'zero_plan':
                rows, decision = data
                eligible = [row for row in rows if row['status'] == '待删除']
                with_folders = sum(row.get('folder_exists', False) for row in eligible)
                decision['proceed'] = bool(eligible) and messagebox.askyesno(
                    '清理 0 字节 ZIP',
                    f'发现 {len(eligible)} 个大小严格为 0 字节的 ZIP；其中 {with_folders} 个存在同名散图目录。\n\n'
                    '继续后会删除这些 0 字节 ZIP 及其同名散图目录。非零 ZIP 不会处理。是否继续？')
                if not eligible:
                    messagebox.showinfo('无需清理', '没有发现 0 字节 ZIP。')
                if decision['proceed']:
                    self.task_progress.start('准备清理 0 字节 ZIP', len(eligible))
                decision['event'].set()
            elif kind == 'zero_cleaned':
                results, log_path = data
                deleted = sum(row['status'] == '已删除' for row in results)
                failed = sum(row['status'] == '失败' for row in results)
                messagebox.showinfo('0 字节 ZIP 清理结束',
                    f'已删除 {deleted} 项，失败 {failed} 项。\n操作日志：{log_path}')
            elif kind == 'source_page_plan':
                report, decision = data
                self.task_progress.wait('等待人工核对并勾选删除候选')
                self.show_source_page_candidates(report, decision)
            elif kind == 'source_page_deleted':
                results, log_path = data
                deleted = sum(row['status'] == '已删除' for row in results)
                failed = sum(row['status'] == '失败' for row in results)
                messagebox.showinfo('网站缺页清理结束',
                    f'已删除 {deleted} 个散图章节及其对应 ZIP，失败 {failed} 个。\n'
                    f'操作日志：{log_path}\n\n失败项不会继续删除；请查看日志中的原因。')
            elif kind == 'error':
                messagebox.showerror('操作未完成', data)
            elif kind == 'done':
                self.busy = False
                for b in self.buttons:
                    b.configure(state='normal')
                self.status.set(f'{"已停止，显示部分结果" if self.stop.is_set() else "任务结束"}；{len(self.rows)} 部漫画')
                self.task_progress.finish('已停止' if self.stop.is_set() else '任务结束')
        self.after(100, self.poll)

    def export(self):
        if self.busy:
            messagebox.showinfo('正在检查', '请等待结束或停止后导出，避免误认为报告完整。')
            return
        path = filedialog.asksaveasfilename(defaultextension='.json', initialfile='komga-library-report.json')
        if path:
            data = dict(self.result or {}, series=self.rows)
            try:
                Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as exc:
                messagebox.showerror('导出失败', str(exc))

    def preview(self):
        row = self.selected()
        if row is None or row['cancelled']:
            messagebox.showinfo('先检查', '请先选择一部已完成检查的漫画。')
            return
        try:
            plan = rename_plan(row)
        except ValueError as exc:
            messagebox.showerror('需要手动处理', str(exc))
            return
        window = tk.Toplevel(self)
        window.title('选中漫画：排序重命名预览')
        window.geometry('850x560')
        ttk.Label(window, text='统一三位小数：第0011.000话 < 第0011.500话 < 第0012.000话。保留番外/试看后缀。').pack(padx=10, pady=6)
        ttk.Label(window, text='仅修正目录名排序；Komga 的 Sort Number 元数据可能仍需调整。应用前请保留数据库备份。').pack(padx=10)
        view = ScrolledText(window, wrap='word')
        view.pack(fill='both', expand=True, padx=10, pady=10)
        view.insert('end', '\n'.join(f'{Path(r["source"]).name} → {Path(r["target"]).name} {r["error"]}' for r in plan) or '无需修改')
        view.configure(state='disabled')
        def apply():
            if self.busy:
                return
            if not messagebox.askyesno('应用目录重命名', f'将重命名这部漫画的 {len(plan)} 个章节目录。\n图片文件保持原样，操作日志保存在项目 rename_logs。\n是否应用？', parent=window):
                return
            window.destroy()
            self.task_progress.start('正在应用章节目录重命名', len(plan))
            self.run(lambda: self.events.put(('renamed', str(apply_renames(plan, Path(__file__).parent / 'rename_logs')))))
        ttk.Button(window, text='应用预览中的重命名', command=apply,
            state='normal' if plan and not any(r['error'] for r in plan) else 'disabled').pack(pady=8)

    def pack_all_zip(self):
        if self.busy:
            return
        library = Path(self.path.get())
        decision = {'event': threading.Event(), 'proceed': False}

        def task():
            rows = plan_zip_pack(library, self.stop.is_set,
                                 lambda text: self.events.put(('progress', text)))
            self.events.put(('zip_plan', (rows, decision)))
            while not decision['event'].wait(0.2):
                if self.stop.is_set():
                    return
            if not decision['proceed'] or self.stop.is_set():
                return
            results, log_path = apply_zip_pack(
                rows, library, Path(__file__).with_name('zip_pack_logs'), self.stop.is_set,
                lambda text: self.events.put(('progress', text)))
            self.events.put(('packed', (results, str(log_path))))

        self.status.set('正在查找散图章节…')
        self.task_progress.start('正在统计可打包的散图章节')
        self.run(task)

    def clean_zero_zips(self):
        if self.busy:
            return
        library = Path(self.path.get())
        decision = {'event': threading.Event(), 'proceed': False}

        def task():
            rows = plan_zero_zip_cleanup(library, self.stop.is_set,
                lambda text: self.events.put(('progress', text)))
            self.events.put(('zero_plan', (rows, decision)))
            while not decision['event'].wait(0.2):
                if self.stop.is_set():
                    return
            if not decision['proceed'] or self.stop.is_set():
                return
            results, log_path = apply_zero_zip_cleanup(
                rows, library, Path(__file__).with_name('zip_pack_logs'), self.stop.is_set,
                lambda text: self.events.put(('progress', text)))
            self.events.put(('zero_cleaned', (results, str(log_path))))

        self.status.set('正在检查 0 字节 ZIP…')
        self.task_progress.start('正在查找 0 字节 ZIP')
        self.run(task)

    def audit_source_pages(self):
        """Run the deliberately separate, network-backed phase of a full audit."""
        if self.busy:
            return
        library = Path(self.path.get())
        decision = {'event': threading.Event(), 'proceed': False, 'rows': []}

        def task():
            here = Path(__file__).resolve().parent
            report = plan_source_page_audit(
                library, here / 'copy_sources.json', here / 'source_preflight.json',
                cancelled=self.stop.is_set,
                progress=lambda text: self.events.put(('progress', text)))
            self.events.put(('source_page_plan', (report, decision)))
            while not decision['event'].wait(0.2):
                if self.stop.is_set():
                    return
            if not decision['proceed'] or self.stop.is_set():
                return
            results, log_path = apply_source_page_deletions(
                decision['rows'], library, here / 'page_audit_logs',
                lambda text: self.events.put(('progress', text)))
            self.events.put(('source_page_deleted', (results, str(log_path))))

        self.status.set('正在读取已配置的网址并逐章核对页数；此阶段不会删除任何文件…')
        self.task_progress.start('正在统计有网址的漫画和散图章节')
        self.run(task)

    def show_source_page_candidates(self, report, decision):
        candidates = report['candidates']
        skipped = report['skipped']
        if not candidates:
            messagebox.showinfo(
                '网站缺页核对完成',
                f'没有发现可确认的缺页章节。\n跳过 {len(skipped)} 项；网址缺失、网站异常或无法唯一对应时均会跳过。')
            decision['event'].set()
            return
        window = tk.Toplevel(self)
        window.title('网站确认缺页：人工勾选删除项')
        window.geometry('1500x720')
        window.transient(self.winfo_toplevel())
        window.grab_set()
        ttk.Label(window, text=(
            '当前只是候选清单，尚未删除。请逐行点击第一列打钩；缺页页码来自网站当前图片列表与本地纯数字散图名的对比。'),
            foreground='#9a4d00').pack(anchor='w', padx=12, pady=(10, 2))
        ttk.Label(window, text=(
            '删除会永久移除整个位于表中的散图章节目录，以及同名 .zip（若存在）。未勾选、无网址、网站读取失败或对应不明确的项目绝不删除。')).pack(anchor='w', padx=12, pady=(0, 8))

        holder = ttk.Frame(window)
        holder.pack(fill='both', expand=True, padx=12)
        columns = ('checked', 'series', 'chapter', 'web', 'local', 'missing', 'zip', 'url')
        view = ttk.Treeview(holder, columns=columns, show='headings', selectmode='browse')
        specs = [('checked', '删除', 60), ('series', '漫画', 300), ('chapter', '散图章节目录', 170),
                 ('web', '网站页数', 80), ('local', '本地页数', 80),
                 ('missing', '确认缺少的页码', 260), ('zip', '同名 ZIP', 90),
                 ('url', '核对网址', 430)]
        for key, title, width in specs:
            view.heading(key, text=title)
            view.column(key, width=width, stretch=key in {'series', 'missing', 'url'})
        yscroll = ttk.Scrollbar(holder, orient='vertical', command=view.yview)
        xscroll = ttk.Scrollbar(holder, orient='horizontal', command=view.xview)
        view.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        yscroll.pack(side='right', fill='y')
        xscroll.pack(side='bottom', fill='x')
        view.pack(fill='both', expand=True)
        count_var = tk.StringVar()

        def render_row(index):
            row = candidates[index]
            values = ('☑' if row['selected'] else '☐', row['series'], row['chapter'],
                      row['website_pages'], row['local_pages'], row['missing_text'],
                      '存在，将删除' if row['archive'] else '无', row['url'])
            iid = str(index)
            if view.exists(iid):
                view.item(iid, values=values)
            else:
                view.insert('', 'end', iid=iid, values=values)
            count_var.set(f'候选 {len(candidates)} 项；已勾选 {sum(row["selected"] for row in candidates)} 项；安全跳过 {len(skipped)} 项')

        for index in range(len(candidates)):
            render_row(index)

        def toggle(event=None):
            if event is not None and view.identify_region(event.x, event.y) not in {'cell', 'tree'}:
                return
            iid = view.identify_row(event.y) if event is not None else (view.selection()[0] if view.selection() else '')
            if not iid:
                return
            if event is not None and view.identify_column(event.x) != '#1':
                return
            candidates[int(iid)]['selected'] = not candidates[int(iid)]['selected']
            render_row(int(iid))

        view.bind('<Button-1>', toggle, add='+')
        view.bind('<space>', lambda _event: toggle())

        footer = ttk.Frame(window)
        footer.pack(fill='x', padx=12, pady=10)
        ttk.Label(footer, textvariable=count_var).pack(side='left')

        def show_skipped():
            detail = tk.Toplevel(window)
            detail.title('安全跳过的项目（不会删除）')
            detail.geometry('1050x650')
            text = ScrolledText(detail, wrap='word')
            text.pack(fill='both', expand=True, padx=10, pady=10)
            text.insert('end', '\n'.join(
                f'{row["series"]}' + (f' / {row["chapter"]}' if row['chapter'] else '')
                + f'\n  {row["reason"]}\n' for row in skipped) or '没有跳过项')
            text.configure(state='disabled')

        ttk.Button(footer, text='查看跳过原因', command=show_skipped).pack(side='right', padx=5)

        def close():
            decision['proceed'] = False
            decision['event'].set()
            window.grab_release()
            window.destroy()

        def delete_checked():
            selected = [row for row in candidates if row['selected']]
            if not selected:
                messagebox.showinfo('尚未勾选', '请点击表格第一列，为确实要删除的章节逐项打钩。', parent=window)
                return
            folder_count = len(selected)
            zip_count = sum(bool(row['archive']) for row in selected)
            if not messagebox.askyesno(
                    '最后确认：永久删除',
                    f'将永久删除 {folder_count} 个已勾选的散图章节目录，以及 {zip_count} 个对应 ZIP。\n\n'
                    '此操作不能撤销。程序会在执行前再次验证文件未发生变化。是否确认删除？',
                    icon='warning', parent=window):
                return
            decision['rows'] = selected
            decision['proceed'] = True
            self.task_progress.start('正在删除已人工确认的缺页章节', len(selected))
            decision['event'].set()
            window.grab_release()
            window.destroy()

        ttk.Button(footer, text='取消，不删除', command=close).pack(side='right', padx=5)
        ttk.Button(footer, text='删除已勾选项…', command=delete_checked).pack(side='right', padx=5)
        window.protocol('WM_DELETE_WINDOW', close)


class HelpPanel(ttk.Frame):
    """In-app reference for every visible control and the intended workflows."""
    CONTENT = r'''Komga 工具箱功能说明

建议先看：三个常用流程

所有需要等待的前台任务都有统一进度区：显示当前正在处理的漫画/章节/页面、完成数量、百分比、已用时间、预计剩余时间和预计总耗时。刚开始统计、等待网站重试或等待人工确认时无法可靠估算，会明确显示“计算中”或“等待操作”；完成若干项目后预计时间会按本轮实际速度动态修正。后台更新区也会自动显示整批任务的时间估算。

流程 A｜Aidoku 下载了“一部新漫画的一话”
1. 打开“Aidoku 新漫画导入”。
2. 扫描并解析网址：已有漫画整部跳过，新漫画显示为“待导入”。
3. 导入新漫画并更新到最新：验证 CopyManga 章节 UUID 后，转换种子话并补齐网站章节。
4. 回到“书库检查与话数排序”，点击“一键打包章节为 ZIP”。
5. 在 Komga 执行“扫描所有书库”，等待 Komga 任务队列结束。

流程 B｜更新已经存在的漫画
1. 打开“网站补缺与更新”，先“检查全部更新”。
2. 确认本地最新、网站最新和缺失话数后，点击“补齐到最新”。
3. 下载结束后使用“一键打包章节为 ZIP”。
4. 最后让 Komga 扫描书库。

流程 C｜怀疑散图损坏或缺页
1. 在“书库检查与话数排序”先快速检查。
2. 选中可疑漫画，点击“完整检查选中漫画”。
3. 查看下方详情。完整检查能发现无法解码、被截断或校验值变化的图片。
4. 若要确认首尾缺页，点击“网站核对缺页并清理”。它只处理 copy_sources.json 中已有对应网址的漫画。
5. 在候选表中查看明确缺失的页码，逐项人工打钩；再次确认后才删除整章散图和对应 ZIP。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
一、书库检查与话数排序

Komga 漫画目录 / 选择目录
指定书库根目录。正常结构是“书库 / 漫画 / 章节”。
例：Z:\Manga\飛翔的魔女\第0012.000话\001.png

快速检查全部
检查所有散图章节目录，但不真正解码每张图片。主要检查：
• 漫画和章节目录能否读取；
• 是否没有图片、存在零字节图片或子目录；
• 001、002、004 这种页码间隙；
• 转换清单声明的图片是否缺失；
• 纯数字正篇是否从第 1 话开始、是否存在明显话数间隙。
例：目录有 001.png、002.png、004.png，会提示疑似缺第 3 页。
适合日常快速巡检。注意：它只检查散图章节目录，不会打开 ZIP。

完整检查全部
包含快速检查的全部项目，并逐张打开、验证、完整解码图片；若章节由转换器生成，还会计算散图哈希并与转换清单比较。
例：005.png 文件大小不是 0，但文件尾部已损坏；快速检查可能看不出来，完整检查会报告图片无法解码。
代价：会读取大量 NAS 数据，几百部漫画可能需要很长时间。

完整检查选中漫画
只对表格中当前选中的一部漫画执行完整检查。适合先快速检查全部，再精查有问题的作品。
例：快速检查发现某漫画问题数为 2，选中它后只完整解码这一部，不必重查整个书库。

网站核对缺页并清理
这是完整检查的联网扩展。程序读取 copy_sources.json 中已经登记的 CopyManga 网址，逐章取得网站当前图片总数，再与本地纯数字散图名比较。
例：网站某话有 48 页，本地只有 001–020、022–047，候选表会写明“缺少 021、048”。这能发现本地编号连续但末页缺失的情况。
没有对应网址的漫画会整部跳过；网站限制、目录不完整、同编号版本无法唯一选择、章节无法对应、非纯数字页名或本地有超出网站范围的页码时也会安全跳过，绝不会因此生成删除项。
候选表默认全部不勾选。必须点击第一列逐项打钩，再经过永久删除确认，才会删除该散图章节目录及其同名 .zip。执行前还会验证目录和 ZIP 自检查后没有变化；变化则拒绝删除并记入日志。
注意：网页核对需要逐章访问网站，大书库可能耗时很长；可点“停止”。检查阶段本身完全不会删除文件。

话数排序重命名预览
预览并应用散图章节目录的固定宽度命名，使小数话按数值顺序排列。
例：第11话 → 第0011.000话；第11.5话 → 第0011.500话。
这样文件名排序就是 11、11.5、12，而不会把 11.5 放在 11 前面。仅处理散图目录；应用前会显示所有改名项目和冲突。

一键打包章节为 ZIP
把每个合格的散图章节目录新增为同名 ZIP，格式与旧书库一致：ZIP 根目录直接是 001.png、002.png……，使用 Deflate 压缩，无额外目录和注释。
例：第0011.500话\001.png → 新增 第0011.500话.zip。
散图目录始终保留。已有非零同名 ZIP 会直接跳过，不再逐个重新解压和计算校验，因此重复运行的前置检查会明显缩短；已有 ZIP 永不覆盖。0 字节 ZIP 会提示先使用专用清理功能。隐藏的下载临时目录不会处理。

清理 0 字节 ZIP
只查找大小严格等于 0 字节的 .zip。确认后删除该 ZIP 以及同名散图目录。
例：第0012.000话.zip 为 0 字节时，同时删除“第0012.000话”目录；第0013.000话.zip 即使只有 1 字节也不会处理。
这是破坏性功能，执行前会显示数量并再次确认，日志会记录结果。

停止
请求当前前台任务在安全边界停止。已经完整写入的结果保留；正在生成但尚未完成的临时内容不会作为正式章节使用。

导出报告
把当前书库检查结果保存为 JSON，便于之后查找问题。检查仍在运行时不允许导出，以免把部分结果误认为完整报告。

搜索漫画
只过滤当前表格显示，不修改文件。清空搜索框即可恢复全部结果。

结果表格与下方详情
• 章节数、最新话、图片数均来自散图目录检查；ZIP 不计入这些数值。
• “问题/提示”是线索数量，不代表每项都确认损坏。
• 点击一行可查看具体章节、页数和错误。
• “疑似缺话”只根据编号间隙判断；番外、拆分话或网站从中间开始发布时可能不是错误。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
二、Aidoku 转换

输入文件夹
选择 Aidoku 下载的 CBZ 所在目录；程序会递归查找 .cbz 和 .zip。

输出文件夹
通常选择 Z:\Manga。源文件始终保留，转换器不会覆盖无法确认一致的已有章节。

补全网站
默认 https://www.copy20.com。只有 CBZ 缺少漫画名或章节名时才尝试联网补全。

名称映射 JSON（可选）
把网站名称或 slug 映射到指定本地漫画目录名。
例：{"网站漫画名":"本地漫画名_作者"}。

仅在 CBZ 缺少标题时联网补全
开启时，优先使用 ComicInfo.xml；字段缺失才访问网页。关闭后，缺少必要元数据的文件会直接失败，更适合完全离线检查。

排序兼容命名
开启后，新话使用第0011.000话、第0011.500话格式；卷仍使用 005、010 等三位数字。

1. 扫描预览
读取元数据、校验压缩包结构和图片列表、计算源文件哈希，并显示将写入的漫画名和章节名。不会写入书库。

2. 开始自动转换
必须先按相同设置完成预览。转换时逐页转为 PNG，写入临时目录，全部验证后才成为正式章节。已有目标不会盲目覆盖。

停止
安全停止扫描或转换；未完成章节不会以正式目标目录出现。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
三、Aidoku 新漫画导入

Aidoku 下载目录
默认 Z:\未命名文件夹\Downloads 2。预期结构为 zh.copymanga\漫画slug\章节UUID.cbz。

Komga 漫画目录
新漫画和下载章节的目标书库，通常为 Z:\Manga。

1. 扫描并解析网址
从 CBZ 内的 Aidoku mangaKey 生成 CopyManga 详情页，并读取 Series 与 chapterKey。不会靠书名猜网址，也不会修改书库。
判断为已有漫画的条件包括：同一 slug 已配置、书库有完全同名目录，或有“漫画名_作者”目录。已有漫画整部跳过。
例：CBZ 的 mangaKey 为 badgirl，详情页即 https://www.copy20.com/comic/badgirl。

2. 导入新漫画并更新到最新
先用章节 UUID 反查网站，确认 CBZ 确实属于该漫画；再转换已下载的一话、保存来源对应关系并补齐网站目录中的缺失话。
如果网站同一话有多个翻译分组，会沿用种子 CBZ 所属分组；仍无法唯一确定时停止该漫画，避免误下。

状态列
• 待导入：网址已解析，但尚未联网验证和写入。
• 已跳过：漫画已经在书库中。
• 已完成：种子话与网站缺失话处理完成。
• 失败：该漫画未能安全继续；其他漫画仍会继续处理。

停止
在当前安全步骤结束后停止。已经导入并登记的漫画，可以到“网站补缺与更新”继续。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
四、网站补缺与更新

漫画目录 / CopyManga 详情页
用于手工建立一部本地漫画与网站 slug 的对应关系。
例：漫画目录 Z:\Manga\BadGirl；详情页 https://www.copy20.com/comic/badgirl。

保存对应关系
验证目录必须位于 Z:\Manga，验证网址为 CopyManga 漫画详情页，然后写入 copy_sources.json。它只保存关系，不立即下载。

移除选中对应关系
只删除工具中的网站链接配置，不删除漫画文件、Komga 数据或阅读进度。

规范已有卷名
把卷目录统一成三位数字，话目录不动。
例：第5卷或 5 → 005；第12卷 → 012。
有同名冲突时不会执行任何改名。

1. 检查全部更新
逐部读取网站目录，与本地 ZIP、CBZ和散图章节比较，计算本地最新、网站最新及缺失项目。只读，不下载。
例：本地最新 18，网站最新 21，可能显示缺 19、20、21；也会发现 7.5 等历史缺话。
检查模式下单部失败会记录错误并继续下一部。

2. 补齐到最新
必须先完成同一份配置的检查。下载缺失卷和章节，逐页验证后才写入正式章节目录；已有同类型同编号内容不会覆盖。
遇到网站限制、内容不一致或无法选择的重复版本时会停止，防止连续错误。

停止
请求前台检查或更新停止。已经完成的章节保留。

启动/续跑后台更新
启动独立后台进程，使用 source_preflight.json 中已经确认且有本地重合的来源继续下载。关闭 GUI 后后台仍可运行；再次启动会从仍缺失的章节继续。

查看后台进度
弹窗显示本轮作品数、章节数、当前漫画、当前话、页面进度、重试等待和最近错误。

停止后台更新
写入停止请求；后台在当前安全边界结束后退出，不强杀进程、不删除已完成章节。

后台更新自动刷新区域
无需点击即可每秒读取状态文件。章节进度条表示整批完成量，页面进度条表示当前话的下载页数，并显示已用时间、预计剩余时间和预计总耗时。“等待网站恢复”说明正在按退避时间重试，并不等于程序卡死。

更新表格及顶部栏排序
点击“漫画、本地最新、网站最新、缺失话数、状态”标题即可排序；再次点击同一标题切换升降序。未知值始终放在最后。

常见错误解释
• HTTP 520/网络异常：程序会按限定次数等待重试；仍失败时停止，稍后可以续跑。
• 章节目录数量不完整：网站可能触发限制，程序不会把残缺目录误当完整目录。
• 同类型同编号重复：没有足够证据选择版本；新漫画导入可依据种子分组解决一部分情况。
• 本地最新高于网站：可能对应关系错误或网站目录受限，需要先核对。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
五、Komga 入库与文件格式

下载或转换完成并不等于 Komga 已经显示。推荐顺序是：
散图章节完成 → 一键打包为 ZIP → Komga“扫描所有书库” → 等待 Komga 后台任务队列结束。

ZIP 打包按钮不会删除散图，所以磁盘会同时保留目录和 ZIP。Komga 识别 ZIP 作为书籍；书库检查页则继续用散图目录做逐页检查。

如果普通扫描后仍缺少已经存在的非零 ZIP，可在 Komga 执行 Deep Scan。不要用“清理 0 字节 ZIP”处理正常 ZIP；该按钮只会列出严格为 0 字节的文件。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
六、报告与日志位置

• ZIP 打包和 0 字节清理：程序目录\zip_pack_logs
• 网站缺页确认后的删除：程序目录\page_audit_logs
• 话数目录重命名：程序目录\rename_logs
• 卷名规范：程序目录\volume_rename_logs
• 后台更新：程序目录\update_logs

日志用于确认完成、失败和中断位置。发生错误时，保留日志并重新扫描通常比手工移动未确认文件更安全。
'''

    def __init__(self, parent):
        super().__init__(parent, padding=10)
        ttk.Label(self, text='功能说明与示例', font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(self, text='按 F1 可随时切换到本页。建议第一次使用某个按钮前先查看对应章节。').pack(anchor='w', pady=(2, 8))
        view = ScrolledText(self, wrap='word', padx=12, pady=10)
        view.pack(fill='both', expand=True)
        view.insert('1.0', self.CONTENT)
        view.tag_configure('title', font=('Microsoft YaHei UI', 16, 'bold'), spacing3=10)
        view.tag_configure('heading', font=('Microsoft YaHei UI', 12, 'bold'),
                           foreground='#1558a6', spacing1=12, spacing3=4)
        view.tag_add('title', '1.0', '1.end')
        line_count = int(view.index('end-1c').split('.')[0])
        for line in range(1, line_count + 1):
            text = view.get(f'{line}.0', f'{line}.end')
            if (text.startswith('一、') or text.startswith('二、') or text.startswith('三、')
                    or text.startswith('四、') or text.startswith('五、') or text.startswith('六、')
                    or text.startswith('流程 ')):
                view.tag_add('heading', f'{line}.0', f'{line}.end')
        view.configure(state='disabled')


def main():
    enable_windows_dpi_awareness()
    root = tk.Tk()
    configure_fonts(root)
    root.title('Komga 书库工具箱')
    root.geometry('1180x800')
    root.minsize(950, 650)
    tabs = ttk.Notebook(root)
    tabs.pack(fill='both', expand=True)
    library = LibraryPanel(tabs)
    tabs.add(library, text='书库检查与话数排序')
    converter = ttk.Frame(tabs)
    tabs.add(converter, text='Aidoku 转换')
    converter_state, converter_stop = aidoku_to_komga.gui(converter)
    importer = ttk.Frame(tabs)
    tabs.add(importer, text='Aidoku 新漫画导入')
    importer_state, importer_stop = aidoku_library_import.gui(importer)
    updater = ttk.Frame(tabs)
    tabs.add(updater, text='网站补缺与更新')
    updater_state, updater_stop = copy_manga_update.gui(updater)
    help_panel = HelpPanel(tabs)
    tabs.add(help_panel, text='功能说明（F1）')
    root.bind('<F1>', lambda _event: tabs.select(help_panel))
    def close():
        if library.busy or converter_state['busy'] or importer_state['busy'] or updater_state['busy']:
            library.stop.set()
            converter_stop.set()
            importer_stop.set()
            updater_stop.set()
            messagebox.showinfo('正在停止', '请等待当前工作结束后关闭；已完成的转换/重命名会保留。')
        else:
            root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    root.mainloop()


if __name__ == '__main__':
    main()
