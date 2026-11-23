"""fsck, clone/fetch, перепаковка, ссылки и конфигурация: сверка с настоящим git в обе стороны."""

from __future__ import annotations

import os
import zlib
from pathlib import Path

import pytest
from conftest import git_text, run_git, write_tree

from minigit.config import Config
from minigit.fsck import fsck
from minigit.objects import MODE_FILE, GitError, TreeEntry, serialize_tree
from minigit.refs import RefStore, check_ref_name
from minigit.repo import Repository
from minigit.transport import clone, fetch, repack

# ---------- fsck ----------


def _errors(repo: Repository) -> list[str]:
    return [str(p) for p in fsck(repo) if p.severity == "error"]


def test_fsck_accepts_repositories_built_by_git_and_by_us(ref_repo: Path, tmp_path: Path) -> None:
    assert _errors(Repository.discover(ref_repo)) == []
    ours = Repository.init(tmp_path / "o")
    write_tree(ours.wt, {"a": "1\n", "d/b": "2\n"})
    cwd = os.getcwd()
    os.chdir(ours.wt)
    try:
        ours.add_paths(["."])
    finally:
        os.chdir(cwd)
    ours.commit("c1")
    ours.create_tag("t", message="note")
    assert _errors(ours) == []
    run_git(ours.wt, "fsck", "--strict")  # и настоящий git согласен


def test_fsck_reports_dangling_objects_as_information(ref_repo: Path) -> None:
    repo = Repository.discover(ref_repo)
    sha = repo.write_blob(b"unreferenced\n")
    problems = fsck(repo)
    assert any(p.sha == sha and p.severity == "info" and "повисший blob" in p.message for p in problems)
    assert not [p for p in problems if p.severity == "error"]
    assert sha in git_text(ref_repo, "fsck", "--lost-found") or sha in run_git(ref_repo, "fsck").stdout.decode()


def test_fsck_catches_corruption(tmp_path: Path) -> None:
    repo = Repository.init(tmp_path / "r")
    write_tree(repo.wt, {"a": "1\n", "b": "2\n"})
    cwd = os.getcwd()
    os.chdir(repo.wt)
    try:
        repo.add_paths(["."])
    finally:
        os.chdir(cwd)
    commit = repo.commit("c")
    tree = repo.read_commit(commit).tree
    blob = next(e.sha for e in repo.read_tree(tree) if e.name == b"a")
    # 1. потерянный объект
    path = repo.odb._loose_path(blob)
    data = path.read_bytes()
    path.chmod(0o666)
    path.unlink()
    assert any("отсутствующий" in e for e in _errors(repo))
    assert run_git(repo.wt, "fsck", check=False).returncode != 0  # git тоже недоволен
    # 2. повреждённое содержимое под правильным именем
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(zlib.compress(b"blob 5\0wrong"))
    assert any("повреждён" in e for e in _errors(repo))
    path.chmod(0o666)
    path.write_bytes(data)
    assert _errors(repo) == []
    # 3. ссылка на отсутствующий коммит
    (repo.git_dir / "refs/heads/ghost").write_text("1" * 40 + "\n")
    assert any("отсутствующий" in e for e in _errors(repo))
    (repo.git_dir / "refs/heads/ghost").unlink()
    # 4. дерево с нарушенным порядком записей и недопустимым режимом
    bad = repo.odb.write("tree", b"100644 b\0" + bytes.fromhex(blob) + b"100644 a\0" + bytes.fromhex(blob))
    assert any("не отсортированы" in e for e in _errors(repo)), bad
    weird = repo.odb.write("tree", b"100666 x\0" + bytes.fromhex(blob))
    assert any("недопустимый режим" in e for e in _errors(repo)), weird


def test_fsck_checks_object_types(tmp_path: Path) -> None:
    repo = Repository.init(tmp_path / "r")
    blob = repo.write_blob(b"x")
    tree_with_wrong_kind = repo.odb.write("tree", serialize_tree([TreeEntry(0o040000, b"notatree", blob)]))
    repo.refs.update(
        "refs/heads/odd",
        repo.odb.write("commit", b"tree " + tree_with_wrong_kind.encode() + b"\nauthor a <a@b> 1 +0000\ncommitter a <a@b> 1 +0000\n\nm\n"),
    )
    assert any("должен быть tree" in e for e in _errors(repo))


# ---------- clone / fetch / repack ----------


def test_we_clone_a_repository_built_by_git(ref_repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "ours"
    repo = clone(str(ref_repo), dest)
    assert repo.head_commit() == git_text(ref_repo, "rev-parse", "HEAD").strip()
    assert (dest / "a.txt").read_bytes() == (ref_repo / "a.txt").read_bytes() and (dest / "feature.txt").exists()
    assert set(repo.refs.tags()) == {"v1", "v2"}
    assert set(repo.refs.branches()) == {"main"} and "feature" in {n.split("/")[-1] for n, _ in repo.refs.iter_refs("refs/remotes/")}
    run_git(dest, "fsck", "--strict")
    assert run_git(dest, "status", "--porcelain").stdout == b""
    assert repo.config.get("remote.origin.url") and repo.config.get("branch.main.remote") == "origin"


def test_we_clone_a_packed_repository(ref_repo: Path, tmp_path: Path) -> None:
    run_git(ref_repo, "gc", "--aggressive", "--prune=now", "-q")
    repo = clone(str(ref_repo), tmp_path / "ours")
    assert repo.head_commit() == git_text(ref_repo, "rev-parse", "HEAD").strip()
    assert _errors(repo) == []
    run_git(tmp_path / "ours", "fsck", "--strict")


def test_git_clones_a_repository_built_by_us(tmp_path: Path) -> None:
    ours = Repository.init(tmp_path / "src")
    write_tree(ours.wt, {"a": "hello\n", "d/b": "x\n" * 30})
    cwd = os.getcwd()
    os.chdir(ours.wt)
    try:
        ours.add_paths(["."])
    finally:
        os.chdir(cwd)
    c1 = ours.commit("one")
    ours.create_branch("side")
    write_tree(ours.wt, {"a": "hello again\n"})
    os.chdir(ours.wt)
    try:
        ours.add_paths(["a"])
    finally:
        os.chdir(cwd)
    ours.commit("two")
    run_git(tmp_path, "clone", "-q", str(ours.wt), "gitclone")
    clone_dir = tmp_path / "gitclone"
    assert git_text(clone_dir, "rev-parse", "HEAD").strip() == ours.head_commit()
    assert git_text(clone_dir, "log", "--format=%s").split() == ["two", "one"]
    assert git_text(clone_dir, "rev-parse", "origin/side").strip() == c1
    run_git(clone_dir, "fsck", "--strict")


def test_fetch_brings_new_commits_and_refs(ref_repo: Path, tmp_path: Path) -> None:
    repo = clone(str(ref_repo), tmp_path / "ours")
    write_tree(ref_repo, {"later.txt": "new\n"})
    run_git(ref_repo, "add", "-A")
    run_git(ref_repo, "commit", "-q", "-m", "later", tick=20)
    run_git(ref_repo, "tag", "v3")
    updated = fetch(repo)
    assert updated == {"main": git_text(ref_repo, "rev-parse", "main").strip()}
    assert repo.refs.resolve("refs/remotes/origin/main") == updated["main"]
    assert repo.read_commit(updated["main"]).message == b"later\n"
    assert "v3" in repo.refs.tags()
    assert fetch(repo) == {}  # повторный fetch ничего не меняет
    with pytest.raises(GitError, match="удалённого"):
        fetch(repo, "nope")


def test_clone_errors(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="не поддерживается"):
        clone("https://example.com/x.git", tmp_path / "x")
    with pytest.raises(GitError, match="не репозиторий"):
        clone(str(tmp_path / "missing"), tmp_path / "y")


def test_bare_clone(ref_repo: Path, tmp_path: Path) -> None:
    repo = clone(str(ref_repo), tmp_path / "bare.git", bare=True)
    assert repo.work_tree is None and set(repo.refs.branches()) >= {"main", "feature"}
    run_git(tmp_path / "bare.git", "fsck", "--strict")


def test_repack_makes_a_pack_git_accepts_and_keeps_everything_readable(ref_repo: Path, tmp_path: Path) -> None:
    repo = clone(str(ref_repo), tmp_path / "ours")
    before = set(repo.odb.all_shas())
    path = repack(repo)
    assert path is not None and path.exists()
    assert not list((repo.git_dir / "objects").glob("??/*"))  # рыхлых больше нет
    run_git(tmp_path / "ours", "verify-pack", str(path))
    run_git(tmp_path / "ours", "fsck", "--strict")
    assert set(repo.odb.all_shas()) == before
    assert repo.read_commit(repo.resolve("HEAD")).message  # чтение из пакета работает
    assert repack(Repository.init(tmp_path / "empty")) is None


# ---------- ссылки ----------


def test_check_ref_name_agrees_with_git(tmp_path: Path) -> None:
    names = [
        "refs/heads/ok",
        "refs/heads/a/b",
        "refs/heads/with-dash_and.dot",
        "refs/heads/bad name",
        "refs/heads/a..b",
        "refs/heads/x.lock",
        "refs/heads/end.",
        "refs/heads//x",
        "refs/heads/@{x",
        "refs/heads/a~b",
        "refs/heads/a^b",
        "refs/heads/a:b",
        "refs/heads/a?b",
        "refs/heads/a*b",
        "refs/heads/a[b",
        "refs/heads/.hidden",
        "refs/heads/a/.b",
        "/lead",
        "trail/",
        "refs/heads/юникод",
        "refs/heads/a\\b",
        "@",
    ]
    for n in names:
        git_ok = run_git(tmp_path, "check-ref-format", n, check=False).returncode == 0
        try:
            check_ref_name(n)
            ours_ok = True
        except GitError:
            ours_ok = False
        assert ours_ok == git_ok, n


def test_refs_read_packed_and_loose(ref_repo: Path) -> None:
    run_git(ref_repo, "pack-refs", "--all")
    refs = RefStore(ref_repo / ".git")
    assert refs.resolve("refs/heads/main") == git_text(ref_repo, "rev-parse", "main").strip()
    assert set(refs.branches()) == {"main", "feature"} and set(refs.tags()) == {"v1", "v2"}
    run_git(ref_repo, "branch", "loose")  # новая ссылка лежит файлом рядом с packed-refs
    assert "loose" in RefStore(ref_repo / ".git").branches()
    assert refs.resolve("refs/heads/missing") is None


def test_refs_update_delete_and_locking(tmp_path: Path) -> None:
    repo = Repository.init(tmp_path / "r")
    a, b = repo.write_blob(b"a"), repo.write_blob(b"b")
    repo.refs.update("refs/heads/x", a)
    assert repo.refs.resolve("refs/heads/x") == a
    repo.refs.update("refs/heads/x", b, expected_old=a)
    with pytest.raises(GitError, match="изменилась"):
        repo.refs.update("refs/heads/x", a, expected_old=a)  # устаревшее ожидание
    with pytest.raises(GitError, match="изменилась"):
        repo.refs.update("refs/heads/new", a, expected_old=b)
    lock = repo.git_dir / "refs/heads/x.lock"
    lock.write_text("held")
    with pytest.raises(GitError, match="заблокировать"):
        repo.refs.update("refs/heads/x", a)
    lock.unlink()
    repo.refs.update("refs/heads/deep/er/name", a)
    assert repo.refs.delete("refs/heads/deep/er/name")
    assert not (repo.git_dir / "refs/heads/deep").exists()  # пустые каталоги убраны
    assert not repo.refs.delete("refs/heads/never-existed")
    with pytest.raises(GitError):
        repo.refs.update("refs/heads/ok", "xyz")
    # символические ссылки и отсоединённый HEAD
    repo.refs.set_symbolic("HEAD", "refs/heads/x")
    assert repo.refs.head_branch() == "x" and repo.refs.resolve("HEAD") == b
    assert git_text(repo.wt, "symbolic-ref", "HEAD").strip() == "refs/heads/x"


def test_deleting_a_packed_ref(ref_repo: Path) -> None:
    run_git(ref_repo, "pack-refs", "--all")
    refs = RefStore(ref_repo / ".git")
    assert refs.delete("refs/heads/feature")
    assert run_git(ref_repo, "rev-parse", "--verify", "-q", "refs/heads/feature", check=False).returncode != 0
    assert git_text(ref_repo, "branch", "--list", "main").strip().endswith("main")
    run_git(ref_repo, "fsck")


def test_symbolic_loop_and_garbage_are_reported(tmp_path: Path) -> None:
    repo = Repository.init(tmp_path / "r")
    (repo.git_dir / "refs/heads/a").write_text("ref: refs/heads/b\n")
    (repo.git_dir / "refs/heads/b").write_text("ref: refs/heads/a\n")
    with pytest.raises(GitError, match="цепочка"):
        repo.refs.resolve("refs/heads/a")
    (repo.git_dir / "refs/heads/c").write_text("not a hash\n")
    with pytest.raises(GitError, match="повреждена"):
        repo.refs.resolve("refs/heads/c")


# ---------- конфигурация ----------


def test_config_parsing_against_git(tmp_path: Path) -> None:
    r = tmp_path / "r"
    r.mkdir()
    run_git(r, "init", "-q")
    (r / ".git/config").write_text(
        '[core]\n\tfilemode = false\n\tname = value with spaces   \n[user]\n\tname = "Quoted Name"\n\temail = a@b.c ; comment\n'
        '[remote "origin"]\n\turl = https://example.com/x.git\n[branch "feature/x"]\n\tmerge = refs/heads/x\n[Section.Sub]\n\tKey = V\n\tflag\n# comment\n; other\n'
        "[core]\n\tfilemode = true\n"
    )
    cfg = Config(r / ".git/config")
    for key in [
        "core.filemode",
        "core.name",
        "user.name",
        "user.email",
        "remote.origin.url",
        "branch.feature/x.merge",
        "section.Sub.key",
        "section.Sub.flag",
        "no.such",
    ]:
        git_val = run_git(r, "config", "--get", key, check=False).stdout.decode().strip()
        assert (cfg.get(key) or "") == git_val, key
    with pytest.raises(GitError):
        (r / ".git/config.bad").write_text("key = value before section\n")
        Config(r / ".git/config.bad")


def test_config_set_roundtrip_with_git(tmp_path: Path) -> None:
    repo = Repository.init(tmp_path / "r")
    repo.config.set("user.name", "Имя Фамилия")
    repo.config.set("user.email", "x@y.z")
    repo.config.set('remote "origin".url', "/some/path")
    repo.config.set("core.filemode", "true")  # переопределение существующего ключа
    assert git_text(repo.wt, "config", "--get", "user.email").strip() == "x@y.z"
    assert git_text(repo.wt, "config", "--get", "remote.origin.url").strip() == "/some/path"
    assert git_text(repo.wt, "config", "--get", "core.filemode").strip() == "true"
    assert (repo.git_dir / "config").read_text(encoding="utf-8").count("filemode") == 1
    assert Repository.discover(repo.wt).config.get("user.name") == "Имя Фамилия"
    assert MODE_FILE == 0o100644
