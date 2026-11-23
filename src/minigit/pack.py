"""Пакеты git: чтение pack (v2) и idx (v2) с дельтами OFS/REF, запись пакета без дельт и его индекса."""

from __future__ import annotations

import hashlib
import struct
import zlib
from collections.abc import Callable, Iterable
from pathlib import Path

from .objects import GitError, hash_object

__all__ = ["PackFile", "apply_delta", "write_pack"]

_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_TYPE_NUM = {v: k for k, v in _TYPES.items()}
#: предел размера восстанавливаемого объекта (защита от «дельта-бомб»: крошечная дельта, раздувающаяся до огромного объекта)
MAX_OBJECT_SIZE = 1 << 30
OFS_DELTA = 6
REF_DELTA = 7


def _varint(data: bytes, i: int) -> tuple[int, int]:
    """Целое переменной длины из дельты: младшие 7 бит вперёд."""
    result = shift = 0
    while True:
        if i >= len(data):
            raise GitError("дельта оборвана")
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return result, i


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Применяет дельту git (инструкции «копировать из основы» и «вставить»). Размеры проверяются строго."""
    src_size, i = _varint(delta, 0)
    dst_size, i = _varint(delta, i)
    if dst_size > MAX_OBJECT_SIZE:
        raise GitError(f"дельта обещает объект в {dst_size} байт: слишком много")
    if src_size != len(base):
        raise GitError(f"дельта рассчитана на основу в {src_size} байт, а их {len(base)}")
    out = bytearray()
    while i < len(delta):
        op = delta[i]
        i += 1
        if op & 0x80:
            off = size = 0
            for bit in range(4):
                if op & (1 << bit):
                    off |= delta[i] << (8 * bit)
                    i += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    size |= delta[i] << (8 * bit)
                    i += 1
            size = size or 0x10000
            if off + size > len(base):
                raise GitError("дельта копирует за пределами основы")
            out += base[off : off + size]
            if len(out) > dst_size:
                raise GitError("дельта дала больше байт, чем заявлено")
        elif op:
            if i + op > len(delta):
                raise GitError("дельта оборвана внутри вставки")
            out += delta[i : i + op]
            i += op
            if len(out) > dst_size:
                raise GitError("дельта дала больше байт, чем заявлено")
        else:
            raise GitError("дельта: недопустимая инструкция 0")
    if len(out) != dst_size:
        raise GitError(f"дельта дала {len(out)} байт вместо {dst_size}")
    return bytes(out)


class PackFile:
    """Один пакет (pack-*.pack) с индексом (pack-*.idx): поиск объекта по хэшу и чтение с раскрытием дельт."""

    def __init__(self, pack_path: Path) -> None:
        self.path = pack_path
        idx_path = pack_path.with_suffix(".idx")
        self._pack = pack_path.read_bytes()
        idx = idx_path.read_bytes()
        if self._pack[:4] != b"PACK" or struct.unpack(">I", self._pack[4:8])[0] != 2:
            raise GitError(f"{pack_path.name}: не пакет версии 2")
        if idx[:4] != b"\xfftOc" or struct.unpack(">I", idx[4:8])[0] != 2:
            raise GitError(f"{idx_path.name}: поддерживается только индекс версии 2")
        self._count = int(struct.unpack(">I", idx[8 + 255 * 4 : 8 + 256 * 4])[0])
        self._fanout = struct.unpack(">256I", idx[8 : 8 + 1024])
        n = self._count
        base = 8 + 1024
        self._shas = idx[base : base + 20 * n]
        off_base = base + 20 * n + 4 * n
        self._offsets = idx[off_base : off_base + 4 * n]
        self._big = idx[off_base + 4 * n : len(idx) - 40]
        if hashlib.sha1(self._pack[:-20]).digest() != self._pack[-20:]:
            raise GitError(f"{pack_path.name}: контрольная сумма пакета не сходится")
        if self._pack[-20:] != idx[-40:-20]:
            raise GitError(f"{pack_path.name}: пакет не соответствует индексу")
        if hashlib.sha1(idx[:-20]).digest() != idx[-20:]:
            raise GitError(f"{idx_path.name}: контрольная сумма индекса не сходится")
        self._cache: dict[int, tuple[str, bytes]] = {}

    def __len__(self) -> int:
        return self._count

    def _sha_at(self, i: int) -> bytes:
        return self._shas[20 * i : 20 * i + 20]

    def _offset_at(self, i: int) -> int:
        (v,) = struct.unpack(">I", self._offsets[4 * i : 4 * i + 4])
        if v & 0x80000000:
            (v,) = struct.unpack(">Q", self._big[8 * (v & 0x7FFFFFFF) : 8 * (v & 0x7FFFFFFF) + 8])
        return int(v)

    def find(self, sha: str) -> int | None:
        """Смещение объекта в пакете или None. Бинарный поиск внутри корзины первого байта."""
        raw = bytes.fromhex(sha)
        lo = self._fanout[raw[0] - 1] if raw[0] else 0
        hi = self._fanout[raw[0]]
        while lo < hi:
            mid = (lo + hi) // 2
            cur = self._sha_at(mid)
            if cur == raw:
                return self._offset_at(mid)
            if cur < raw:
                lo = mid + 1
            else:
                hi = mid
        return None

    def shas(self) -> Iterable[str]:
        for i in range(self._count):
            yield self._sha_at(i).hex()

    def read_at(self, offset: int, by_sha: Callable[[str], tuple[str, bytes]] | None = None) -> tuple[str, bytes]:
        cached = self._cache.get(offset)
        if cached is not None:
            return cached
        data = self._pack
        i = offset
        c = data[i]
        i += 1
        kind = (c >> 4) & 7
        size = c & 15
        shift = 4
        while c & 0x80:
            c = data[i]
            i += 1
            size |= (c & 0x7F) << shift
            shift += 7
        if kind in _TYPES:
            body = self._inflate(i, size)
            result = (_TYPES[kind], body)
        elif kind == OFS_DELTA:
            c = data[i]
            i += 1
            back = c & 0x7F
            while c & 0x80:
                c = data[i]
                i += 1
                back = ((back + 1) << 7) | (c & 0x7F)
            base_kind, base = self.read_at(offset - back, by_sha)
            result = (base_kind, apply_delta(base, self._inflate(i, size)))
        elif kind == REF_DELTA:
            base_sha = data[i : i + 20].hex()
            i += 20
            local = self.find(base_sha)
            if local is not None:
                base_kind, base = self.read_at(local, by_sha)
            elif by_sha is not None:
                base_kind, base = by_sha(base_sha)
            else:
                raise GitError(f"в пакете нет основы дельты {base_sha}")
            result = (base_kind, apply_delta(base, self._inflate(i, size)))
        else:
            raise GitError(f"в пакете неизвестный тип объекта {kind}")
        if len(self._cache) < 4096:
            self._cache[offset] = result
        return result

    def _inflate(self, start: int, size: int) -> bytes:
        d = zlib.decompressobj()
        try:
            out = d.decompress(memoryview(self._pack)[start:], size)
        except zlib.error as e:
            raise GitError(f"{self.path.name}: поток zlib повреждён: {e}") from e
        if len(out) != size:
            raise GitError(f"{self.path.name}: объект распаковался в {len(out)} байт вместо {size}")
        return out

    def read(self, sha: str, by_sha: Callable[[str], tuple[str, bytes]] | None = None) -> tuple[str, bytes] | None:
        off = self.find(sha)
        return None if off is None else self.read_at(off, by_sha)


# ---------- запись ----------


def write_pack(objects: Iterable[tuple[str, bytes]], directory: Path) -> Path:
    """Пишет пакет без дельт и индекс v2 в каталог; возвращает путь к .pack. Имя файла по хэшу пакета, как у git."""
    items = list(objects)
    body = bytearray(b"PACK" + struct.pack(">II", 2, len(items)))
    entries: list[tuple[bytes, int, int]] = []  # (sha, offset, crc32)
    for kind, data in items:
        offset = len(body)
        size = len(data)
        first = (_TYPE_NUM[kind] << 4) | (size & 15)
        size >>= 4
        head = bytearray()
        while size:
            head.append(first | 0x80)
            first = size & 0x7F
            size >>= 7
        head.append(first)
        packed = bytes(head) + zlib.compress(data)
        body += packed
        entries.append((bytes.fromhex(hash_object(kind, data)), offset, zlib.crc32(packed) & 0xFFFFFFFF))
    pack_sha = hashlib.sha1(bytes(body)).digest()
    body += pack_sha

    entries.sort()
    fanout = [0] * 256
    for sha, _, _ in entries:
        fanout[sha[0]] += 1
    for i in range(1, 256):
        fanout[i] += fanout[i - 1]
    idx = bytearray(b"\xfftOc" + struct.pack(">I", 2) + struct.pack(">256I", *fanout))
    idx += b"".join(s for s, _, _ in entries)
    idx += b"".join(struct.pack(">I", c) for _, _, c in entries)
    big = bytearray()
    offs = bytearray()
    for _, o, _ in entries:
        if o >= 0x80000000:
            offs += struct.pack(">I", 0x80000000 | (len(big) // 8))
            big += struct.pack(">Q", o)
        else:
            offs += struct.pack(">I", o)
    idx += offs + big + pack_sha
    idx += hashlib.sha1(bytes(idx)).digest()

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"pack-{pack_sha.hex()}.pack"
    path.write_bytes(bytes(body))
    path.with_suffix(".idx").write_bytes(bytes(idx))
    return path
