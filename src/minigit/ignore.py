""".gitignore: шаблоны с «*», «?», «[...]», «**», отрицанием «!», привязкой «/» и признаком каталога. Последнее подходящее правило побеждает."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["IgnoreRules", "translate"]


def translate(pattern: str) -> str:
    """Шаблон gitignore → регулярное выражение для пути, записанного через «/» (без ведущего «/»)."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                # «**/» в начале или середине: любое число каталогов; «/**» в конце: всё внутри; иначе обычная «*»
                prev_ok = i == 0 or pattern[i - 1] == "/"
                if prev_ok and pattern.startswith("**/", i):
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                if prev_ok and i + 2 == n:
                    out.append(".*")
                    i += 2
                    continue
                out.append("[^/]*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : j]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pattern[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@dataclass
class _Rule:
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool
    base: str  # каталог, в котором лежит .gitignore («» для корня)


def _parse(text: str, base: str) -> list[_Rule]:
    rules: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        # завершающие пробелы отбрасываются, если не экранированы
        while line.endswith(" ") and not line.endswith("\\ "):
            line = line[:-1]
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        if line.startswith("\\#") or line.startswith("\\!"):
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/") if dir_only else line
        if not line:
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        body = translate(line)
        # без «/» (кроме завершающего) шаблон действует на имя на любой глубине
        regex = re.compile("^" + body + "$") if anchored else re.compile("^(?:.*/)?" + body + "$")
        rules.append(_Rule(regex, negated, dir_only, base))
    return rules


class IgnoreRules:
    """Набор правил репозитория: .git/info/exclude и .gitignore из каталогов (подгружаются лениво)."""

    def __init__(self, work_tree: Path, git_dir: Path) -> None:
        self.work_tree = work_tree
        self._by_dir: dict[str, list[_Rule]] = {}
        exclude = git_dir / "info" / "exclude"
        self._global = _parse(exclude.read_text(encoding="utf-8", errors="replace"), "") if exclude.exists() else []

    def _dir_rules(self, rel_dir: str) -> list[_Rule]:
        if rel_dir not in self._by_dir:
            f = self.work_tree / rel_dir / ".gitignore" if rel_dir else self.work_tree / ".gitignore"
            self._by_dir[rel_dir] = _parse(f.read_text(encoding="utf-8", errors="replace"), rel_dir) if f.is_file() else []
        return self._by_dir[rel_dir]

    def _match_one(self, path: str, is_dir: bool) -> bool | None:
        """Решение по самому пути (без родителей): True игнорировать, False вернуть, None нет правила."""
        parts = path.split("/")
        verdict: bool | None = None
        # приоритет: каталоги ближе к файлу сильнее; внутри файла последнее правило сильнее; info/exclude слабее всех
        dirs = [""] + ["/".join(parts[:k]) for k in range(1, len(parts))]
        for rules in [self._global] + [self._dir_rules(d) for d in dirs]:
            for r in rules:
                if r.dir_only and not is_dir:
                    continue
                rel = path[len(r.base) + 1 :] if r.base else path
                if r.base and not path.startswith(r.base + "/"):
                    continue
                if r.regex.match(rel):
                    verdict = not r.negated
        return verdict

    def is_ignored(self, path: str, is_dir: bool = False) -> bool:
        """Игнорируется ли путь (с «/»). Если игнорируется любой каталог выше, игнорируется и всё внутри него."""
        parts = path.split("/")
        for k in range(1, len(parts)):
            if self._match_one("/".join(parts[:k]), True):
                return True
        return bool(self._match_one(path, is_dir))
