"""Объекты git: blob, tree, commit, tag. Разбор и сериализация побайтно совпадают с настоящим git (иначе хэши разойдутся)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

__all__ = [
    "MODE_DIR",
    "MODE_EXEC",
    "MODE_FILE",
    "MODE_GITLINK",
    "MODE_SYMLINK",
    "Commit",
    "GitError",
    "Identity",
    "Tag",
    "TreeEntry",
    "check_tree_name",
    "hash_object",
    "is_hex_sha",
    "parse_commit",
    "parse_tag",
    "parse_tree",
    "serialize_commit",
    "serialize_tag",
    "serialize_tree",
]

MODE_FILE = 0o100644
MODE_EXEC = 0o100755
MODE_SYMLINK = 0o120000
MODE_DIR = 0o040000
MODE_GITLINK = 0o160000
KINDS = ("blob", "tree", "commit", "tag")


class GitError(Exception):
    """Ошибка работы с репозиторием: сообщение показывается пользователю как есть."""


def hash_object(kind: str, data: bytes) -> str:
    """SHA-1 объекта: хэш от «тип длина\\0» и содержимого, как у git hash-object."""
    if kind not in KINDS:
        raise GitError(f"неизвестный тип объекта: {kind}")
    return hashlib.sha1(f"{kind} {len(data)}".encode() + b"\0" + data).hexdigest()


def is_hex_sha(s: str) -> bool:
    return len(s) == 40 and all(c in "0123456789abcdef" for c in s)


# ---------- tree ----------


def check_tree_name(name: bytes) -> None:
    """Имя записи дерева из чужого репозитория безопасно для рабочего каталога: не «.», не «..», не «.git» (в любом регистре и с обходами NTFS), без слешей."""
    low = name.lower().rstrip(b" .")
    if not name or name in (b".", b"..") or b"/" in name or b"\\" in name or b"\0" in name or low in (b".git", b"git~1"):
        raise GitError(f"небезопасное имя в дереве: {name!r}")


@dataclass(frozen=True)
class TreeEntry:
    mode: int
    name: bytes
    sha: str  # 40 hex

    @property
    def is_dir(self) -> bool:
        return self.mode == MODE_DIR


def _tree_sort_key(e: TreeEntry) -> bytes:
    # git сравнивает имена каталогов так, будто в конце стоит «/»: поэтому «a-b» < «a/» < «a0»
    return e.name + b"/" if e.is_dir else e.name


def serialize_tree(entries: list[TreeEntry]) -> bytes:
    names = [e.name for e in entries]
    if len(set(names)) != len(names):
        raise GitError("в дереве повторяются имена")
    out = bytearray()
    for e in sorted(entries, key=_tree_sort_key):
        if not e.name or b"/" in e.name or b"\0" in e.name or e.name in (b".", b".."):
            raise GitError(f"недопустимое имя в дереве: {e.name!r}")
        out += f"{e.mode:o}".encode() + b" " + e.name + b"\0" + bytes.fromhex(e.sha)
    return bytes(out)


def parse_tree(data: bytes) -> list[TreeEntry]:
    entries: list[TreeEntry] = []
    i = 0
    while i < len(data):
        sp = data.find(b" ", i)
        nul = data.find(b"\0", sp + 1) if sp >= 0 else -1
        if sp < 0 or nul < 0 or nul + 21 > len(data):
            raise GitError("дерево повреждено: запись оборвана")
        mode_s = data[i:sp]
        if not mode_s or not all(48 <= c <= 55 for c in mode_s):
            raise GitError(f"дерево повреждено: режим {mode_s!r}")
        entries.append(TreeEntry(int(mode_s, 8), data[sp + 1 : nul], data[nul + 1 : nul + 21].hex()))
        i = nul + 21
    return entries


# ---------- commit и tag ----------


@dataclass(frozen=True)
class Identity:
    """Автор или коммитер: «Имя <почта> секунды зона»."""

    name: str
    email: str
    time: int
    tz: str = "+0000"

    def format(self) -> bytes:
        return f"{self.name} <{self.email}> {self.time} {self.tz}".encode()

    @staticmethod
    def parse(raw: bytes) -> Identity:
        s = raw.decode("utf-8", "replace")
        lt = s.rfind("<")
        gt = s.rfind(">")
        if lt < 0 or gt < lt:
            raise GitError(f"неверная подпись: {s!r}")
        tail = s[gt + 1 :].split()
        if len(tail) != 2 or not tail[0].lstrip("-").isdigit() or len(tail[1]) != 5:
            raise GitError(f"неверное время в подписи: {s!r}")
        return Identity(s[:lt].rstrip(), s[lt + 1 : gt], int(tail[0]), tail[1])


@dataclass
class Commit:
    tree: str
    parents: list[str]
    author: Identity
    committer: Identity
    message: bytes
    #: прочие заголовки (gpgsig, encoding, mergetag) в исходном порядке: нужны, чтобы хэш не менялся при перезаписи
    extra: list[tuple[bytes, bytes]] = field(default_factory=list)


def _split_headers(data: bytes) -> tuple[list[tuple[bytes, bytes]], bytes]:
    head, sep, body = data.partition(b"\n\n")
    if not sep:
        head, body = data.rstrip(b"\n"), b""
    out: list[tuple[bytes, bytes]] = []
    for line in head.split(b"\n"):
        if line.startswith(b" ") and out:
            key, val = out[-1]
            out[-1] = (key, val + b"\n" + line[1:])  # продолжение многострочного заголовка
        else:
            k, _, v = line.partition(b" ")
            out.append((k, v))
    return out, body


def parse_commit(data: bytes) -> Commit:
    headers, message = _split_headers(data)
    tree = ""
    parents: list[str] = []
    author = committer = None
    extra: list[tuple[bytes, bytes]] = []
    for k, v in headers:
        if k == b"tree":
            tree = v.decode()
        elif k == b"parent":
            parents.append(v.decode())
        elif k == b"author":
            author = Identity.parse(v)
        elif k == b"committer":
            committer = Identity.parse(v)
        else:
            extra.append((k, v))
    if not is_hex_sha(tree) or author is None or committer is None:
        raise GitError("коммит повреждён: нет tree, author или committer")
    return Commit(tree, parents, author, committer, message, extra)


def serialize_commit(c: Commit) -> bytes:
    out = b"tree " + c.tree.encode() + b"\n"
    for p in c.parents:
        out += b"parent " + p.encode() + b"\n"
    out += b"author " + c.author.format() + b"\n"
    out += b"committer " + c.committer.format() + b"\n"
    for k, v in c.extra:
        out += k + b" " + v.replace(b"\n", b"\n ") + b"\n"
    return out + b"\n" + c.message


@dataclass
class Tag:
    object: str
    type: str
    name: str
    tagger: Identity | None
    message: bytes


def parse_tag(data: bytes) -> Tag:
    headers, message = _split_headers(data)
    d = dict(headers)
    if b"object" not in d or b"type" not in d or b"tag" not in d:
        raise GitError("тег повреждён")
    return Tag(d[b"object"].decode(), d[b"type"].decode(), d[b"tag"].decode(), Identity.parse(d[b"tagger"]) if b"tagger" in d else None, message)


def serialize_tag(t: Tag) -> bytes:
    out = b"object " + t.object.encode() + b"\ntype " + t.type.encode() + b"\ntag " + t.name.encode() + b"\n"
    if t.tagger is not None:
        out += b"tagger " + t.tagger.format() + b"\n"
    return out + b"\n" + t.message
