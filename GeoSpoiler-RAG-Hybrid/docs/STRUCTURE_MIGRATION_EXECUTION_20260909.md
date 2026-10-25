# Исполнение миграции структуры — 2026-09-09/10

Статус: **СТРУКТУРНАЯ МИГРАЦИЯ ПРИНЯТА; POST-STRUCTURE LIVE SMOKE 10/10 ПРОЙДЕН 2026-09-10; РИСКИ R24 ПРИНЯТЫ И ОТЛОЖЕНЫ**.

Основание: [ТЗ](PROJECT_STRUCTURE_MIGRATION_TZ_20260909.md). Реализация и отдельный прямой проход ревью выполнены без субагентов.

## Резервная точка

- Приватный source snapshot: <https://github.com/kovr33k/geospoiler-rag-hybrid-backup>, коммит `b48d80d70b72ae3011117968f095e193e22ad286`, тег `before-structure-migration-20260909`; приватность и remote SHA проверены.
- Публичный origin не изменялся.
- Локальный исходный checkpoint: ветка `codex/project-structure`, коммит `2005c0f7c76dec78d3da14394338b2b886714a02`.
- Полная внешняя копия: `D:\GeoSpoiler-backups\structure-20260909`: 10069 файлов, 2405748599 bytes, побайтовые SHA-256 совпали; SQLite backup API и `integrity_check` прошли; сохранён Git bundle.
- Перед отправкой 354 source/test/doc файлов проверены на совпадения с секретными значениями `.env` и известными форматами токенов. Рабочие данные, `.env` и сессии не публиковались.

## Исходная точка

Первый тест выявил несовместимость network guard с Windows `socket.socketpair`; исправление ограничено тестовой инфраструктурой и отдельно проверяет запрет обычных loopback/внешних соединений, включая subprocess. Затем отсутствовал локальный tokenizer cache; словари были подготовлены до включения offline-запрета.

Принятый baseline: **1152 passed, 4 skipped, 44 subtests passed**, exit 0. Все изменяемые каталоги и pytest basetemp находились вне checkout; сеть была запрещена.

До переноса: Card FTS — 131 карточка, YouTube FTS — 22 сегмента, source registry — 133 sources / 129 normalized documents / 131 enriched cards. Обе активные SQLite базы прошли `integrity_check`. Сохранены результаты шести детерминированных запросов, 133 document ID и четыре YouTube generation fingerprints.

## Что изменено

- Канонический пакет находится в `src/geospoiler/` и разделён на `ingestion`, `retrieval`, `answering`, `wiki`, `sources`, `providers`, `contracts`, `cli`, `ui`, `evaluation`, `operations`.
- Старые import/CLI пути оставлены тонкими совместимыми оболочками с общей module identity. Карта содержит 147 переносов: [STRUCTURE_MIGRATION_MAP.md](STRUCTURE_MIGRATION_MAP.md).
- Обратных импортов `retrieval -> answering/cli` и импортов канонического пакета из legacy-модулей нет.
- Editable install и wheel используют один пакет. Wheel проверен в отдельном venv, импортируется из `site-packages`, содержит evaluation prompts и не содержит `.env`, данных, backup, state, sessions или `sitecustomize.py`.
- Windows launcher разрешает скрипты относительно своего каталога, поддерживает `enrich` и `search`, не перезаписывает явные process env значениями `.env`.
- Reviewer сохраняет обычную очередь, скрывает Wiki и показывает безопасные кликабельные HTTP(S)-ссылки.

## Перенос данных

Инструмент `python -m geospoiler.operations.migrate_layout` реализует `plan`, `stage`, `activate`, `rollback`. Он запрещает traversal/link-компоненты, активные SQLite sidecars, конфликтующие targets и потерю новых записей при rollback; журнал не содержит значений `.env`.

Активировано layout v2: **739 файлов / 428675308 bytes**. Рабочие данные находятся под `data/`; старые деревья сохранены в ignored `backups/structure-layout/legacy/`, исходный `.env` — в `backups/structure-layout/original.env`. Исходные файлы и индексы не регенерировались ради переименования.

На полной копии пройдена последовательность stage → activate → SQLite/read checks → rollback → повторная сверка. После активации в рабочем checkout повторная проверка всех 739 target-файлов и SHA-256 прошла. Финальный снимок равен baseline по retrieval, 133 document IDs, четырём YouTube fingerprints и flags. Активные SQLite базы снова вернули `integrity_check=ok`.

## Проверки

- Финальная полная offline-регрессия на точном коде перед коммитом: **1178 passed, 4 skipped, 44 subtests passed**, exit 0, 266.72 s.
- После live QA и нормализации `fallback: null` в строгий JSON boolean: **1179 passed, 4 skipped, 44 subtests passed**, exit 0, 211.70 s.
- После исправления reviewer: 23 профильных теста passed.
- Wiki matrix: 35 тестов passed; отдельные SQLite traps доказывают отсутствие open/create для существующей и отсутствующей Wiki DB.
- Release-boundary/compatibility проверки: 214 профильных тестов и дополнительный набор из 20 boundary tests passed.
- Ruff, PowerShell parser и `git diff --check` прошли.
- Запуск `python -m geospoiler status` из другого CWD с пробелом и кириллицей нашёл рабочие 129 normalized файлов и показал `Wiki: disabled`.
- Браузерная проверка Streamlit: 0 pending, 1 processed, 9 skipped, 10 total; фильтры и обработанные записи открываются, ссылки кликабельны, Wiki-вкладки нет.
- Производительность, медиана пяти отдельных процессов на шести запросах: import +1.8%, search +1.7%; наборы результатов равны, допуск +20% выполнен.

Четыре пропуска относятся к directory-symlink сценариям: текущий Windows token не имеет привилегии создания символических ссылок (`WinError 1314`). Эквивалентные lexical/junction и fail-closed контракты на этой платформе покрыты остальными тестами. Два предупреждения приходят из стороннего `pypinyin` об устаревшем `codecs.open`.

После финальной редакции: wheel пересобран и повторно установлен, compileall, Ruff, PowerShell parser, Markdown links, `git diff --check` и итоговый source secret scan прошли.

## Wiki OFF

Рабочий `.env` и свежий процесс показывают `WIKI_ENABLED=false`, effective `HYBRID_QUERY_WIKI_ENABLED=false`. Проверены варианты отсутствующих flags, `master=false/hybrid=true`, оба false, CLI/module/Windows/UI, существующая и отсутствующая DB. Обычные enrich/run/query/search ветви не вызывают Wiki при master=false; Wiki CLI явно сообщает disabled. Отдельные Wiki-on регрессии продолжают проходить в изолированных тестах. Финальных тестовых Wiki-on процессов не оставлено.

## R24 и последующая live-проверка

[R24](RELEASE_EVIDENCE_REVIEW_FIX_TZ_R24_20260829.md) зафиксировал два P1 и один P2 в prepared-publication/SQLite-recovery контуре. Во время первоначальной приёмки они блокировали cold/live/release execution, поэтому новый live smoke тогда не запускался. 2026-09-10 владелец проекта принял эти остаточные риски и снял обязательный gate без заявления, что findings исправлены.

После снятия gate официальный smoke на текущем Luna-профиле прошёл **10/10**, все кейсы использовали `pipeline=late_fusion`, без fallback и контрактных ошибок; Wiki оставалась выключенной. Ручное сравнение и наблюдения записаны в [post-structure live QA](POST_STRUCTURE_LIVE_QA_20260910.md). Реальный внешний ingestion mini-corpus и cold/release publication этим запуском не проверялись.

## Этапы

| Этап | Итог |
|---|---|
| Backup и GitHub snapshot | PASS |
| 0. Discovery и baseline | PASS |
| 1. Документация и навигация | PASS |
| 2–4. Пакет и слои кода | PASS |
| 5. Перенос данных и rollback rehearsal | PASS |
| 6. Регрессия и прямой независимый проход | PASS |
| 7. UI, целостность, документация | PASS; live answer smoke PASS; external ingestion mini-corpus и cold/release publication NOT RUN |

Подробности: [review](STRUCTURE_MIGRATION_REVIEW_20260910.md) и [матрица](STRUCTURE_MIGRATION_ACCEPTANCE_20260910.md). Машиночитаемые журналы находятся во внешнем каталоге резервной копии.
