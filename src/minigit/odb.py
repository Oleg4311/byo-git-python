"""База объектов: «рыхлые» объекты (objects/xx/yyyy) и пакеты (objects/pack). Запись только в рыхлые, чтение из обоих мест."""

from __future__ import annotations

import os
import tempfile
import zlib
from collections.abc import Iterator
from pathlib import Path

from .objects import GitError, hash_object, is_hex_sha
from .pack import PackFile

__all__ = ["ObjectStore"]


class ObjectStore:
    def __init__(self, objects_dir: Path) -> None:
        self.dir = objects_dir
        self._packs: list[PackFile] | None = None

    # ---------- пакеты ----------

    @property
    def packs(self) -> list[PackFile]:
        if self._packs is None:
            pack_dir = self.dir / "pack"
            self._packs = [PackFile(p) for p in sorted(pack_dir.glob("pack-*.pack")) if p.with_suffix(".idx").exists()] if pack_dir.is_dir() else []
        return self._packs

    def refresh(self) -> None:
        """Перечитать список пакетов (после внешнего `git gc` или добавления пакета)."""
        self._packs = None

    # ---------- чтение ----------

    def _loose_path(self, sha: str) -> Path:
        return self.dir / sha[:2] / sha[2:]

    def read(self, sha: str) -> tuple[str, bytes]:
        """Тип и содержимое объекта. Хэш прочитанного проверяется: повреждённый объект не принимается молча."""
        if not is_hex_sha(sha):
            raise GitError(f"неверный идентификатор объекта: {sha!r}")
        path = self._loose_path(sha)
        if path.is_file():
            kind, data = self._parse_loose(path.read_bytes(), sha)
        else:
            found = None
            for pack in self.packs:
                found = pack.read(sha, self.read)
                if found is not None:
                    break
            if found is None:
                self.refresh()  # возможно, пакет появился после открытия
                for pack in self.packs:
                    found = pack.read(sha, self.read)
                    if found is not None:
                        break
            if found is None:
                raise GitError(f"объект {sha} не найден")
            kind, data = found
        if hash_object(kind, data) != sha:
            raise GitError(f"объект {sha} повреждён: хэш содержимого другой")
        return kind, data

    @staticmethod
    def _parse_loose(raw: bytes, sha: str) -> tuple[str, bytes]:
        try:
            full = zlib.decompress(raw)
        except zlib.error as e:
            raise GitError(f"объект {sha}: поток zlib повреждён: {e}") from e
        nul = full.find(b"\0")
        if nul < 0:
            raise GitError(f"объект {sha}: нет заголовка")
        kind, _, size = full[:nul].decode("ascii", "replace").partition(" ")
        data = full[nul + 1 :]
        if not size.isdigit() or int(size) != len(data):
            raise GitError(f"объект {sha}: длина в заголовке не совпадает с содержимым")
        return kind, data

    def exists(self, sha: str) -> bool:
        if self._loose_path(sha).is_file():
            return True
        return any(p.find(sha) is not None for p in self.packs)

    def kind(self, sha: str) -> str:
        return self.read(sha)[0]

    def resolve_prefix(self, prefix: str) -> str:
        """Полный хэш по началу (от 4 знаков). Неоднозначность и отсутствие дают понятные ошибки."""
        prefix = prefix.lower()
        if len(prefix) < 4 or any(c not in "0123456789abcdef" for c in prefix):
            raise GitError(f"неверное сокращение хэша: {prefix!r}")
        if len(prefix) == 40:
            return prefix
        found = {s for s in self.all_shas() if s.startswith(prefix)}
        if not found:
            raise GitError(f"нет объекта, начинающегося с {prefix}")
        if len(found) > 1:
            raise GitError(f"сокращение {prefix} неоднозначно: подходят {', '.join(sorted(found)[:5])}")
        return found.pop()

    # ---------- запись ----------

    def write(self, kind: str, data: bytes) -> str:
        sha = hash_object(kind, data)
        path = self._loose_path(sha)
        if path.exists() or any(p.find(sha) is not None for p in self.packs):
            return sha
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = zlib.compress(f"{kind} {len(data)}".encode() + b"\0" + data)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="tmp_obj_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.chmod(tmp, 0o444)
            os.replace(tmp, path)  # атомарно: читатель не увидит полуфайл
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return sha

    # ---------- перебор ----------

    def loose_shas(self) -> Iterator[str]:
        if not self.dir.is_dir():
            return
        for sub in sorted(self.dir.iterdir()):
            if len(sub.name) == 2 and sub.is_dir():
                for f in sorted(sub.iterdir()):
                    sha = sub.name + f.name
                    if is_hex_sha(sha):
                        yield sha

    def all_shas(self) -> Iterator[str]:
        seen: set[str] = set()
        for sha in self.loose_shas():
            seen.add(sha)
            yield sha
        for pack in self.packs:
            for sha in pack.shas():
                if sha not in seen:
                    seen.add(sha)
                    yield sha
