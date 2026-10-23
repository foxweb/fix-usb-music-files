#!/usr/bin/env python3
"""Выставляет дату создания и дату изменения аудиофайлов по номеру трека.

Плеер в машине играет сначала более новые файлы, потом более старые.
Поэтому внутри каждой папки-альбома трек 1 получает самую новую дату,
следующий трек — на 2 секунды старше, и так далее. Шаг 2 секунды нужен
из-за FAT32: дата изменения там хранится с точностью до 2 секунд.

Содержимое файлов не меняется, только метки времени.
По умолчанию скрипт ничего не записывает, а печатает план.

На Linux у os.stat нет даты создания. Она читается через statx.
Записать на FAT можно только дату изменения: ядро не принимает новую дату создания.

Если два альбома скопированы почти одновременно, их интервалы раздвигаются,
чтобы при общей сортировке по дате альбомы не перемешивались.
Дата самого нового файла на флешке не сдвигается в будущее.

Запуск:
  python3 /Volumes/MUSIC/fix_track_dates.py
  python3 /Volumes/MUSIC/fix_track_dates.py --apply

Если плеер на самом деле играет от старых к новым:
  python3 /Volumes/MUSIC/fix_track_dates.py --oldest-first
  python3 /Volumes/MUSIC/fix_track_dates.py --oldest-first --apply
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
STEP_SECONDS = 2
CAN_SET_BIRTH = sys.platform == "darwin"

AT_FDCWD = -100
AT_SYMLINK_NOFOLLOW = 0x100
STATX_BTIME = 0x00000800

AUDIO_EXT = {
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".wma",
    ".aiff",
    ".aif",
    ".ape",
    ".alac",
}

SKIP_DIRS = {
    ".fseventsd",
    ".Spotlight-V100",
    ".Trashes",
    "System Volume Information",
    "LOST.DIR",
}

# 1-06 Title  -> диск 1, трек 6
DISC_TRACK_RE = re.compile(r"^(\d{1,2})-(\d{1,3})(?!\d)")
# 01. Title / 01 - Title / 01_Title / 01 Title
TRACK_RE = re.compile(r"^(\d{1,3})[.\-_\s]")

ATTR_BIT_MAP_COUNT = 5
ATTR_CMN_CRTIME = 0x00000200


class attrlist(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort),
        ("commonattr", ctypes.c_uint),
        ("volattr", ctypes.c_uint),
        ("dirattr", ctypes.c_uint),
        ("fileattr", ctypes.c_uint),
        ("forkattr", ctypes.c_uint),
    ]


class timespec(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_long),
        ("tv_nsec", ctypes.c_long),
    ]


_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def parse_track(filename: str) -> tuple[int, int] | None:
    stem = os.path.splitext(filename)[0]
    match = DISC_TRACK_RE.match(stem)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = TRACK_RE.match(stem)
    if match:
        return 1, int(match.group(1))
    return None


def sort_key(filename: str) -> tuple:
    parsed = parse_track(filename)
    if parsed is None:
        return (1, 0, 0, filename.casefold())
    disc, track = parsed
    return (0, disc, track, filename.casefold())


def even_timestamp(ts: float) -> int:
    value = int(ts)
    return value - (value % STEP_SECONDS)


def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


class statx_timestamp(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("_reserved", ctypes.c_int32),
    ]


class statx_buf(ctypes.Structure):
    # Полный struct statx из linux/stat.h — 256 байт, до 0x100.
    # Ядро пишет структуру целиком. Короткий буфер затирает память и даёт SIGSEGV.
    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("_spare0", ctypes.c_uint16),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", statx_timestamp),
        ("stx_btime", statx_timestamp),
        ("stx_ctime", statx_timestamp),
        ("stx_mtime", statx_timestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("stx_mnt_id", ctypes.c_uint64),
        ("stx_dio_mem_align", ctypes.c_uint32),
        ("stx_dio_offset_align", ctypes.c_uint32),
        ("_spare3", ctypes.c_uint64 * 12),
    ]


if sys.platform.startswith("linux") and ctypes.sizeof(statx_buf) != 256:
    raise RuntimeError(f"statx buffer is {ctypes.sizeof(statx_buf)} bytes, expected 256")


_statx = getattr(_libc, "statx", None)
if _statx is not None:
    _statx.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(statx_buf),
    ]
    _statx.restype = ctypes.c_int


def statx_btime(path: str) -> float | None:
    if _statx is None:
        return None
    buf = statx_buf()
    result = _statx(
        AT_FDCWD,
        os.fsencode(path),
        AT_SYMLINK_NOFOLLOW,
        STATX_BTIME,
        ctypes.byref(buf),
    )
    if result != 0 or not (buf.stx_mask & STATX_BTIME):
        return None
    return buf.stx_btime.tv_sec + buf.stx_btime.tv_nsec / 1_000_000_000


def birth_time(path: str, stat: os.stat_result) -> float:
    value = getattr(stat, "st_birthtime", None)
    if value is not None:
        return float(value)
    value = statx_btime(path)
    if value is not None:
        return value
    return float(stat.st_mtime)


def set_creation_time(path: str, timestamp: int) -> None:
    attr = attrlist()
    attr.bitmapcount = ATTR_BIT_MAP_COUNT
    attr.commonattr = ATTR_CMN_CRTIME
    spec = timespec(int(timestamp), 0)
    result = _libc.setattrlist(
        os.fsencode(path),
        ctypes.byref(attr),
        ctypes.byref(spec),
        ctypes.sizeof(spec),
        0,
    )
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), path)


def set_both_times(path: str, timestamp: int) -> None:
    os.utime(path, (timestamp, timestamp))
    if CAN_SET_BIRTH:
        set_creation_time(path, timestamp)


def is_audio(filename: str) -> bool:
    if filename.startswith("._") or filename.startswith("."):
        return False
    return os.path.splitext(filename)[1].lower() in AUDIO_EXT


def collect_albums(root: str) -> tuple[list[dict], list[str]]:
    albums = []
    problems = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and not name.startswith(".")]
        tracks = []
        for filename in filenames:
            if not is_audio(filename):
                continue
            path = os.path.join(dirpath, filename)
            try:
                stat = os.lstat(path)
            except OSError as exc:
                problems.append(f"битая запись, пропуск: {path} ({exc.strerror})")
                continue
            tracks.append(
                {
                    "path": path,
                    "name": filename,
                    "birth": birth_time(path, stat),
                    "mtime": stat.st_mtime,
                    "parsed": parse_track(filename),
                }
            )

        if len(tracks) < 2:
            continue

        tracks.sort(key=lambda item: sort_key(item["name"]))
        albums.append({"dir": dirpath, "tracks": tracks})

    albums.sort(key=lambda album: album["dir"].casefold())
    return albums, problems


def album_anchor(album: dict) -> int:
    return even_timestamp(max(track["birth"] for track in album["tracks"]))


def plan_times(albums: list[dict], oldest_first: bool) -> dict[str, int]:
    """Раскладывает альбомы в непересекающиеся интервалы.

    Альбомы идут от более новых к более старым по текущей дате создания.
    Внутри альбома трек 1 — край интервала, с которого плеер начнёт играть.
    """
    ranked = sorted(albums, key=lambda album: (-album_anchor(album), album["dir"].casefold()))
    ceiling = None
    planned: dict[str, int] = {}

    for album in ranked:
        natural = album_anchor(album)
        top = natural if ceiling is None else min(natural, ceiling)
        last_index = len(album["tracks"]) - 1
        for index, track in enumerate(album["tracks"]):
            if oldest_first:
                timestamp = top - (last_index - index) * STEP_SECONDS
            else:
                timestamp = top - index * STEP_SECONDS
            planned[track["path"]] = timestamp
        oldest = top - last_index * STEP_SECONDS
        ceiling = oldest - STEP_SECONDS

    return planned


def shown_time(track: dict) -> float:
    if CAN_SET_BIRTH:
        return track["birth"]
    return track["mtime"]


def needs_update(track: dict, timestamp: int) -> bool:
    if int(track["mtime"]) != timestamp:
        return True
    return CAN_SET_BIRTH and int(track["birth"]) != timestamp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать даты. Без этого флага скрипт только печатает план",
    )
    parser.add_argument(
        "--oldest-first",
        action="store_true",
        help="трек 1 получает самую старую дату (если плеер играет от старых к новым)",
    )
    parser.add_argument(
        "--album",
        help="обработать только папки, в пути которых есть эта подстрока",
    )
    args = parser.parse_args()

    if args.apply:
        print("РЕЖИМ: запись дат")
    else:
        print("РЕЖИМ: просмотр, файлы не меняются. Для записи добавьте --apply")

    if args.oldest_first:
        print("Порядок дат: трек 1 — самый старый, дальше новее на 2 секунды.")
    else:
        print("Порядок дат: трек 1 — самый новый, дальше старше на 2 секунды.")
        print("Так плеер, который начинает с новых файлов, играет альбом с первого трека.")

    if not CAN_SET_BIRTH:
        print("На Linux записывается дата изменения. Дату создания на FAT ядро не меняет.")
        print("Слева в списке — текущая дата изменения. Альбомы раскладываются по дате создания.")

    all_albums, problems = collect_albums(ROOT)
    planned = plan_times(all_albums, args.oldest_first)

    albums = all_albums
    if args.album:
        needle = args.album.casefold()
        albums = [album for album in albums if needle in album["dir"].casefold()]

    changed = 0
    unchanged = 0
    unnumbered = 0
    duplicates = 0

    for album in albums:
        rel = os.path.relpath(album["dir"], ROOT)
        tracks = album["tracks"]
        print(f"\n== {rel}  ({len(tracks)} треков)")

        seen_numbers: dict[tuple[int, int], int] = {}
        for track in tracks:
            if track["parsed"] is None:
                unnumbered += 1
            else:
                seen_numbers[track["parsed"]] = seen_numbers.get(track["parsed"], 0) + 1

        for number, count in sorted(seen_numbers.items()):
            if count > 1:
                duplicates += 1
                print(f"  ! номер {number[1]:02d} встречается {count} раз, порядок между ними по имени")

        for index, track in enumerate(tracks, start=1):
            timestamp = planned[track["path"]]
            marker = "  " if track["parsed"] else "??"
            note = "" if track["parsed"] else "  [нет номера, в конец альбома по имени]"
            old = fmt(shown_time(track))
            new = fmt(timestamp)
            arrow = "==" if not needs_update(track, timestamp) else "->"
            print(f"  {marker} {index:02d}  {old} {arrow} {new}  {track['name']}{note}")
            if needs_update(track, timestamp):
                changed += 1
                if args.apply:
                    set_both_times(track["path"], timestamp)
            else:
                unchanged += 1

    if args.apply:
        if hasattr(os, "sync"):
            os.sync()
        else:
            _libc.sync()

    print("\n---")
    print(f"Альбомов: {len(albums)}")
    print(f"Файлов, у которых дата изменится: {changed}")
    print(f"Файлов, у которых дата уже такая: {unchanged}")
    print(f"Файлов без номера в имени: {unnumbered}")
    print(f"Повторяющихся номеров: {duplicates}")
    print(f"Битых записей: {len(problems)}")
    for problem in problems:
        print(f"  ! {problem}")

    if not args.apply:
        script = os.path.abspath(__file__)
        print(f"\nФайлы не изменены. После проверки: python3 {script} --apply")
    else:
        if CAN_SET_BIRTH:
            print("\nДаты записаны. Перед тем как вынимать флешку, извлеките том в Finder.")
        else:
            print("\nДаты изменения записаны. Перед тем как вынимать флешку, размонтируйте её.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
