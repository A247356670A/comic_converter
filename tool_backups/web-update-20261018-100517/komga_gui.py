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
                              ('完整检查选中漫画', self.deep_selected), ('话数排序重命名预览', self.preview)]:
            button = ttk.Button(actions, text=name, command=command)
            button.pack(side='left', padx=3)
            self.buttons.append(button)
        ttk.Button(actions, text='停止', command=self.stop.set).pack(side='left', padx=3)
        ttk.Button(actions, text='导出报告', command=self.export).pack(side='left', padx=3)
        ttk.Label(self, text='快速：页码间隙、零字节、清单缺页。完整：另逐张解码、校验转换清单，NAS 大书库可能耗时较长。').pack(anchor='w')
        ttk.Label(self, text='疑似缺号不等于确认缺失；没有原始页数，无法可靠发现首尾缺页。最新话按数字比较，保留“试看”等标签。').pack(anchor='w')
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
            elif kind == 'replace':
                self.rows = [data if r['path'] == data['path'] else r for r in self.rows]
                self.render()
                self.details()
            elif kind == 'progress':
                self.status.set('检查中：' + data)
            elif kind == 'result':
                self.result = data
            elif kind == 'renamed':
                messagebox.showinfo('重命名完成', f'操作日志：{data}\n请重新扫描书库。Komga 中需重新扫描并检查 Sort Number。')
                self.rows = []
                self.result = None
                self.render()
            elif kind == 'error':
                messagebox.showerror('操作未完成', data)
            elif kind == 'done':
                self.busy = False
                for b in self.buttons:
                    b.configure(state='normal')
                self.status.set(f'{"已停止，显示部分结果" if self.stop.is_set() else "任务结束"}；{len(self.rows)} 部漫画')
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
            self.run(lambda: self.events.put(('renamed', str(apply_renames(plan, Path(__file__).parent / 'rename_logs')))))
        ttk.Button(window, text='应用预览中的重命名', command=apply,
            state='normal' if plan and not any(r['error'] for r in plan) else 'disabled').pack(pady=8)


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
    def close():
        if library.busy or converter_state['busy']:
            library.stop.set()
            converter_stop.set()
            messagebox.showinfo('正在停止', '请等待当前工作结束后关闭；已完成的转换/重命名会保留。')
        else:
            root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    root.mainloop()


if __name__ == '__main__':
    main()
