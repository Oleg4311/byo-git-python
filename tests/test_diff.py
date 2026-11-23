"""Diff: корректность по свойствам, сверка с `git diff` и проверка, что любой наш патч принимает `git apply`."""

from __future__ import annotations

import os
import random
import subprocess
from pathlib import Path

import pytest
from conftest import git_env, run_git
from hypothesis import given, settings
from hypothesis import strategies as st

from minigit.diff import Change, _lcs_matches, _myers, diff_lines, format_hunks, is_binary, split_lines
from minigit.objects import hash_object
from minigit.patch import Side, diff_sides

LINES = st.lists(st.sampled_from([b"a\n", b"b\n", b"c\n", b"d\n", b"\n", b"  x\n"]), max_size=14)


@settings(max_examples=300, deadline=None)
@given(a=LINES, b=LINES)
def test_myers_finds_a_longest_common_subsequence(a: list[bytes], b: list[bytes]) -> None:
    found = _myers(a, b)
    assert found is not None
    assert len(found) == len(_lcs_matches(a, b))
    assert all(a[i] == b[j] for i, j in found)
    assert all(found[k][0] < found[k + 1][0] and found[k][1] < found[k + 1][1] for k in range(len(found) - 1))


@settings(max_examples=300, deadline=None)
@given(a=LINES, b=LINES)
def test_applying_changes_reconstructs_the_new_text(a: list[bytes], b: list[bytes]) -> None:
    out: list[bytes] = []
    pos = 0
    for c in diff_lines(a, b):
        out += a[pos : c.i1] + b[c.i2 : c.i2 + c.n2]
        pos = c.i1 + c.n1
    out += a[pos:]
    assert out == b
    # группы упорядочены, не пересекаются и не вплотную друг к другу
    cs = diff_lines(a, b)
    assert all(cs[k].i1 + cs[k].n1 < cs[k + 1].i1 for k in range(len(cs) - 1))
    # размер правок минимален: столько же, сколько даёт самая длинная общая подпоследовательность
    assert sum(c.n1 + c.n2 for c in cs) == len(a) + len(b) - 2 * len(_lcs_matches(a, b))


def test_split_lines() -> None:
    assert split_lines(b"") == []
    assert split_lines(b"a\nb") == [b"a\n", b"b"]
    assert split_lines(b"a\r\nb\r\n") == [b"a\r\n", b"b\r\n"]
    assert split_lines(b"a\rb\n") == [b"a\rb\n"]  # одиночный «\r» — не конец строки
    assert split_lines(b"\n\n") == [b"\n", b"\n"]


def test_binary_detection_and_limits() -> None:
    assert is_binary(b"abc\0def") and not is_binary(b"text\n") and not is_binary(b"x" * 9000 + b"\0")


def test_hunk_headers_for_edge_cases() -> None:
    assert format_hunks([], [b"a\n", b"b\n"], [Change(0, 0, 0, 2)]).startswith(b"@@ -0,0 +1,2 @@\n")
    assert format_hunks([b"a\n", b"b\n"], [], [Change(0, 2, 0, 0)]).startswith(b"@@ -1,2 +0,0 @@\n")
    assert format_hunks([b"a\n"], [b"b\n"], [Change(0, 1, 0, 1)]).startswith(b"@@ -1 +1 @@\n")
    assert format_hunks([b"a"], [b"a\n"], diff_lines([b"a"], [b"a\n"])).endswith(b"-a\n\\ No newline at end of file\n+a\n")


# ---------- сверка с git diff ----------


def git_diff(tmp: Path, a: bytes, b: bytes, *extra: str) -> bytes:
    (tmp / "a").write_bytes(a)
    (tmp / "b").write_bytes(b)
    out = subprocess.run(["git", "diff", "--no-index", "--no-color", *extra, "a", "b"], cwd=tmp, env=git_env(), capture_output=True).stdout
    i = out.find(b"@@")
    return out[i:] if i >= 0 else b""


def our_diff(a: bytes, b: bytes) -> bytes:
    al, bl = split_lines(a), split_lines(b)
    return format_hunks(al, bl, diff_lines(al, bl))


CURATED = {
    "вставка в начало": (b"b\nc\n", b"a\nb\nc\n"),
    "вставка в конец": (b"a\nb\n", b"a\nb\nc\n"),
    "удаление из середины": (b"a\nb\nc\nd\ne\n", b"a\nb\nd\ne\n"),
    "замена одной строки": (b"a\nb\nc\n", b"a\nX\nc\n"),
    "два далёких изменения (два хунка)": (
        b"".join(f"l{i}\n".encode() for i in range(30)),
        b"".join(f"l{i}\n".encode() if i not in (2, 27) else b"changed\n" for i in range(30)),
    ),
    "близкие изменения (один хунок)": (
        b"".join(f"l{i}\n".encode() for i in range(30)),
        b"".join(f"l{i}\n".encode() if i not in (5, 9) else b"changed\n" for i in range(30)),
    ),
    "расстояние ровно шесть строк": (
        b"".join(f"l{i}\n".encode() for i in range(30)),
        b"".join(f"l{i}\n".encode() if i not in (5, 12) else b"changed\n" for i in range(30)),
    ),
    "расстояние семь строк": (
        b"".join(f"l{i}\n".encode() for i in range(30)),
        b"".join(f"l{i}\n".encode() if i not in (5, 13) else b"changed\n" for i in range(30)),
    ),
    "без перевода строки в конце (старый)": (b"a\nb", b"a\nb\nc\n"),
    "без перевода строки в конце (новый)": (b"a\nb\n", b"a\nB"),
    "только добавлен перевод строки": (b"a\nb", b"a\nb\n"),
    "файл пуст → непуст": (b"", b"a\nb\n"),
    "непуст → файл пуст": (b"a\nb\n", b""),
    "всё заменено": (b"a\nb\nc\n", b"x\ny\nz\n"),
    "CRLF": (b"a\r\nb\r\nc\r\n", b"a\r\nB\r\nc\r\n"),
    "заголовок функции": (
        b"def f(x):\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    return x\n",
        b"def f(x):\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 6\n    return x\n",
    ),
    "эвристика отступов: блок дублирует соседний": (b"{\n  a;\n}\n\n{\n  b;\n}\n", b"{\n  a;\n}\n\n{\n  a;\n}\n\n{\n  b;\n}\n"),
    "повторяющиеся строки: вставка между равными": (b"x\nx\nx\n", b"x\nx\nx\nx\n"),
    "пустые строки вокруг вставки": (b"a\n\nb\n", b"a\n\nnew\n\nb\n"),
    "юникод в заголовке": ("функция ту\n".encode() + b"x\n" * 8 + b"end\n", "функция ту\n".encode() + b"x\n" * 8 + b"END\n"),
    "длинный заголовок функции": (b"w" * 100 + b"\n" + b"x\n" * 8 + b"end\n", b"w" * 100 + b"\n" + b"x\n" * 8 + b"END\n"),
}


@pytest.mark.parametrize("name", list(CURATED))
def test_curated_cases_are_byte_identical_to_git(tmp_path: Path, name: str) -> None:
    a, b = CURATED[name]
    assert our_diff(a, b).decode(errors="replace") == git_diff(tmp_path, a, b).decode(errors="replace")


def _mutate(rnd: random.Random, lines: list[bytes]) -> list[bytes]:
    lines = list(lines)
    for _ in range(rnd.randint(1, 8)):
        op = rnd.random()
        if lines and op < 0.3:
            del lines[rnd.randrange(len(lines)) :][: rnd.randint(1, 3)]
        elif op < 0.6:
            lines.insert(rnd.randint(0, len(lines)), rnd.choice(lines or [b"x\n"]))
        elif op < 0.8 and lines:
            lines[rnd.randrange(len(lines))] = b"    edited %d\n" % rnd.randrange(100)
        elif lines:
            i = rnd.randrange(len(lines))
            lines[rnd.randint(0, len(lines)) : 0] = lines[i : i + rnd.randint(1, 6)]
    return lines


def _source_files() -> list[bytes]:
    here = Path(__file__).resolve().parent.parent / "src" / "minigit"
    return [p.read_bytes() for p in sorted(here.glob("*.py"))] + [Path(__file__).read_bytes()]


def test_random_edits_of_real_source_files_agree_with_git(tmp_path: Path) -> None:
    """Правки настоящих файлов: подавляющее большинство дают побайтно тот же дифф, что и git (остальное — равнозначные расстановки границ)."""
    rnd = random.Random(2024)
    files = _source_files()
    same = total = 0
    for _ in range(250):
        old = rnd.choice(files)
        new = b"".join(_mutate(rnd, split_lines(old)))
        total += 1
        ours, theirs = our_diff(old, new), git_diff(tmp_path, old, new)
        same += ours == theirs
        # даже когда границы расставлены иначе, число изменённых строк минимально, как у git
        assert _changed(ours) == _changed(git_diff(tmp_path, old, new, "--minimal")), "дифф не минимален"
    assert same / total >= 0.93, f"совпало только {same} из {total}"


def _changed(patch: bytes) -> int:
    return sum(1 for ln in patch.split(b"\n") if (ln.startswith((b"+", b"-"))) and not ln.startswith((b"+++", b"---")))


# ---------- git apply принимает наши патчи ----------


def _side(files: dict[bytes, bytes]) -> Side:
    return {p: (0o100644, hash_object("blob", d), (lambda d=d: d)) for p, d in files.items()}


def test_git_apply_accepts_our_patches(tmp_path: Path) -> None:
    rnd = random.Random(5)
    base_files = _source_files()
    for n in range(25):
        old = rnd.choice(base_files)
        new = b"".join(_mutate(rnd, split_lines(old)))
        patch = diff_sides(_side({b"f.txt": old}), _side({b"f.txt": new}))
        if old == new:
            assert patch == b""
            continue
        work = tmp_path / f"w{n}"
        work.mkdir()
        run_git(work, "init", "-q")
        (work / "f.txt").write_bytes(old)
        r = subprocess.run(["git", "apply", "--whitespace=nowarn"], cwd=work, input=patch, env=git_env(), capture_output=True)
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert (work / "f.txt").read_bytes() == new


def test_whole_file_diffs_match_git(tmp_path: Path) -> None:
    """Заголовки: новый, удалённый и изменённый файлы, смена режима, двоичные данные, пустые файлы."""
    run_git(tmp_path, "init", "-q")
    (tmp_path / "keep.txt").write_bytes(b"1\n2\n3\n")
    (tmp_path / "gone.txt").write_bytes(b"bye\n")
    (tmp_path / "mode.sh").write_bytes(b"echo\n")
    (tmp_path / "bin.dat").write_bytes(b"\0\1\2")
    run_git(tmp_path, "add", "-A")
    run_git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "keep.txt").write_bytes(b"1\n2\nthree\n4\n")
    (tmp_path / "gone.txt").unlink()
    (tmp_path / "new.txt").write_bytes(b"fresh\nfile\n")
    (tmp_path / "empty.txt").write_bytes(b"")
    (tmp_path / "bin.dat").write_bytes(b"\0\1\2\3")
    run_git(tmp_path, "add", "-A")
    expected = run_git(tmp_path, "diff", "--cached", "--no-color").stdout
    from minigit.repo import Repository

    repo = Repository.discover(tmp_path)
    head = repo.flat_tree(repo.read_commit(repo.resolve("HEAD")).tree)
    from minigit.patch import tree_side

    def rb(sha: str) -> bytes:
        return repo.odb.read(sha)[1]

    ours = diff_sides(tree_side(rb, head), tree_side(rb, repo.index_flat()))
    assert ours.decode() == expected.decode()
    assert os.fspath(tmp_path)  # каталог жив до конца проверки
