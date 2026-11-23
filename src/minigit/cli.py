"""Командная строка minigit: те же команды и (где возможно) тот же вывод, что у git."""

from __future__ import annotations

import argparse
import itertools
import os
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from . import __version__
from .fsck import fsck
from .index import Index
from .merge import merge3, merge_commits
from .objects import MODE_DIR, GitError, hash_object, parse_commit, parse_tag, parse_tree
from .patch import Side, diff_sides, tree_side
from .repo import Flat, Repository, Status, format_date
from .transport import clone, fetch, repack


def out(data: bytes | str) -> None:
    sys.stdout.buffer.write(data if isinstance(data, bytes) else data.encode())
    sys.stdout.buffer.flush()


def _paths(repo: Repository, items: list[str]) -> list[bytes]:
    res: list[bytes] = []
    for raw in items:
        target = (Path.cwd() / raw).resolve()
        try:
            rel = target.relative_to(repo.wt.resolve()).as_posix()
        except ValueError as e:
            raise GitError(f"путь вне репозитория: {raw}") from e
        res.append(os.fsencode("" if rel == "." else rel))
    return [p for p in res if p]


# ---------- команды ----------


def cmd_init(a: argparse.Namespace) -> int:
    path = Path(a.directory)
    repo = Repository.init(path, a.initial_branch, bare=a.bare)
    out(f"Создан пустой репозиторий в {repo.git_dir.resolve()}\n")
    return 0


def cmd_add(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    repo.add_paths(a.paths, force=a.force)
    return 0


def cmd_rm(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    for p in repo.remove_paths(a.paths, cached=a.cached):
        out(f"rm '{os.fsdecode(p)}'\n")
    return 0


def cmd_commit(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    if a.all:
        st = repo.status()
        if st.unstaged:
            repo.add_paths([os.fsdecode(p) for _c, p in st.unstaged])
    msg = "\n\n".join(a.message or [])
    if not msg:
        raise GitError("нужно сообщение: -m «текст»")
    sha = repo.commit(msg, allow_empty=a.allow_empty)
    branch = repo.refs.head_branch() or "HEAD отсоединён"
    subject = msg.strip().split("\n", 1)[0]
    out(f"[{branch} {sha[:7]}] {subject}\n")
    return 0


def _status_code(st: Status) -> list[tuple[str, bytes]]:
    rows: dict[bytes, list[str]] = {}
    for c, p in st.staged:
        rows.setdefault(p, [" ", " "])[0] = c
    for c, p in st.unstaged:
        rows.setdefault(p, [" ", " "])[1] = c
    for p in st.conflicts:
        rows[p] = ["U", "U"]
    lines = [("".join(v), p) for p, v in sorted(rows.items())]
    return lines + [("??", p) for p in st.untracked]


def cmd_status(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    st = repo.status()
    if a.short or a.porcelain:
        for code, p in _status_code(st):
            out(f"{code} {os.fsdecode(p)}\n".encode("utf-8", "surrogateescape"))
        return 0
    branch = repo.refs.head_branch()
    out(f"На ветке {branch}\n" if branch else f"HEAD отсоединён на {(repo.head_commit() or '')[:7]}\n")
    names = {"A": "новый файл", "M": "изменено", "D": "удалено", "T": "тип изменён"}
    if st.conflicts:
        out("Неразрешённые конфликты:\n" + "".join(f"\tконфликт: {os.fsdecode(p)}\n" for p in st.conflicts) + "\n")
    if st.staged:
        out("Изменения, готовые к коммиту:\n" + "".join(f"\t{names[c]}: {os.fsdecode(p)}\n" for c, p in st.staged) + "\n")
    if st.unstaged:
        out("Изменения, не добавленные в индекс:\n" + "".join(f"\t{names[c]}: {os.fsdecode(p)}\n" for c, p in st.unstaged) + "\n")
    if st.untracked:
        out("Неотслеживаемые файлы:\n" + "".join(f"\t{os.fsdecode(p)}\n" for p in st.untracked) + "\n")
    if not (st.staged or st.unstaged or st.untracked or st.conflicts):
        out("нечего коммитить, рабочий каталог чист\n")
    return 0


def _log_line(line: str) -> str:
    """Строка сообщения в логе, как у git: табуляции разворачиваются в пробелы (шаг 8 от начала строки сообщения), пробелы в конце убираются."""
    out: list[str] = []
    col = 0
    for ch in line:
        if ch == "\t":
            n = 8 - col % 8
            out.append(" " * n)
            col += n
        else:
            out.append(ch)
            col += 1
    return "".join(out).rstrip(" \t\r")


def _format_log(repo: Repository, sha: str, c: object, fmt: str | None) -> bytes:
    from .objects import Commit

    assert isinstance(c, Commit)
    if fmt == "oneline":
        return f"{sha[:7]} {c.message.decode(errors='replace').split(chr(10), 1)[0]}\n".encode()
    if fmt is not None:
        text = fmt.replace("%H", sha).replace("%h", sha[:7]).replace("%an", c.author.name).replace("%ae", c.author.email)
        text = text.replace("%s", c.message.decode(errors="replace").split("\n", 1)[0]).replace("%P", " ".join(c.parents)).replace("%T", c.tree)
        text = text.replace("%at", str(c.author.time)).replace("%ct", str(c.committer.time)).replace("%cn", c.committer.name).replace("%n", "\n")
        return (text + "\n").encode()
    lines = [f"commit {sha}"]
    if len(c.parents) > 1:
        lines.append("Merge: " + " ".join(p[:7] for p in c.parents))
    lines += [f"Author: {c.author.name} <{c.author.email}>", f"Date:   {format_date(c.author)}", ""]
    body = c.message.decode(errors="replace").rstrip("\n").split("\n")
    lines += ["    " + _log_line(ln) for ln in body]  # git отступает и пустые строки сообщения
    return ("\n".join(lines) + "\n").encode()


def render_log(entries: Iterable[tuple[str, object]], repo: Repository, fmt: str | None) -> Iterator[bytes]:
    """Записи лога: в полном формате между коммитами пустая строка (после последнего её нет), как у git."""
    first = True
    for sha, c in entries:
        if not first and fmt is None:
            yield b"\n"
        first = False
        yield _format_log(repo, sha, c, fmt)


def cmd_log(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    starts = [repo.resolve(r) for r in (a.revs or ["HEAD"]) if not r.startswith("^")]
    excl = [repo.resolve(r[1:]) for r in (a.revs or []) if r.startswith("^")]
    entries: Iterable[tuple[str, object]] = repo.walk(starts, excl)
    if a.max_count:
        entries = itertools.islice(entries, a.max_count)
    for chunk in render_log(entries, repo, "oneline" if a.oneline else a.format):
        out(chunk)
    return 0


def _commit_side(repo: Repository, rev: str) -> Flat:
    return repo.flat_tree(repo.peel(repo.resolve(rev), "tree"))


def _const(data: bytes) -> Callable[[], bytes]:
    return lambda: data


def _worktree_side(repo: Repository, idx: Index) -> Side:
    """Отслеживаемые файлы рабочего каталога: режим, хэш содержимого и чтение (без записи в базу объектов)."""
    side: Side = {}
    for p in repo.index_flat(idx):
        fp = repo.fs(p)
        if not os.path.lexists(fp):
            continue
        data = repo._read_worktree_file(fp)
        side[p] = (repo._file_mode(fp, os.lstat(fp), idx.get(p)), hash_object("blob", data), _const(data))
    return side


def cmd_diff(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    paths = _paths(repo, a.paths)
    revs = [r for r in a.revs if r != "--"]

    def rb(sha: str) -> bytes:
        return repo.odb.read(sha)[1]

    if len(revs) == 2:
        out(diff_sides(tree_side(rb, _commit_side(repo, revs[0])), tree_side(rb, _commit_side(repo, revs[1])), paths, a.unified))
        return 0
    idx = repo.load_index()
    head = repo.head_commit()
    if a.cached:
        base: Flat = _commit_side(repo, revs[0]) if revs else (repo.flat_tree(repo.read_commit(head).tree) if head else {})
        out(diff_sides(tree_side(rb, base), tree_side(rb, repo.index_flat(idx)), paths, a.unified))
        return 0
    old = tree_side(rb, _commit_side(repo, revs[0])) if revs else tree_side(rb, repo.index_flat(idx))
    out(diff_sides(old, _worktree_side(repo, idx), paths, a.unified))
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    sha = repo.resolve(a.rev, peel=False)
    kind, data = repo.odb.read(sha)
    if kind == "blob":
        out(data)
        return 0
    if kind == "tree":
        out(f"tree {a.rev}\n\n" + "".join(f"{e.name.decode(errors='replace')}{'/' if e.mode == MODE_DIR else ''}\n" for e in parse_tree(data)))
        return 0
    if kind == "tag":
        t = parse_tag(data)
        out(f"tag {t.name}\nTagger: {t.tagger.name if t.tagger else '?'}\n\n{t.message.decode(errors='replace')}\n")
        sha = t.object
        kind, data = repo.odb.read(sha)
    c = parse_commit(data)
    out(_format_log(repo, sha, c, None).rstrip(b"\n") + b"\n\n")

    def rb(sha: str) -> bytes:
        return repo.odb.read(sha)[1]

    parent_flat = repo.flat_tree(repo.read_commit(c.parents[0]).tree) if c.parents else {}
    out(diff_sides(tree_side(rb, parent_flat), tree_side(rb, repo.flat_tree(c.tree)), None, 3))
    return 0


def cmd_branch(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    if a.delete or a.force_delete:
        for n in a.names:
            sha = repo.delete_branch(n, force=a.force_delete)
            out(f"Удалена ветка {n} (была {sha[:7]})\n")
        return 0
    if a.names:
        repo.create_branch(a.names[0], a.names[1] if len(a.names) > 1 else "HEAD")
        return 0
    cur = repo.refs.head_branch()
    for n in repo.refs.branches():
        out(("* " if n == cur else "  ") + n + "\n")
    return 0


def cmd_tag(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    if a.delete:
        for n in a.names:
            repo.refs.delete(f"refs/tags/{n}")
        return 0
    if a.names:
        repo.create_tag(a.names[0], a.names[1] if len(a.names) > 1 else "HEAD", message=a.message if (a.annotate or a.message) else None)
        return 0
    for n in repo.refs.tags():
        out(n + "\n")
    return 0


def cmd_checkout(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    if a.paths:
        repo.restore_paths(a.paths, source=a.target if a.target and a.target != "--" else None)
        return 0
    if a.new_branch:
        repo.checkout(a.target or "HEAD", create=a.new_branch, force=a.force)
        out(f"Переключено на новую ветку «{a.new_branch}»\n")
        return 0
    if not a.target:
        raise GitError("укажите ветку или коммит")
    repo.checkout(a.target, force=a.force)
    out(f"Переключено на «{a.target}»\n")
    return 0


def cmd_reset(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    mode = "soft" if a.soft else "hard" if a.hard else "mixed"
    sha = repo.reset(a.rev, mode)
    out(f"HEAD теперь на {sha[:7]}\n")
    return 0


def cmd_merge(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    r = merge_commits(repo, a.rev, message=a.message, no_ff=a.no_ff, no_commit=a.no_commit)
    for m in r.messages:
        out(m + "\n")
    if r.status == "conflict":
        out("Автоматическое слияние не удалось: исправьте конфликты и сделайте коммит.\n")
        return 1
    if r.status == "merged" and r.commit:
        out(f"Слияние выполнено: {r.commit[:7]}\n")
    return 0


def cmd_merge_base(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    bases = repo.merge_bases(repo.peel(repo.resolve(a.a)), repo.peel(repo.resolve(a.b)))
    if not bases:
        return 1
    for b in bases if a.all else bases[:1]:
        out(b + "\n")
    return 0


def cmd_merge_file(a: argparse.Namespace) -> int:
    merged, n = merge3(Path(a.base).read_bytes(), Path(a.ours).read_bytes(), Path(a.theirs).read_bytes(), a.label_ours, a.label_theirs)
    out(merged)
    return min(n, 127)


def cmd_cat_file(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    sha = repo.resolve(a.object, peel=False)
    kind, data = repo.odb.read(sha)
    if a.type:
        out(kind + "\n")
    elif a.size:
        out(f"{len(data)}\n")
    elif a.pretty:
        if kind == "tree":
            for e in parse_tree(data):
                out(
                    f"{e.mode:06o} {'tree' if e.mode == MODE_DIR else 'commit' if e.mode == 0o160000 else 'blob'} {e.sha}\t".encode() + e.name + b"\n"
                )
        else:
            out(data)
    else:
        out(data)
    return 0


def cmd_hash_object(a: argparse.Namespace) -> int:
    data = sys.stdin.buffer.read() if a.stdin else Path(a.file).read_bytes()
    if a.write:
        out(Repository.discover().odb.write(a.type, data) + "\n")
    else:
        out(hash_object(a.type, data) + "\n")
    return 0


def cmd_ls_files(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    for e in repo.load_index().sorted_entries():
        out((f"{e.mode:o} {e.sha} {e.stage}\t" if a.stage else "").encode() + e.path + b"\n")
    return 0


def _ls_tree(repo: Repository, sha: str, recursive: bool, prefix: bytes = b"") -> None:
    for e in repo.read_tree(sha):
        if e.mode == MODE_DIR and recursive:
            _ls_tree(repo, e.sha, True, prefix + e.name + b"/")
            continue
        kind = "tree" if e.mode == MODE_DIR else "commit" if e.mode == 0o160000 else "blob"
        out(f"{e.mode:06o} {kind} {e.sha}\t".encode() + prefix + e.name + b"\n")


def cmd_ls_tree(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    _ls_tree(repo, repo.peel(repo.resolve(a.rev), "tree"), a.recursive)
    return 0


def cmd_rev_parse(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    for r in a.revs:
        if r == "--git-dir":
            out(str(repo.git_dir.resolve()) + "\n")
        elif r == "--show-toplevel":
            out(str(repo.wt.resolve()) + "\n")
        else:
            out(repo.resolve(r) + "\n")
    return 0


def cmd_write_tree(_a: argparse.Namespace) -> int:
    out(Repository.discover().write_tree() + "\n")
    return 0


def cmd_fsck(_a: argparse.Namespace) -> int:
    problems = fsck(Repository.discover())
    for p in problems:
        out(str(p) + "\n")
    errors = [p for p in problems if p.severity == "error"]
    out(f"проверено: ошибок {len(errors)}, повисших объектов {len(problems) - len(errors)}\n")
    return 1 if errors else 0


def cmd_repack(_a: argparse.Namespace) -> int:
    path = repack(Repository.discover())
    out(f"пакет записан: {path}\n" if path else "нечего упаковывать\n")
    return 0


def cmd_clone(a: argparse.Namespace) -> int:
    dest = Path(a.directory) if a.directory else Path(Path(a.url.rstrip("/\\")).name.removesuffix(".git") or "repo")
    clone(a.url, dest, bare=a.bare)
    out(f"Клонировано в {dest}\n")
    return 0


def cmd_fetch(a: argparse.Namespace) -> int:
    updated = fetch(Repository.discover(), a.remote)
    for n, s in updated.items():
        out(f"{a.remote}/{n} → {s[:7]}\n")
    return 0


def cmd_config(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    if a.value is None:
        v = repo.config.get(a.key)
        if v is None:
            return 1
        out(v + "\n")
    else:
        repo.config.set(a.key, a.value)
    return 0


def cmd_update_ref(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    repo.refs.update(a.ref, repo.resolve(a.value))
    return 0


def cmd_symbolic_ref(a: argparse.Namespace) -> int:
    repo = Repository.discover()
    if a.target:
        repo.refs.set_symbolic(a.name, a.target)
    else:
        t = repo.refs.read_symbolic(a.name)
        if t is None:
            raise GitError(f"{a.name} не символическая ссылка")
        out(t + "\n")
    return 0


# ---------- разбор аргументов ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="minigit", description="Свой git на чистом Python.")
    p.add_argument("--version", action="version", version=f"minigit {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: Callable[[argparse.Namespace], int], help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    s = add("init", cmd_init, "создать репозиторий")
    s.add_argument("directory", nargs="?", default=".")
    s.add_argument("-b", "--initial-branch", default="main")
    s.add_argument("--bare", action="store_true")
    s = add("add", cmd_add, "добавить файлы в индекс")
    s.add_argument("paths", nargs="+")
    s.add_argument("-f", "--force", action="store_true")
    s = add("rm", cmd_rm, "удалить файлы")
    s.add_argument("paths", nargs="+")
    s.add_argument("--cached", action="store_true")
    s = add("commit", cmd_commit, "записать коммит")
    s.add_argument("-m", "--message", action="append")
    s.add_argument("-a", "--all", action="store_true")
    s.add_argument("--allow-empty", action="store_true")
    s = add("status", cmd_status, "состояние рабочего каталога")
    s.add_argument("-s", "--short", action="store_true")
    s.add_argument("--porcelain", action="store_true")
    s = add("log", cmd_log, "история коммитов")
    s.add_argument("revs", nargs="*")
    s.add_argument("--oneline", action="store_true")
    s.add_argument("--format")
    s.add_argument("-n", "--max-count", type=int, default=0)
    s = add("diff", cmd_diff, "показать изменения")
    s.add_argument("revs", nargs="*")
    s.add_argument("--cached", "--staged", dest="cached", action="store_true")
    s.add_argument("-U", "--unified", type=int, default=3)
    s.add_argument("--paths", nargs="*", default=[])
    s = add("show", cmd_show, "показать объект")
    s.add_argument("rev", nargs="?", default="HEAD")
    s = add("branch", cmd_branch, "ветки")
    s.add_argument("names", nargs="*")
    s.add_argument("-d", "--delete", action="store_true")
    s.add_argument("-D", dest="force_delete", action="store_true")
    s = add("tag", cmd_tag, "теги")
    s.add_argument("names", nargs="*")
    s.add_argument("-a", "--annotate", action="store_true")
    s.add_argument("-m", "--message")
    s.add_argument("-d", "--delete", action="store_true")
    s = add("checkout", cmd_checkout, "переключить ветку или восстановить файлы")
    s.add_argument("target", nargs="?")
    s.add_argument("paths", nargs="*")
    s.add_argument("-b", dest="new_branch")
    s.add_argument("-f", "--force", action="store_true")
    s = add("reset", cmd_reset, "сбросить HEAD")
    s.add_argument("rev", nargs="?", default="HEAD")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--soft", action="store_true")
    g.add_argument("--mixed", action="store_true")
    g.add_argument("--hard", action="store_true")
    s = add("merge", cmd_merge, "слить ветку")
    s.add_argument("rev")
    s.add_argument("-m", "--message")
    s.add_argument("--no-ff", action="store_true")
    s.add_argument("--no-commit", action="store_true")
    s = add("merge-base", cmd_merge_base, "общий предок")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--all", action="store_true")
    s = add("merge-file", cmd_merge_file, "слияние трёх файлов (результат в stdout)")
    s.add_argument("ours")
    s.add_argument("base")
    s.add_argument("theirs")
    s.add_argument("--label-ours", default="ours")
    s.add_argument("--label-theirs", default="theirs")
    s = add("cat-file", cmd_cat_file, "содержимое объекта")
    g = s.add_mutually_exclusive_group()
    g.add_argument("-t", dest="type", action="store_true")
    g.add_argument("-s", dest="size", action="store_true")
    g.add_argument("-p", dest="pretty", action="store_true")
    s.add_argument("object")
    s = add("hash-object", cmd_hash_object, "хэш объекта")
    s.add_argument("file", nargs="?")
    s.add_argument("-w", dest="write", action="store_true")
    s.add_argument("-t", dest="type", default="blob")
    s.add_argument("--stdin", action="store_true")
    s = add("ls-files", cmd_ls_files, "файлы индекса")
    s.add_argument("-s", "--stage", action="store_true")
    s = add("ls-tree", cmd_ls_tree, "содержимое дерева")
    s.add_argument("rev")
    s.add_argument("-r", "--recursive", action="store_true")
    s = add("rev-parse", cmd_rev_parse, "разрешить ревизию")
    s.add_argument("revs", nargs="+")
    add("write-tree", cmd_write_tree, "записать дерево из индекса")
    add("fsck", cmd_fsck, "проверить целостность")
    add("repack", cmd_repack, "упаковать объекты")
    s = add("clone", cmd_clone, "клонировать локальный репозиторий")
    s.add_argument("url")
    s.add_argument("directory", nargs="?")
    s.add_argument("--bare", action="store_true")
    s = add("fetch", cmd_fetch, "забрать объекты из удалённого")
    s.add_argument("remote", nargs="?", default="origin")
    s = add("config", cmd_config, "прочитать или записать настройку")
    s.add_argument("key")
    s.add_argument("value", nargs="?")
    s = add("update-ref", cmd_update_ref, "обновить ссылку")
    s.add_argument("ref")
    s.add_argument("value")
    s = add("symbolic-ref", cmd_symbolic_ref, "символическая ссылка")
    s.add_argument("name")
    s.add_argument("target", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except GitError as e:
        sys.stderr.write(f"ошибка: {e}\n")
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
