"""Слияние: трёхстороннее слияние текста (по схеме xdiff: общие правки, конфликты, уточнение конфликтов) и слияние деревьев."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .diff import Change, diff_lines, is_binary, split_lines
from .index import Index, IndexEntry
from .objects import MODE_FILE, MODE_GITLINK, MODE_SYMLINK, GitError
from .repo import Flat, Repository

__all__ = ["MergeResult", "merge3", "merge_commits", "merge_trees"]

MARKER = 7


@dataclass
class _Piece:
    """Кусок результата: обычный текст либо конфликт (наши строки, строки базы, чужие строки)."""

    lines: list[bytes] = field(default_factory=list)
    conflict: tuple[list[bytes], list[bytes], list[bytes]] | None = None


def _apply(base: list[bytes], other: list[bytes], changes: list[Change], start: int, end: int) -> list[bytes]:
    """Строки стороны other для диапазона базы [start, end), если применить к нему только изменения changes."""
    out: list[bytes] = []
    pos = start
    for c in changes:
        out += base[pos : c.i1]
        out += other[c.i2 : c.i2 + c.n2]
        pos = c.i1 + c.n1
    out += base[pos:end]
    return out


def _refine(ours: list[bytes], theirs: list[bytes], base: list[bytes]) -> list[_Piece]:
    """Уточнение конфликта (zealous): общие строки внутри и по краям выносятся из конфликта, остаются только различающиеся участки."""
    changes = diff_lines(ours, theirs)
    pieces: list[_Piece] = []
    pos = 0
    for c in changes:
        if c.i1 > pos:
            pieces.append(_Piece(ours[pos : c.i1]))
        pieces.append(_Piece(conflict=(ours[c.i1 : c.i1 + c.n1], [], theirs[c.i2 : c.i2 + c.n2])))
        pos = c.i1 + c.n1
    if pos < len(ours):
        pieces.append(_Piece(ours[pos:]))
    del base
    return pieces


def _join_close_conflicts(pieces: list[_Piece]) -> list[_Piece]:
    """Конфликты, между которыми не больше 3 общих строк, сливаются в один (xdl_simplify_non_conflicts)."""
    out: list[_Piece] = []
    for p in pieces:
        if p.conflict is not None and len(out) >= 2 and out[-1].conflict is None and len(out[-1].lines) <= 3 and out[-2].conflict is not None:
            gap = out.pop().lines
            prev = out.pop()
            assert prev.conflict is not None
            o1, b1, t1 = prev.conflict
            o2, b2, t2 = p.conflict
            out.append(_Piece(conflict=(o1 + gap + o2, b1 + gap + b2, t1 + gap + t2)))
        else:
            out.append(p)
    return out


def merge3(base: bytes, ours: bytes, theirs: bytes, ours_label: str = "ours", theirs_label: str = "theirs") -> tuple[bytes, int]:
    """Трёхстороннее слияние текстов. Возвращает (результат с маркерами конфликтов, число конфликтов)."""
    b, o, t = split_lines(base), split_lines(ours), split_lines(theirs)
    c1 = diff_lines(b, o)
    c2 = diff_lines(b, t)
    pieces: list[_Piece] = []
    pos = i = j = 0
    while i < len(c1) or j < len(c2):
        a = c1[i] if i < len(c1) else None
        d = c2[j] if j < len(c2) else None
        if d is None or (a is not None and a.i1 + a.n1 < d.i1):
            assert a is not None
            pieces.append(_Piece(b[pos : a.i1] + o[a.i2 : a.i2 + a.n2]))
            pos = a.i1 + a.n1
            i += 1
            continue
        if a is None or d.i1 + d.n1 < a.i1:
            pieces.append(_Piece(b[pos : d.i1] + t[d.i2 : d.i2 + d.n2]))
            pos = d.i1 + d.n1
            j += 1
            continue
        # правки пересекаются или соприкасаются: собираем всю связную область
        g1, g2 = [a], [d]
        i += 1
        j += 1
        start = min(a.i1, d.i1)
        end = max(a.i1 + a.n1, d.i1 + d.n1)
        grew = True
        while grew:
            grew = False
            while i < len(c1) and c1[i].i1 <= end:
                g1.append(c1[i])
                end = max(end, c1[i].i1 + c1[i].n1)
                i += 1
                grew = True
            while j < len(c2) and c2[j].i1 <= end:
                g2.append(c2[j])
                end = max(end, c2[j].i1 + c2[j].n1)
                j += 1
                grew = True
        ours_r = _apply(b, o, g1, start, end)
        theirs_r = _apply(b, t, g2, start, end)
        pieces.append(_Piece(b[pos:start]))
        if ours_r == theirs_r:
            pieces.append(_Piece(ours_r))  # обе стороны сделали одну и ту же правку
        else:
            pieces.extend(_refine(ours_r, theirs_r, b[start:end]))
        pos = end
    pieces.append(_Piece(b[pos:]))
    pieces = _join_close_conflicts([p for p in pieces if p.lines or p.conflict is not None])

    out = bytearray()
    conflicts = 0
    for p in pieces:
        if p.conflict is None:
            out += b"".join(p.lines)
            continue
        conflicts += 1
        ol, _bl, tl = p.conflict
        out += b"<" * MARKER + b" " + ours_label.encode() + b"\n"
        out += _nl(b"".join(ol))
        out += b"=" * MARKER + b"\n"
        out += _nl(b"".join(tl))
        out += b">" * MARKER + b" " + theirs_label.encode() + b"\n"
    return bytes(out), conflicts


def _nl(chunk: bytes) -> bytes:
    return chunk if not chunk or chunk.endswith(b"\n") else chunk + b"\n"


# ---------- слияние деревьев ----------


@dataclass
class MergeResult:
    status: str  # «up-to-date», «fast-forward», «merged», «conflict»
    commit: str | None = None
    conflicts: list[bytes] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def _blob(repo: Repository, entry: tuple[int, str] | None) -> bytes:
    return repo.odb.read(entry[1])[1] if entry else b""


def _merge_entry(
    repo: Repository,
    path: bytes,
    base: tuple[int, str] | None,
    ours: tuple[int, str] | None,
    theirs: tuple[int, str] | None,
    ours_label: str,
    theirs_label: str,
) -> tuple[str, tuple[int, str] | None, bytes | None, str]:
    """Слияние одного пути. Возвращает (итог: «ok» или «conflict», запись результата, содержимое для рабочего каталога при конфликте, сообщение)."""
    if ours == theirs:
        return "ok", ours, None, ""
    if ours == base:
        return "ok", theirs, None, ""
    if theirs == base:
        return "ok", ours, None, ""
    name = os.fsdecode(path)
    if ours is None or theirs is None:
        kept = ours or theirs
        who = "нашей" if theirs is None else "чужой"
        what = "изменён" if base is not None else "добавлен"
        return (
            "conflict",
            kept,
            _blob(repo, kept),
            f"КОНФЛИКТ (изменение/удаление): {name} удалён в {'чужой' if theirs is None else 'нашей'} ветке и {what} в {who}",
        )
    if (ours[0] & 0o170000) != (theirs[0] & 0o170000) or MODE_GITLINK in (ours[0], theirs[0]) or MODE_SYMLINK in (ours[0], theirs[0]):
        return "conflict", ours, _blob(repo, ours), f"КОНФЛИКТ (тип): {name}: разные типы объектов, оставлен наш"
    # режим: если изменила одна сторона, берётся её режим
    base_mode = base[0] if base else None
    if ours[0] == theirs[0]:
        mode = ours[0]
    elif base_mode == ours[0]:
        mode = theirs[0]
    elif base_mode == theirs[0]:
        mode = ours[0]
    else:
        return "conflict", ours, _blob(repo, ours), f"КОНФЛИКТ (режим): {name}: режимы изменены по-разному"
    ob, tb, bb = _blob(repo, ours), _blob(repo, theirs), _blob(repo, base)
    if ours[1] == theirs[1]:
        return "ok", (mode, ours[1]), None, ""
    if is_binary(ob) or is_binary(tb) or is_binary(bb):
        return "conflict", ours, ob, f"КОНФЛИКТ (двоичный файл): {name}"
    merged, n = merge3(bb, ob, tb, ours_label, theirs_label)
    if n:
        kind = "добавление/добавление" if base is None else "содержимое"
        return "conflict", (mode, ours[1]), merged, f"КОНФЛИКТ ({kind}): конфликт слияния в {name}"
    return "ok", (mode, repo.write_blob(merged)), None, ""


def merge_trees(
    repo: Repository, base: Flat, ours: Flat, theirs: Flat, ours_label: str, theirs_label: str
) -> tuple[Flat, dict[bytes, tuple[Flat, bytes]], list[str]]:
    """Слияние плоских деревьев. Возвращает (чистые файлы результата, конфликты: путь → (стадии 1/2/3, содержимое), сообщения)."""
    result: Flat = {}
    conflicts: dict[bytes, tuple[Flat, bytes]] = {}
    messages: list[str] = []

    def ancestors(flat: Flat) -> set[bytes]:
        out: set[bytes] = set()
        for q in flat:
            while b"/" in q:
                q = q.rsplit(b"/", 1)[0]
                out.add(q)
        return out

    dirs_ours, dirs_theirs = ancestors(ours), ancestors(theirs)
    for path in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(path), ours.get(path), theirs.get(path)
        # файл на одной стороне и каталог с тем же именем на другой не поддерживается
        if (path in ours and path in dirs_theirs) or (path in theirs and path in dirs_ours):
            raise GitError(f"конфликт «файл/каталог» на пути {os.fsdecode(path)} не поддерживается")
        state, entry, content, msg = _merge_entry(repo, path, b, o, t, ours_label, theirs_label)
        if state == "ok":
            if entry is not None:
                result[path] = entry
            continue
        stages: Flat = {}
        if b:
            stages[b"1"] = b
        if o:
            stages[b"2"] = o
        if t:
            stages[b"3"] = t
        conflicts[path] = (stages, content if content is not None else b"")
        messages.append(msg)
    return result, conflicts, messages


def _virtual_base(repo: Repository, bases: list[str]) -> Flat:
    """Несколько лучших общих предков (перекрёстное слияние): их деревья сливаются друг с другом, как в merge-recursive."""
    cur = repo.flat_tree(repo.read_commit(bases[0]).tree)
    cur_sha = bases[0]
    for nxt in bases[1:]:
        inner = repo.merge_bases(cur_sha, nxt)
        base = _virtual_base(repo, inner) if inner else {}
        other = repo.flat_tree(repo.read_commit(nxt).tree)
        merged, conflicts, _ = merge_trees(repo, base, cur, other, "Temporary merge branch 1", "Temporary merge branch 2")
        for path, (_stages, content) in conflicts.items():
            merged[path] = (MODE_FILE, repo.write_blob(content))  # конфликтное содержимое остаётся в виртуальной базе как есть
        cur = merged
    return cur


def merge_commits(repo: Repository, target: str, message: str | None = None, no_ff: bool = False, no_commit: bool = False) -> MergeResult:
    """git merge: уже слито, перемотка вперёд или трёхстороннее слияние с коммитом (или остановкой на конфликте)."""
    head = repo.head_commit()
    if head is None:
        raise GitError("в текущей ветке ещё нет коммитов: сливать некуда")
    theirs = repo.peel(repo.resolve(target))
    repo.read_commit(theirs)
    if repo.git_dir.joinpath("MERGE_HEAD").exists():
        raise GitError("слияние уже идёт: завершите его коммитом или отмените (reset --hard)")
    idx = repo.load_index()
    if idx.conflicted_paths():
        raise GitError("в индексе есть конфликты")
    if repo.is_ancestor(theirs, head):
        return MergeResult("up-to-date", messages=["Уже актуально."])
    label = target
    branch_ref = repo.refs.resolve(f"refs/heads/{target}")
    kind = "branch" if branch_ref else ("tag" if repo.refs.resolve(f"refs/tags/{target}") else "commit")
    default_msg = f"Merge {kind} '{label}'"
    cur_branch = repo.refs.head_branch()
    if cur_branch and cur_branch not in ("main", "master"):
        default_msg += f" into {cur_branch}"

    if repo.is_ancestor(head, theirs) and not no_ff:
        head_flat = repo.flat_tree(repo.read_commit(head).tree)
        tgt_flat = repo.flat_tree(repo.read_commit(theirs).tree)
        bad = repo._checkout_conflicts(head_flat, tgt_flat, idx)
        if bad:
            raise GitError("локальные изменения будут перезаписаны слиянием: " + ", ".join(os.fsdecode(p) for p in bad[:10]))
        repo.save_index(repo._apply_tree(head_flat, tgt_flat))
        branch = repo.refs.read_symbolic("HEAD")
        if branch:
            repo.refs.update(branch, theirs)
        else:
            (repo.git_dir / "HEAD").write_text(theirs + "\n", encoding="utf-8", newline="\n")
        return MergeResult("fast-forward", commit=theirs, messages=[f"Перемотка {head[:7]}..{theirs[:7]}"])

    bases = repo.merge_bases(head, theirs)
    base_flat: Flat = {} if not bases else (repo.flat_tree(repo.read_commit(bases[0]).tree) if len(bases) == 1 else _virtual_base(repo, bases))
    head_flat = repo.flat_tree(repo.read_commit(head).tree)
    their_flat = repo.flat_tree(repo.read_commit(theirs).tree)
    # неотслеживаемые и изменённые файлы, которые слияние затронет, не должны потеряться
    touched = {p for p in set(base_flat) | set(head_flat) | set(their_flat) if head_flat.get(p) != their_flat.get(p)}
    only = {p: v for p, v in their_flat.items() if p in touched}
    bad = repo._checkout_conflicts({p: v for p, v in head_flat.items() if p in touched}, only, idx)
    if bad:
        raise GitError("локальные изменения будут перезаписаны слиянием: " + ", ".join(os.fsdecode(p) for p in bad[:10]))
    merged, conflicts, messages = merge_trees(repo, base_flat, head_flat, their_flat, "HEAD", label)

    # рабочий каталог и индекс
    new_idx = Index()
    for p, (mode, sha) in merged.items():
        fp = repo.fs(p)
        if head_flat.get(p) != (mode, sha) or not os.path.lexists(fp):
            repo._write_worktree_file(fp, mode, sha)
        new_idx.add(IndexEntry(p, sha, mode) if mode == MODE_GITLINK else repo._stat_entry(p, sha, mode, os.lstat(fp)))
    for p in set(head_flat) - set(merged) - set(conflicts):
        fp = repo.fs(p)
        if os.path.lexists(fp):
            fp.unlink()
            repo._prune_dirs(fp.parent)
    for p, (stages, content) in conflicts.items():
        for key, (mode, sha) in stages.items():
            new_idx.add(IndexEntry(p, sha, mode, stage=int(key)))
        fp = repo.fs(p)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(content)
    repo.save_index(new_idx)

    full_msg = message or default_msg
    if conflicts:
        (repo.git_dir / "MERGE_HEAD").write_text(theirs + "\n", encoding="utf-8", newline="\n")
        (repo.git_dir / "MERGE_MSG").write_text(
            full_msg + "\n\nConflicts:\n" + "".join(f"\t{os.fsdecode(p)}\n" for p in sorted(conflicts)), encoding="utf-8", newline="\n"
        )
        return MergeResult("conflict", conflicts=sorted(conflicts), messages=messages)
    if no_commit:
        (repo.git_dir / "MERGE_HEAD").write_text(theirs + "\n", encoding="utf-8", newline="\n")
        (repo.git_dir / "MERGE_MSG").write_text(full_msg + "\n", encoding="utf-8", newline="\n")
        return MergeResult("merged", messages=messages)
    sha = repo.commit(full_msg, extra_parents=[theirs], idx=new_idx, reflog_message=f"merge {label}: Merge made by the 'ort' strategy.")
    return MergeResult("merged", commit=sha, messages=messages)
