"""Командная строка: настоящий запуск `python -m minigit` как процесса; вывод сверяется с git там, где формат общий."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git_env, git_text, run_git, write_tree


def mg(cwd: Path, *args: str, check: bool = True, input: bytes | None = None, tick: int = 1) -> subprocess.CompletedProcess[bytes]:
    env = git_env(tick)
    env["PYTHONUTF8"] = "1"
    r = subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd, env=env, capture_output=True, input=input)
    if check and r.returncode != 0:
        raise AssertionError(f"minigit {' '.join(args)} → {r.returncode}: {r.stderr.decode(errors='replace')}")
    return r


@pytest.fixture
def work(tmp_path: Path) -> Path:
    mg(tmp_path, "init", "w")
    return tmp_path / "w"


def test_init_commit_log_status_and_git_agrees(work: Path) -> None:
    write_tree(work, {"a.txt": "hello\n", "dir/b.txt": "world\n"})
    assert mg(work, "status", "--porcelain").stdout.decode().splitlines() == ["?? a.txt", "?? dir/"]
    mg(work, "add", ".")
    assert mg(work, "status", "-s").stdout.decode().splitlines() == ["A  a.txt", "A  dir/b.txt"]
    out = mg(work, "commit", "-m", "первый коммит").stdout.decode()
    assert out.startswith("[main ") and "первый коммит" in out
    assert mg(work, "status", "--porcelain").stdout == b""
    assert git_text(work, "log", "--format=%s").strip() == "первый коммит"
    assert mg(work, "log").stdout == run_git(work, "log").stdout
    assert mg(work, "log", "--oneline").stdout.decode().split()[0] == git_text(work, "rev-parse", "--short=7", "HEAD").strip()
    assert mg(work, "rev-parse", "HEAD").stdout.decode().strip() == git_text(work, "rev-parse", "HEAD").strip()
    run_git(work, "fsck", "--strict")


def test_plumbing_commands_match_git(work: Path) -> None:
    write_tree(work, {"f": "data\n", "d/g": "more\n"})
    mg(work, "add", ".")
    mg(work, "commit", "-m", "c")
    sha = mg(work, "hash-object", "f").stdout.decode().strip()
    assert sha == git_text(work, "hash-object", "f").strip()
    assert (
        mg(work, "hash-object", "--stdin", input=b"abc").stdout.decode().strip()
        == run_git(work, "hash-object", "--stdin", input=b"abc").stdout.decode().strip()
    )
    assert mg(work, "cat-file", "-t", "HEAD").stdout.decode().strip() == "commit"
    assert mg(work, "cat-file", "-s", sha).stdout.decode().strip() == "5"
    assert mg(work, "cat-file", "-p", "HEAD:f").stdout == b"data\n"
    assert mg(work, "cat-file", "-p", "HEAD^{tree}").stdout == run_git(work, "cat-file", "-p", "HEAD^{tree}").stdout
    assert mg(work, "ls-files", "-s").stdout.decode().replace("\r", "") == git_text(work, "ls-files", "-s")
    assert mg(work, "ls-tree", "-r", "HEAD").stdout == run_git(work, "ls-tree", "-r", "HEAD").stdout
    assert mg(work, "write-tree").stdout == run_git(work, "write-tree").stdout
    mg(work, "hash-object", "-w", "f")
    assert mg(work, "fsck").returncode == 0


def test_diff_show_and_rm(work: Path) -> None:
    write_tree(work, {"f.txt": "1\n2\n3\n"})
    mg(work, "add", ".")
    mg(work, "commit", "-m", "base")
    write_tree(work, {"f.txt": "1\nTWO\n3\n", "new.txt": "x\n"})
    assert mg(work, "diff").stdout == run_git(work, "diff").stdout  # новый файл не отслеживается: в diff его нет
    mg(work, "add", ".")
    assert mg(work, "diff", "--cached").stdout == run_git(work, "diff", "--cached").stdout
    mg(work, "commit", "-m", "second")
    assert mg(work, "diff", "HEAD~1", "HEAD").stdout == run_git(work, "diff", "HEAD~1", "HEAD").stdout
    shown = mg(work, "show", "HEAD").stdout.decode()
    assert "second" in shown and "+TWO" in shown and "diff --git" in shown
    assert mg(work, "show", "HEAD:f.txt").stdout == b"1\nTWO\n3\n"
    assert "rm 'new.txt'" in mg(work, "rm", "new.txt").stdout.decode()
    assert not (work / "new.txt").exists()


def test_branching_checkout_merge_flow(work: Path) -> None:
    write_tree(work, {"f": "a\nb\nc\nd\ne\nf\ng\n"})
    mg(work, "add", ".")
    mg(work, "commit", "-m", "base")
    mg(work, "checkout", "-b", "topic")
    write_tree(work, {"f": "a\nb\nc\nd\ne\nf\nG\n", "t.txt": "t\n"})
    mg(work, "add", ".")
    mg(work, "commit", "-m", "topic", tick=2)
    mg(work, "checkout", "main")
    write_tree(work, {"f": "A\nb\nc\nd\ne\nf\ng\n"})
    mg(work, "commit", "-a", "-m", "main", tick=3)
    assert mg(work, "branch").stdout.decode().splitlines() == ["  main", "* main"][:0] or "* main" in mg(work, "branch").stdout.decode()
    out = mg(work, "merge", "topic", tick=4).stdout.decode()
    assert "Слияние выполнено" in out
    assert (work / "f").read_bytes() == b"A\nb\nc\nd\ne\nf\nG\n" and (work / "t.txt").exists()
    assert git_text(work, "log", "--format=%s", "-n", "1").strip() == "Merge branch 'topic'"
    run_git(work, "fsck", "--strict")
    mg(work, "branch", "-d", "topic")
    assert "topic" not in mg(work, "branch").stdout.decode()
    mg(work, "tag", "v1")
    mg(work, "tag", "-a", "v2", "-m", "annotated")
    assert mg(work, "tag").stdout.decode().split() == ["v1", "v2"]
    mg(work, "reset", "--hard", "HEAD~1")
    assert not (work / "t.txt").exists()


def test_merge_conflict_exit_status_and_merge_file(work: Path) -> None:
    write_tree(work, {"f": "a\nb\nc\n"})
    mg(work, "add", ".")
    mg(work, "commit", "-m", "base")
    mg(work, "checkout", "-b", "x")
    write_tree(work, {"f": "a\nX\nc\n"})
    mg(work, "commit", "-a", "-m", "x", tick=2)
    mg(work, "checkout", "main")
    write_tree(work, {"f": "a\nM\nc\n"})
    mg(work, "commit", "-a", "-m", "m", tick=3)
    r = mg(work, "merge", "x", check=False, tick=4)
    assert r.returncode == 1 and "Автоматическое слияние не удалось" in r.stdout.decode()
    assert mg(work, "status", "--porcelain").stdout.decode().splitlines() == ["UU f"]
    assert b"<<<<<<< HEAD" in (work / "f").read_bytes()
    write_tree(work, {"o": "a\nO\nc\n", "b": "a\nb\nc\n", "t": "a\nT\nc\n"})
    out = mg(work, "merge-file", "o", "b", "t", "--label-ours", "o", "--label-theirs", "t", check=False).stdout
    expected = run_git(work, "merge-file", "-p", "-L", "o", "-L", "b", "-L", "t", "o", "b", "t", check=False).stdout
    assert out == expected


def test_errors_are_reported_without_traceback(work: Path) -> None:
    r = mg(work, "commit", "-m", "nothing", check=False)
    assert r.returncode == 1 and r.stderr.decode().startswith("ошибка:") and b"Traceback" not in r.stderr
    assert mg(work, "rev-parse", "nope", check=False).returncode == 1
    assert mg(work, "checkout", "ghost", check=False).returncode == 1
    assert mg(work, "add", "missing.txt", check=False).returncode == 1
    outside = work.parent / "outside"
    outside.mkdir()
    r = mg(outside, "status", check=False)
    assert r.returncode == 1 and "не репозиторий" in r.stderr.decode()
    assert mg(work, "frobnicate", check=False).returncode == 2  # неизвестная команда: ошибка разбора аргументов
    assert mg(work, "--version").stdout.decode().startswith("minigit ")


def test_clone_fetch_config_and_repack_commands(work: Path, tmp_path: Path) -> None:
    write_tree(work, {"f": "1\n"})
    mg(work, "add", ".")
    mg(work, "commit", "-m", "c1")
    mg(tmp_path, "clone", str(work), "copy")
    copy = tmp_path / "copy"
    assert (copy / "f").read_bytes() == b"1\n"
    write_tree(work, {"g": "2\n"})
    mg(work, "add", ".")
    mg(work, "commit", "-am", "c2", tick=2)
    assert "origin/main" in mg(copy, "fetch").stdout.decode()
    mg(copy, "config", "user.name", "Zed")
    assert mg(copy, "config", "user.name").stdout.decode().strip() == "Zed"
    assert mg(copy, "config", "no.such", check=False).returncode == 1
    assert "пакет записан" in mg(copy, "repack").stdout.decode()
    run_git(copy, "fsck", "--strict")
    mg(copy, "update-ref", "refs/heads/manual", "origin/main")
    assert mg(copy, "symbolic-ref", "HEAD").stdout.decode().strip() == "refs/heads/main"
