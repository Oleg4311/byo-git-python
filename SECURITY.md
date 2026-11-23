# Политика безопасности

## Как сообщить об уязвимости

Пожалуйста, **не создавайте публичный issue**. Сообщите приватно через GitHub: вкладка **Security** → **Report a vulnerability**
([создать сообщение](https://github.com/Oleg4311/byo-git-python/security/advisories/new)).

Укажите, что именно не так, как это воспроизвести и какая версия затронута. Я постараюсь ответить в течение недели, но это проект одного человека, поэтому сроки не гарантирую.

## Поддерживаемые версии

Исправления выходят для последней версии: ветка `main` и последний релиз.

## Что считается уязвимостью

minigit читает чужие репозитории, поэтому интересны: выход за пределы рабочего каталога при `checkout`, `clone`, `merge` и `restore` (пути с `..`, абсолютные пути, `.git` внутри дерева, символьные ссылки, ведущие наружу), перезапись неотслеживаемых или изменённых файлов без предупреждения, зависание и неограниченный расход памяти на вредоносных объектах, пакетах (дельты-бомбы), индексе и конфигурации, неверная проверка контрольных сумм и хэшей (приём повреждённого объекта как корректного), небезопасное создание временных файлов, раскрытие данных через сообщения об ошибках.

## Что вне области

Отсутствие сетевых протоколов, дельта-сжатия при записи пакетов и других описанных в README возможностей, уже известные проблемы в зависимостях разработки (их закрывает Dependabot), отчёты без способа воспроизведения.

---

## English

Please report vulnerabilities privately via GitHub Security Advisories (**Security** → **Report a vulnerability**) and do not open a public issue. Include what is wrong, how to reproduce it and which version is affected. This is a one-person project, so response times are best-effort. Only the latest release and `main` receive fixes. In scope: path traversal on checkout/clone/merge/restore (`..`, absolute paths, `.git` inside trees, outward symlinks), silently overwriting local files, hangs or unbounded memory on hostile objects, packs (delta bombs), indexes and configs, accepting corrupted objects, unsafe temporary files. Out of scope: documented missing features (network protocols, delta compression on write), known dev-dependency issues.
