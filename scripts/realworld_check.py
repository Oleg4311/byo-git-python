"""Сверка minigit с git на настоящих репозиториях: каждый клонируется во временный каталог и упаковывается (`git gc --aggressive`, дельты),
затем сравниваются полный `log`, `ls-tree -r HEAD`, `status --porcelain` и результат нашей проверки целостности.

Запуск: python scripts/realworld_check.py путь/к/репозиторию [ещё пути ...]
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from minigit.cli import render_log
from minigit.fsck import fsck
from minigit.repo import Repository

ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "PYTHONUTF8": "1"}


def run(cwd: Path, *args: str) -> bytes:
    return subprocess.run(list(args), cwd=cwd, env=ENV, capture_output=True, check=False).stdout


def check(src: Path, work: Path) -> tuple[bool, str]:
    dst = work / src.name
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(src), str(dst)], env=ENV, check=True, capture_output=True)
    subprocess.run(["git", "gc", "--aggressive", "--prune=now", "-q"], cwd=dst, env=ENV, check=True, capture_output=True)
    repo = Repository.discover(dst)
    head = repo.resolve("HEAD")
    log_ok = b"".join(render_log(repo.walk([head]), repo, None)) == run(dst, "git", "log")
    tree_ok = run(dst, sys.executable, "-m", "minigit", "ls-tree", "-r", "HEAD") == run(dst, "git", "ls-tree", "-r", "HEAD")
    status_ok = run(dst, sys.executable, "-m", "minigit", "status", "--porcelain") == run(dst, "git", "status", "--porcelain")
    errors = [p for p in fsck(repo) if p.severity == "error"]
    commits = int(run(dst, "git", "rev-list", "--count", "HEAD"))
    objects = sum(1 for _ in repo.odb.all_shas())
    good = log_ok and tree_ok and status_ok and not errors
    return (
        good,
        f"коммитов {commits}, объектов {objects}, log {'=' if log_ok else '≠'}, ls-tree {'=' if tree_ok else '≠'}, status {'=' if status_ok else '≠'}, ошибок fsck {len(errors)}",
    )


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2
    ok = 0
    with tempfile.TemporaryDirectory() as tmp:
        for p in paths:
            good, info = check(Path(p).resolve(), Path(tmp))
            ok += good
            print(("ок          " if good else "РАСХОЖДЕНИЕ ") + Path(p).name + ": " + info)
    print(f"итого: {ok} из {len(paths)}")
    return 0 if ok == len(paths) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
