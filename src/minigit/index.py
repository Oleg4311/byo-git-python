"""Индекс (staging area) в формате git DIRC версий 2 и 3. Запись всегда версии 2 без расширений: git читает такой индекс без вопросов."""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .objects import GitError

__all__ = ["Index", "IndexEntry"]

FLAG_EXTENDED = 0x4000
FLAG_ASSUME_VALID = 0x8000


@dataclass
class IndexEntry:
    path: bytes
    sha: str
    mode: int
    stage: int = 0
    size: int = 0
    mtime: int = 0
    mtime_ns: int = 0
    ctime: int = 0
    ctime_ns: int = 0
    dev: int = 0
    ino: int = 0
    uid: int = 0
    gid: int = 0
    flags_extra: int = 0  # расширенные флаги версии 3 (skip-worktree, intent-to-add)

    @property
    def key(self) -> tuple[bytes, int]:
        return (self.path, self.stage)


def _u32(v: int) -> int:
    return v & 0xFFFFFFFF


@dataclass
class Index:
    entries: dict[tuple[bytes, int], IndexEntry] = field(default_factory=dict)

    # ---------- доступ ----------

    def get(self, path: bytes, stage: int = 0) -> IndexEntry | None:
        return self.entries.get((path, stage))

    def add(self, e: IndexEntry) -> None:
        if e.stage == 0:
            for s in (1, 2, 3):
                self.entries.pop((e.path, s), None)  # добавление разрешает конфликт
        self.entries[e.key] = e

    def remove(self, path: bytes) -> bool:
        keys = [k for k in self.entries if k[0] == path]
        for k in keys:
            del self.entries[k]
        return bool(keys)

    def sorted_entries(self) -> list[IndexEntry]:
        return [self.entries[k] for k in sorted(self.entries)]

    def conflicted_paths(self) -> list[bytes]:
        return sorted({p for (p, s) in self.entries if s != 0})

    def staged(self) -> dict[bytes, IndexEntry]:
        """Записи нулевой стадии (без конфликтов): путь → запись."""
        return {p: e for (p, s), e in self.entries.items() if s == 0}

    # ---------- чтение и запись ----------

    @staticmethod
    def parse(data: bytes) -> Index:
        if len(data) < 32 or data[:4] != b"DIRC":
            raise GitError("индекс повреждён: нет сигнатуры DIRC")
        if hashlib.sha1(data[:-20]).digest() != data[-20:]:
            raise GitError("индекс повреждён: контрольная сумма не сходится")
        version, count = struct.unpack(">II", data[4:12])
        if version not in (2, 3):
            raise GitError(f"версия индекса {version} не поддерживается (нужна 2 или 3)")
        idx = Index()
        i = 12
        end = len(data) - 20
        for _ in range(count):
            if i + 62 > end:
                raise GitError("индекс оборван")
            ctime, ctime_ns, mtime, mtime_ns, dev, ino, mode, uid, gid, size = struct.unpack(">10I", data[i : i + 40])
            sha = data[i + 40 : i + 60].hex()
            (flags,) = struct.unpack(">H", data[i + 60 : i + 62])
            j = i + 62
            extra = 0
            if flags & FLAG_EXTENDED:
                if version < 3:
                    raise GitError("индекс версии 2 с расширенными флагами")
                (extra,) = struct.unpack(">H", data[j : j + 2])
                j += 2
            name_len = flags & 0xFFF
            if name_len < 0xFFF:
                path = data[j : j + name_len]
            else:  # длинный путь: настоящую длину знает только завершающий ноль
                nul = data.find(b"\0", j)
                if nul < 0:
                    raise GitError("индекс повреждён: путь без завершающего нуля")
                path = data[j:nul]
            if b"\0" in path or len(path) == 0:
                raise GitError("индекс повреждён: неверный путь")
            header = j - i  # 62 или 64 при расширенных флагах
            entry_len = (header + len(path) + 8) & ~7  # запись дополняется 1–8 нулями до кратности 8, как в git
            if i + entry_len > end:
                raise GitError("индекс оборван")
            if any(data[i + header + len(path) : i + entry_len]):
                raise GitError("индекс повреждён: ненулевое выравнивание")
            e = IndexEntry(path, sha, mode, (flags >> 12) & 3, size, mtime, mtime_ns, ctime, ctime_ns, dev, ino, uid, gid, extra)
            idx.entries[e.key] = e
            i += entry_len
        # после записей может идти область расширений (TREE, REUC, UNTR...): они лишь кэши, пропускаем с проверкой границ
        while i < end:
            if i + 8 > end:
                raise GitError("индекс повреждён: оборванное расширение")
            (ext_size,) = struct.unpack(">I", data[i + 4 : i + 8])
            if i + 8 + ext_size > end:
                raise GitError("индекс повреждён: расширение выходит за границу")
            if data[i : i + 1].isupper() is False:
                raise GitError(f"индекс содержит обязательное расширение {data[i : i + 4]!r}, которое не поддерживается")
            i += 8 + ext_size
        return idx

    def serialize(self) -> bytes:
        body = bytearray(b"DIRC" + struct.pack(">II", 2, len(self.entries)))
        for e in self.sorted_entries():
            name_len = min(len(e.path), 0xFFF)
            flags = (e.stage << 12) | name_len
            head = struct.pack(
                ">10I",
                _u32(e.ctime),
                _u32(e.ctime_ns),
                _u32(e.mtime),
                _u32(e.mtime_ns),
                _u32(e.dev),
                _u32(e.ino),
                _u32(e.mode),
                _u32(e.uid),
                _u32(e.gid),
                _u32(e.size),
            )
            rec = head + bytes.fromhex(e.sha) + struct.pack(">H", flags) + e.path
            pad = 8 - (len(rec) % 8)  # от 1 до 8 нулевых байт
            body += rec + b"\0" * pad
        return bytes(body) + hashlib.sha1(bytes(body)).digest()

    @staticmethod
    def load(path: Path) -> Index:
        if not path.exists():
            return Index()
        return Index.parse(path.read_bytes())

    def save(self, path: Path) -> None:
        data = self.serialize()
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="index.tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
