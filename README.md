# byo-git-python

[![CI](https://github.com/Oleg4311/byo-git-python/actions/workflows/ci.yml/badge.svg)](https://github.com/Oleg4311/byo-git-python/actions/workflows/ci.yml)
[Релизы](https://github.com/Oleg4311/byo-git-python/releases)

**Свой git на чистом Python, совместимый с настоящим на уровне байтов.** Без зависимостей (только стандартная библиотека). Репозитории пишутся в том же формате на диске: хэши объектов и коммитов совпадают с git, `git fsck --strict` принимает наши репозитории, а наш `log`, `diff`, `status` и слияние сверяются с git и на синтетических случаях, и на настоящих историях. Нужен ли такой git? Скорее как учебный и проверочный: показать, как устроены объекты, индекс, пакеты, diff и слияние, и доказать это сравнением, а не словами.

*English: a zero-dependency Python implementation of git that reads and writes real git repositories. Every claim below is checked against the real `git` binary — see the [English section](#english).*

```bash
pip install https://github.com/Oleg4311/byo-git-python/releases/download/v0.1.0/minigit-0.1.0-py3-none-any.whl

minigit init demo && cd demo
echo "hello" > a.txt && minigit add a.txt && minigit commit -m "первый"
minigit log && minigit status
git fsck --strict          # настоящий git принимает наш репозиторий
```

```python
from pathlib import Path
from minigit.repo import Repository

repo = Repository.discover(Path("."))
for sha, commit in repo.walk([repo.resolve("HEAD")]):
    print(sha[:7], commit.message.decode().splitlines()[0])
```

## Что умеет

| Область | Что сделано |
|---|---|
| **Объекты** | blob, tree, commit, tag; SHA-1; порядок записей дерева (каталоги как «имя/»); дополнительные заголовки коммита (gpgsig, encoding, mergetag) сохраняются, поэтому хэш не меняется |
| **База объектов** | рыхлые объекты и пакеты: чтение pack/idx v2 с дельтами OFS и REF, запись пакета и его индекса; хэш каждого прочитанного объекта проверяется |
| **Индекс** | чтение и запись DIRC версий 2 и 3, стадии конфликтов, расширения пропускаются, контрольная сумма |
| **Ссылки** | ветки, теги, HEAD (в том числе отсоединённый), packed-refs, блокировки `.lock`, reflog |
| **Команды** | `init add rm commit status log diff show branch tag checkout reset merge merge-base merge-file cat-file hash-object ls-files ls-tree rev-parse write-tree fsck repack clone fetch config update-ref symbolic-ref` |
| **Diff** | Майерс и уплотнение групп изменений как в xdiff (включая эвристику отступов), хунки, заголовок функции, `\ No newline at end of file`, двоичные файлы |
| **Слияние** | трёхстороннее слияние текста по схеме xdiff (уточнение конфликтов), слияние деревьев, конфликты со стадиями индекса, быстрая перемотка, перекрёстные слияния через виртуальную основу |
| **.gitignore** | `*`, `?`, `[...]`, `**`, отрицание `!`, привязка `/`, каталоги, вложенные файлы, `.git/info/exclude` |
| **Передача** | `clone` и `fetch` из локального репозитория (путь или `file://`) |

## Что проверено

Всё сверяется с настоящим `git` (в тестах и в CI он обязателен):

- **Хэши.** Один и тот же набор файлов и одни и те же даты дают у нас и у git **одинаковый хэш коммита**, на разных историях, с юникодными именами, пустыми файлами, исполняемыми файлами, всеми часовыми поясами и странными сообщениями. Хэши блобов и деревьев сверяются со `git hash-object` и `git mktree` на случайных данных (hypothesis).
- **Настоящие репозитории.** Скрипт `scripts/realworld_check.py` клонирует репозиторий, упаковывает его `git gc --aggressive` (дельты) и сравнивает полный `log`, `ls-tree -r`, `status --porcelain` и результат нашей проверки целостности. Все **24 репозитория автора совпали побайтно** (100 коммитов, 1505 объектов). На публичном `pallets/click` (**3416 коммитов, 16 030 объектов в пакете с дельтами**): `git log` совпал побайтно (826 779 байт), деревья совпали на 21 срезе истории, наша проверка целостности не нашла ошибок (37 секунд), `git fsck --strict` согласен.
- **Diff.** 22 образцовых случая (концы файла, склейка хунков, заголовки функций, CRLF, юникод, эвристика отступов) совпадают с `git diff` побайтно. На 250 случайных правках настоящих файлов совпадает 96–97% (остальное: равнозначные расстановки границ при повторяющихся строках); число изменённых строк во всех случаях минимально, как у `git diff --minimal`. Любой наш патч принимает `git apply` и воспроизводит новый файл.
- **Слияние.** Текст: 99,9% побайтных совпадений с `git merge-file` на 1200 случайных тройках (из них 286 с конфликтами), наличие конфликта совпадает всегда. Деревья: результат чистого слияния (**тот же хэш коммита**), конфликтные файлы, стадии индекса (`git ls-files -u`), коммит после разрешения конфликта и перекрёстное слияние с двумя основами совпадают с `git merge`. Общие предки сверены с `git merge-base --all` на случайных графах.
- **Безопасность чтения чужих репозиториев.** Деревья с именами `..`, `.git` (в любом регистре и с обходами NTFS вроде `git~1`) отвергаются при `checkout`, `clone`, `reset` и слиянии, ничего не записывая; символьная ссылка на месте будущего каталога заменяется, а не разыменовывается; «дельта-бомба» (крошечная дельта, раздувающаяся до терабайта) отклоняется сразу, при превышении заявленного размера, а размер распаковки zlib ограничен заявленным.
- **Целостность.** `git fsck --strict` принимает репозитории после любой нашей операции; наш `fsck` находит удалённые, повреждённые и неверно отсортированные объекты и ссылки на несуществующее. Пакет, записанный нами, проходит `git verify-pack` и читается `git cat-file`.
- **Двусторонняя совместимость.** Настоящий git клонирует репозиторий, созданный нами; мы клонируем репозиторий git (в том числе упакованный).
- **Мутации.** В код вручную вносятся 17 поломок (порядок записей дерева, выравнивание индекса, размер копирования в дельте, слияние соприкасающихся правок, проверка имён в чужих деревьях, предел размера дельты, удаление из packed-refs, склейка хунков, порядок коммитов с одинаковыми датами, проверка при checkout, эвристика отступов, контрольные суммы и др.): `scripts/mutation_check.py` подтверждает, что тесты ловят каждую. Две первые прогонки пропустили две поломки: это были дыры в тестах (нет проверки копирования ровно 64 КиБ и порядка при одинаковых датах), теперь закрыты.
- **167 тестов**, `mypy --strict` и `ruff`; CI на Linux, Windows и macOS, Python 3.10–3.13.

Находки по ходу: сообщения в `git log` отступаются и разворачивают табуляции иначе, чем я думал (пустые строки тоже с 4 пробелами, табуляции по 8 колонок, пробелы в конце строк убираются): это показали только реальные истории, синтетические тесты были зелёными. Запись значений конфигурации с обратной косой чертой (пути Windows) делала `.git/config` нечитаемым для git: поймал тест `clone`.

## Честные ограничения

- **Только локальная передача.** Протоколы `http`, `ssh`, `git://` не реализованы; `push` нет.
- **Запись пакетов без дельт.** Наши пакеты корректны, но больше, чем у git; сжатие дельтами при записи не реализовано (читаются дельты любые).
- **Нет обнаружения переименований** при слиянии и в `status`: переименование выглядит как удаление и добавление. Конфликт «файл против каталога» отклоняется с ошибкой.
- **Diff совпадает с git не всегда.** На повторяющихся строках xdiff выбирает другие (равнозначные) границы изменений; алгоритмы patience и histogram не реализованы. Слияние совпало не на всех случайных тройках (99,9%).
- **Нет**: `stash`, `rebase`, `cherry-pick`, `gc`, рабочих деревьев, подмодулей (записи gitlink сохраняются, но не раскрываются), хуков, подписей, shallow, `core.autocrlf` и атрибутов, LFS, индекса версии 4 и расширений индекса при записи (читаются и пропускаются).
- **Вывод `status` и `diff` не квотирует пути** с необычными символами так, как это делает git.
- **Скорость.** Это Python: проверка целостности 16 тысяч объектов занимает около 37 секунд.
- **Windows:** символьные ссылки и бит исполняемости не поддерживаются (как у git с `core.symlinks=false`, `core.filemode=false`).
- **Параллельная работа** защищена только блокировками ссылок и атомарной заменой файлов индекса и объектов.

## Установка и разработка

Колесо и архив на странице [Releases](https://github.com/Oleg4311/byo-git-python/releases) (контрольные суммы и подтверждение происхождения: `gh attestation verify minigit-0.1.0-py3-none-any.whl --repo Oleg4311/byo-git-python`). В PyPI пакет не публикуется.

```bash
python -m venv .venv && . .venv/bin/activate        # на Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                                               # нужен установленный git
ruff check . && ruff format --check . && mypy
python scripts/mutation_check.py                     # проверка тестов мутациями (несколько минут)
python scripts/realworld_check.py путь/к/репозиторию # сверка с git на своих репозиториях
```

Лицензия: MIT. Сделано с помощью Claude.

---

## English

**byo-git-python** (`minigit`) is a zero-dependency Python implementation of git that reads and writes real git repositories: objects, loose and packed storage (OFS/REF deltas), index v2/v3, refs, `.gitignore`, a Myers diff with xdiff's change-compaction and indent heuristic, three-way text merge with conflict refinement, tree merges with conflict stages and virtual merge bases, fsck, local clone/fetch, and a CLI.

Everything is checked against the real `git`: commit hashes are identical for the same inputs; `git fsck --strict` accepts our repositories; on 24 real repositories (and on `pallets/click`: 3416 commits, 16,030 objects in a delta-compressed pack) the full `git log` is byte-identical and trees match; our diff is byte-identical to `git diff` on curated cases and ~96-97% of random edits (always minimal, always accepted by `git apply`); three-way merge matches `git merge-file` on 99.9% of random triples and merge commits/conflict files/index stages match `git merge`; 17 manual mutations are all caught; 167 tests, `mypy --strict`. Honest limits: local transport only, packs are written without deltas, no rename detection, no stash/rebase/cherry-pick/gc/submodules, diff differs from git on repetitive inputs. MIT licensed. Built with Claude.
