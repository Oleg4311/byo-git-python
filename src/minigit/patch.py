"""Вывод `git diff`: заголовки файлов (new/deleted file, режимы, index) и хунки в формате unified."""

from __future__ import annotations

import os
from collections.abc import Callable

from .diff import diff_lines, format_hunks, is_binary, split_lines
from .objects import MODE_DIR, GitError, hash_object

__all__ = ["Side", "diff_sides", "tree_side"]

ABBREV = 7
ZERO = "0" * 40
#: сторона сравнения: путь → (режим, хэш, функция чтения содержимого)
Side = dict[bytes, tuple[int, str, Callable[[], bytes]]]


def _reader(read_blob: Callable[[str], bytes], sha: str) -> Callable[[], bytes]:
    return lambda: read_blob(sha)


def tree_side(read_blob: Callable[[str], bytes], flat: dict[bytes, tuple[int, str]]) -> Side:
    return {p: (m, s, _reader(read_blob, s)) for p, (m, s) in flat.items()}


def _name(path: bytes) -> str:
    # как git: имена с необычными знаками берутся в кавычки; здесь достаточно обычного вывода для читаемых имён
    return os.fsdecode(path)


def _file_pair(
    path: bytes, old: tuple[int, str, Callable[[], bytes]] | None, new: tuple[int, str, Callable[[], bytes]] | None, context: int
) -> bytes:
    name = _name(path)
    head = f"diff --git a/{name} b/{name}\n"
    if old is None and new is not None:
        head += f"new file mode {new[0]:o}\n"
        old_sha, new_sha = ZERO, new[1]
        mode_part = ""  # режим новому файлу уже указан строкой new file mode
    elif new is None and old is not None:
        head += f"deleted file mode {old[0]:o}\n"
        old_sha, new_sha = old[1], ZERO
        mode_part = ""
    elif old is not None and new is not None:
        old_sha, new_sha = old[1], new[1]
        if old[0] != new[0]:
            head += f"old mode {old[0]:o}\nnew mode {new[0]:o}\n"
            mode_part = ""
        else:
            mode_part = f" {new[0]:o}"
        if old_sha == new_sha:
            return head.encode()  # изменился только режим
    else:
        raise GitError("пустая пара файлов")
    head += f"index {old_sha[:ABBREV]}..{new_sha[:ABBREV]}{mode_part}\n"
    a_data = old[2]() if old else b""
    b_data = new[2]() if new else b""
    if is_binary(a_data) or is_binary(b_data):
        left = f"a/{name}" if old else "/dev/null"
        right = f"b/{name}" if new else "/dev/null"
        return (head + f"Binary files {left} and {right} differ\n").encode()
    if not a_data and not b_data:
        return head.encode()  # пустой файл создан или удалён: хунков нет
    a_lines, b_lines = split_lines(a_data), split_lines(b_data)
    body = format_hunks(a_lines, b_lines, diff_lines(a_lines, b_lines), context)
    left = f"--- a/{name}\n" if old else "--- /dev/null\n"
    right = f"+++ b/{name}\n" if new else "+++ /dev/null\n"
    return (head + left + right).encode() + body


def diff_sides(old: Side, new: Side, paths: list[bytes] | None = None, context: int = 3) -> bytes:
    """Дифф двух состояний (деревьев, индекса, рабочего каталога): по путям в порядке возрастания."""
    out = bytearray()
    for p in sorted(set(old) | set(new)):
        if paths and not any(p == q or p.startswith(q.rstrip(b"/") + b"/") for q in paths):
            continue
        o, n = old.get(p), new.get(p)
        if o is not None and n is not None and o[0] == n[0] and o[1] == n[1]:
            continue
        if (o and o[0] == MODE_DIR) or (n and n[0] == MODE_DIR):
            continue
        out += _file_pair(p, o, n, context)
    return bytes(out)


def blob_sha(data: bytes) -> str:
    return hash_object("blob", data)
