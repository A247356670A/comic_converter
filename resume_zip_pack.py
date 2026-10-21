"""Resume only the missing-ZIP rows from a previous ZIP plan with live status."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time

from zip_packager import apply_zip_pack


HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / 'zip_pack_logs'
STATUS = LOG_DIR / 'latest_zip_status.json'
LIBRARY = Path(r'Z:\Manga')


def write_status(value):
    value['updated_at'] = datetime.now().isoformat(timespec='seconds')
    temporary = STATUS.with_name(f'{STATUS.name}.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        for attempt in range(12):
            try:
                os.replace(temporary, STATUS)
                break
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(min(0.1 * 2 ** attempt, 2.0))
    finally:
        temporary.unlink(missing_ok=True)


def run(plan_log: Path):
    LOG_DIR.mkdir(exist_ok=True)
    source = json.loads(plan_log.read_text(encoding='utf-8-sig'))
    rows = [row for row in source if row.get('status') == '待打包']
    status = {'running': True, 'pid': os.getpid(), 'started': datetime.now().isoformat(timespec='seconds'),
              'source_plan': str(plan_log), 'total': len(rows), 'completed': 0,
              'current': '准备开始', 'current_page': '', 'result_log': '',
              'failed': 0, 'outcome': '', 'error': ''}
    write_status(status)

    def progress(message):
        status['current'] = message
        outer = re.search(r'^打包\s+(\d+)\s*/\s*(\d+)', message)
        page = re.search(r'^打包页面\s+(\d+)\s*/\s*(\d+)', message)
        if outer:
            status['completed'] = max(0, int(outer[1]) - 1)
            status['current_page'] = ''
        elif page:
            status['current_page'] = f'{page[1]}/{page[2]} 页'
        write_status(status)
        print(message, flush=True)

    try:
        results, result_log = apply_zip_pack(rows, LIBRARY, LOG_DIR, progress=progress)
        status['result_log'] = str(result_log)
        status['completed'] = sum(row.get('status') in {'已完成', '已存在且校验一致'}
                                  for row in results)
        status['failed'] = sum(row.get('status') == '失败' for row in results)
        status['running'] = False
        status['outcome'] = 'complete' if not status['failed'] else 'complete_with_errors'
        status['current'] = '全部处理结束'
        write_status(status)
        print(json.dumps({'completed': status['completed'], 'failed': status['failed'],
                          'log': str(result_log)}, ensure_ascii=False), flush=True)
        return 0 if not status['failed'] else 1
    except Exception as exc:
        status['running'] = False
        status['outcome'] = 'fatal'
        status['error'] = str(exc)
        status['current'] = '任务异常停止'
        write_status(status)
        print('FATAL: ' + str(exc), file=sys.stderr, flush=True)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-log', type=Path, required=True)
    args = parser.parse_args()
    return run(args.plan_log.resolve())


if __name__ == '__main__':
    sys.exit(main())
