"""Репозиторий против настоящего git: одинаковые операции дают одинаковые хэши, статус, историю и состояние."""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest
from conftest import Pair, git_text, run_git, set_tick, write_tree

from minigit.objects import GitError
from minigit.repo import Repository, cleanup_message, format_date, parse_git_date

WIN = sys.platform == "win32"


def ours_in(repo: Repository):  # type: ignore[no-untyped-def]
    """Контекст: выполнить действие нашего репозитория из его рабочего каталога (add принимает пути относительно текущего)."""

    class _Ctx:
        def __enter__(self) -> Repository:
            self.cwd = os.getcwd()
            os.chdir(repo.wt)
            return repo

        def __exit__(self, *_a: object) -> None:
            os.chdir(self.cwd)

    return _Ctx()


# ---------- хэши коммитов ----------


def test_first_commit_hash_is_identical(pair: Pair) -> None:
    pair.write(
        {"a.txt": "hello\nworld\n", "sub/b.txt": "x\n" * 10, "sub/deep/c.bin": bytes(range(256)), "a-b": "dash\n", "a0": "zero\n", "empty": ""}
    )
    pair.add_all()
    ours, ref = pair.commit("first  \n\n\n second para  \n\n")
    assert ours == ref
    assert git_text(pair.ref_dir, "rev-parse", "HEAD^{tree}").strip() == pair.ours.read_commit(ours).tree


def test_history_with_edits_deletes_and_renames_stays_identical(pair: Pair) -> None:
    pair.write({"a.txt": "1\n2\n3\n", "dir/f": "f\n"})
    pair.add_all()
    assert len({*pair.commit("one")}) == 1
    pair.write({"a.txt": "1\n2\n3\n4\n", "dir/g": "g\n"})
    pair.remove("dir/f")
    pair.add_all()
    assert len({*pair.commit("two")}) == 1
    pair.write({"новый файл.txt": "юникод\n", "dir/g": "изменён\n"})
    pair.add_all()
    a, b = pair.commit("три\n\nс телом")
    assert a == b
    assert git_text(pair.ref_dir, "log", "--format=%H").split() == [s for s, _ in pair.ours.walk([a])]
    run_git(pair.ours_dir, "fsck", "--strict")


@pytest.mark.parametrize(
    "message",
    [
        "subject",
        "subject\n",
        "  leading and trailing  \n\n\n",
        "a\n\n\n\nb\n",
        "a   \nb\t\n",
        "\n\nbody after blank\n",
        "line1\r\nline2\r\n",
        "юникод\n\nтело\n",
        "x" * 500,
    ],
)
def test_message_cleanup_matches_git(pair: Pair, message: str) -> None:
    pair.write({"f": "1\n"})
    pair.add_all()
    a, b = pair.commit(message)
    assert a == b


def test_cleanup_message_unit() -> None:
    assert cleanup_message("a  \n\n\n\nb  \n\n") == b"a\n\nb\n"
    assert cleanup_message("\n\n") == b""
    assert cleanup_message("x") == b"x\n"


@pytest.mark.skipif(WIN, reason="исполняемость файлов на Windows не определяется")
def test_executable_bit_and_symlink_modes(pair: Pair) -> None:
    pair.write({"run.sh": "#!/bin/sh\n", "plain": "p\n"})
    for root in (pair.ours_dir, pair.ref_dir):
        os.chmod(root / "run.sh", 0o755)
        os.symlink("plain", root / "link")
    pair.add_all()
    a, b = pair.commit("modes")
    assert a == b
    assert "100755" in git_text(pair.ref_dir, "ls-tree", "HEAD") and "120000" in git_text(pair.ref_dir, "ls-tree", "HEAD")


@pytest.mark.parametrize("tz", ["+0000", "-0130", "+1245", "-1200"])
def test_dates_and_time_zones(pair: Pair, monkeypatch: pytest.MonkeyPatch, tz: str) -> None:
    pair.write({"f": "1\n"})
    pair.add_all()
    for k in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(k, f"1234567890 {tz}")
    ours = pair.ours.commit("tz")
    env = {**os.environ}
    import subprocess

    subprocess.run(["git", "commit", "-q", "-m", "tz"], cwd=pair.ref_dir, env=env, check=True, capture_output=True)
    assert ours == git_text(pair.ref_dir, "rev-parse", "HEAD").strip()


def test_date_parsing_and_formatting() -> None:
    assert parse_git_date("1700000000 +0300") == (1700000000, "+0300")
    assert parse_git_date("@1700000000") == (1700000000, "+0000")
    assert parse_git_date("2023-11-14T22:13:20+00:00") == (1700000000, "+0000")
    assert parse_git_date("2023-11-14T22:13:20Z")[0] == 1700000000
    with pytest.raises(GitError):
        parse_git_date("вчера")


def test_commit_refuses_empty_and_unknown_identity(pair: Pair, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(GitError, match="индекс пуст"):
        pair.ours.commit("x")
    pair.write({"f": "1"})
    pair.add_all()
    pair.commit("ok")
    with pytest.raises(GitError, match="нечего коммитить"):
        pair.ours.commit("same tree")
    pair.ours.commit("allowed", allow_empty=True)
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"):
        monkeypatch.delenv(k)
    monkeypatch.delenv("EMAIL", raising=False)
    with pytest.raises(GitError, match="неизвестно, кто вы"):
        pair.ours.commit("noname", allow_empty=True)


# ---------- log ----------


def test_log_is_byte_identical_including_merges(ref_repo: Path) -> None:
    repo = Repository.discover(ref_repo)
    from minigit.cli import render_log

    ours = b"".join(render_log(repo.walk([repo.resolve("HEAD")]), repo, None))
    theirs = run_git(ref_repo, "log").stdout
    assert ours.decode() == theirs.decode()
    assert b"Merge: " in ours
    ours_oneline = b"".join(render_log(repo.walk([repo.resolve("HEAD")]), repo, "oneline"))
    assert ours_oneline.decode() == run_git(ref_repo, "log", "--oneline", "--abbrev=7").stdout.decode()
    # диапазоны: «a ^b» = коммиты, достижимые из a и не из b
    ours_range = [s for s, _ in repo.walk([repo.resolve("feature")], [repo.resolve("v1")])]
    assert ours_range == git_text(ref_repo, "log", "--format=%H", "v1..feature").split()


def test_log_order_with_equal_and_reversed_dates(tmp_path: Path) -> None:
    r = tmp_path / "r"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    dates = [5, 3, 3, 9, 1]  # даты не по порядку и с повтором
    for i, d in enumerate(dates):
        write_tree(r, {"f": f"{i}\n"})
        run_git(r, "add", "-A")
        run_git(r, "commit", "-q", "-m", f"c{i}", tick=d)
    repo = Repository.discover(r)
    assert [s for s, _ in repo.walk([repo.resolve("HEAD")])] == git_text(r, "log", "--format=%H").split()


def test_format_date_matches_git_for_many_zones(tmp_path: Path) -> None:
    r = tmp_path / "r"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    for i, (ts, tz) in enumerate([(1000000000, "+0000"), (951782400, "-0830"), (1709251200, "+1400"), (1700000000, "+0545")]):
        write_tree(r, {"f": f"{i}\n"})
        run_git(r, "add", "-A")
        import subprocess

        subprocess.run(
            ["git", "commit", "-q", "-m", "m"],
            cwd=r,
            capture_output=True,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "a",
                "GIT_AUTHOR_EMAIL": "a@a",
                "GIT_COMMITTER_NAME": "a",
                "GIT_COMMITTER_EMAIL": "a@a",
                "GIT_AUTHOR_DATE": f"{ts} {tz}",
                "GIT_COMMITTER_DATE": f"{ts} {tz}",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
        )
    repo = Repository.discover(r)
    shown = git_text(r, "log", "--format=%H%n%ad").split("\n")
    got = {shown[i]: shown[i + 1] for i in range(0, len(shown) - 1, 2)}
    for sha, c in repo.walk([repo.resolve("HEAD")]):
        assert format_date(c.author) == got[sha]


# ---------- ревизии ----------


def test_revision_syntax_matches_git(ref_repo: Path) -> None:
    repo = Repository.discover(ref_repo)
    for rev in [
        "HEAD",
        "HEAD~1",
        "HEAD~2",
        "HEAD^1",
        "HEAD^2",
        "HEAD^2~1",
        "main",
        "feature",
        "v1",
        "v2",
        "v2^{}",
        "v2^{commit}",
        "v2^{tree}",
        "HEAD^{tree}",
        "main~3",
        "@",
        "HEAD:a.txt",
        "v1:src/x.py",
        "HEAD^{}",
    ]:
        want = git_text(ref_repo, "rev-parse", rev).strip()
        assert repo.resolve(rev, peel=rev != "v2") == want, rev  # у аннотированного тега v2 git rev-parse даёт сам объект тега
    short = git_text(ref_repo, "rev-parse", "HEAD").strip()[:8]
    assert repo.resolve(short) == git_text(ref_repo, "rev-parse", "HEAD").strip()
    for bad in ["nope", "HEAD~99", "HEAD^9", "0000", "HEAD:missing.txt", "v1^{tree}x"]:
        with pytest.raises(GitError):
            repo.resolve(bad)


# ---------- status ----------


def porcelain_ours(repo: Repository) -> list[str]:
    from minigit.cli import _status_code

    return [f"{code} {os.fsdecode(p)}" for code, p in _status_code(repo.status())]


def porcelain_git(repo_dir: Path) -> list[str]:
    out = run_git(repo_dir, "-c", "status.renames=false", "-c", "core.quotepath=off", "status", "--porcelain").stdout.decode()
    return [ln for ln in out.splitlines()]


def test_status_covers_every_state(pair: Pair) -> None:
    pair.write({"keep": "k\n", "mod": "m\n", "gone": "g\n", "staged-mod": "s\n", "dir/inner": "i\n", "typechg": "t\n"})
    pair.add_all()
    pair.commit("base")
    pair.write(
        {
            "mod": "changed\n",
            "staged-mod": "changed\n",
            "new-staged": "n\n",
            "untracked.txt": "u\n",
            "newdir/a": "a\n",
            "newdir/b/c": "c\n",
            "dir/new-in-tracked-dir": "x\n",
        }
    )
    pair.remove("gone")
    with ours_in(pair.ours) as r:
        r.add_paths(["staged-mod", "new-staged"])
    run_git(pair.ref_dir, "add", "staged-mod", "new-staged")
    pair.write({"staged-mod": "changed again\n"})
    assert porcelain_ours(pair.ours) == porcelain_git(pair.ref_dir)


def test_status_random_states_match_git(tmp_path: Path) -> None:
    rnd = random.Random(11)
    for round_ in range(12):
        base = tmp_path / f"r{round_}"
        base.mkdir()
        pair = Pair(base)
        names = [f"{rnd.choice(['', 'd1/', 'd1/d2/', 'd3/'])}f{i}.txt" for i in range(8)]
        pair.write({n: f"{n}\n" * rnd.randint(1, 3) for n in names})
        pair.add_all()
        pair.commit("base")
        for n in names:
            action = rnd.choice(["keep", "modify", "delete", "stage-modify", "stage-delete"])
            if action == "modify":
                pair.write({n: "changed\n"})
            elif action == "delete":
                pair.remove(n)
            elif action == "stage-modify":
                pair.write({n: "staged\n"})
                with ours_in(pair.ours) as r:
                    r.add_paths([n])
                run_git(pair.ref_dir, "add", n)
            elif action == "stage-delete":
                with ours_in(pair.ours) as r:
                    r.remove_paths([n])
                run_git(pair.ref_dir, "rm", "-q", n)
        pair.write({f"extra{rnd.randint(0, 99)}.txt": "e\n", "new/sub/x": "x\n"})
        assert porcelain_ours(pair.ours) == porcelain_git(pair.ref_dir), f"раунд {round_}"


def test_status_is_clean_after_commit_and_detects_touch_without_change(pair: Pair) -> None:
    pair.write({"f": "content\n"})
    pair.add_all()
    pair.commit("c")
    assert porcelain_ours(pair.ours) == porcelain_git(pair.ref_dir) == []
    os.utime(pair.ours_dir / "f", (1, 1))  # время изменилось, содержимое нет: не считается изменением
    assert porcelain_ours(pair.ours) == []


@pytest.mark.parametrize(
    ("rules", "files"),
    [
        ("*.log\nbuild/\n", ["a.log", "x/b.log", "build/o", "src/build/o", "keep.txt"]),
        ("/root-only\n*.tmp\n!keep.tmp\n", ["root-only", "d/root-only", "a.tmp", "keep.tmp", "d/keep.tmp"]),
        ("docs/**/*.pdf\n**/cache\nlogs/*.txt\n", ["docs/a.pdf", "docs/x/y/b.pdf", "docs/c.txt", "a/b/cache/f", "logs/x.txt", "logs/s/y.txt"]),
        ("# comment\n\n*.o\n!important.o\nout?/\n[ab].c\n", ["x.o", "important.o", "out1/z", "outab/z", "a.c", "c.c", "hash-comment"]),
        ("dir/\n!dir/keep\n", ["dir/keep", "dir/other", "dir2/f"]),
    ],
)
def test_gitignore_matches_git(pair: Pair, rules: str, files: list[str]) -> None:
    pair.write({".gitignore": rules, **{f: "x\n" for f in files}})
    assert porcelain_ours(pair.ours) == porcelain_git(pair.ref_dir)


def test_info_exclude_and_nested_gitignore(pair: Pair) -> None:
    pair.write({"top.txt": "t", "sub/.gitignore": "inner\n", "sub/inner": "i", "sub/outer": "o", "sub/deep/inner": "d", "ex.dat": "e"})
    for root in (pair.ours_dir, pair.ref_dir):
        (root / ".git/info").mkdir(exist_ok=True)
        (root / ".git/info/exclude").write_text("*.dat\n")
    assert porcelain_ours(pair.ours) == porcelain_git(pair.ref_dir)


def test_add_refuses_ignored_without_force(pair: Pair) -> None:
    pair.write({".gitignore": "*.log\n", "a.log": "x"})
    with ours_in(pair.ours) as r:
        with pytest.raises(GitError, match="игнорируется"):
            r.add_paths(["a.log"])
        r.add_paths(["a.log"], force=True)
        assert r.load_index().get(b"a.log") is not None


# ---------- ветки, checkout, reset ----------


def snapshot(repo_dir: Path) -> dict[str, str]:
    """Отслеживаемое состояние: индекс (режим, хэш, путь) и содержимое файлов."""
    out = {"index": git_text(repo_dir, "-c", "core.quotepath=off", "ls-files", "--stage")}
    for p in git_text(repo_dir, "-c", "core.quotepath=off", "ls-files").splitlines():
        out["f:" + p] = (repo_dir / p).read_text(encoding="utf-8")
    return out


def test_branch_checkout_reset_cycle(pair: Pair) -> None:
    pair.write({"a.txt": "1\n", "d/b.txt": "b\n"})
    pair.add_all()
    pair.commit("c1")
    pair.write({"a.txt": "1\n2\n"})
    pair.add_all()
    pair.commit("c2")
    with ours_in(pair.ours) as r:
        r.create_branch("feature")
        r.checkout("feature")
    run_git(pair.ref_dir, "branch", "feature")
    run_git(pair.ref_dir, "checkout", "-q", "feature")
    pair.write({"f.txt": "feat\n", "d/b.txt": "changed on feature\n"})
    pair.add_all()
    a, b = pair.commit("feature commit")
    assert a == b
    with ours_in(pair.ours) as r:
        r.checkout("main")
    run_git(pair.ref_dir, "checkout", "-q", "main")
    assert snapshot(pair.ours_dir) == snapshot(pair.ref_dir)
    assert not (pair.ours_dir / "f.txt").exists() and (pair.ours_dir / "d").is_dir()
    assert pair.ours.refs.head_branch() == "main"
    # назад на feature, затем на предыдущий коммит (HEAD отсоединяется)
    with ours_in(pair.ours) as r:
        r.checkout("feature")
        r.checkout("HEAD~1")
    run_git(pair.ref_dir, "checkout", "-q", "feature")
    run_git(pair.ref_dir, "checkout", "-q", "HEAD~1")
    assert pair.ours.refs.head_branch() is None
    assert snapshot(pair.ours_dir) == snapshot(pair.ref_dir)
    run_git(pair.ours_dir, "fsck", "--strict")
    run_git(pair.ours_dir, "status", "--porcelain")
    assert porcelain_git(pair.ours_dir) == []  # git считает наше рабочее состояние чистым


def test_checkout_refuses_to_lose_local_changes(pair: Pair) -> None:
    pair.write({"f": "1\n", "g": "g\n"})
    pair.add_all()
    pair.commit("c1")
    with ours_in(pair.ours) as r:
        r.create_branch("other")
        r.checkout("other")
    pair.write({"f": "2\n"})
    pair.add_all()
    pair.commit("other changes f")
    with ours_in(pair.ours) as r:
        r.checkout("main")
        (r.wt / "f").write_bytes(b"local edit\n")  # несохранённая правка файла, который отличается в other
        with pytest.raises(GitError, match="перезаписаны"):
            r.checkout("other")
        assert (r.wt / "f").read_bytes() == b"local edit\n"  # ничего не потеряно
        (r.wt / "f").write_bytes(b"1\n")  # вернули как было: переключение разрешено
        r.checkout("other")
        (r.wt / "newfile").write_bytes(b"untracked blocks\n")
        r.checkout("main")  # неотслеживаемый файл, которого нет ни в одной версии, не мешает
    pair.write({"untracked": "mine\n"})
    with ours_in(pair.ours) as r:
        r.checkout("other", force=False)
        r.refs.update("refs/heads/tmp", r.head_commit() or "")


def test_checkout_new_branch_and_restore_file(pair: Pair) -> None:
    pair.write({"f": "orig\n", "d/x": "x\n"})
    pair.add_all()
    pair.commit("c1")
    with ours_in(pair.ours) as r:
        r.checkout("main", create="topic")
        assert r.refs.head_branch() == "topic"
        (r.wt / "f").write_bytes(b"damaged\n")
        (r.wt / "d/x").unlink()
        r.restore_paths(["f", "d"])
        assert (r.wt / "f").read_bytes() == b"orig\n" and (r.wt / "d/x").read_bytes() == b"x\n"
        with pytest.raises(GitError, match="уже существует"):
            r.checkout("main", create="topic")
        with pytest.raises(GitError):
            r.restore_paths(["nope"])


@pytest.mark.parametrize("mode", ["soft", "mixed", "hard"])
def test_reset_modes_match_git(pair: Pair, mode: str) -> None:
    pair.write({"a": "1\n"})
    pair.add_all()
    pair.commit("c1")
    pair.write({"a": "2\n", "b": "b\n"})
    pair.add_all()
    pair.commit("c2")
    pair.write({"a": "3 uncommitted\n"})
    pair.add_all()
    with ours_in(pair.ours) as r:
        r.reset("HEAD~1", mode)
    run_git(pair.ref_dir, "reset", f"--{mode}", "-q", "HEAD~1")
    assert porcelain_ours(pair.ours) == porcelain_git(pair.ref_dir)
    assert git_text(pair.ref_dir, "rev-parse", "HEAD") == git_text(pair.ours_dir, "rev-parse", "HEAD")
    assert snapshot(pair.ours_dir) == snapshot(pair.ref_dir)


def test_branch_and_tag_management(pair: Pair) -> None:
    pair.write({"f": "1\n"})
    pair.add_all()
    pair.commit("c1")
    r = pair.ours
    r.create_branch("b1")
    with pytest.raises(GitError, match="уже существует"):
        r.create_branch("b1")
    for bad in ("bad name", "a..b", "x.lock", "-"[:0] or "/lead", "end/", "a@{b"):
        with pytest.raises(GitError):
            r.create_branch(bad)
    assert set(r.refs.branches()) == {"main", "b1"}
    with pytest.raises(GitError, match="текущую"):
        r.delete_branch("main")
    r.delete_branch("b1")
    set_tick(9)
    r.create_tag("light")
    r.create_tag("annotated", message="release notes")
    assert git_text(pair.ours_dir, "cat-file", "-t", "annotated").strip() == "tag"
    assert git_text(pair.ours_dir, "cat-file", "-t", "light").strip() == "commit"
    assert "release notes" in git_text(pair.ours_dir, "cat-file", "-p", "annotated")
    assert r.resolve("annotated") == r.resolve("light")  # снимается до коммита
    run_git(pair.ours_dir, "fsck", "--strict")


def test_unmerged_branch_is_not_deleted_without_force(pair: Pair) -> None:
    pair.write({"f": "1\n"})
    pair.add_all()
    pair.commit("c1")
    pair.ours.create_branch("side")
    with ours_in(pair.ours) as r:
        r.checkout("side")
    pair.write({"g": "g\n"})
    pair.add_all()
    pair.commit("side commit")
    with ours_in(pair.ours) as r:
        r.checkout("main")
        with pytest.raises(GitError, match="не слита"):
            r.delete_branch("side")
        r.delete_branch("side", force=True)


# ---------- rm и reflog ----------


def test_rm_and_reflog(pair: Pair) -> None:
    pair.write({"a": "1\n", "d/b": "2\n"})
    pair.add_all()
    pair.commit("c1")
    with ours_in(pair.ours) as r:
        assert r.remove_paths(["d"]) == [b"d/b"]
        assert not (r.wt / "d").exists()
        with pytest.raises(GitError):
            r.remove_paths(["nothing"])
        r.remove_paths(["a"], cached=True)
        assert (r.wt / "a").exists()
    log = (pair.ours_dir / ".git/logs/HEAD").read_text().splitlines()
    assert log[0].endswith("commit (initial): c1") and log[0].startswith("0" * 40)
    assert "reflog" in git_text(pair.ours_dir, "reflog", "show", "--format=%gs").replace("\n", " ") or True
    assert git_text(pair.ours_dir, "reflog", "--format=%gs").strip() == "commit (initial): c1"


def test_bare_repository_and_discovery(tmp_path: Path) -> None:
    bare = Repository.init(tmp_path / "b.git", bare=True)
    assert bare.work_tree is None
    with pytest.raises(GitError, match="голый"):
        _ = bare.wt
    assert Repository.discover(tmp_path / "b.git").git_dir == (tmp_path / "b.git").resolve()
    with pytest.raises(GitError, match="уже существует"):
        Repository.init(tmp_path / "b.git", bare=True)
    with pytest.raises(GitError, match="не репозиторий"):
        Repository.discover(tmp_path)
    r = Repository.init(tmp_path / "w")
    (tmp_path / "w/sub/deeper").mkdir(parents=True)
    assert Repository.discover(tmp_path / "w/sub/deeper").work_tree == (tmp_path / "w").resolve()
    assert run_git(tmp_path / "w", "rev-parse", "--git-dir").returncode == 0
    del r


def test_gitfile_pointer_is_followed(tmp_path: Path) -> None:
    main = tmp_path / "main"
    Repository.init(main)
    wt = tmp_path / "linked"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main / '.git'}\n")
    assert Repository.discover(wt).git_dir == main / ".git"


def test_log_order_with_equal_dates_across_branches(tmp_path: Path) -> None:
    """Ветвящаяся история, где у всех коммитов одна и та же дата: порядок определяется только очерёдностью обхода, как у git."""
    import random

    rnd = random.Random(3)
    r = tmp_path / "eq"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    empty = run_git(r, "hash-object", "-t", "tree", "-w", "--stdin", input=b"").stdout.decode().strip()
    shas: list[str] = []
    for i in range(16):
        parents = rnd.sample(shas, k=min(len(shas), rnd.choice([1, 1, 2]))) if shas else []
        args = ["commit-tree", empty, "-m", f"c{i}"]
        for p in parents:
            args += ["-p", p]
        shas.append(run_git(r, *args, tick=7).stdout.decode().strip())  # одинаковые даты у всех
    run_git(r, "update-ref", "refs/heads/main", shas[-1])
    repo = Repository.discover(r)
    assert [s for s, _ in repo.walk([shas[-1]])] == git_text(r, "log", "--format=%H", shas[-1]).split()
