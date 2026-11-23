"""Общие помощники: запуск настоящего git в изолированной среде и пары репозиториев (наш и настоящий), в которых делается одно и то же."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from minigit.repo import Repository

if shutil.which("git") is None:  # без настоящего git сверять не с чем
    pytest.skip("нужен установленный git: тесты сверяют результат с ним", allow_module_level=True)

BASE_ENV = {
    "GIT_AUTHOR_NAME": "A U Thor",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0300",
    "GIT_COMMITTER_NAME": "C O Mitter",
    "GIT_COMMITTER_EMAIL": "committer@example.com",
    "GIT_COMMITTER_DATE": "1700000100 -0500",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}


def git_env(tick: int | None = None) -> dict[str, str]:
    """Окружение git; tick сдвигает обе даты, чтобы у коммитов были разные хэши и порядок по времени."""
    env = {**os.environ, **BASE_ENV}
    if tick is not None:
        env["GIT_AUTHOR_DATE"] = f"{1700000000 + tick * 100} +0300"
        env["GIT_COMMITTER_DATE"] = f"{1700000100 + tick * 100} -0500"
    return env


def run_git(cwd: Path, *args: str, check: bool = True, input: bytes | None = None, tick: int | None = None) -> subprocess.CompletedProcess[bytes]:
    r = subprocess.run(["git", *args], cwd=cwd, env=git_env(tick), capture_output=True, input=input)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} завершился с {r.returncode}: {r.stderr.decode(errors='replace')}")
    return r


def git_text(cwd: Path, *args: str, tick: int | None = None) -> str:
    return run_git(cwd, *args, tick=tick).stdout.decode("utf-8", "surrogateescape")


def set_tick(tick: int) -> None:
    """Те же даты для нашего процесса: он читает GIT_*_DATE из окружения, как и git."""
    env = git_env(tick)
    for k in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        os.environ[k] = env[k]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in git_env().items():
        if k.startswith("GIT_") or k == "LC_ALL":
            monkeypatch.setenv(k, v)


def write_tree(root: Path, files: dict[str, bytes | str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode() if isinstance(content, str) else content)


class Pair:
    """Два репозитория: `ours` (minigit) и `ref` (настоящий git). Одни и те же операции выполняются в обоих."""

    def __init__(self, tmp: Path) -> None:
        self.ours_dir = tmp / "ours"
        self.ref_dir = tmp / "ref"
        self.ref_dir.mkdir()
        run_git(self.ref_dir, "init", "-q", "-b", "main")
        self.ours = Repository.init(self.ours_dir, "main")
        self.tick = 0

    def write(self, files: dict[str, bytes | str]) -> None:
        write_tree(self.ours_dir, files)
        write_tree(self.ref_dir, files)

    def remove(self, *paths: str) -> None:
        for p in paths:
            for root in (self.ours_dir, self.ref_dir):
                (root / p).unlink()

    def add_all(self) -> None:
        cwd = Path.cwd()
        os.chdir(self.ours_dir)
        try:
            self.ours.add_paths(["."])
        finally:
            os.chdir(cwd)
        run_git(self.ref_dir, "add", "-A")

    def commit(self, message: str) -> tuple[str, str]:
        """Коммит в обоих репозиториях с одинаковыми датами; возвращает (наш хэш, хэш git)."""
        self.tick += 1
        set_tick(self.tick)
        ours = self.ours.commit(message)
        run_git(self.ref_dir, "commit", "-q", "-m", message, tick=self.tick)
        return ours, git_text(self.ref_dir, "rev-parse", "HEAD").strip()


@pytest.fixture
def pair(tmp_path: Path) -> Iterator[Pair]:
    p = Pair(tmp_path)
    cwd = Path.cwd()
    yield p
    os.chdir(cwd)


@pytest.fixture
def ref_repo(tmp_path: Path) -> Path:
    """Репозиторий, созданный настоящим git, с несколькими коммитами, слиянием и тегами."""
    r = tmp_path / "real"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    write_tree(r, {"a.txt": "one\ntwo\nthree\n", "src/x.py": "print(1)\n" * 20, "docs/readme.md": "# title\n"})
    run_git(r, "add", "-A")
    run_git(
        r,
        "commit",
        "-q",
        "--cleanup=verbatim",
        "-m",
        "first\n\nsecond paragraph  \n#\tindented with tab\n   trailing spaces   \n\n\nafter two blanks\n",
        tick=1,
    )
    write_tree(r, {"a.txt": "one\ntwo\nthree\nfour\n", "src/x.py": "print(1)\n" * 10 + "print(2)\n" + "print(1)\n" * 10})
    run_git(r, "commit", "-q", "-am", "second", tick=2)
    run_git(r, "tag", "v1")
    run_git(r, "checkout", "-q", "-b", "feature")
    write_tree(r, {"feature.txt": "f\n", "a.txt": "zero\none\ntwo\nthree\nfour\n"})
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "feature work", tick=3)
    run_git(r, "checkout", "-q", "main")
    write_tree(r, {"main-only.txt": "m\n"})
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "main work", tick=4)
    run_git(r, "merge", "-q", "--no-edit", "feature", tick=5)
    run_git(r, "tag", "-a", "v2", "-m", "release two", tick=6)
    return r
