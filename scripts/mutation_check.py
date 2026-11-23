"""Проверка тестов мутациями: в код вносится одна поломка, нужные тесты обязаны упасть. Запуск: python scripts/mutation_check.py"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "minigit"

# (описание, файл, что заменить, на что, какие тесты должны упасть)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "каталоги в дереве сортируются без «/»",
        "objects.py",
        'return e.name + b"/" if e.is_dir else e.name',
        "return e.name",
        "tests/test_objects_pack_index.py",
    ),
    (
        "очистка сообщения не убирает пробелы в конце строк",
        "repo.py",
        "lines = [ln.rstrip() for ln in",
        "lines = [ln for ln in",
        "tests/test_repo_vs_git.py",
    ),
    (
        "выравнивание записи индекса без обязательного нуля",
        "index.py",
        "pad = 8 - (len(rec) % 8)",
        "pad = (8 - (len(rec) % 8)) % 8",
        "tests/test_objects_pack_index.py",
    ),
    (
        "нулевой размер копирования в дельте не означает 0x10000",
        "pack.py",
        "size = size or 0x10000",
        "size = size or 0",
        "tests/test_objects_pack_index.py",
    ),
    (
        "соприкасающиеся правки при слиянии считаются независимыми",
        "merge.py",
        "while i < len(c1) and c1[i].i1 <= end:",
        "while i < len(c1) and c1[i].i1 < end:",
        "tests/test_merge.py",
    ),
    ("удаление ссылки не трогает packed-refs", "refs.py", "if name in packed:", "if False:", "tests/test_fsck_transport_refs.py"),
    (
        "хунки склеиваются при расстоянии больше контекста",
        "diff.py",
        "if c.i1 - (prev.i1 + prev.n1) > 2 * context:",
        "if c.i1 - (prev.i1 + prev.n1) > context:",
        "tests/test_diff.py",
    ),
    (
        "при равных датах коммиты идут в другом порядке",
        "repo.py",
        "heapq.heappush(heap, (-c.committer.time, counter, sha))",
        "heapq.heappush(heap, (-c.committer.time, -counter, sha))",
        "tests/test_repo_vs_git.py",
    ),
    (
        "checkout не проверяет локальные изменения",
        "repo.py",
        "bad = self._checkout_conflicts(current, tgt, idx)\n            if bad:",
        "bad = []\n            if bad:",
        "tests/test_repo_vs_git.py",
    ),
    (
        "эвристика отступов выключена",
        "diff.py",
        "def diff_lines(a: list[bytes], b: list[bytes], indent_heuristic: bool = True)",
        "def diff_lines(a: list[bytes], b: list[bytes], indent_heuristic: bool = False)",
        "tests/test_diff.py",
    ),
    ("хэш объекта при чтении не проверяется", "odb.py", "if hash_object(kind, data) != sha:", "if False:", "tests/test_objects_pack_index.py"),
    (
        "контрольная сумма индекса не проверяется",
        "index.py",
        "if hashlib.sha1(data[:-20]).digest() != data[-20:]:",
        "if False:",
        "tests/test_objects_pack_index.py",
    ),
    ("счётчик конфликтов слияния всегда ноль", "merge.py", "conflicts += 1", "conflicts += 0", "tests/test_merge.py"),
    (
        "checkout не проверяет имена записей дерева (.., .git)",
        "repo.py",
        "            check_tree_name(e.name)\n",
        "            pass\n",
        "tests/test_security.py",
    ),
    ("дельта не ограничена по размеру результата", "pack.py", "if dst_size > MAX_OBJECT_SIZE:", "if False:", "tests/test_security.py"),
    ("дельта не останавливается при превышении заявленного размера", "pack.py", "if len(out) > dst_size:", "if False:", "tests/test_security.py"),
    ("fsck не проверяет порядок записей дерева", "fsck.py", "if keys != sorted(keys):", "if False:", "tests/test_fsck_transport_refs.py"),
]


def main() -> int:
    # без исходной проверки на целых тестах мутации ничего не доказывают: упавший заранее набор «ловит» что угодно
    base = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-x", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True)
    if base.returncode != 0:
        print("исходные тесты не проходят: мутационная проверка невозможна\n" + base.stdout[-1500:])
        return 2
    survived: list[str] = []
    for title, name, old, new, tests in MUTATIONS:
        path = SRC / name
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"НЕТ МЕСТА ДЛЯ МУТАЦИИ: {title} ({name})")
            survived.append(title)
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8", newline="")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", tests, "-q", "-x", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True
            )
        finally:
            path.write_text(original, encoding="utf-8", newline="")
        caught = r.returncode != 0
        print(f"{'поймана ' if caught else 'ПРОПУЩЕНА'}: {title}")
        if not caught:
            survived.append(title)
    print(f"\nвсего {len(MUTATIONS)}, поймано {len(MUTATIONS) - len(survived)}")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
