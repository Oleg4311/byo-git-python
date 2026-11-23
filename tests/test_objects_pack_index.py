"""Объекты, пакеты и индекс: каждый формат сверяется с тем, что пишет и читает настоящий git."""

from __future__ import annotations

import os
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from conftest import git_env, git_text, run_git, write_tree
from hypothesis import given, settings
from hypothesis import strategies as st

from minigit.index import Index, IndexEntry
from minigit.objects import (
    GitError,
    Identity,
    TreeEntry,
    hash_object,
    parse_commit,
    parse_tag,
    parse_tree,
    serialize_commit,
    serialize_tag,
    serialize_tree,
)
from minigit.odb import ObjectStore
from minigit.pack import PackFile, apply_delta, write_pack
from minigit.repo import Repository

# ---------- объекты ----------


def test_known_hashes() -> None:
    assert hash_object("blob", b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert hash_object("blob", b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert hash_object("tree", b"") == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # пустое дерево
    with pytest.raises(GitError):
        hash_object("bogus", b"")


@settings(max_examples=40, deadline=None)
@given(data=st.binary(max_size=2000))
def test_blob_hash_matches_git(data: bytes) -> None:
    r = subprocess.run(["git", "hash-object", "--stdin"], input=data, capture_output=True, env=git_env())
    assert hash_object("blob", data) == r.stdout.decode().strip()


NAMES = st.text(alphabet=st.sampled_from(list("abcAB0-._ xyz~")), min_size=1, max_size=6).filter(lambda s: s not in (".", "..") and s != ".git")


@settings(max_examples=40, deadline=None)
@given(
    entries=st.lists(st.tuples(NAMES, st.sampled_from([0o100644, 0o100755, 0o040000, 0o120000])), min_size=1, max_size=12, unique_by=lambda t: t[0])
)
def test_tree_serialization_matches_git_mktree(entries: list[tuple[str, int]]) -> None:
    """Порядок записей в дереве (каталоги как «имя/») и формат совпадают с git mktree."""
    sha = "ce013625030ba8dba906f756967f9e9ca394464a"
    ours = serialize_tree([TreeEntry(m, n.encode(), sha if m != 0o040000 else "4b825dc642cb6eb9a060e54bf8d69288fbee4904") for n, m in entries])
    lines = "".join(
        f"{m:06o} {'tree' if m == 0o040000 else 'blob'} {sha if m != 0o040000 else '4b825dc642cb6eb9a060e54bf8d69288fbee4904'}\t{n}\n"
        for n, m in entries
    )
    r = subprocess.run(["git", "mktree", "--missing"], input=lines.encode(), capture_output=True, env=git_env())
    assert r.returncode == 0, r.stderr
    assert hash_object("tree", ours) == r.stdout.decode().strip()


def test_tree_rules() -> None:
    sha = "ce013625030ba8dba906f756967f9e9ca394464a"
    # «a-b» < «a/» < «a0»: каталог a сортируется как «a/»
    order = [
        e.name
        for e in parse_tree(serialize_tree([TreeEntry(0o100644, b"a0", sha), TreeEntry(0o040000, b"a", sha), TreeEntry(0o100644, b"a-b", sha)]))
    ]
    assert order == [b"a-b", b"a", b"a0"]
    for bad in (b"", b"a/b", b".", b"..", b"a\0b"):
        with pytest.raises(GitError):
            serialize_tree([TreeEntry(0o100644, bad, sha)])
    with pytest.raises(GitError):
        serialize_tree([TreeEntry(0o100644, b"x", sha), TreeEntry(0o100644, b"x", sha)])
    for broken in (b"100644 name", b"100644 name\0short", b"1006x4 name\0" + b"\0" * 20, b"100644\0" + b"\0" * 20):
        with pytest.raises(GitError):
            parse_tree(broken)


def test_commit_roundtrip_with_extra_headers_and_merge() -> None:
    raw = (
        b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\n"
        b"parent ce013625030ba8dba906f756967f9e9ca394464a\n"
        b"parent e69de29bb2d1d6434b8b29ae775ad8c2e48c5391\n"
        b"author A <a@x> 1700000000 +0300\n"
        b"committer C <c@x> 1700000100 -0500\n"
        b"encoding ISO-8859-1\n"
        b"gpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n -----END PGP SIGNATURE-----\n"
        b"\n"
        b"message\n\nwith body\n"
    )
    c = parse_commit(raw)
    assert len(c.parents) == 2 and c.author.tz == "+0300" and c.committer.time == 1700000100
    assert [k for k, _ in c.extra] == [b"encoding", b"gpgsig"]
    assert serialize_commit(c) == raw
    with pytest.raises(GitError):
        parse_commit(b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\n\nno author\n")


def test_every_real_commit_roundtrips(ref_repo: Path) -> None:
    repo = Repository.discover(ref_repo)
    n = 0
    for sha in repo.odb.all_shas():
        kind, data = repo.odb.read(sha)
        if kind == "commit":
            assert serialize_commit(parse_commit(data)) == data
            n += 1
        elif kind == "tag":
            assert serialize_tag(parse_tag(data)) == data
        elif kind == "tree":
            assert serialize_tree(parse_tree(data)) == data
    assert n == 5


def test_identity_parsing() -> None:
    i = Identity.parse("Имя Фамилия <a@b.c> 1700000000 -0130".encode())
    assert (i.name, i.email, i.time, i.tz) == ("Имя Фамилия", "a@b.c", 1700000000, "-0130")
    assert Identity.parse(i.format()) == i
    for bad in (b"no email 1 +0000", b"A <a@b> notnumber +0000", b"A <a@b> 1 +00"):
        with pytest.raises(GitError):
            Identity.parse(bad)


# ---------- рыхлые объекты ----------


def test_loose_objects_are_read_by_git_and_hash_is_checked(tmp_path: Path) -> None:
    repo = Repository.init(tmp_path / "r")
    sha = repo.write_blob(b"payload\n")
    assert git_text(repo.work_tree or tmp_path, "cat-file", "-p", sha) == "payload\n"
    assert git_text(repo.work_tree or tmp_path, "cat-file", "-t", sha).strip() == "blob"
    # подмена содержимого под тем же именем обнаруживается
    path = repo.odb._loose_path(sha)
    path.chmod(0o666)
    path.write_bytes(zlib.compress(b"blob 5\0wrong"))
    with pytest.raises(GitError, match="повреждён"):
        repo.odb.read(sha)
    path.write_bytes(b"not zlib")
    with pytest.raises(GitError):
        repo.odb.read(sha)
    path.write_bytes(zlib.compress(b"blob 99\0short"))
    with pytest.raises(GitError, match="длина"):
        repo.odb.read(sha)


def test_prefix_resolution(tmp_path: Path) -> None:
    repo = Repository.init(tmp_path / "r")
    sha = repo.write_blob(b"x")
    assert repo.odb.resolve_prefix(sha[:6]) == sha
    with pytest.raises(GitError, match="нет объекта"):
        repo.odb.resolve_prefix("0000")
    with pytest.raises(GitError):
        repo.odb.resolve_prefix("zz")
    with pytest.raises(GitError):
        repo.odb.resolve_prefix("abc")


# ---------- пакеты ----------


def test_apply_delta_by_hand() -> None:
    base = b"hello world"
    # размер основы 11, результата 8: копировать 5 байт с нуля, вставить «!!!»
    delta = bytes([11, 8, 0x90, 5, 3]) + b"!!!"
    assert apply_delta(base, delta) == b"hello!!!"
    with pytest.raises(GitError, match="основу"):
        apply_delta(b"short", delta)
    with pytest.raises(GitError):
        apply_delta(base, bytes([11, 8, 0x90, 50, 3]) + b"!!!")  # копирование за пределами
    with pytest.raises(GitError):
        apply_delta(base, bytes([11, 9, 0x90, 5, 3]) + b"!!!")  # итог не той длины
    with pytest.raises(GitError):
        apply_delta(base, bytes([11, 8, 0x90, 5, 5]) + b"!")  # вставка оборвана
    with pytest.raises(GitError):
        apply_delta(base, bytes([11, 8, 0]))  # инструкция 0


def _packed_copy(src: Path, dest: Path, *git_args: str) -> Path:
    run_git(dest.parent, "clone", "-q", "--no-hardlinks", str(src), dest.name)
    run_git(dest, *git_args)
    return dest


def _all_objects_readable(repo_dir: Path) -> int:
    repo = Repository.discover(repo_dir)
    n = 0
    for sha in repo.odb.all_shas():
        kind, data = repo.odb.read(sha)  # хэш проверяется внутри
        assert hash_object(kind, data) == sha
        n += 1
    return n


def test_reads_packs_with_offset_deltas(tmp_path: Path, ref_repo: Path) -> None:
    big = ref_repo.parent / "big"
    big.mkdir()
    run_git(big, "init", "-q", "-b", "main")
    text = "".join(f"строка {i}\n" for i in range(400))
    for k in range(8):
        write_tree(big, {"f.txt": text.replace("строка 10\n", f"правка {k}\n"), f"extra{k}.txt": text[: 200 * k]})
        run_git(big, "add", "-A")
        run_git(big, "commit", "-q", "-m", f"c{k}", tick=k)
    run_git(big, "gc", "--aggressive", "--prune=now", "-q")
    verify = git_text(big, "verify-pack", "-v", *[str(p) for p in (big / ".git/objects/pack").glob("*.idx")])
    assert any(len(line.split()) >= 7 for line in verify.splitlines()), "в пакете должны быть дельты"
    assert _all_objects_readable(big) == int(git_text(big, "count-objects", "-v").split("in-pack: ")[1].split()[0])
    assert not list((big / ".git/objects").glob("??/*"))  # рыхлых объектов не осталось: читаем только из пакета


def test_reads_packs_with_reference_deltas(tmp_path: Path) -> None:
    r = tmp_path / "r"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    base = "".join(f"line {i}\n" for i in range(300))
    for k in range(6):
        write_tree(r, {"f.txt": base + f"tail {k}\n"})
        run_git(r, "add", "-A")
        run_git(r, "commit", "-q", "-m", f"c{k}", tick=k)
    run_git(r, "-c", "repack.useDeltaBaseOffset=false", "repack", "-adf", "-q")
    out = git_text(r, "verify-pack", "-v", *[str(p) for p in (r / ".git/objects/pack").glob("*.idx")])
    assert any(len(line.split()) >= 7 for line in out.splitlines())
    assert _all_objects_readable(r) > 10


def test_our_pack_is_accepted_by_git(tmp_path: Path, ref_repo: Path) -> None:
    src = Repository.discover(ref_repo)
    objs = [src.odb.read(s) for s in sorted(src.odb.all_shas())]
    target = tmp_path / "t"
    target.mkdir()
    run_git(target, "init", "-q", "-b", "main")
    pack = write_pack(objs, target / ".git/objects/pack")
    assert "ok" in git_text(target, "verify-pack", str(pack)) or run_git(target, "verify-pack", str(pack)).returncode == 0
    run_git(target, "index-pack", "--stdin", "--fix-thin", input=pack.read_bytes()) if False else None
    for sha, (kind, data) in zip(sorted(src.odb.all_shas()), objs, strict=True):
        assert git_text(target, "cat-file", "-t", sha).strip() == kind
        assert run_git(target, "cat-file", kind, sha).stdout == data
    run_git(target, "fsck", "--no-dangling")  # целостность: связи между объектами на месте
    ours = ObjectStore(target / ".git/objects")
    assert {s for s in ours.all_shas()} == set(src.odb.all_shas())


def test_pack_corruption_is_detected(tmp_path: Path) -> None:
    pack = write_pack([("blob", b"a" * 100), ("blob", b"b" * 100)], tmp_path)
    data = bytearray(pack.read_bytes())
    data[20] ^= 0xFF
    pack.write_bytes(bytes(data))
    with pytest.raises(GitError, match="контрольная сумма"):
        PackFile(pack)
    good = write_pack([("blob", b"x")], tmp_path / "g")
    idx = good.with_suffix(".idx")
    raw = bytearray(idx.read_bytes())
    raw[-30] ^= 0xFF
    idx.write_bytes(bytes(raw))
    with pytest.raises(GitError):
        PackFile(good)


def test_large_object_sizes_encode_correctly(tmp_path: Path) -> None:
    payloads = [b"", b"x" * 15, b"x" * 16, b"y" * 127, b"z" * 128, os.urandom(70000)]
    pack = write_pack([("blob", p) for p in payloads], tmp_path)
    pf = PackFile(pack)
    for p in payloads:
        got = pf.read(hash_object("blob", p))
        assert got == ("blob", p)
    run_git(tmp_path, "verify-pack", str(pack))


# ---------- индекс ----------


def test_parses_index_written_by_git(ref_repo: Path) -> None:
    idx = Index.load(ref_repo / ".git/index")
    expected = git_text(ref_repo, "ls-files", "--stage").strip().splitlines()
    ours = [f"{e.mode:o} {e.sha} {e.stage}\t{e.path.decode()}" for e in idx.sorted_entries()]
    assert ours == expected
    # git пишет в индекс расширение TREE: оно должно спокойно пропускаться
    assert b"TREE" in (ref_repo / ".git/index").read_bytes()


def test_parses_version_3_with_intent_to_add(tmp_path: Path) -> None:
    r = tmp_path / "r"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    write_tree(r, {"a": "1\n", "b": "2\n"})
    run_git(r, "add", "a")
    run_git(r, "add", "-N", "b")
    raw = (r / ".git/index").read_bytes()
    assert struct.unpack(">I", raw[4:8])[0] == 3
    idx = Index.load(r / ".git/index")
    assert {e.path for e in idx.entries.values()} == {b"a", b"b"}
    assert next(e for e in idx.entries.values() if e.path == b"b").flags_extra & 0x2000  # intent-to-add


def test_index_written_by_us_is_read_by_git(tmp_path: Path) -> None:
    repo = Repository.init(tmp_path / "r")
    files = {"z.txt": "z", "a/b/c.txt": "c", "a/b.txt": "b", "a-b": "d", "a0": "e", "ünï/ф.txt": "u", "sp ace": "s"}
    write_tree(repo.wt, files)
    cwd = Path.cwd()
    os.chdir(repo.wt)
    try:
        repo.add_paths(["."])
    finally:
        os.chdir(cwd)
    staged = git_text(repo.wt, "-c", "core.quotepath=off", "ls-files", "--stage").strip().splitlines()
    assert len(staged) == len(files)
    assert [ln.split("\t")[1] for ln in staged] == sorted(files, key=lambda s: s.encode())
    status = run_git(repo.wt, "status", "--porcelain", "-uno").stdout.decode().splitlines()
    assert len(status) == len(files) and all(
        ln[:2] == "A " for ln in status
    )  # все добавлены и ни одной правки в рабочем каталоге: git считает индекс актуальным
    assert run_git(repo.wt, "diff", "--exit-code").returncode == 0


def test_index_round_trip_and_corruption() -> None:
    idx = Index()
    for i, name in enumerate([b"a", b"dir/x", b"dir/y", b"z" * 300]):
        idx.add(IndexEntry(name, f"{i:040x}", 0o100644, size=i, mtime=1700000000 + i, mtime_ns=i, ino=i + 1))
    idx.add(IndexEntry(b"conflict", "1" * 40, 0o100644, stage=1))
    idx.add(IndexEntry(b"conflict", "2" * 40, 0o100644, stage=2))
    idx.add(IndexEntry(b"conflict", "3" * 40, 0o100755, stage=3))
    data = idx.serialize()
    again = Index.parse(data)
    assert again.entries == idx.entries
    assert again.conflicted_paths() == [b"conflict"]
    assert again.serialize() == data
    for bad in (data[:-1], data[:30], b"DIRX" + data[4:], data[:-20] + b"\0" * 20):
        with pytest.raises(GitError):
            Index.parse(bad)
    flipped = bytearray(data)
    flipped[20] ^= 1
    with pytest.raises(GitError, match="контрольная"):
        Index.parse(bytes(flipped))


def test_index_with_very_long_path_is_understood_by_git(tmp_path: Path) -> None:
    r = tmp_path / "r"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    long_name = "/".join(["d" * 200] * 25) + "/file"  # длиннее 4095: поле длины в записи насыщается
    idx = Index()
    idx.add(IndexEntry(long_name.encode(), hash_object("blob", b"x\n"), 0o100644))
    idx.add(IndexEntry(b"short", hash_object("blob", b"x\n"), 0o100644))
    assert Index.parse(idx.serialize()).entries == idx.entries
    idx.save(r / ".git/index")
    listed = git_text(r, "ls-files").splitlines()
    assert listed == ["short", long_name] or listed == sorted([long_name, "short"])


def test_add_removes_vanished_files_and_resolves_conflicts() -> None:
    idx = Index()
    idx.add(IndexEntry(b"f", "1" * 40, 0o100644, stage=2))
    assert idx.get(b"f", 2) is not None
    idx.add(IndexEntry(b"f", "4" * 40, 0o100644))  # добавление в нулевую стадию снимает конфликт
    assert idx.conflicted_paths() == []
    assert idx.remove(b"f") and not idx.remove(b"f")


def test_delta_copy_of_exactly_64_kib_uses_the_zero_size_encoding() -> None:
    """Размер копирования 0 в дельте означает 0x10000 (так git кодирует копирование ровно 64 КиБ)."""
    base = bytes(range(256)) * 300  # 76800 байт
    delta = _varint(len(base)) + _varint(0x10000) + bytes([0x80])  # копирование без смещения и размера: с нуля, 0x10000 байт
    assert apply_delta(base, delta) == base[:0x10000]
    with_tail = _varint(len(base)) + _varint(0x10000 + 3) + bytes([0x80, 3]) + b"end"
    assert apply_delta(base, with_tail) == base[:0x10000] + b"end"


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def test_reads_large_file_deltas_made_by_git(tmp_path: Path) -> None:
    r = tmp_path / "big"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main")
    data = os.urandom(300_000)
    for k in range(3):
        write_tree(r, {"blob.bin": data[:150_000] + bytes([k]) * 10 + data[150_000:]})
        run_git(r, "add", "-A")
        run_git(r, "commit", "-q", "-m", f"c{k}", tick=k)
    run_git(r, "gc", "--aggressive", "--prune=now", "-q")
    out = git_text(r, "verify-pack", "-v", *[str(p) for p in (r / ".git/objects/pack").glob("*.idx")])
    assert any(len(line.split()) >= 7 for line in out.splitlines()), "нужна дельта на большом файле"
    assert _all_objects_readable(r) >= 9
