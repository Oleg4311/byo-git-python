"""Проверка целостности (аналог git fsck): хэши объектов, разбор, сортировка и режимы деревьев, достижимость и обрывы связей."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .index import Index
from .objects import MODE_DIR, MODE_EXEC, MODE_FILE, MODE_GITLINK, MODE_SYMLINK, GitError, check_tree_name, parse_commit, parse_tag, parse_tree
from .repo import Repository

__all__ = ["Problem", "fsck"]

VALID_MODES = {MODE_FILE, MODE_EXEC, MODE_SYMLINK, MODE_DIR, MODE_GITLINK}


@dataclass(frozen=True)
class Problem:
    severity: str  # «error» (репозиторий повреждён) или «info» (повисшие объекты)
    sha: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: {self.sha[:12] if self.sha else '-'}: {self.message}"


def fsck(repo: Repository) -> list[Problem]:
    problems: list[Problem] = []
    store = repo.odb
    refs_by_obj: dict[str, list[str]] = {}
    kinds: dict[str, str] = {}
    links: dict[str, list[tuple[str, str]]] = {}  # объект → [(цель, ожидаемый тип)]

    for sha in store.all_shas():
        try:
            kind, data = store.read(sha)
        except GitError as err:
            problems.append(Problem("error", sha, str(err)))
            continue
        kinds[sha] = kind
        out: list[tuple[str, str]] = []
        try:
            if kind == "tree":
                entries = parse_tree(data)
                keys = [e.name + (b"/" if e.mode == MODE_DIR else b"") for e in entries]
                if keys != sorted(keys):
                    problems.append(Problem("error", sha, "записи дерева не отсортированы"))
                if len({e.name for e in entries}) != len(entries):
                    problems.append(Problem("error", sha, "в дереве повторяются имена"))
                for e in entries:
                    if e.mode not in VALID_MODES:
                        problems.append(Problem("error", sha, f"недопустимый режим {e.mode:o} у {os.fsdecode(e.name)}"))
                    try:
                        check_tree_name(e.name)
                    except GitError:
                        problems.append(Problem("error", sha, f"недопустимое имя в дереве: {os.fsdecode(e.name)!r}"))
                    if e.mode != MODE_GITLINK:
                        out.append((e.sha, "tree" if e.mode == MODE_DIR else "blob"))
            elif kind == "commit":
                c = parse_commit(data)
                out.append((c.tree, "tree"))
                out += [(p, "commit") for p in c.parents]
            elif kind == "tag":
                t = parse_tag(data)
                out.append((t.object, t.type))
        except GitError as err:
            problems.append(Problem("error", sha, f"{kind}: {err}"))
        links[sha] = out

    # ссылки-корни
    roots: list[tuple[str, str]] = []
    for name, sha in repo.refs.iter_refs("refs/"):
        roots.append((name, sha))
    head = repo.refs.resolve("HEAD") if (repo.git_dir / "HEAD").exists() else None
    if head:
        roots.append(("HEAD", head))
    for name, sha in roots:
        if sha not in kinds:
            problems.append(Problem("error", sha, f"ссылка {name} указывает на отсутствующий объект"))
        refs_by_obj.setdefault(sha, []).append(name)
    index_path = repo.git_dir / "index"
    if index_path.exists():
        try:
            for ie in Index.load(index_path).entries.values():
                if ie.mode != MODE_GITLINK:
                    roots.append(("индекс:" + os.fsdecode(ie.path), ie.sha))
                    if ie.sha not in kinds:
                        problems.append(Problem("error", ie.sha, f"индекс ссылается на отсутствующий объект ({os.fsdecode(ie.path)})"))
        except GitError as err:
            problems.append(Problem("error", "", f"индекс: {err}"))

    reachable: set[str] = set()
    stack = [s for _, s in roots if s in kinds]
    while stack:
        s = stack.pop()
        if s in reachable:
            continue
        reachable.add(s)
        for target, want in links.get(s, []):
            if target not in kinds:
                problems.append(Problem("error", s, f"ссылается на отсутствующий объект {target}"))
            elif kinds[target] != want:
                problems.append(Problem("error", s, f"объект {target[:12]} должен быть {want}, а он {kinds[target]}"))
            else:
                stack.append(target)
    for sha in sorted(set(kinds) - reachable):
        problems.append(Problem("info", sha, f"повисший {kinds[sha]} (недостижим из ссылок)"))
    return problems
