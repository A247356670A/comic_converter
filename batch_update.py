"""Resumable, single-threaded updater for source mappings already preflighted."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time

from copy_manga_update import (Client, TemporaryNetworkProblem, parse_preferred_groups,
                               plan_update, save_chapter)

HERE = Path(__file__).resolve().parent
PREFLIGHT = HERE / 'source_preflight.json'
LOG_DIR = HERE / 'update_logs'
STATUS = LOG_DIR / 'latest_status.json'
STOP = LOG_DIR / 'stop_requested'
LIBRARY = Path(r'Z:\Manga').resolve()


def write_json(path: Path, value):
    value['updated_at'] = datetime.now().isoformat(timespec='seconds')
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


def run(max_chapters=0):
    if not PREFLIGHT.is_file():
        raise ValueError('缺少来源预检结果')
    data = json.loads(PREFLIGHT.read_text(encoding='utf-8'))
    queue = sorted(((Path(path), row) for path, row in data.items()
                    if row.get('status') == 'ok' and row.get('overlap', 0) > 0),
                   key=lambda item: (len(item[1]['missing']), item[1]['slug']))
    if not queue:
        print('没有需要更新且来源已确认的漫画')
        return 0
    LOG_DIR.mkdir(exist_ok=True)
    if STOP.exists():
        raise RuntimeError(f'检测到停止标记：{STOP}；请先在 GUI 中清除，再重新运行')
    log_path = LOG_DIR / ('batch-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.jsonl')
    status = {'running': True, 'started': datetime.now().isoformat(timespec='seconds'),
              'total_series': len(queue), 'planned_chapters': sum(len(row['missing']) for _, row in queue),
              'completed_series': 0, 'completed_chapters': 0, 'current_series': '',
              'current_chapter': '', 'current_kind': '', 'current_page': '',
              'last_error': '', 'log': str(log_path),
              'phase': '准备中', 'outcome': '', 'retry': ''}

    def network_retry(message):
        status['retry'] = message
        status['phase'] = '等待网站恢复'
        write_json(STATUS, status)

    client = Client(delay=1.5, on_retry=network_retry, cancelled=STOP.exists)

    def record(stream, event, **fields):
        payload = {'time': datetime.now().isoformat(timespec='seconds'), 'event': event, **fields}
        stream.write(json.dumps(payload, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
        write_json(STATUS, status)
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    write_json(STATUS, status)
    with log_path.open('x', encoding='utf-8') as journal:
        try:
            for path, row in queue:
                if STOP.exists():
                    break
                resolved = path.resolve()
                if not path.is_dir() or path.is_symlink() or not resolved.is_relative_to(LIBRARY):
                    raise ValueError(f'无效的漫画路径：{path}')
                slug = row['slug']
                status['current_series'] = str(path)
                status['current_chapter'] = ''
                status['current_kind'] = ''
                status['current_page'] = ''
                status['retry'] = ''
                status['phase'] = '检查网站目录'
                preferred_groups = parse_preferred_groups(row.get('preferred_groups', ()))
                catalog = client.catalog(slug, preferred_groups=preferred_groups)
                plan = plan_update(path, catalog)
                latest = plan['local_latest']
                volumes = [c for c in plan['missing'] if c.kind == 'volume']
                missing_chapters = [c for c in plan['missing'] if c.kind == 'chapter']
                chapters = (volumes
                            + [c for c in missing_chapters if latest is None or c.number > latest]
                            + [c for c in missing_chapters if latest is not None and c.number <= latest])
                record(journal, 'series_start', path=str(path), slug=slug,
                       missing=len(chapters), remote_latest=str(plan['remote_latest']))
                for chapter in chapters:
                    if STOP.exists():
                        break
                    status['current_chapter'] = str(chapter.number)
                    status['current_kind'] = chapter.kind
                    status['current_page'] = ''
                    status['phase'] = '下载章节'
                    status['retry'] = ''

                    def progress(message):
                        status['current_page'] = message
                        status['phase'] = '下载章节'
                        status['retry'] = ''
                        write_json(STATUS, status)

                    try:
                        for restart in range(3):
                            try:
                                outcome = save_chapter(client, slug, path, chapter, STOP.exists, progress)
                                break
                            except TemporaryNetworkProblem as exc:
                                if restart == 2:
                                    raise
                                seconds = (120, 300)[restart]
                                status['retry'] = f'{exc}；{seconds} 秒后重新下载本话（{restart + 1}/2）'
                                status['phase'] = '等待网站恢复'
                                record(journal, 'chapter_retry', path=str(path), number=str(chapter.number),
                                       kind=chapter.kind,
                                       error=str(exc), wait_seconds=seconds)
                                client.wait(seconds)
                    except InterruptedError:
                        status['running'] = False
                        status['outcome'] = 'stopped'
                        status['phase'] = '用户已停止'
                        record(journal, 'stopped', completed_series=status['completed_series'],
                               completed_chapters=status['completed_chapters'])
                        return 0
                    if outcome == '已完成':
                        status['completed_chapters'] += 1
                    status['retry'] = ''
                    record(journal, 'chapter_done', path=str(path), slug=slug,
                           number=str(chapter.number), kind=chapter.kind, result=outcome)
                    if max_chapters and status['completed_chapters'] >= max_chapters:
                        status['running'] = False
                        status['outcome'] = 'limit_reached'
                        status['phase'] = '测试上限已达到'
                        record(journal, 'limit_reached', chapters=status['completed_chapters'])
                        return 0
                if STOP.exists():
                    break
                status['completed_series'] += 1
                record(journal, 'series_done', path=str(path), slug=slug)
            status['running'] = False
            status['outcome'] = 'stopped' if STOP.exists() else 'complete'
            status['phase'] = '用户已停止' if STOP.exists() else '全部完成'
            record(journal, 'stopped' if STOP.exists() else 'complete',
                   completed_series=status['completed_series'], completed_chapters=status['completed_chapters'])
            return 0
        except Exception as exc:
            status['running'] = False
            status['outcome'] = 'fatal'
            status['phase'] = '已停止：需要处理'
            status['last_error'] = str(exc)
            record(journal, 'fatal', path=status['current_series'], chapter=status['current_chapter'],
                   error=str(exc))
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-chapters', type=int, default=0, help='仅做小批量验证，0 为全部')
    args = parser.parse_args()
    return run(args.max_chapters)


if __name__ == '__main__':
    sys.exit(main())
