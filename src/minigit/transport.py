"""Локальные clone и fetch (по пути или file://) и перепаковка объектов. Сетевые протоколы (http, ssh, git://) не реализованы."""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse

from .objects import GitError
from .pack import write_pack
from .repo import Repository

__all__ = ["clone", "fetch", "repack"]


def _source_path(url: str) -> Path:
    if "://" in url:
        u = urlparse(url)
        if u.scheme != "file":
            raise GitError(f"протокол {u.scheme}:// не поддерживается: только локальные пути и file://")
        p = unquote(u.path)
        if os.name == "nt" and p.startswith("/") and len(p) > 2 and p[2] == ":":
            p = p[1:]
        return Path(p)
    return Path(url)


def _open_source(url: str) -> Repository:
    path = _source_path(url).resolve()
    for cand in (path / ".git", path):
        if (cand / "HEAD").is_file() and (cand / "objects").is_dir():
            return Repository(cand, cand.parent if cand.name == ".git" else None)
    raise GitError(f"не репозиторий: {url}")


def _copy_objects(src: Repository, dst: Repository) -> int:
    """Копирует все объекты источника, которых нет в приёмнике: пакеты файлами целиком, рыхлые побайтно."""
    copied = 0
    sp, dp = src.git_dir / "objects" / "pack", dst.git_dir / "objects" / "pack"
    if sp.is_dir():
        dp.mkdir(parents=True, exist_ok=True)
        for f in sp.iterdir():
            if f.suffix in (".pack", ".idx") and not (dp / f.name).exists():
                shutil.copy2(f, dp / f.name)
                copied += f.suffix == ".pack"
    for sha in src.odb.loose_shas():
        target = dst.git_dir / "objects" / sha[:2] / sha[2:]
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src.odb._loose_path(sha), target)
            copied += 1
    dst.odb.refresh()
    return copied


def fetch(repo: Repository, remote: str = "origin") -> dict[str, str]:
    """Забирает недостающие объекты и обновляет refs/remotes/<remote>/*. Возвращает обновлённые ветки."""
    url = repo.config.get(f"remote.{remote}.url")
    if not url:
        raise GitError(f"у репозитория нет удалённого «{remote}»")
    src = _open_source(url)
    _copy_objects(src, repo)
    updated: dict[str, str] = {}
    for name, sha in src.refs.branches().items():
        ref = f"refs/remotes/{remote}/{name}"
        if repo.refs.resolve(ref) != sha:
            repo.refs.update(ref, sha)
            updated[name] = sha
    for name, sha in src.refs.tags().items():
        if not repo.refs.resolve(f"refs/tags/{name}"):
            repo.refs.update(f"refs/tags/{name}", sha)
    return updated


def clone(url: str, dest: Path, bare: bool = False) -> Repository:
    src = _open_source(url)
    head_branch = src.refs.head_branch() or "main"
    repo = Repository.init(dest, head_branch, bare=bare)
    shown = str(_source_path(url).resolve())
    repo.config.set('remote "origin".url', shown)
    repo.config.set('remote "origin".fetch', "+refs/heads/*:refs/remotes/origin/*")
    _copy_objects(src, repo)
    if bare:
        for name, sha in src.refs.branches().items():
            repo.refs.update(f"refs/heads/{name}", sha)
        for name, sha in src.refs.tags().items():
            repo.refs.update(f"refs/tags/{name}", sha)
        return repo
    for name, sha in src.refs.branches().items():
        repo.refs.update(f"refs/remotes/origin/{name}", sha)
    for name, sha in src.refs.tags().items():
        repo.refs.update(f"refs/tags/{name}", sha)
    tip = src.refs.branches().get(head_branch)
    if tip:
        repo.refs.update(f"refs/heads/{head_branch}", tip)
        repo.config.set(f'branch "{head_branch}".remote', "origin")
        repo.config.set(f'branch "{head_branch}".merge', f"refs/heads/{head_branch}")
        repo.reset(head_branch, "hard")  # заполняет рабочий каталог и индекс
    return repo


def repack(repo: Repository) -> Path | None:
    """Собирает все объекты в один пакет (без дельт) и удаляет рыхлые копии. Возвращает путь к пакету."""
    shas = sorted(repo.odb.all_shas())
    if not shas:
        return None
    loose = set(repo.odb.loose_shas())
    objects = [repo.odb.read(s) for s in shas]
    path = write_pack(objects, repo.git_dir / "objects" / "pack")
    repo.odb.refresh()
    for sha in loose:
        victim = repo.odb._loose_path(sha)
        if victim.exists():
            victim.chmod(0o666)  # на Windows рыхлые объекты только для чтения, и удалить их без этого нельзя
            victim.unlink()
        with contextlib.suppress(OSError):
            repo.odb._loose_path(sha).parent.rmdir()
    return path
