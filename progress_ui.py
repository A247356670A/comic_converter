"""Shared Tk progress display with elapsed time and conservative ETA handling."""
from __future__ import annotations

import re
import time
from tkinter import ttk
import tkinter as tk


FRACTION = re.compile(r'(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)')


def format_duration(seconds):
    if seconds is None or seconds < 0:
        return '计算中'
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f'{hours}小时{minutes:02d}分{seconds:02d}秒'
    if minutes:
        return f'{minutes}分{seconds:02d}秒'
    return f'{seconds}秒'


def estimate_remaining(elapsed, completed, total):
    if not total or completed <= 0 or completed > total or elapsed < 0:
        return None
    return max(0.0, elapsed / completed * (total - completed))


class TaskProgress(ttk.LabelFrame):
    """A reusable progress strip. Unknown work uses indeterminate animation."""

    def __init__(self, parent, title='任务进度'):
        super().__init__(parent, text=title, padding=8)
        self.current_var = tk.StringVar(value='当前：等待任务')
        self.count_var = tk.StringVar(value='进度：—')
        self.time_var = tk.StringVar(value='已用时间：0秒｜预计剩余：—｜预计总耗时：—')
        ttk.Label(self, textvariable=self.current_var, wraplength=1050).pack(fill='x')
        self.bar = ttk.Progressbar(self, mode='determinate', maximum=1, value=0)
        self.bar.pack(fill='x', pady=(5, 3))
        line = ttk.Frame(self)
        line.pack(fill='x')
        ttk.Label(line, textvariable=self.count_var).pack(side='left')
        ttk.Label(line, textvariable=self.time_var).pack(side='right')
        self.running = False
        self.waiting = False
        self.started = None
        self.phase_started = None
        self.finished_elapsed = 0.0
        self.completed = 0
        self.total = None
        self._tick()

    def start(self, current='正在准备…', total=None):
        self.running = True
        self.waiting = False
        self.started = time.monotonic()
        self.phase_started = self.started
        self.finished_elapsed = 0.0
        self.completed = 0
        self.total = total if total and total > 0 else None
        self.current_var.set('当前：' + current)
        self._render_bar()
        self._render_time()

    def update(self, current=None, completed=None, total=None, infer=True):
        if not self.running:
            self.start(current or '正在处理…', total)
        if current:
            self.current_var.set('当前：' + current)
        inferred = None
        if infer and current:
            matches = FRACTION.findall(current)
            if matches:
                inferred = tuple(map(int, matches[-1]))
        if completed is None and inferred:
            completed, total = inferred
        if total is not None and total > 0:
            if total != self.total or (completed is not None and completed < self.completed):
                self.phase_started = time.monotonic()
            self.total = total
            self.waiting = False
        if completed is not None:
            self.completed = max(0, min(completed, self.total if self.total else completed))
        self._render_bar()
        self._render_time()

    def wait(self, current):
        if not self.running:
            self.start(current)
        self.waiting = True
        self.current_var.set('当前：' + current)
        self._render_bar()
        self._render_time()

    def finish(self, current='任务结束'):
        if self.started is not None:
            self.finished_elapsed = time.monotonic() - self.started
        self.running = False
        self.waiting = False
        if self.total is not None:
            self.completed = self.total
        self.current_var.set('当前：' + current)
        self._render_bar()
        self._render_time()

    def _elapsed(self):
        if self.running and self.started is not None:
            return time.monotonic() - self.started
        return self.finished_elapsed

    def _render_bar(self):
        known = self.total is not None and not self.waiting
        if known:
            if str(self.bar.cget('mode')) != 'determinate':
                self.bar.stop()
                self.bar.configure(mode='determinate')
            self.bar.configure(maximum=max(self.total, 1), value=min(self.completed, self.total))
            percent = 100 * self.completed / self.total if self.total else 0
            self.count_var.set(f'进度：{self.completed}/{self.total}（{percent:.1f}%）')
        elif self.running:
            if str(self.bar.cget('mode')) != 'indeterminate':
                self.bar.configure(mode='indeterminate', maximum=100)
                self.bar.start(12)
            self.count_var.set('进度：正在计算总量' if not self.waiting else '进度：等待中')
        else:
            self.bar.stop()
            self.bar.configure(mode='determinate', maximum=1, value=0)
            self.count_var.set('进度：—')

    def _render_time(self):
        elapsed = self._elapsed()
        phase_elapsed = (time.monotonic() - self.phase_started
                         if self.running and self.phase_started is not None else elapsed)
        remaining = (None if self.waiting else
                     estimate_remaining(phase_elapsed, self.completed, self.total))
        total_estimate = elapsed + remaining if remaining is not None else None
        remaining_text = '等待操作' if self.waiting else format_duration(remaining)
        self.time_var.set(
            f'已用时间：{format_duration(elapsed)}｜预计剩余：{remaining_text}'
            f'｜预计总耗时：{format_duration(total_estimate)}')

    def _tick(self):
        if self.running:
            self._render_time()
        self.after(500, self._tick)
