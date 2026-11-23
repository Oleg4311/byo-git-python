"""Ссылки: HEAD, ветки, теги, packed-refs. Обновление через блокирующий файл .lock и атомарную подмену, как в git."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from .objects import GitError, is_hex_sha

__all__ = ["RefStore", "check_ref_name"]

ZERO = "0" * 40


def check_ref_name(name: str) -> None:
    """Правила git check-ref-format (достаточная часть): без пробелов, «..», «~^:?*[\\», «@{», завершающих «/» и «.lock»."""
    bad = (
        not name
        or name.startswith("/")
        or name.endswith("/")
        or name.endswith(".")
        or name.endswith(".lock")
        or ".." in name
        or "@{" in name
        or "//" in name
        or name == "@"
        or any(c in name for c in " ~^:?*[\\\x7f")
        or any(ord(c) < 32 for c in name)
        or any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))
    )
    if bad:
        raise GitError(f"недопустимое имя ссылки: {name!r}")


class RefStore:
    def __init__(self, git_dir: Path) -> None:
        self.git_dir = git_dir

    # ---------- чтение ----------

    def _packed(self) -> dict[str, str]:
        path = self.git_dir / "packed-refs"
        out: dict[str, str] = {}
        if not path.exists():
            return out
        last: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            if line.startswith("^"):  # снятая цель аннотированного тега: к самой ссылке не относится
                continue
            sha, _, name = line.partition(" ")
            if is_hex_sha(sha) and name:
                out[name] = sha
                last = name
        del last
        return out

    def read_symbolic(self, name: str) -> str | None:
        """Куда указывает символическая ссылка (HEAD → refs/heads/main), или None, если она не символическая."""
        path = self.git_dir / name
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text.startswith("ref: "):
                return text[5:].strip()
        return None

    def resolve(self, name: str, depth: int = 0) -> str | None:
        """Хэш, на который указывает ссылка (по цепочке символических), или None, если её нет (пустая ветка)."""
        if depth > 8:
            raise GitError(f"слишком длинная цепочка символических ссылок у {name}")
        path = self.git_dir / name
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text.startswith("ref: "):
                return self.resolve(text[5:].strip(), depth + 1)
            if is_hex_sha(text):
                return text
            raise GitError(f"ссылка {name} повреждена: {text[:50]!r}")
        return self._packed().get(name)

    def head_branch(self) -> str | None:
        """Имя текущей ветки или None при отсоединённом HEAD."""
        target = self.read_symbolic("HEAD")
        if target is None:
            return None
        return target[len("refs/heads/") :] if target.startswith("refs/heads/") else target

    def iter_refs(self, prefix: str = "refs/") -> Iterator[tuple[str, str]]:
        seen: set[str] = set()
        root = self.git_dir / prefix.rstrip("/")
        if root.is_dir():
            for dirpath, _dirs, files in os.walk(root):
                for f in sorted(files):
                    if f.endswith(".lock"):
                        continue
                    p = Path(dirpath) / f
                    name = p.relative_to(self.git_dir).as_posix()
                    sha = self.resolve(name)
                    if sha:
                        seen.add(name)
                        yield name, sha
        for name, sha in sorted(self._packed().items()):
            if name.startswith(prefix) and name not in seen:
                yield name, sha

    def branches(self) -> dict[str, str]:
        return {n[len("refs/heads/") :]: s for n, s in sorted(self.iter_refs("refs/heads/"))}

    def tags(self) -> dict[str, str]:
        return {n[len("refs/tags/") :]: s for n, s in sorted(self.iter_refs("refs/tags/"))}

    # ---------- запись ----------

    def _write_locked(self, name: str, content: str) -> None:
        path = self.git_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_name(path.name + ".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as e:
            raise GitError(f"не удалось заблокировать {name}: есть файл {lock.name} (другой процесс или прерванная операция)") from e
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            os.replace(lock, path)
        except BaseException:
            if lock.exists():
                lock.unlink()
            raise

    def update(self, name: str, sha: str, expected_old: str | bool | None = False) -> None:
        """Устанавливает ссылку. expected_old: хэш (должен совпасть), None (ссылки не должно быть) или False (не проверять)."""
        if name != "HEAD":
            check_ref_name(name)
        if not is_hex_sha(sha):
            raise GitError(f"неверный хэш: {sha!r}")
        if expected_old is not False:
            cur = self.resolve(name)
            if cur != expected_old:
                raise GitError(f"ссылка {name} изменилась: ожидалось {expected_old}, сейчас {cur}")
        target = self.read_symbolic(name) if name == "HEAD" else None
        self._write_locked(target if target else name, sha + "\n")

    def set_symbolic(self, name: str, target: str) -> None:
        self._write_locked(name, f"ref: {target}\n")

    def delete(self, name: str) -> bool:
        check_ref_name(name)
        removed = False
        path = self.git_dir / name
        if path.is_file():
            path.unlink()
            removed = True
            # пустые каталоги после refs/heads/a/b убираем, как git
            parent = path.parent
            while parent != self.git_dir / "refs" and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        packed = self._packed()
        if name in packed:
            del packed[name]
            lines = ["# pack-refs with: peeled fully-peeled sorted \n"] + [f"{s} {n}\n" for n, s in sorted(packed.items())]
            self._write_locked("packed-refs", "".join(lines))
            removed = True
        return removed
