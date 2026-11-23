"""Построчный diff: алгоритм Майерса, уплотнение групп изменений (порт xdl_change_compact из xdiff, с эвристикой отступов) и вывод unified, совместимый с `git diff`."""

from __future__ import annotations

from array import array
from dataclasses import dataclass

__all__ = ["Change", "diff_lines", "format_hunks", "is_binary", "split_lines"]

#: сверх этой «стоимости» (числа правок) точный поиск прекращается и средняя часть считается заменой целиком: дифф остаётся верным, но не минимальным
MAX_COST = 3000


@dataclass(frozen=True)
class Change:
    """Группа изменений: строки a[i1:i1+n1] заменены строками b[i2:i2+n2] (любая из длин может быть нулевой)."""

    i1: int
    n1: int
    i2: int
    n2: int


def split_lines(data: bytes) -> list[bytes]:
    """Строки с сохранением переводов строк; последняя строка может быть без «\\n». Делит только по «\\n», как git."""
    parts = data.split(b"\n")
    lines = [x + b"\n" for x in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def is_binary(data: bytes) -> bool:
    """Как в git: двоичный, если в первых 8000 байтах есть нулевой байт."""
    return b"\0" in data[:8000]


# ---------- Майерс ----------


def _myers(a: list[bytes], b: list[bytes]) -> list[tuple[int, int]] | None:
    """Совпадающие пары (индекс в a, индекс в b) наибольшей общей подпоследовательности; None, если превышен предел стоимости."""
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return []
    max_d = min(n + m, MAX_COST)
    offset = max_d + 1
    v = array("i", [0]) * (2 * max_d + 3)
    trace: list[array[int]] = []
    found_d = -1
    for d in range(max_d + 1):
        trace.append(array("i", v[offset - d - 1 : offset + d + 2]))
        for k in range(-d, d + 1, 2):
            x = v[offset + k + 1] if k == -d or (k != d and v[offset + k - 1] < v[offset + k + 1]) else v[offset + k - 1] + 1
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
            v[offset + k] = x
            if x >= n and y >= m:
                found_d = d
                break
        if found_d >= 0:
            break
    if found_d < 0:
        return None
    # обратный ход по сохранённым срезам: срез d хранит V до раунда d (индекс j соответствует диагонали k = j - d - 1)
    matches: list[tuple[int, int]] = []
    x, y = n, m
    for d in range(found_d, -1, -1):
        snap = trace[d]
        k = x - y
        prev_k = k + 1 if k == -d or (k != d and snap[k - 1 + d + 1] < snap[k + 1 + d + 1]) else k - 1
        prev_x = snap[prev_k + d + 1]
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            x -= 1
            y -= 1
            matches.append((x, y))
        x, y = prev_x, prev_y
    matches.reverse()
    return matches


def _lcs_matches(a: list[bytes], b: list[bytes]) -> list[tuple[int, int]]:
    """Надёжная общая подпоследовательность через таблицу (медленнее Майерса): эталон для тестов."""
    n, m = len(a), len(b)
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            table[i][j] = table[i + 1][j + 1] + 1 if a[i] == b[j] else max(table[i + 1][j], table[i][j + 1])
    out: list[tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        if a[i] == b[j]:
            out.append((i, j))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return out


# ---------- уплотнение групп изменений (порт xdl_change_compact из xdiff) ----------

MAX_INDENT = 200
MAX_BLANKS = 20
START_OF_FILE_PENALTY = 1
END_OF_FILE_PENALTY = 21
TOTAL_BLANK_WEIGHT = -30
POST_BLANK_WEIGHT = 6
RELATIVE_INDENT_PENALTY = -4
RELATIVE_INDENT_WITH_BLANK_PENALTY = 10
RELATIVE_OUTDENT_PENALTY = 24
RELATIVE_OUTDENT_WITH_BLANK_PENALTY = 17
RELATIVE_DEDENT_PENALTY = 23
RELATIVE_DEDENT_WITH_BLANK_PENALTY = 17
INDENT_WEIGHT = 60
INDENT_HEURISTIC_MAX_SLIDING = 100

_WHITESPACE = b" \t\n\r\x0b\x0c"


def _indent(line: bytes) -> int:
    """Отступ строки (табуляция до кратного 8), или -1, если строка пустая или из одних пробелов."""
    ret = 0
    for c in line:
        if c not in _WHITESPACE:
            return ret
        if c == 0x20:
            ret += 1
        elif c == 0x09:
            ret += 8 - ret % 8
        if ret >= MAX_INDENT:
            return MAX_INDENT
    return -1


class _Side:
    """Один файл с отметками «строка изменена» (rchg) и сторожами по краям, как в xdiff."""

    def __init__(self, lines: list[bytes], changed: list[bool]) -> None:
        self.lines = lines
        self.n = len(lines)
        self.rchg = [False, *changed, False]  # отметка строки i лежит в rchg[i + 1]

    def chg(self, i: int) -> bool:
        return self.rchg[i + 1]

    def set(self, i: int, v: bool) -> None:
        self.rchg[i + 1] = v


class _Group:
    """Группа подряд идущих изменённых строк (возможно, пустая) между двумя неизменёнными; группы двух файлов соответствуют друг другу по номеру."""

    def __init__(self, side: _Side) -> None:
        self.side = side
        self.start = self.end = 0
        while side.chg(self.end):
            self.end += 1

    def next(self) -> bool:
        if self.end == self.side.n:
            return False
        self.start = self.end + 1
        self.end = self.start
        while self.side.chg(self.end):
            self.end += 1
        return True

    def previous(self) -> bool:
        if self.start == 0:
            return False
        self.end = self.start - 1
        self.start = self.end
        while self.side.chg(self.start - 1):
            self.start -= 1
        return True

    def slide_down(self) -> bool:
        s = self.side
        if self.end < s.n and s.lines[self.start] == s.lines[self.end]:
            s.set(self.start, False)
            self.start += 1
            s.set(self.end, True)
            self.end += 1
            while s.chg(self.end):
                self.end += 1
            return True
        return False

    def slide_up(self) -> bool:
        s = self.side
        if self.start > 0 and s.lines[self.start - 1] == s.lines[self.end - 1]:
            self.start -= 1
            s.set(self.start, True)
            self.end -= 1
            s.set(self.end, False)
            while s.chg(self.start - 1):
                self.start -= 1
            return True
        return False


_Measure = tuple[bool, int, int, int, int, int]


def _measure(side: _Side, split: int) -> _Measure:
    """(конец файла, отступ строки после разреза, пустых выше, отступ выше, пустых ниже, отступ ниже)."""
    end_of_file = split >= side.n
    indent = -1 if end_of_file else _indent(side.lines[split])
    pre_blank = 0
    pre_indent = -1
    for i in range(split - 1, -1, -1):
        pre_indent = _indent(side.lines[i])
        if pre_indent != -1:
            break
        pre_blank += 1
        if pre_blank == MAX_BLANKS:
            pre_indent = 0
            break
    post_blank = 0
    post_indent = -1
    for i in range(split + 1, side.n):
        post_indent = _indent(side.lines[i])
        if post_indent != -1:
            break
        post_blank += 1
        if post_blank == MAX_BLANKS:
            post_indent = 0
            break
    return end_of_file, indent, pre_blank, pre_indent, post_blank, post_indent


def _score(m: _Measure) -> tuple[int, int]:
    """(эффективный отступ, штраф) одного разреза."""
    end_of_file, indent, pre_blank, pre_indent, post_blank_n, post_indent = m
    penalty = 0
    if pre_indent == -1 and pre_blank == 0:
        penalty += START_OF_FILE_PENALTY
    if end_of_file:
        penalty += END_OF_FILE_PENALTY
    post_blank = 1 + post_blank_n if indent == -1 else 0
    total_blank = pre_blank + post_blank
    penalty += TOTAL_BLANK_WEIGHT * total_blank + POST_BLANK_WEIGHT * post_blank
    eff = indent if indent != -1 else post_indent
    any_blanks = total_blank != 0
    if eff == -1 or pre_indent == -1:
        pass
    elif eff > pre_indent:
        penalty += RELATIVE_INDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_INDENT_PENALTY
    elif eff < pre_indent:
        if post_indent != -1 and post_indent > eff:
            penalty += RELATIVE_OUTDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_OUTDENT_PENALTY
        else:
            penalty += RELATIVE_DEDENT_WITH_BLANK_PENALTY if any_blanks else RELATIVE_DEDENT_PENALTY
    return eff, penalty


def _cmp(e1: int, p1: int, e2: int, p2: int) -> int:
    return INDENT_WEIGHT * ((e1 > e2) - (e1 < e2)) + (p1 - p2)


def _compact(x: _Side, other: _Side, indent_heuristic: bool) -> None:
    """Сдвигает группы изменений вверх и вниз, сливая соседние и выравнивая по группам другого файла; при неоднозначности работает эвристика отступов."""
    g = _Group(x)
    go = _Group(other)
    while True:
        if g.end != g.start:
            while True:
                size = g.end - g.start
                end_matching_other = -1
                while g.slide_up():
                    go.previous()
                earliest_end = g.end
                if go.end > go.start:
                    end_matching_other = g.end
                while g.slide_down():
                    go.next()
                    if go.end > go.start:
                        end_matching_other = g.end
                if size == g.end - g.start:
                    break
            if g.end == earliest_end:
                pass  # сдвинуть было нельзя
            elif end_matching_other != -1:
                while go.end == go.start:  # возвращаем группу к последнему месту, где она совпадает с группой другого файла
                    g.slide_up()
                    go.previous()
            elif indent_heuristic:
                shift = max(earliest_end, g.end - size - 1, g.end - INDENT_HEURISTIC_MAX_SLIDING)
                best_shift = -1
                best = (0, 0)
                while shift <= g.end:
                    e1, p1 = _score(_measure(x, shift))
                    e2, p2 = _score(_measure(x, shift - size))
                    eff, pen = e1 + e2, p1 + p2
                    if best_shift == -1 or _cmp(eff, pen, best[0], best[1]) <= 0:
                        best = (eff, pen)
                        best_shift = shift
                    shift += 1
                while g.end > best_shift:
                    g.slide_up()
                    go.previous()
        if not g.next():
            break
        go.next()


def _script(a: _Side, b: _Side) -> list[Change]:
    out: list[Change] = []
    i = j = 0
    while i < a.n or j < b.n:
        if i < a.n and j < b.n and not a.chg(i) and not b.chg(j):
            i += 1
            j += 1
            continue
        s1, s2 = i, j
        while i < a.n and a.chg(i):
            i += 1
        while j < b.n and b.chg(j):
            j += 1
        out.append(Change(s1, i - s1, s2, j - s2))
    return out


def diff_lines(a: list[bytes], b: list[bytes], indent_heuristic: bool = True) -> list[Change]:
    """Минимальный (в пределах MAX_COST) набор групп изменений между списками строк, расставленных как в git diff."""
    n, m = len(a), len(b)
    lo = 0
    while lo < n and lo < m and a[lo] == b[lo]:
        lo += 1
    hi = 0
    while hi < n - lo and hi < m - lo and a[n - 1 - hi] == b[m - 1 - hi]:
        hi += 1
    mid_a, mid_b = a[lo : n - hi], b[lo : m - hi]
    if not mid_a and not mid_b:
        return []
    found = _myers(mid_a, mid_b)
    chg_a = [True] * n
    chg_b = [True] * m
    for i in range(lo):
        chg_a[i] = chg_b[i] = False
    for k in range(hi):
        chg_a[n - 1 - k] = chg_b[m - 1 - k] = False
    for i, j in found or []:
        chg_a[lo + i] = False
        chg_b[lo + j] = False
    sa, sb = _Side(a, chg_a), _Side(b, chg_b)
    _compact(sa, sb, indent_heuristic)
    _compact(sb, sa, indent_heuristic)
    return _script(sa, sb)


# ---------- вывод ----------

FUNC_MAX = 80


def _func_line(a: list[bytes], before: int) -> bytes:
    """Строка-«заголовок функции» для @@: ближайшая выше начала хунка, что начинается с буквы, «_» или «$» (правило git по умолчанию).
    Если между хунками такой строки нет, git оставляет прежний заголовок, что равносильно поиску до начала файла."""
    for i in range(before - 1, -1, -1):
        line = a[i]
        if line and line[0] < 0x80 and (line[0:1].isalpha() or line[0:1] in (b"_", b"$")):
            return _cut_utf8(line.rstrip(b"\r\n"), FUNC_MAX).rstrip()
    return b""


def _cut_utf8(line: bytes, limit: int) -> bytes:
    """Обрезка до limit байт без разрыва многобайтного символа UTF-8 (git не оставляет половину символа)."""
    if len(line) <= limit:
        return line
    cut = line[:limit]
    i = limit
    while i > 0 and cut[i - 1] & 0xC0 == 0x80:  # назад по продолжающим байтам к начальному байту последнего символа
        i -= 1
    if i > 0 and cut[i - 1] >= 0xC0:
        need = 2 if cut[i - 1] < 0xE0 else 3 if cut[i - 1] < 0xF0 else 4
        if limit - (i - 1) < need:
            cut = cut[: i - 1]
    return cut


def _range(start: int, count: int) -> str:
    # единица опускается; для пустого диапазона указывается строка перед ним
    first = start + 1 if count else start
    return f"{first}" if count == 1 else f"{first},{count}"


def format_hunks(a: list[bytes], b: list[bytes], changes: list[Change], context: int = 3) -> bytes:
    """Тело unified diff (хунки). Хунки с расстоянием между изменениями не больше 2*context склеиваются, как в git."""
    if not changes:
        return b""
    groups: list[list[Change]] = [[changes[0]]]
    for c in changes[1:]:
        prev = groups[-1][-1]
        if c.i1 - (prev.i1 + prev.n1) > 2 * context:
            groups.append([c])
        else:
            groups[-1].append(c)
    out = bytearray()
    for g in groups:
        first, last = g[0], g[-1]
        s1 = max(first.i1 - context, 0)
        s2 = first.i2 - (first.i1 - s1)
        e1 = min(last.i1 + last.n1 + context, len(a))
        e2 = last.i2 + last.n2 + (e1 - (last.i1 + last.n1))
        header = f"@@ -{_range(s1, e1 - s1)} +{_range(s2, e2 - s2)} @@"
        func = _func_line(a, s1)
        out += header.encode() + ((b" " + func) if func else b"") + b"\n"
        i1 = s1
        for c in g:
            for k in range(i1, c.i1):
                out += _line(b" ", a[k])
            for k in range(c.i1, c.i1 + c.n1):
                out += _line(b"-", a[k])
            for k in range(c.i2, c.i2 + c.n2):
                out += _line(b"+", b[k])
            i1 = c.i1 + c.n1
        for k in range(i1, e1):
            out += _line(b" ", a[k])
    return bytes(out)


def _line(prefix: bytes, line: bytes) -> bytes:
    if line.endswith(b"\n"):
        return prefix + line
    return prefix + line + b"\n\\ No newline at end of file\n"
