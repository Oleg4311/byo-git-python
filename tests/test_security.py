"""Безопасность при чтении чужих репозиториев: вредоносные имена в деревьях, «дельта-бомбы», повреждённые структуры."""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest
from conftest import run_git

from minigit.fsck import fsck
from minigit.objects import GitError, check_tree_name, serialize_commit
from minigit.pack import MAX_OBJECT_SIZE, apply_delta
from minigit.repo import Repository
from minigit.transport import clone


def _evil_repo(tmp: Path, name: bytes) -> tuple[Path, str]:
    """Репозиторий, в котором дерево коммита содержит запись с вредоносным именем (настоящий git такое не запишет, а чужой репозиторий может)."""
    repo = Repository.init(tmp / "evil")
    blob = repo.write_blob(b"payload\n")
    raw_tree = b"100644 " + name + b"\0" + bytes.fromhex(blob)  # собираем байты дерева вручную, минуя проверки serialize_tree
    tree = repo.odb.write("tree", raw_tree)
    from minigit.objects import Commit, Identity

    who = Identity("e", "e@e", 1700000000, "+0000")
    commit = repo.odb.write("commit", serialize_commit(Commit(tree, [], who, who, b"evil\n")))
    repo.refs.update("refs/heads/main", commit)
    return repo.wt, commit


@pytest.mark.parametrize("name", [b"..", b".", b".git", b".GIT", b".Git", b".git ", b".git.", b"git~1", b"GIT~1", b"a\\b"])
def test_hostile_tree_names_are_rejected(name: bytes) -> None:
    with pytest.raises(GitError, match="небезопасное имя"):
        check_tree_name(name)


@pytest.mark.parametrize("name", [b"file.txt", b".gitignore", b".github", b"git", b"a.git", b"...", "юникод".encode()])
def test_ordinary_names_are_accepted(name: bytes) -> None:
    check_tree_name(name)


@pytest.mark.parametrize("name", [b"..", b".git", b".GIT", b"git~1"])
def test_checkout_of_a_hostile_tree_writes_nothing(tmp_path: Path, name: bytes) -> None:
    work, commit = _evil_repo(tmp_path, name)
    repo = Repository.discover(work)
    outside = tmp_path / "payload-marker"
    with pytest.raises(GitError, match="небезопасное имя"):
        repo.reset(commit, "hard")
    with pytest.raises(GitError, match="небезопасное имя"):
        repo.flat_tree(repo.read_commit(commit).tree)
    assert not outside.exists()
    assert sorted(p.name for p in work.iterdir()) == [".git"]  # в рабочий каталог ничего не попало


def test_hostile_clone_is_refused(tmp_path: Path) -> None:
    work, _ = _evil_repo(tmp_path, b"..")
    with pytest.raises(GitError, match="небезопасное имя"):
        clone(str(work), tmp_path / "victim")
    assert not (tmp_path / "payload").exists()


def test_fsck_flags_hostile_names(tmp_path: Path) -> None:
    work, _ = _evil_repo(tmp_path, b".git")
    problems = [str(p) for p in fsck(Repository.discover(work)) if p.severity == "error"]
    assert any("недопустимое имя" in p for p in problems)


def test_delta_bomb_is_refused_quickly() -> None:
    def varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)

    base = b"x" * 70000
    # заявлен размер в терабайт: отказ сразу, без попытки выделить память
    with pytest.raises(GitError, match="слишком много"):
        apply_delta(base, varint(len(base)) + varint(MAX_OBJECT_SIZE + 1) + bytes([0x80]))
    # размер заявлен небольшой, а инструкции копируют больше: остановка в процессе, а не в конце
    many_copies = varint(len(base)) + varint(100) + bytes([0x80]) * 50  # каждое копирование по 64 КиБ
    with pytest.raises(GitError, match="больше байт"):
        apply_delta(base, many_copies)


def test_symlink_in_the_way_is_replaced_not_followed(tmp_path: Path) -> None:
    """Если на месте будущего каталога лежит символьная ссылка наружу, запись идёт в сам каталог, а не по ссылке."""
    import os

    if os.name == "nt":
        pytest.skip("символьные ссылки на Windows не поддерживаются")
    run_git(tmp_path, "init", "-q", "-b", "main", "w")
    repo = Repository.discover(tmp_path / "w")
    blob = repo.write_blob(b"inside\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, repo.wt / "dir")
    tree_inner = repo.odb.write("tree", b"100644 f\0" + bytes.fromhex(blob))
    tree = repo.odb.write("tree", b"40000 dir\0" + bytes.fromhex(tree_inner))
    from minigit.objects import Commit, Identity

    who = Identity("e", "e@e", 1, "+0000")
    commit = repo.odb.write("commit", serialize_commit(Commit(tree, [], who, who, b"m\n")))
    repo.refs.update("refs/heads/main", commit)
    repo.reset(commit, "hard")
    assert list(outside.iterdir()) == []  # через ссылку ничего не записано
    assert (repo.wt / "dir" / "f").read_bytes() == b"inside\n" and not (repo.wt / "dir").is_symlink()


def test_truncated_and_garbage_objects_raise_clean_errors(tmp_path: Path) -> None:
    repo = Repository.init(tmp_path / "r")
    sha = repo.write_blob(b"x")
    path = repo.odb._loose_path(sha)
    path.chmod(0o666)
    for payload in (b"", zlib.compress(b"blob")[:-3], zlib.compress(b"nonsense without nul"), zlib.compress(b"blob abc\0x")):
        path.write_bytes(payload)
        with pytest.raises(GitError):
            repo.odb.read(sha)
