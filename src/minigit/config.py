"""Минимальный разбор .git/config: разделы, подразделы в кавычках, ключи без учёта регистра, комментарии. Достаточно для user.name, core.*, branch.*."""

from __future__ import annotations

import re
from pathlib import Path

from .objects import GitError

__all__ = ["Config"]

_SECTION = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*(?:[#;].*)?$')


def _quote_value(value: str) -> str:
    """Значение для записи в файл: экранирует обратную косую, кавычку и перевод строки; при пробелах по краям и «#» или «;» берёт в кавычки."""
    esc = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    if esc != esc.strip() or "#" in esc or ";" in esc:
        return f'"{esc}"'
    return esc


class Config:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: list[tuple[str, str]] = []  # («раздел.подраздел.ключ», значение) в порядке файла
        if path.exists():
            self._parse(path.read_text(encoding="utf-8"))

    @staticmethod
    def _strip(value: str) -> str:
        out: list[str] = []
        quoted = False
        i = 0
        while i < len(value):
            c = value[i]
            if c == "\\" and i + 1 < len(value):
                out.append({"n": "\n", "t": "\t", "b": "\b"}.get(value[i + 1], value[i + 1]))
                i += 2
                continue
            if c == '"':
                quoted = not quoted
            elif not quoted and c in "#;":
                break
            else:
                out.append(c)
            i += 1
        return "".join(out).strip()

    def _parse(self, text: str) -> None:
        section = ""
        for n, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            m = _SECTION.match(line)
            if m:
                section = m.group(1).lower() + (("." + m.group(2).replace('\\"', '"')) if m.group(2) is not None else "")
                continue
            key, eq, value = line.partition("=")
            key = key.strip()
            if not section or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", key):
                raise GitError(f"{self.path}: строка {n}: не разобрать «{raw}»")
            self.items.append((f"{section}.{key.lower()}", self._strip(value) if eq else "true"))

    def get(self, key: str, default: str | None = None) -> str | None:
        """Последнее значение ключа (правило git: поздние перекрывают ранние)."""
        sect, _, name = key.rpartition(".")
        norm = sect.split(".", 1)
        full = (norm[0].lower() + ("." + norm[1] if len(norm) > 1 else "")) + "." + name.lower()
        for k, v in reversed(self.items):
            if k == full:
                return v
        return default

    def set(self, key: str, value: str) -> None:
        """Записывает ключ в файл (добавляет раздел при необходимости), сохраняя остальное содержимое."""
        sect, _, name = key.rpartition(".")
        head, _, sub = sect.partition(".")
        quoted = f' "{sub}"' if sub else ""
        header = f"[{head}{quoted}]"
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        out: list[str] = []
        in_target = False
        done = False
        for line in lines:
            m = _SECTION.match(line.strip())
            if m:
                if in_target and not done:
                    out.append(f"\t{name} = {_quote_value(value)}")
                    done = True
                cur = m.group(1).lower() + (("." + m.group(2)) if m.group(2) is not None else "")
                in_target = cur == head.lower() + (("." + sub) if sub else "")
            elif in_target and re.match(rf"^\s*{re.escape(name)}\s*=", line, re.I):
                out.append(f"\t{name} = {_quote_value(value)}")
                done = True
                continue
            out.append(line)
        if not done:
            if not in_target:
                out.append(header)
            out.append(f"\t{name} = {_quote_value(value)}")
        self.path.write_text("\n".join(out) + "\n", encoding="utf-8")
        self.items = Config(self.path).items
