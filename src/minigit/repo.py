"""Репозиторий: init, add, commit, log, status, checkout, ветки, теги, reset. Работает с тем же форматом на диске, что и настоящий git."""

from __future__ import annotations

import heapq
import os
import re
import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .ignore import IgnoreRules
from .index import Index, IndexEntry
from .objects import (
    MODE_DIR,
    MODE_EXEC,
    MODE_FILE,
    MODE_GITLINK,
    MODE_SYMLINK,
    Commit,
    GitError,
    Identity,
    Tag,
    TreeEntry,
    check_tree_name,
    is_hex_sha,
    parse_commit,
    parse_tag,
    parse_tree,
    serialize_commit,
    serialize_tag,
    serialize_tree,
)
from .odb import ObjectStore
from .refs import ZERO, RefStore, check_ref_name

__all__ = ["Repository", "Status", "cleanup_message", "format_date", "parse_git_date"]

Flat = dict[bytes, tuple[int, str]]  # путь → (режим, хэш)


# ---------- вспомогательное ----------

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def format_date(ident: Identity) -> str:
    """Дата в формате git log по умолчанию: «Mon Jan 2 15:04:05 2006 +0300» (в часовом поясе автора, не локальном)."""
    sign = -1 if ident.tz.startswith("-") else 1
    offset = sign * (int(ident.tz[1:3]) * 3600 + int(ident.tz[3:5]) * 60)
    d = datetime.fromtimestamp(ident.time, timezone(timedelta(seconds=offset)))
    return f"{_DAYS[d.weekday()]} {_MONTHS[d.month - 1]} {d.day} {d:%H:%M:%S} {d.year} {ident.tz}"


def parse_git_date(s: str) -> tuple[int, str]:
    """Секунды и пояс из GIT_AUTHOR_DATE: «1234567890 +0300», «@1234567890 +0300» или ISO 8601."""
    s = s.strip()
    m = re.fullmatch(r"@?(\d+)(?:\s+([+-]\d{4}))?", s)
    if m:
        return int(m.group(1)), m.group(2) or "+0000"
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise GitError(f"не разобрать дату: {s!r}") from e
    if d.tzinfo is None:
        d = d.astimezone()
    off = d.utcoffset() or timedelta(0)
    total = int(off.total_seconds() // 60)
    sign = "-" if total < 0 else "+"
    return int(d.timestamp()), f"{sign}{abs(total) // 60:02d}{abs(total) % 60:02d}"


def cleanup_message(msg: str) -> bytes:
    """Как `git commit -m` (cleanup=whitespace): без пробелов в конце строк, без пустых строк в начале и конце, подряд идущие пустые склеены."""
    lines = [ln.rstrip() for ln in msg.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln == "" and (not out or out[-1] == ""):
            continue
        out.append(ln)
    while out and out[-1] == "":
        out.pop()
    return ("\n".join(out) + "\n").encode() if out else b""


def _local_tz() -> str:
    off = datetime.now().astimezone().utcoffset() or timedelta(0)
    total = int(off.total_seconds() // 60)
    sign = "-" if total < 0 else "+"
    return f"{sign}{abs(total) // 60:02d}{abs(total) % 60:02d}"


@dataclass
class Status:
    staged: list[tuple[str, bytes]] = field(default_factory=list)  # («A»|«M»|«D»|«T», путь)
    unstaged: list[tuple[str, bytes]] = field(default_factory=list)  # («M»|«D»|«T», путь)
    untracked: list[bytes] = field(default_factory=list)
    conflicts: list[bytes] = field(default_factory=list)


class Repository:
    def __init__(self, git_dir: Path, work_tree: Path | None) -> None:
        self.git_dir = git_dir
        self.work_tree = work_tree
        self.odb = ObjectStore(git_dir / "objects")
        self.refs = RefStore(git_dir)
        self.config = Config(git_dir / "config")

    # ---------- создание и поиск ----------

    @classmethod
    def init(cls, path: Path, initial_branch: str = "main", bare: bool = False) -> Repository:
        path.mkdir(parents=True, exist_ok=True)
        git_dir = path if bare else path / ".git"
        if (git_dir / "HEAD").exists():
            raise GitError(f"репозиторий уже существует: {git_dir}")
        check_ref_name("refs/heads/" + initial_branch)
        for d in ("objects/pack", "objects/info", "refs/heads", "refs/tags", "info", "hooks"):
            (git_dir / d).mkdir(parents=True, exist_ok=True)
        (git_dir / "HEAD").write_text(f"ref: refs/heads/{initial_branch}\n", encoding="utf-8", newline="\n")
        (git_dir / "description").write_text(
            "Unnamed repository; edit this file 'description' to name the repository.\n", encoding="utf-8", newline="\n"
        )
        win = os.name == "nt"
        lines = ["[core]", "\trepositoryformatversion = 0", f"\tfilemode = {'false' if win else 'true'}", f"\tbare = {'true' if bare else 'false'}"]
        if not bare:
            lines.append("\tlogallrefupdates = true")
        if win:
            lines += ["\tsymlinks = false", "\tignorecase = true"]
        (git_dir / "config").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return cls(git_dir, None if bare else path)

    @classmethod
    def discover(cls, start: Path | None = None) -> Repository:
        here = (start or Path.cwd()).resolve()
        for d in [here, *here.parents]:
            dot = d / ".git"
            if dot.is_dir():
                return cls(dot, d)
            if dot.is_file():
                text = dot.read_text(encoding="utf-8").strip()
                if text.startswith("gitdir:"):
                    gd = Path(text[7:].strip())
                    return cls(gd if gd.is_absolute() else (d / gd).resolve(), d)
            if (d / "HEAD").is_file() and (d / "objects").is_dir() and (d / "refs").is_dir():
                return cls(d, None)  # голый репозиторий
        raise GitError("это не репозиторий git (и ни в одном из родительских каталогов)")

    @property
    def wt(self) -> Path:
        if self.work_tree is None:
            raise GitError("операция требует рабочего каталога, а репозиторий голый")
        return self.work_tree

    def fs(self, path: bytes) -> Path:
        return self.wt / os.fsdecode(path)

    # ---------- объекты ----------

    def read_commit(self, sha: str) -> Commit:
        kind, data = self.odb.read(sha)
        if kind != "commit":
            raise GitError(f"{sha} — это {kind}, а не коммит")
        return parse_commit(data)

    def read_tree(self, sha: str) -> list[TreeEntry]:
        kind, data = self.odb.read(sha)
        if kind != "tree":
            raise GitError(f"{sha} — это {kind}, а не дерево")
        return parse_tree(data)

    def write_blob(self, data: bytes) -> str:
        return self.odb.write("blob", data)

    def flat_tree(self, tree_sha: str, prefix: bytes = b"") -> Flat:
        """Все файлы дерева (рекурсивно): путь → (режим, хэш). Подмодули (gitlink) остаются листьями."""
        out: Flat = {}
        for e in self.read_tree(tree_sha):
            check_tree_name(e.name)
            path = prefix + e.name
            if e.mode == MODE_DIR:
                out.update(self.flat_tree(e.sha, path + b"/"))
            else:
                out[path] = (e.mode, e.sha)
        return out

    def build_tree(self, flat: Flat) -> str:
        """Записывает деревья для плоского списка файлов и возвращает хэш корня."""
        root: dict[bytes, object] = {}
        for path, val in flat.items():
            parts = path.split(b"/")
            node = root
            for p in parts[:-1]:
                nxt = node.setdefault(p, {})
                if not isinstance(nxt, dict):
                    raise GitError(f"путь {path.decode(errors='replace')} конфликтует с файлом {p.decode(errors='replace')}")
                node = nxt
            if isinstance(node.get(parts[-1]), dict):
                raise GitError(f"файл {path.decode(errors='replace')} конфликтует с каталогом")
            node[parts[-1]] = val
        return self._write_node(root)

    def _write_node(self, node: dict[bytes, object]) -> str:
        entries: list[TreeEntry] = []
        for name, val in node.items():
            if isinstance(val, dict):
                entries.append(TreeEntry(MODE_DIR, name, self._write_node(val)))
            else:
                assert isinstance(val, tuple)
                entries.append(TreeEntry(val[0], name, val[1]))
        return self.odb.write("tree", serialize_tree(entries))

    # ---------- индекс ----------

    def load_index(self) -> Index:
        return Index.load(self.git_dir / "index")

    def save_index(self, idx: Index) -> None:
        idx.save(self.git_dir / "index")

    def index_flat(self, idx: Index | None = None) -> Flat:
        idx = idx or self.load_index()
        return {p: (e.mode, e.sha) for (p, s), e in idx.entries.items() if s == 0}

    def write_tree(self, idx: Index | None = None) -> str:
        idx = idx or self.load_index()
        if idx.conflicted_paths():
            raise GitError("в индексе есть неразрешённые конфликты: " + ", ".join(p.decode(errors="replace") for p in idx.conflicted_paths()))
        return self.build_tree(self.index_flat(idx))

    # ---------- чтение рабочего каталога ----------

    def _stat_entry(self, path: bytes, sha: str, mode: int, st: os.stat_result) -> IndexEntry:
        win = os.name == "nt"
        return IndexEntry(
            path=path,
            sha=sha,
            mode=mode,
            size=st.st_size & 0xFFFFFFFF,
            mtime=int(st.st_mtime),
            mtime_ns=st.st_mtime_ns % 1_000_000_000,
            ctime=int(st.st_ctime),
            ctime_ns=st.st_ctime_ns % 1_000_000_000,
            dev=0 if win else st.st_dev & 0xFFFFFFFF,
            ino=0 if win else st.st_ino & 0xFFFFFFFF,
            uid=0 if win else st.st_uid,
            gid=0 if win else st.st_gid,
        )

    def _file_mode(self, path: Path, st: os.stat_result, existing: IndexEntry | None) -> int:
        if path.is_symlink() and os.name != "nt":
            return MODE_SYMLINK
        if os.name == "nt":
            return existing.mode if existing and existing.mode in (MODE_FILE, MODE_EXEC) else MODE_FILE  # на Windows исполняемость не определить
        return MODE_EXEC if st.st_mode & 0o111 else MODE_FILE

    def _read_worktree_file(self, path: Path) -> bytes:
        if path.is_symlink() and os.name != "nt":
            return os.fsencode(os.readlink(path))
        return path.read_bytes()

    def worktree_files(self, ignore: IgnoreRules | None = None, tracked_dirs: set[bytes] | None = None) -> Iterator[bytes]:
        """Пути всех файлов рабочего каталога (кроме .git и игнорируемых), по возрастанию."""

        def walk(rel: str) -> Iterator[bytes]:
            base = self.wt / rel if rel else self.wt
            try:
                entries = sorted(os.scandir(base), key=lambda e: os.fsencode(e.name))
            except FileNotFoundError:
                return
            for e in entries:
                if e.name == ".git":
                    continue
                relp = f"{rel}/{e.name}" if rel else e.name
                is_dir = e.is_dir(follow_symlinks=False)
                if ignore and ignore.is_ignored(relp, is_dir) and (tracked_dirs is None or os.fsencode(relp) not in tracked_dirs):
                    continue
                if is_dir:
                    yield from walk(relp)
                else:
                    yield os.fsencode(relp)

        yield from walk("")

    # ---------- add / rm ----------

    def add_paths(self, paths: list[str], force: bool = False) -> list[bytes]:
        """git add: индексирует файлы и каталоги; пропавшие отслеживаемые файлы убираются из индекса. Возвращает затронутые пути."""
        idx = self.load_index()
        ignore = IgnoreRules(self.wt, self.git_dir)
        touched: list[bytes] = []
        tracked = {p for (p, _s) in idx.entries}
        for raw in paths:
            target = (Path.cwd() / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
            try:
                rel = target.relative_to(self.wt.resolve()).as_posix()
            except ValueError as e:
                raise GitError(f"путь вне репозитория: {raw}") from e
            rel_b = os.fsencode("" if rel == "." else rel)
            if target.is_dir() or (rel == "."):
                files = [f for f in self.worktree_files(None if force else ignore, tracked) if rel == "." or f == rel_b or f.startswith(rel_b + b"/")]
                gone = [p for p in tracked if (rel == "." or p == rel_b or p.startswith(rel_b + b"/")) and not os.path.lexists(self.fs(p))]
                for p in gone:
                    idx.remove(p)
                    touched.append(p)
                for f in files:
                    self._add_file(idx, f)
                    touched.append(f)
            elif os.path.lexists(target):
                if not force and rel_b not in tracked and ignore.is_ignored(rel, False):
                    raise GitError(f"путь игнорируется правилами .gitignore: {raw} (используйте --force)")
                self._add_file(idx, rel_b)
                touched.append(rel_b)
            elif rel_b in tracked:
                idx.remove(rel_b)
                touched.append(rel_b)
            else:
                raise GitError(f"путь не найден: {raw}")
        self.save_index(idx)
        return touched

    def _add_file(self, idx: Index, path: bytes) -> None:
        p = self.fs(path)
        st = os.lstat(p)
        existing = idx.get(path)
        mode = self._file_mode(p, st, existing)
        sha = self.write_blob(self._read_worktree_file(p))
        idx.add(self._stat_entry(path, sha, mode, st))

    def remove_paths(self, paths: list[str], cached: bool = False) -> list[bytes]:
        idx = self.load_index()
        removed: list[bytes] = []
        for raw in paths:
            rel_b = os.fsencode(Path(raw).as_posix())
            matches = [p for (p, _s) in idx.entries if p == rel_b or p.startswith(rel_b.rstrip(b"/") + b"/")]
            if not matches:
                raise GitError(f"нет такого отслеживаемого пути: {raw}")
            for p in sorted(set(matches)):
                idx.remove(p)
                removed.append(p)
                if not cached and self.fs(p).exists():
                    self.fs(p).unlink()
                    self._prune_dirs(self.fs(p).parent)
        self.save_index(idx)
        return removed

    def _prune_dirs(self, d: Path) -> None:
        while d != self.wt and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent

    # ---------- подписи и коммиты ----------

    def identity(self, role: str) -> Identity:
        """role: «AUTHOR» или «COMMITTER». Порядок как у git: GIT_<ROLE>_*, затем user.* из конфигурации."""
        name = os.environ.get(f"GIT_{role}_NAME") or self.config.get("user.name")
        email = os.environ.get(f"GIT_{role}_EMAIL") or self.config.get("user.email") or os.environ.get("EMAIL")
        if not name or not email:
            raise GitError("неизвестно, кто вы: задайте user.name и user.email в .git/config или переменные GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL")
        date = os.environ.get(f"GIT_{role}_DATE")
        ts, tz = parse_git_date(date) if date else (int(time.time()), _local_tz())
        return Identity(name, email, ts, tz)

    def head_commit(self) -> str | None:
        return self.refs.resolve("HEAD")

    def commit(
        self,
        message: str,
        allow_empty: bool = False,
        extra_parents: list[str] | None = None,
        idx: Index | None = None,
        reflog_message: str | None = None,
    ) -> str:
        idx = idx or self.load_index()
        tree = self.write_tree(idx)
        head = self.head_commit()
        parents = ([head] if head else []) + (extra_parents or [])
        merge_head = self.git_dir / "MERGE_HEAD"
        if merge_head.exists() and extra_parents is None:
            parents += [ln.strip() for ln in merge_head.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if head and not allow_empty and len(parents) == 1 and self.read_commit(head).tree == tree:
            raise GitError("нечего коммитить: индекс совпадает с HEAD")
        if not head and not idx.entries and not allow_empty:
            raise GitError("нечего коммитить: индекс пуст")
        body = cleanup_message(message)
        if not body and not allow_empty:
            raise GitError("пустое сообщение коммита")
        author = self.identity("AUTHOR")
        committer = self.identity("COMMITTER")
        sha = self.odb.write("commit", serialize_commit(Commit(tree, parents, author, committer, body)))
        subject = body.split(b"\n", 1)[0].decode(errors="replace")
        kind = "commit (initial)" if not head else ("commit (merge)" if len(parents) > 1 else "commit")
        self._advance_head(sha, head, reflog_message or f"{kind}: {subject}", committer)
        for f in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
            (self.git_dir / f).unlink(missing_ok=True)
        return sha

    def _advance_head(self, new: str, old: str | None, message: str, who: Identity) -> None:
        branch = self.refs.read_symbolic("HEAD")
        self.refs.update("HEAD", new, expected_old=old)
        self._reflog("HEAD", old, new, message, who)
        if branch:
            self._reflog(branch, old, new, message, who)

    def _reflog(self, ref: str, old: str | None, new: str, message: str, who: Identity) -> None:
        if self.config.get("core.logallrefupdates", "true") == "false" or self.work_tree is None:
            return
        log = self.git_dir / "logs" / ref
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as f:
            f.write(f"{old or ZERO} {new} ".encode() + who.format() + b"\t" + message.replace("\n", " ").encode() + b"\n")

    # ---------- разрешение имён ----------

    def peel(self, sha: str, want: str | None = None) -> str:
        """Снимает аннотированные теги; want = 'commit' или 'tree' ещё и сворачивает коммит в дерево."""
        while True:
            kind, data = self.odb.read(sha)
            if kind == "tag":
                sha = parse_tag(data).object
                continue
            if want == "tree" and kind == "commit":
                return parse_commit(data).tree
            return sha

    def resolve(self, rev: str, peel: bool = True) -> str:
        """Хэш по ревизии: HEAD, ветка, тег, сокращение хэша, с суффиксами ^, ^N, ~N, ^{commit|tree}, и «ревизия:путь»."""
        if ":" in rev and not rev.startswith(":"):
            base, _, path = rev.partition(":")
            tree = self.peel(self.resolve(base or "HEAD"), "tree")
            return self._tree_path(tree, path)
        m = re.match(r"^([^\^~]+)(.*)$", rev)
        if not m:
            raise GitError(f"неверная ревизия: {rev!r}")
        name, rest = m.group(1), m.group(2)
        if not re.fullmatch(r"(?:\^\{[a-z]*\}|\^\d*|~\d*)*", rest):
            raise GitError(f"неверная ревизия: {rev!r}")
        sha = self._resolve_name(name)
        for op in re.findall(r"\^\{[a-z]*\}|\^\d*|~\d*", rest):
            if op.startswith("^{"):
                want = op[2:-1]
                sha = self.peel(sha, "tree" if want == "tree" else None) if want in ("tree", "commit", "") else sha
                continue
            sha = self.peel(sha)
            if op.startswith("~"):
                for _ in range(int(op[1:] or 1)):
                    c = self.read_commit(sha)
                    if not c.parents:
                        raise GitError(f"у {rev} нет предка на таком расстоянии")
                    sha = c.parents[0]
            else:
                n = int(op[1:] or 1)
                if n == 0:
                    continue
                c = self.read_commit(sha)
                if n > len(c.parents):
                    raise GitError(f"у коммита {sha[:7]} нет родителя №{n}")
                sha = c.parents[n - 1]
        if rest and rest.replace("^", "").replace("~", "") == rest:
            raise GitError(f"неверная ревизия: {rev!r}")
        return self.peel(sha) if peel and self.odb.kind(sha) == "tag" else sha

    def _resolve_name(self, name: str) -> str:
        if name == "@":
            name = "HEAD"
        for cand in (name, f"refs/{name}", f"refs/tags/{name}", f"refs/heads/{name}", f"refs/remotes/{name}", f"refs/remotes/{name}/HEAD"):
            if cand == name and name not in ("HEAD", "ORIG_HEAD", "MERGE_HEAD", "FETCH_HEAD"):
                continue
            sha = self.refs.resolve(cand)
            if sha:
                return sha
        if re.fullmatch(r"[0-9a-fA-F]{4,40}", name):
            return self.odb.resolve_prefix(name)
        raise GitError(f"неизвестная ревизия: {name!r}")

    def _tree_path(self, tree: str, path: str) -> str:
        cur = tree
        kind = "tree"
        for part in [p for p in path.split("/") if p]:
            found = next((e for e in self.read_tree(cur) if e.name == os.fsencode(part)), None)
            if found is None:
                raise GitError(f"в дереве нет пути {path!r}")
            cur = found.sha
            kind = "tree" if found.mode == MODE_DIR else "blob"
        del kind
        return cur

    # ---------- история ----------

    def walk(self, starts: list[str], exclude: list[str] | None = None) -> Iterator[tuple[str, Commit]]:
        """Коммиты от новых к старым по дате коммитера (как git log); при равных датах в порядке обнаружения."""
        hidden: set[str] = set()
        if exclude:
            stack = list(exclude)
            while stack:
                s = stack.pop()
                if s in hidden:
                    continue
                hidden.add(s)
                stack.extend(self.read_commit(s).parents)
        heap: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        counter = 0
        cache: dict[str, Commit] = {}

        def push(sha: str) -> None:
            nonlocal counter
            if sha in seen or sha in hidden:
                return
            seen.add(sha)
            c = cache[sha] = self.read_commit(sha)
            heapq.heappush(heap, (-c.committer.time, counter, sha))
            counter += 1

        for s in starts:
            push(self.peel(s))
        while heap:
            _, _, sha = heapq.heappop(heap)
            c = cache.pop(sha)
            yield sha, c
            for p in c.parents:
                push(p)

    def merge_bases(self, a: str, b: str) -> list[str]:
        """Лучшие общие предки (git merge-base --all): общие предки, не являющиеся предками других общих предков."""
        anc_a = self._ancestors(a)
        common = [s for s in self._ancestors_ordered(b) if s in anc_a]
        if not common:
            return []
        cs = set(common)
        redundant: set[str] = set()
        for c in common:
            if c in redundant:
                continue
            for p in self.read_commit(c).parents:
                redundant |= self._ancestors(p) & cs
        return [c for c in common if c not in redundant]

    def _ancestors(self, sha: str) -> set[str]:
        seen: set[str] = set()
        stack = [sha]
        while stack:
            s = stack.pop()
            if s in seen:
                continue
            seen.add(s)
            stack.extend(self.read_commit(s).parents)
        return seen

    def _ancestors_ordered(self, sha: str) -> list[str]:
        return [s for s, _ in self.walk([sha])]

    def is_ancestor(self, anc: str, desc: str) -> bool:
        return anc in self._ancestors(desc)

    # ---------- статус ----------

    def status(self) -> Status:
        st = Status()
        idx = self.load_index()
        head = self.head_commit()
        head_flat: Flat = self.flat_tree(self.read_commit(head).tree) if head else {}
        st.conflicts = idx.conflicted_paths()
        staged_idx = idx.staged()
        conflict_set = set(st.conflicts)
        for p in sorted(set(head_flat) | set(staged_idx)):
            if p in conflict_set:
                continue
            h = head_flat.get(p)
            e = staged_idx.get(p)
            if h is None and e is not None:
                st.staged.append(("A", p))
            elif h is not None and e is None:
                st.staged.append(("D", p))
            elif h is not None and e is not None and h != (e.mode, e.sha):
                st.staged.append(("T" if (h[0] ^ e.mode) & 0o170000 else "M", p))
        ignore = IgnoreRules(self.wt, self.git_dir)
        tracked = {p for (p, _s) in idx.entries}
        tracked_dirs = {p.rsplit(b"/", 1)[0] for p in tracked if b"/" in p}
        for p in sorted(staged_idx):
            e = staged_idx[p]
            fp = self.fs(p)
            if not os.path.lexists(fp):
                st.unstaged.append(("D", p))
                continue
            if fp.is_dir() and not fp.is_symlink():
                st.unstaged.append(("T", p))
                continue
            s = os.lstat(fp)
            mode = self._file_mode(fp, s, e)
            if (mode ^ e.mode) & 0o170000 and mode != e.mode:
                st.unstaged.append(("T", p))
                continue
            if s.st_size == e.size and int(s.st_mtime) == e.mtime and s.st_mtime_ns % 1_000_000_000 == e.mtime_ns and mode == e.mode:
                continue  # метаданные те же: файл не менялся
            if os.name != "nt" and mode != e.mode:
                st.unstaged.append(("M", p))
                continue
            from .objects import hash_object

            if hash_object("blob", self._read_worktree_file(fp)) != e.sha:
                st.unstaged.append(("M", p))
        # неотслеживаемые: каталог, в котором нет ни одного отслеживаемого файла, показывается целиком со слешем
        untracked_files = [f for f in self.worktree_files(ignore, tracked_dirs) if f not in tracked]
        shown: list[bytes] = []
        for f in untracked_files:
            parts = f.split(b"/")
            collapsed = f
            for k in range(1, len(parts)):
                d = b"/".join(parts[:k])
                if not any(t == d or t.startswith(d + b"/") for t in tracked):
                    collapsed = d + b"/"
                    break
            if collapsed not in shown:
                shown.append(collapsed)
        st.untracked = sorted(shown)
        return st

    # ---------- ветки и теги ----------

    def create_branch(self, name: str, start: str = "HEAD", force: bool = False) -> str:
        check_ref_name("refs/heads/" + name)
        ref = f"refs/heads/{name}"
        if self.refs.resolve(ref) and not force:
            raise GitError(f"ветка {name} уже существует")
        sha = self.peel(self.resolve(start))
        self.read_commit(sha)
        self.refs.update(ref, sha)
        return sha

    def delete_branch(self, name: str, force: bool = False) -> str:
        ref = f"refs/heads/{name}"
        sha = self.refs.resolve(ref)
        if not sha:
            raise GitError(f"нет ветки {name}")
        if self.refs.read_symbolic("HEAD") == ref:
            raise GitError(f"нельзя удалить текущую ветку {name}")
        head = self.head_commit()
        if not force and head and not self.is_ancestor(sha, head):
            raise GitError(f"ветка {name} не слита в HEAD (удалить принудительно: -D)")
        self.refs.delete(ref)
        (self.git_dir / "logs" / ref).unlink(missing_ok=True)
        return sha

    def create_tag(self, name: str, target: str = "HEAD", message: str | None = None, force: bool = False) -> str:
        check_ref_name("refs/tags/" + name)
        ref = f"refs/tags/{name}"
        if self.refs.resolve(ref) and not force:
            raise GitError(f"тег {name} уже существует")
        sha = self.resolve(target, peel=False)
        if message is None:
            self.refs.update(ref, sha)
            return sha
        kind = self.odb.kind(sha)
        tag = Tag(sha, kind, name, self.identity("COMMITTER"), cleanup_message(message))
        tag_sha = self.odb.write("tag", serialize_tag(tag))
        self.refs.update(ref, tag_sha)
        return tag_sha

    # ---------- checkout и reset ----------

    def _checkout_conflicts(self, current: Flat, target: Flat, idx: Index) -> list[bytes]:
        """Пути, локальные изменения или неотслеживаемые файлы которых были бы затёрты переключением."""
        bad: list[bytes] = []
        staged = idx.staged()
        for p in sorted(set(current) | set(target)):
            if current.get(p) == target.get(p):
                continue
            e = staged.get(p)
            in_idx = (e.mode, e.sha) if e else None
            fp = self.fs(p)
            if in_idx != current.get(p) and in_idx != target.get(p):
                bad.append(p)  # в индексе есть правка, которой нет в целевой версии
                continue
            if e is None:
                if p not in current and os.path.lexists(fp):
                    from .objects import hash_object

                    tv = target.get(p)
                    if not (tv and fp.is_file() and hash_object("blob", self._read_worktree_file(fp)) == tv[1]):
                        bad.append(p)  # неотслеживаемый файл мешает
                continue
            if not os.path.lexists(fp):
                if in_idx == current.get(p) and p in current:
                    bad.append(p)  # файл удалён вручную, а целевая версия его меняет
                continue
            if fp.is_file() or fp.is_symlink():
                from .objects import hash_object

                if hash_object("blob", self._read_worktree_file(fp)) != e.sha and (
                    target.get(p) is None or hash_object("blob", self._read_worktree_file(fp)) != target[p][1]
                ):
                    bad.append(p)  # несохранённые правки в файле
        return bad

    def _apply_tree(self, current: Flat, target: Flat, rewrite_all: bool = False) -> Index:
        """Приводит рабочий каталог и индекс от состояния current к target."""
        for p in sorted(set(current) - set(target), reverse=True):
            fp = self.fs(p)
            if os.path.lexists(fp):
                fp.unlink()
                self._prune_dirs(fp.parent)
        idx = Index()
        for p in sorted(target):
            mode, sha = target[p]
            fp = self.fs(p)
            if rewrite_all or current.get(p) != target[p] or not os.path.lexists(fp):
                self._write_worktree_file(fp, mode, sha)
            if mode == MODE_GITLINK:
                idx.add(IndexEntry(p, sha, mode))
                continue
            idx.add(self._stat_entry(p, sha, mode, os.lstat(fp)))
        return idx

    def _write_worktree_file(self, fp: Path, mode: int, sha: str) -> None:
        if mode == MODE_GITLINK:
            fp.mkdir(parents=True, exist_ok=True)
            return
        # на пути могут лежать файл вместо каталога или каталог вместо файла: освобождаем место
        for parent in reversed(fp.relative_to(self.wt).parents):
            q = self.wt / parent
            if q.is_file() or q.is_symlink():
                q.unlink()
        if fp.is_dir() and not fp.is_symlink():
            shutil.rmtree(fp)
        fp.parent.mkdir(parents=True, exist_ok=True)
        _, data = self.odb.read(sha)
        if fp.is_symlink() or fp.exists():
            fp.unlink()
        if mode == MODE_SYMLINK and os.name != "nt":
            os.symlink(os.fsdecode(data), fp)
            return
        fp.write_bytes(data)
        if os.name != "nt":
            fp.chmod(0o755 if mode == MODE_EXEC else 0o644)

    def checkout(self, target: str, create: str | None = None, force: bool = False) -> str:
        """Переключает на ветку или коммит. create=имя: создать ветку из target и перейти на неё."""
        who = (
            self.identity("COMMITTER")
            if (self.config.get("user.name") or os.environ.get("GIT_COMMITTER_NAME"))
            else Identity("minigit", "minigit@localhost", int(time.time()), "+0000")
        )
        old_head = self.head_commit()
        old_branch = self.refs.head_branch()
        if create:
            check_ref_name("refs/heads/" + create)
            if self.refs.resolve(f"refs/heads/{create}"):
                raise GitError(f"ветка {create} уже существует")
            new_sha = self.peel(self.resolve(target))
            branch_ref: str | None = f"refs/heads/{create}"
        else:
            branch_sha = self.refs.resolve(f"refs/heads/{target}")
            if branch_sha:
                new_sha = branch_sha
                branch_ref = f"refs/heads/{target}"
            else:
                new_sha = self.peel(self.resolve(target))
                branch_ref = None
        commit = self.read_commit(new_sha)
        idx = self.load_index()
        current: Flat = self.flat_tree(self.read_commit(old_head).tree) if old_head else {}
        tgt = self.flat_tree(commit.tree)
        if idx.conflicted_paths():
            raise GitError("есть неразрешённые конфликты: сначала завершите слияние")
        if not force:
            bad = self._checkout_conflicts(current, tgt, idx)
            if bad:
                raise GitError("локальные изменения будут перезаписаны переключением: " + ", ".join(p.decode(errors="replace") for p in bad[:10]))
        new_idx = self._apply_tree(current if not force else {**current, **{p: v for p, v in self.index_flat(idx).items()}}, tgt)
        self.save_index(new_idx)
        if branch_ref:
            if create:
                self.refs.update(branch_ref, new_sha)
            self.refs.set_symbolic("HEAD", branch_ref)
        else:
            (self.git_dir / "HEAD").write_text(new_sha + "\n", encoding="utf-8", newline="\n")
        frm = old_branch or (old_head or "")[:7]
        to = (branch_ref or "")[len("refs/heads/") :] or new_sha[:7]
        self._reflog("HEAD", old_head, new_sha, f"checkout: moving from {frm} to {to}", who)
        return new_sha

    def restore_paths(self, paths: list[str], source: str | None = None, staged: bool = False) -> list[bytes]:
        """git checkout -- путь / git restore: возвращает файлы из индекса (или из коммита source) в рабочий каталог."""
        idx = self.load_index()
        if source:
            flat: Flat = self.flat_tree(self.peel(self.resolve(source), "tree"))
        else:
            flat = self.index_flat(idx)
        restored: list[bytes] = []
        for raw in paths:
            rel = os.fsencode(Path(raw).as_posix().rstrip("/"))
            cands = [p for p in flat if p == rel or p.startswith(rel + b"/")] if rel not in (b"", b".") else list(flat)
            if not cands:
                raise GitError(f"путь не найден в источнике: {raw}")
            for p in cands:
                mode, sha = flat[p]
                self._write_worktree_file(self.fs(p), mode, sha)
                if source or staged:
                    idx.add(self._stat_entry(p, sha, mode, os.lstat(self.fs(p))))
                restored.append(p)
        self.save_index(idx)
        return restored

    def reset(self, target: str = "HEAD", mode: str = "mixed") -> str:
        """soft: двигает ветку; mixed: ещё и индекс; hard: ещё и рабочий каталог."""
        if mode not in ("soft", "mixed", "hard"):
            raise GitError(f"неизвестный режим reset: {mode}")
        new = self.peel(self.resolve(target))
        commit = self.read_commit(new)
        old = self.head_commit()
        if mode == "hard":
            current = self.flat_tree(self.read_commit(old).tree) if old else {}
            idx = self.load_index()
            # неотслеживаемые файлы hard-сброс не трогает; всё отслеживаемое возвращается к версии цели
            current = {**current, **self.index_flat(idx)}
            self.save_index(self._apply_tree(current, self.flat_tree(commit.tree), rewrite_all=True))
        elif mode == "mixed":
            tgt = self.flat_tree(commit.tree)
            idx = Index()
            for p, (m, sha) in tgt.items():
                fp = self.fs(p)
                same = os.path.isfile(fp) and not os.path.islink(fp)
                if same:
                    from .objects import hash_object

                    same = hash_object("blob", fp.read_bytes()) == sha
                if same:
                    idx.add(self._stat_entry(p, sha, m, os.lstat(fp)))
                else:
                    idx.add(IndexEntry(p, sha, m))
            self.save_index(idx)
        who = (
            self.identity("COMMITTER")
            if (self.config.get("user.name") or os.environ.get("GIT_COMMITTER_NAME"))
            else Identity("minigit", "minigit@localhost", int(time.time()), "+0000")
        )
        branch = self.refs.read_symbolic("HEAD")
        if branch:
            self.refs.update(branch, new)
            self._reflog(branch, old, new, f"reset: moving to {target}", who)
        else:
            (self.git_dir / "HEAD").write_text(new + "\n", encoding="utf-8", newline="\n")
        self._reflog("HEAD", old, new, f"reset: moving to {target}", who)
        for f in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
            (self.git_dir / f).unlink(missing_ok=True)
        return new

    # ---------- проверки ----------

    def is_valid_sha(self, sha: str) -> bool:
        return is_hex_sha(sha) and self.odb.exists(sha)
