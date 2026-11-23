"""Слияние: текст против `git merge-file`, деревья и конфликты против `git merge`, общие предки против `git merge-base`."""

from __future__ import annotations

import os
import random
import subprocess
from pathlib import Path

import pytest
from conftest import Pair, git_env, git_text, run_git, set_tick, write_tree

from minigit.merge import merge3, merge_commits
from minigit.objects import GitError
from minigit.repo import Repository

# ---------- merge3 ----------


def git_merge_file(tmp: Path, base: bytes, ours: bytes, theirs: bytes) -> tuple[bytes, int]:
    for name, data in (("o", ours), ("b", base), ("t", theirs)):
        (tmp / name).write_bytes(data)
    r = subprocess.run(
        ["git", "merge-file", "-p", "-L", "ours", "-L", "base", "-L", "theirs", "o", "b", "t"], cwd=tmp, env=git_env(), capture_output=True
    )
    return r.stdout, r.returncode


def test_clean_merges() -> None:
    base = b"a\nb\nc\nd\ne\nf\ng\n"
    assert merge3(base, b"A\nb\nc\nd\ne\nf\ng\n", b"a\nb\nc\nd\ne\nf\nG\n") == (b"A\nb\nc\nd\ne\nf\nG\n", 0)  # правки в разных местах
    assert merge3(base, base, b"x\n") == (b"x\n", 0)  # одна сторона не меняла
    assert merge3(base, b"x\n", base) == (b"x\n", 0)
    assert merge3(base, b"same\n", b"same\n") == (b"same\n", 0)  # обе внесли одно и то же
    assert merge3(b"", b"", b"") == (b"", 0)
    out, n = merge3(base, b"a\nb\nc\nd\ne\nf\ng\nextra\n", b"a\nb\nc\nd\ne\nf\ng\nextra\n")
    assert n == 0 and out.endswith(b"extra\n")


def test_conflict_markers_and_labels() -> None:
    out, n = merge3(b"a\nb\nc\n", b"a\nOURS\nc\n", b"a\nTHEIRS\nc\n", "HEAD", "feature")
    assert n == 1
    assert out == b"a\n<<<<<<< HEAD\nOURS\n=======\nTHEIRS\n>>>>>>> feature\nc\n"


def test_adjacent_edits_conflict_like_git() -> None:
    out, n = merge3(b"a\nb\nc\nd\n", b"a\nB\nc\nd\n", b"a\nb\nC\nd\n")
    assert n == 1 and b"<<<<<<<" in out  # изменения в соседних строках: конфликт, как в git


def test_missing_trailing_newline_inside_conflict() -> None:
    out, n = merge3(b"a\n", b"ours", b"theirs")
    assert n == 1
    assert out == b"<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>> theirs\n"


def test_add_add_with_empty_base() -> None:
    out, n = merge3(b"", b"one\n", b"two\n")
    assert n == 1 and b"one" in out and b"two" in out
    assert merge3(b"", b"same\n", b"same\n") == (b"same\n", 0)


def test_common_lines_inside_a_conflict_follow_git(tmp_path: Path) -> None:
    """Общие строки между правками конфликта: слияние уточняет конфликт и склеивает близкие конфликты, как xdiff (проверено против git)."""
    cases = [
        (b"start\nx\nend\n", b"start\nA\ncommon\nB\nend\n", b"start\nC\ncommon\nD\nend\n"),
        (b"start\nx\nend\n", b"start\nA\ncommon\nm\nn\no\nB\nend\n", b"start\nC\ncommon\nm\nn\no\nD\nend\n"),
        (b"1\n2\n3\n", b"1\nX\n3\n", b"1\nX\nY\n3\n"),
        (b"a\nb\n", b"a\nb\nours\n", b"a\nb\ntheirs\n"),
    ]
    for base, ours, theirs in cases:
        expected, code = git_merge_file(tmp_path, base, ours, theirs)
        got, n = merge3(base, ours, theirs, "ours", "theirs")
        assert got == expected
        assert (n > 0) == (code > 0)


def test_algebraic_properties() -> None:
    rnd = random.Random(4)
    for _ in range(200):
        base = b"".join(b"line %d\n" % rnd.randrange(30) for _ in range(rnd.randint(0, 12)))
        a = b"".join(b"line %d\n" % rnd.randrange(30) for _ in range(rnd.randint(0, 12)))
        assert merge3(base, a, a) == (a, 0)
        assert merge3(base, base, a) == (a, 0)
        assert merge3(base, a, base) == (a, 0)


def test_random_triples_agree_with_git_merge_file(tmp_path: Path) -> None:
    rnd = random.Random(1)
    uniq = [f"line {i}\n".encode() for i in range(40)]
    same = conflict_agree = total = 0
    for t in range(500):
        pool = uniq if t % 2 == 0 else [*uniq[:6], b"\n"]
        base = [rnd.choice(pool) for _ in range(rnd.randint(3, 25))]

        def edit(lines: list[bytes], pool: list[bytes] = pool) -> list[bytes]:
            lines = list(lines)
            for _ in range(rnd.randint(0, 3)):
                op = rnd.random()
                if lines and op < 0.3:
                    del lines[rnd.randrange(len(lines))]
                elif op < 0.65:
                    lines.insert(rnd.randint(0, len(lines)), rnd.choice(pool))
                elif lines:
                    lines[rnd.randrange(len(lines))] = rnd.choice(pool)
            return lines

        b, o, th = b"".join(base), b"".join(edit(base)), b"".join(edit(base))
        git_out, code = git_merge_file(tmp_path, b, o, th)
        ours, n = merge3(b, o, th, "ours", "theirs")
        total += 1
        same += ours == git_out
        conflict_agree += (n > 0) == (code > 0)
    assert conflict_agree == total, "наличие конфликта должно совпадать с git всегда"
    assert same / total >= 0.99, f"побайтно совпало {same} из {total}"


# ---------- слияние деревьев ----------


def _commit_both(pair: Pair, message: str) -> None:
    pair.add_all()
    pair.commit(message)


def _setup_branches(pair: Pair, files: dict[str, str], ours_changes: dict[str, str | None], theirs_changes: dict[str, str | None]) -> None:
    pair.write(files)
    _commit_both(pair, "base")
    pair.ours.create_branch("topic")
    run_git(pair.ref_dir, "branch", "topic")
    cwd = os.getcwd()
    os.chdir(pair.ours_dir)
    try:
        pair.ours.checkout("topic")
    finally:
        os.chdir(cwd)
    run_git(pair.ref_dir, "checkout", "-q", "topic")
    _apply(pair, theirs_changes)
    _commit_both(pair, "topic change")
    os.chdir(pair.ours_dir)
    try:
        pair.ours.checkout("main")
    finally:
        os.chdir(cwd)
    run_git(pair.ref_dir, "checkout", "-q", "main")
    _apply(pair, ours_changes)
    _commit_both(pair, "main change")


def _apply(pair: Pair, changes: dict[str, str | None]) -> None:
    for path, content in changes.items():
        if content is None:
            pair.remove(path)
        else:
            pair.write({path: content})


def _merge_both(pair: Pair, target: str = "topic", no_ff: bool = False) -> tuple[object, subprocess.CompletedProcess[bytes]]:
    set_tick(50)
    cwd = os.getcwd()
    os.chdir(pair.ours_dir)
    try:
        result = merge_commits(pair.ours, target, no_ff=no_ff)
    finally:
        os.chdir(cwd)
    args = ["merge", "-q", "--no-edit", *(["--no-ff"] if no_ff else []), target]
    return result, run_git(pair.ref_dir, *args, check=False, tick=50)


def _stages(repo_dir: Path) -> str:
    return git_text(repo_dir, "-c", "core.quotepath=off", "ls-files", "-u")


def test_clean_merge_produces_the_same_commit_as_git(pair: Pair) -> None:
    _setup_branches(
        pair,
        {"a.txt": "1\n2\n3\n4\n5\n6\n7\n", "b.txt": "b\n", "keep": "k\n"},
        {"a.txt": "ONE\n2\n3\n4\n5\n6\n7\n", "ours-new.txt": "o\n"},
        {"a.txt": "1\n2\n3\n4\n5\n6\nSEVEN\n", "b.txt": "b changed\n", "theirs-new.txt": "t\n"},
    )
    result, ref = _merge_both(pair)
    assert ref.returncode == 0
    assert getattr(result, "status", "") == "merged"
    assert pair.ours.head_commit() == git_text(pair.ref_dir, "rev-parse", "HEAD").strip()  # тот же коммит: дерево, родители, сообщение, даты
    assert (pair.ours_dir / "a.txt").read_bytes() == b"ONE\n2\n3\n4\n5\n6\nSEVEN\n"
    run_git(pair.ours_dir, "fsck", "--strict")
    assert run_git(pair.ours_dir, "status", "--porcelain").stdout == b""


def test_deletions_and_additions_merge_cleanly(pair: Pair) -> None:
    _setup_branches(
        pair,
        {"keep": "k\n", "del-ours": "d\n", "del-theirs": "d\n", "dir/x": "x\n", "dir/y": "y\n"},
        {"del-ours": None, "dir/x": None},
        {"del-theirs": None, "dir/y": None, "new/deep/file": "n\n"},
    )
    result, ref = _merge_both(pair)
    assert ref.returncode == 0 and getattr(result, "status", "") == "merged"
    assert pair.ours.head_commit() == git_text(pair.ref_dir, "rev-parse", "HEAD").strip()
    assert not (pair.ours_dir / "dir").exists() or not any((pair.ours_dir / "dir").iterdir())


def test_content_conflict_matches_git_files_and_index(pair: Pair) -> None:
    _setup_branches(
        pair,
        {"f.txt": "a\nb\nc\nd\ne\n", "ok.txt": "ok\n"},
        {"f.txt": "a\nOURS\nc\nd\ne\n"},
        {"f.txt": "a\nTHEIRS\nc\nd\ne\n", "ok.txt": "ok theirs\n"},
    )
    result, ref = _merge_both(pair)
    assert ref.returncode == 1
    assert getattr(result, "status", "") == "conflict" and getattr(result, "conflicts", []) == [b"f.txt"]
    assert (pair.ours_dir / "f.txt").read_bytes() == (pair.ref_dir / "f.txt").read_bytes()
    assert (pair.ours_dir / "f.txt").read_bytes().startswith(b"a\n<<<<<<< HEAD\nOURS\n=======\nTHEIRS\n>>>>>>> topic\n")
    assert (pair.ours_dir / "ok.txt").read_bytes() == b"ok theirs\n"
    assert _stages(pair.ours_dir) == _stages(pair.ref_dir)
    assert (pair.ours_dir / ".git/MERGE_HEAD").read_text().strip() == git_text(pair.ref_dir, "rev-parse", "MERGE_HEAD").strip()
    with pytest.raises(GitError, match="конфликт"):
        pair.ours.commit("too early")
    # разрешаем одинаково в обоих и коммитим: коммиты совпадают
    pair.write({"f.txt": "a\nRESOLVED\nc\nd\ne\n"})
    cwd = os.getcwd()
    os.chdir(pair.ours_dir)
    try:
        pair.ours.add_paths(["f.txt"])
    finally:
        os.chdir(cwd)
    run_git(pair.ref_dir, "add", "f.txt")
    set_tick(51)
    ours = pair.ours.commit("Merge branch 'topic'")
    run_git(pair.ref_dir, "commit", "-q", "-m", "Merge branch 'topic'", tick=51)
    assert ours == git_text(pair.ref_dir, "rev-parse", "HEAD").strip()
    assert len(pair.ours.read_commit(ours).parents) == 2
    assert not (pair.ours_dir / ".git/MERGE_HEAD").exists()


def test_modify_delete_and_add_add_conflicts(pair: Pair) -> None:
    _setup_branches(pair, {"md": "base\n", "keep": "k\n"}, {"md": "ours changed\n", "aa": "ours add\n"}, {"md": None, "aa": "theirs add\n"})
    result, ref = _merge_both(pair)
    assert ref.returncode == 1 and getattr(result, "status", "") == "conflict"
    assert set(getattr(result, "conflicts", [])) == {b"md", b"aa"}
    assert _stages(pair.ours_dir) == _stages(pair.ref_dir)
    assert (pair.ours_dir / "md").read_bytes() == (pair.ref_dir / "md").read_bytes()
    assert (pair.ours_dir / "aa").read_bytes() == (pair.ref_dir / "aa").read_bytes()


def test_mode_change_merges_like_git(pair: Pair) -> None:
    if os.name == "nt":
        pytest.skip("режим исполняемости на Windows не определяется")
    _setup_branches(pair, {"s": "#!/bin/sh\n", "t": "t\n"}, {"t": "t changed\n"}, {"s": "#!/bin/sh\n"})
    os.chmod(pair.ref_dir / "s", 0o755)
    result, ref = _merge_both(pair)
    assert getattr(result, "status", "") == "merged" and ref.returncode == 0


def test_fast_forward_up_to_date_and_no_ff(pair: Pair) -> None:
    pair.write({"f": "1\n"})
    _commit_both(pair, "c1")
    pair.ours.create_branch("ahead")
    run_git(pair.ref_dir, "branch", "ahead")
    cwd = os.getcwd()
    os.chdir(pair.ours_dir)
    try:
        pair.ours.checkout("ahead")
    finally:
        os.chdir(cwd)
    run_git(pair.ref_dir, "checkout", "-q", "ahead")
    pair.write({"g": "2\n"})
    _commit_both(pair, "c2")
    os.chdir(pair.ours_dir)
    try:
        pair.ours.checkout("main")
    finally:
        os.chdir(cwd)
    run_git(pair.ref_dir, "checkout", "-q", "main")
    # перемотка
    os.chdir(pair.ours_dir)
    try:
        r = merge_commits(pair.ours, "ahead")
    finally:
        os.chdir(cwd)
    run_git(pair.ref_dir, "merge", "-q", "--ff-only", "ahead")
    assert r.status == "fast-forward" and pair.ours.head_commit() == git_text(pair.ref_dir, "rev-parse", "HEAD").strip()
    assert (pair.ours_dir / "g").exists()
    # уже слито
    os.chdir(pair.ours_dir)
    try:
        assert merge_commits(pair.ours, "ahead").status == "up-to-date"
    finally:
        os.chdir(cwd)


def test_no_ff_creates_a_merge_commit_identical_to_git(pair: Pair) -> None:
    pair.write({"f": "1\n"})
    _commit_both(pair, "c1")
    pair.ours.create_branch("ahead")
    run_git(pair.ref_dir, "branch", "ahead")
    cwd = os.getcwd()
    os.chdir(pair.ours_dir)
    try:
        pair.ours.checkout("ahead")
    finally:
        os.chdir(cwd)
    run_git(pair.ref_dir, "checkout", "-q", "ahead")
    pair.write({"g": "2\n"})
    _commit_both(pair, "c2")
    os.chdir(pair.ours_dir)
    try:
        pair.ours.checkout("main")
    finally:
        os.chdir(cwd)
    run_git(pair.ref_dir, "checkout", "-q", "main")
    result, ref = _merge_both(pair, "ahead", no_ff=True)
    assert ref.returncode == 0 and getattr(result, "status", "") == "merged"
    assert pair.ours.head_commit() == git_text(pair.ref_dir, "rev-parse", "HEAD").strip()


def test_merge_refuses_unsafe_states(pair: Pair) -> None:
    _setup_branches(pair, {"f": "1\n2\n3\n"}, {"o": "o\n"}, {"f": "1\n2\nthree\n"})
    (pair.ours_dir / "f").write_bytes(b"1\n2\n3\nlocal edit\n")
    cwd = os.getcwd()
    os.chdir(pair.ours_dir)
    try:
        pair.ours.add_paths(["f"])
        with pytest.raises(GitError, match="перезаписаны"):
            merge_commits(pair.ours, "topic")
        with pytest.raises(GitError):
            merge_commits(pair.ours, "no-such-branch")
    finally:
        os.chdir(cwd)


# ---------- общие предки ----------


def _build_dag(tmp: Path, rnd: random.Random, n: int) -> tuple[Path, list[str]]:
    """Случайный DAG из n коммитов (с перекрёстными слияниями), собранный низкоуровневыми командами git."""
    r = tmp / "dag"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    empty = run_git(r, "hash-object", "-t", "tree", "-w", "--stdin", input=b"").stdout.decode().strip()
    shas: list[str] = []
    for i in range(n):
        parents = rnd.sample(shas, k=min(len(shas), rnd.choice([0, 1, 1, 1, 2]))) if shas else []
        args = ["commit-tree", empty, "-m", f"c{i}"]
        for p in parents:
            args += ["-p", p]
        shas.append(run_git(r, *args, tick=i).stdout.decode().strip())
    run_git(r, "update-ref", "refs/heads/main", shas[-1])
    return r, shas


def test_merge_bases_match_git_on_random_dags(tmp_path: Path) -> None:
    rnd = random.Random(8)
    for round_ in range(4):
        sub = tmp_path / str(round_)
        sub.mkdir()
        r, shas = _build_dag(sub, rnd, 22)
        repo = Repository.discover(r)
        for _ in range(25):
            a, b = rnd.sample(shas, 2)
            expected = sorted(git_text(r, "merge-base", "--all", a, b).split()) if run_git(r, "merge-base", a, b, check=False).returncode == 0 else []
            assert sorted(repo.merge_bases(a, b)) == expected, (round_, a, b)


def test_criss_cross_merge_uses_a_virtual_base_like_git(tmp_path: Path) -> None:
    """Перекрёстное слияние: два лучших предка. Результат (дерево) совпадает с git merge (стратегия ort)."""
    r = tmp_path / "x"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    write_tree(r, {"f": "1\n2\n3\n4\n5\n6\n7\n8\n9\n"})
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "base", tick=1)
    run_git(r, "branch", "other")
    write_tree(r, {"f": "1\nA\n3\n4\n5\n6\n7\n8\n9\n"})
    run_git(r, "commit", "-q", "-am", "main change", tick=2)
    run_git(r, "checkout", "-q", "other")
    write_tree(r, {"f": "1\n2\n3\n4\n5\n6\n7\nB\n9\n"})
    run_git(r, "commit", "-q", "-am", "other change", tick=3)
    run_git(r, "branch", "other2")
    run_git(r, "checkout", "-q", "main")
    run_git(r, "merge", "-q", "--no-edit", "other", tick=4)  # M1 на main
    run_git(r, "checkout", "-q", "other")
    run_git(r, "merge", "-q", "--no-edit", "main~1", tick=5)  # M2 на other (родитель main~1 = «main change»)
    write_tree(r, {"g": "side\n"})
    run_git(r, "add", "-A")
    run_git(r, "commit", "-q", "-m", "other extra", tick=6)
    repo = Repository.discover(r)
    bases = repo.merge_bases(repo.resolve("main"), repo.resolve("other"))
    assert sorted(bases) == sorted(git_text(r, "merge-base", "--all", "main", "other").split())
    # настоящее слияние: наш результат против git
    clone = tmp_path / "clone"
    run_git(tmp_path, "clone", "-q", str(r), "clone")
    run_git(clone, "checkout", "-q", "main")
    run_git(clone, "branch", "-f", "other", "origin/other")
    ours_repo = Repository.discover(clone)
    set_tick(60)
    ours = merge_commits(ours_repo, "other")
    ref = run_git(r, "merge", "-q", "--no-edit", "other", check=False, tick=60)
    assert ours.status == "merged" and ref.returncode == 0
    assert ours_repo.read_commit(ours.commit or "").tree == git_text(r, "rev-parse", "HEAD^{tree}").strip()


def test_in_progress_merge_blocks_a_second_one(pair: Pair) -> None:
    _setup_branches(pair, {"f": "a\nb\nc\n"}, {"f": "a\nOURS\nc\n"}, {"f": "a\nTHEIRS\nc\n"})
    _merge_both(pair)
    cwd = os.getcwd()
    os.chdir(pair.ours_dir)
    try:
        with pytest.raises(GitError, match="уже идёт"):
            merge_commits(pair.ours, "topic")
        pair.ours.reset("HEAD", "hard")  # отмена слияния
    finally:
        os.chdir(cwd)
    assert not (pair.ours_dir / ".git/MERGE_HEAD").exists()
    assert (pair.ours_dir / "f").read_bytes() == b"a\nOURS\nc\n"
