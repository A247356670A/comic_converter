"""Create legacy-style ZIP books while retaining loose-image chapter directories."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import time
import zlib
import zipfile


IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.avif'}
IGNORED = {'.aidoku-conversion.json', '.copy-manga-update.json'}


def natural(value: str):
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r'(\d+)', value)]


def chapter_files(folder: Path) -> list[Path]:
    scanned = list(os.scandir(folder))
    if any(entry.is_symlink() for entry in scanned):
        raise ValueError('包含链接文件')
    if any(entry.is_dir(follow_symlinks=False) for entry in scanned):
        raise ValueError('包含子目录')
    unknown = [entry.name for entry in scanned
               if entry.is_file(follow_symlinks=False)
               and Path(entry.name).suffix.casefold() not in IMAGES
               and entry.name.casefold() not in IGNORED]
    if unknown:
        raise ValueError('包含非图片文件：' + '、'.join(unknown[:3]))
    images = sorted((Path(entry.path) for entry in scanned
                     if entry.is_file(follow_symlinks=False)
                     and Path(entry.name).suffix.casefold() in IMAGES),
                    key=lambda path: natural(path.name))
    if not images:
        raise ValueError('没有图片')
    names = [path.name.casefold() for path in images]
    if len(set(names)) != len(names):
        raise ValueError('存在重名图片')
    return images


def plan_zip_pack(library: Path, cancelled=lambda: False, progress=lambda _: None) -> list[dict]:
    library = Path(library).resolve()
    if not library.is_dir() or library.is_symlink():
        raise ValueError('Komga 漫画目录不存在或是链接目录')
    rows = []
    series_list = sorted((Path(entry.path) for entry in os.scandir(library)
                          if entry.is_dir(follow_symlinks=False)),
                         key=lambda path: path.name.casefold())
    for series_index, series in enumerate(series_list, 1):
        if cancelled():
            break
        progress(f'查找散图章节：{series_index}/{len(series_list)}｜{series.name}')
        for source in sorted((Path(entry.path) for entry in os.scandir(series)
                              if entry.is_dir(follow_symlinks=False)
                              and not entry.name.startswith('.')),
                             key=lambda path: natural(path.name)):
            target = source.with_name(source.name + '.zip')
            row = {'source': str(source), 'target': str(target),
                   'series': series.name, 'chapter': source.name, 'status': '待打包'}
            try:
                if source.parent != series:
                    raise ValueError('章节目录路径不安全')
                # Normal packaging only adds missing archives. Re-hashing every
                # existing ZIP made each run traverse the entire library.
                if target.exists():
                    if not target.is_file() or target.is_symlink():
                        raise ValueError('同名 ZIP 目标不是安全的普通文件')
                    if target.stat().st_size == 0:
                        raise ValueError('同名 ZIP 为 0 字节，请先使用“清理 0 字节 ZIP”')
                    row.update(status='已有 ZIP，跳过', pages=0, size=0)
                    rows.append(row)
                    continue
                images = chapter_files(source)
                row.update(pages=len(images), size=sum(path.stat().st_size for path in images))
            except Exception as exc:
                row.update(status='跳过', error=str(exc))
            rows.append(row)
    return rows


def source_signature(images: list[Path]):
    result = []
    for path in images:
        crc = 0
        size = 0
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                crc = zlib.crc32(chunk, crc)
                size += len(chunk)
        result.append((path.name, size, crc & 0xffffffff))
    return result


def zip_signature(path: Path):
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f'ZIP 内文件校验失败：{bad}')
        if archive.comment:
            raise ValueError('ZIP 含有非预期注释')
        infos = archive.infolist()
        if any(info.is_dir() or '/' in info.filename or '\\' in info.filename for info in infos):
            raise ValueError('ZIP 内不是根目录图片结构')
        if any(Path(info.filename).suffix.casefold() not in IMAGES for info in infos):
            raise ValueError('ZIP 内含非图片文件')
        if any(info.compress_type != zipfile.ZIP_DEFLATED for info in infos):
            raise ValueError('ZIP 压缩方式与旧格式不一致')
        return [(info.filename, info.file_size, info.CRC) for info in infos]


def write_journal(path: Path, value):
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        # Windows/NAS scanners can briefly hold the destination between reads.
        # Retry the atomic replacement instead of aborting a successful pack.
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(min(0.1 * 2 ** attempt, 2.0))
    finally:
        temporary.unlink(missing_ok=True)


def pack_one(row: dict, library: Path, cancelled=lambda: False,
             progress=lambda _: None) -> dict:
    source, target = Path(row['source']), Path(row['target'])
    library = Path(library).resolve()
    temporary = target.with_name(f'.{target.name}.{os.getpid()}.tmp')
    try:
        if cancelled():
            return dict(row, status='已取消')
        resolved = source.resolve()
        if (not source.is_dir() or source.is_symlink()
                or resolved.parent.parent != library
                or target.parent.resolve() != resolved.parent
                or target != source.with_name(source.name + '.zip')):
            raise ValueError('章节路径在预览后改变或超出书库')
        images = chapter_files(source)
        expected = source_signature(images)
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise ValueError('同名 ZIP 目标不是普通文件')
            if zip_signature(target) != expected:
                raise ValueError('同名 ZIP 已存在但内容不同，保留两者并跳过')
            return dict(row, status='已存在且校验一致')

        with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as archive:
            for page_index, image in enumerate(images, 1):
                if cancelled():
                    raise InterruptedError('用户已停止')
                progress(f'打包页面 {page_index}/{len(images)}：{row["series"]} / {row["chapter"]}')
                archive.write(image, image.name)
            archive.comment = b''
        if zip_signature(temporary) != expected:
            raise ValueError('生成的 ZIP 与源图片校验不一致')
        temporary.rename(target)
        return dict(row, status='已完成')
    except InterruptedError:
        return dict(row, status='已取消')
    except Exception as exc:
        return dict(row, status='失败', error=str(exc))
    finally:
        temporary.unlink(missing_ok=True)


def apply_zip_pack(rows: list[dict], library: Path, log_dir: Path,
                   cancelled=lambda: False, progress=lambda _: None):
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / ('zip-pack-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    journal = [dict(row) for row in rows]
    write_journal(log_path, journal)
    eligible = [index for index, row in enumerate(journal)
                if row['status'] in {'待打包', '待校验'}]
    for position, index in enumerate(eligible, 1):
        if cancelled():
            break
        row = journal[index]
        progress(f'打包 {position}/{len(eligible)}：{row["series"]} / {row["chapter"]}')
        journal[index] = pack_one(row, library, cancelled, progress)
        write_journal(log_path, journal)
    return journal, log_path


def plan_zero_zip_cleanup(library: Path, cancelled=lambda: False,
                          progress=lambda _: None) -> list[dict]:
    """List only direct-child ZIP files whose current size is exactly zero bytes."""
    library = Path(library).resolve()
    if not library.is_dir() or library.is_symlink():
        raise ValueError('Komga 漫画目录不存在或是链接目录')
    rows = []
    series_list = sorted((Path(entry.path) for entry in os.scandir(library)
                          if entry.is_dir(follow_symlinks=False)),
                         key=lambda path: path.name.casefold())
    for index, series in enumerate(series_list, 1):
        if cancelled():
            break
        progress(f'检查 0 字节 ZIP：{index}/{len(series_list)}｜{series.name}')
        for entry in os.scandir(series):
            if (not entry.is_file(follow_symlinks=False)
                    or Path(entry.name).suffix.casefold() != '.zip'
                    or entry.stat(follow_symlinks=False).st_size != 0):
                continue
            archive = Path(entry.path)
            folder = archive.with_suffix('')
            row = {'archive': str(archive), 'folder': str(folder), 'series': series.name,
                   'chapter': archive.stem, 'status': '待删除',
                   'folder_exists': folder.exists()}
            if folder.exists() and (not folder.is_dir() or folder.is_symlink()):
                row.update(status='跳过', error='同名路径不是安全的普通目录')
            rows.append(row)
    return rows


def apply_zero_zip_cleanup(rows: list[dict], library: Path, log_dir: Path,
                           cancelled=lambda: False, progress=lambda _: None):
    """Delete an exact zero-byte ZIP and its exact sibling folder after rechecking."""
    library = Path(library).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / ('zero-zip-cleanup-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    journal = [dict(row) for row in rows]
    write_journal(log_path, journal)
    eligible = [index for index, row in enumerate(journal) if row['status'] == '待删除']
    for position, index in enumerate(eligible, 1):
        if cancelled():
            break
        row = journal[index]
        archive, folder = Path(row['archive']), Path(row['folder'])
        progress(f'清理 {position}/{len(eligible)}：{row["series"]} / {row["chapter"]}')
        try:
            resolved_archive = archive.resolve()
            if (not archive.is_file() or archive.is_symlink()
                    or archive.stat().st_size != 0
                    or resolved_archive.parent.parent != library
                    or archive.suffix.casefold() != '.zip'
                    or folder != archive.with_suffix('')):
                raise ValueError('ZIP 状态在确认后改变或路径不安全')
            if folder.exists():
                resolved_folder = folder.resolve()
                if (not folder.is_dir() or folder.is_symlink()
                        or resolved_folder.parent != resolved_archive.parent
                        or resolved_folder.parent.parent != library):
                    raise ValueError('同名散图目录路径不安全')
                # Remove the directory first. If interruption occurs afterward,
                # the zero-byte ZIP remains discoverable for the next cleanup.
                shutil.rmtree(folder)
            archive.unlink()
            journal[index] = dict(row, status='已删除')
        except Exception as exc:
            journal[index] = dict(row, status='失败', error=str(exc))
        write_journal(log_path, journal)
    return journal, log_path
