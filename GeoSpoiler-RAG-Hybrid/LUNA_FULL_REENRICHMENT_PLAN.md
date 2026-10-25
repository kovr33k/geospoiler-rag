# Полная Luna-регенерация Enriched v2

Дата плана: 2026-08-10
Статус: выполнено; корпус принят 2026-08-10
Цель: пересоздать весь текущий Enriched-корпус единственным текстовым backend `Luna`, не смешивая поколения моделей и не допуская украинский текст в русскоязычных смысловых полях.

## 1. Зафиксированный исходный контекст

- Канонический вход: `output/normalized/` и `output/normalized_youtube/`.
- Текущий объём: 131 normalized-документ.
- Из них ожидаются 129 обычных Enriched-карточек и 2 YouTube-only документа без обычной карточки.
- Обнаружены 4 YouTube-источника; ожидаются 4 episode-карточки плюс их manifests и, при необходимости, segment-карточки.
- Ожидаемый итог верхнего уровня: 133 файла `*.enriched.json` — 129 обычных и 4 YouTube episode.
- Текущий corpus содержит 128 обычных и 4 YouTube episode-карточки; `Корея/13` отсутствует.
- `state/enrichment_progress.json` сейчас непригоден для продолжения: в нём осталась тестовая запись `Channel/1` с `test-fingerprint`.
- Текущий целевой backend: `LLM_PROFILE=luna`, модель `gpt-5.6-luna`, reasoning `xhigh`.
- Требуемая идентичность каждой новой карточки: `codex-cli:gpt-5.6-luna@xhigh`.
- `CODEX_FALLBACK_TO_API=false`: скрытый переход на прежние API-модели запрещён.
- `RERANKER_ENABLED=false`, `WIKI_ENABLED=false`; эти подсистемы не участвуют в регенерации.
- `LATE_FUSION_ENABLED=true` нужен только на финальной retrieval-проверке.

## 2. Что из ранее названных работ остаётся

1. Языковая проверка остаётся обязательной и выполняется **до** регенерации.
2. Старый progress восстанавливать не нужно: для полной сборки создаётся новый progress с нуля.
3. Отдельно редактировать пять обнаруженных карточек не нужно: полная регенерация должна заменить их.
4. `graph_text` и `search_text` будут пересобраны автоматически, но их языковую границу нужно исправить и проверить заранее.
5. Отсутствующая `Корея/13` войдёт в общий forced-run; отдельный запуск не требуется.
6. FTS, source registry и Late-Fusion smoke остаются обязательными после приёмки нового корпуса.

## 3. Разрешённые существующие интерфейсы

- `python main.py enrich --force --llm-profile luna` — полный generic + YouTube enrichment (`cli_app.py`, `enricher/pipeline.py:91`, `enricher/youtube_pipeline.py:98`).
- `llm_backend.active_model_for("enrichment")` — единственный источник идентичности модели (`llm_backend.py:82`).
- `validate_payload(...)` и `repair_if_needed(...)` — существующие semantic validation и один repair-pass (`enricher/validator.py:41`, `enricher/repair.py:106`).
- `python main.py validate enriched --fail-on-error` — структурная проверка корпуса (`cli_app.py:118-121`, `DATA_CONTRACTS.md:264-270`).
- `python main.py fts rebuild` — локальная пересборка Card FTS без LLM (`OPERATIONS.md:118-138`).
- `python main.py registry rebuild` — пересборка source registry (`OPERATIONS.md:140-155`).

Запрещено предполагать наличие dry-run у `enrich`: такого параметра сейчас нет. Запрещено вызывать `python main.py rebuild` или `load`: normalized-корпус не меняется, поэтому LightRAG пересобирать не требуется (`OPERATIONS.md:330-345`).

## 4. Фаза 0 — неизменяемый preflight

### Работа

1. Зафиксировать Git commit, ветку и чистоту worktree.
2. Создать manifest входного корпуса:
   - относительный путь;
   - размер;
   - SHA-256 каждого `.txt`, `.meta.json`, dedicated YouTube metadata/transcript/cues;
   - количество generic и YouTube-only документов;
   - количество уникальных `source_id` и YouTube `video_id`.
3. Зафиксировать runtime identity без вывода секретов:
   - `LLM_PROFILE`;
   - `CODEX_LUNA_MODEL`;
   - `CODEX_LUNA_REASONING_EFFORT`;
   - `CODEX_FALLBACK_TO_API`;
   - `ENRICHMENT_SCHEMA_VERSION`;
   - `ENRICHMENT_PROMPT_VERSION`;
   - `YOUTUBE_ENRICHMENT_PROMPT_VERSION`;
   - `llm_backend.active_model_for("enrichment")`.
4. Проверить доступность Codex CLI контролируемым Luna smoke до начала массовой генерации.
5. Проверить свободное место: одновременно должны помещаться старый корпус, новый корпус, checkpoints, логи и индексы.

### Gate

- Входной manifest сохранён под уникальным `run_id`.
- Active model строго равен `codex-cli:gpt-5.6-luna@xhigh`.
- API fallback выключен.
- Состав входа равен ожидаемому: 131 normalized, 129 generic jobs, 2 YouTube-only, 4 YouTube sources.
- Никакие Enriched-файлы на этой фазе не изменены.

## 5. Фаза 1 — hardening до регенерации

### 5.1 Языковой контракт

Расширить `enricher/validator.py` детерминированной проверкой языка:

- только русский: `summary`, `key_points[].text`, `topics[].label`, `theses[].text`, `events[].description`;
- язык оригинала разрешён: `quotes[].text`, entity surface forms, официальные имена источников и организаций;
- смешанный retrieval разрешён: `search_phrases[].text`, `search_text`;
- `graph_text` должен содержать русскую смысловую часть и может сохранять только собственные имена, но не должен повторно вклеивать оригинальные украинские цитаты как основной нарратив.

Проверка должна:

1. Находить украинскую прозу по украинским буквам и служебным словам, а не по общей кириллице.
2. Проверять агрегат обязательных полей и каждое поле отдельно.
3. Исключать из анализа точные entity surface forms и разрешённые дословные цитаты.
4. Передавать языковое нарушение в существующий единственный repair-pass.
5. Запрещать публикацию карточки, если после repair украинская проза осталась в обязательных полях. `extraction_unstable` не должен легализовать нарушение языка.
6. Никогда не переводить и не переписывать `quotes[].text`.

Исправить `enricher/graph_text_builder.py`:

- строить русский retrieval-нарратив из русских смысловых полей;
- не копировать украинский текст цитат в `graph_text`;
- сохранять оригинальные цитаты и оригинальные поисковые формы в `search_text`;
- сохранять собственные имена без принудительной транслитерации.

### 5.2 Изоляция state в тестах

Исправить тест, вызывающий `_handle_enrichment_result(...)` без подмены `_PROGRESS_FILE`. Все тестовые записи progress должны направляться во временный каталог.

Добавить regression guard:

- вычислить hash/mtime реального `state/enrichment_progress.json` до тестов;
- прогнать затронутые тесты;
- доказать, что production state не создан и не изменён тестами.

### Тестовые случаи

- Полностью украинский `summary` наподобие текущего `Корея/25` отклоняется и отправляется в repair.
- Смешанное `об этой події` отклоняется.
- Украинское предложение в `events[].description` отклоняется.
- Украинская цитата в `quotes[].text` принимается без изменения.
- Официальное имя `Українська асоціація китаєзнавців` разрешается как entity, но topic получает русский контекст.
- Украинские варианты в `search_phrases` и `search_text` принимаются.
- Русский текст с `Си Цзиньпин`, латинскими брендами и аббревиатурами не получает false positive.
- `graph_text` не втягивает оригинальную украинскую цитату; `search_text` её сохраняет.

### Gate

- Targeted tests зелёные.
- Полный pytest зелёный в профилях Late-Fusion `false` и `true`.
- Ruff и `git diff --check` зелёные.
- Production state не меняется тестами.
- Изменения hardening зафиксированы отдельным Git-коммитом до live-run.

## 6. Фаза 2 — резервная копия и maintenance boundary

### Работа

1. Остановить процессы, которые читают или изменяют Enriched, FTS и source registry.
2. Создать каталог `artifacts/luna_full_reenrich/<run_id>/backup/`.
3. Скопировать с сохранением структуры:
   - `output/enriched/`;
   - `output/enriched_segments/`;
   - `state/enrichment_progress.json`;
   - `state/youtube_checkpoints/`;
   - `artifacts/card_fts.sqlite` и sidecar-файлы SQLite, если существуют;
   - `artifacts/source_registry.sqlite` и sidecar-файлы SQLite, если существуют.
4. Записать SHA-256 backup-файлов и проверить их чтение.
5. Сохранить pre-run counts и текущую model distribution старого корпуса.
6. Только после проверки backup переместить старые `output/enriched`, `output/enriched_segments`, progress и YouTube checkpoints в каталог run-а и создать пустые штатные каталоги.

### Gate

- Backup существует, читается и имеет проверенный manifest.
- Точные пути перед перемещением разрешены и находятся внутри целевого checkout.
- Старый FTS остаётся нетронутым до полной приёмки новых карточек.
- Есть однозначная команда обратного перемещения каждого каталога.

## 7. Фаза 3 — полная Luna-регенерация

### Зафиксированное окружение

```text
PYTHONUTF8=1
LLM_PROFILE=luna
CODEX_LUNA_MODEL=gpt-5.6-luna
CODEX_LUNA_REASONING_EFFORT=xhigh
CODEX_FALLBACK_TO_API=false
CODEX_LLM_MAX_CONCURRENCY=1
ENRICHMENT_CONCURRENCY=1
WIKI_ENABLED=false
RERANKER_ENABLED=false
```

`--force` является источником решения пересоздать всё; старый progress не используется.

### Единственная команда генерации

```powershell
python main.py enrich --force --llm-profile luna
```

Stdout/stderr сохраняются в run-каталоге. В логах запрещено печатать API-ключи или полный `.env`.

### Контроль выполнения

- Generic jobs: 129.
- YouTube sources: 4.
- Любой `failed`, `partial`, `youtube_failed`, `youtube_partial` или незакрытый checkpoint делает run неприемлемым.
- Прерванный run не продолжает использовать segment-checkpoint другой модели: checkpoint identity включает активную модель (`DATA_CONTRACTS.md:241-253`).
- При ошибке не запускать FTS/registry rebuild и не смешивать частичный корпус со старым.

### Gate

- Процесс завершён с кодом 0.
- 129 generic и 4 YouTube episode-карточки опубликованы.
- Failed/partial counters равны нулю.
- Progress содержит 129 реальных записей и ни одной тестовой.

## 8. Фаза 4 — приёмка корпуса до обновления индексов

### Структура и полнота

1. Выполнить `python main.py validate enriched --fail-on-error`.
2. Проверить bijection:
   - каждый generic normalized имеет ровно одну обычную карточку;
   - два YouTube-only normalized не имеют дублирующей generic-карточки;
   - каждый из четырёх YouTube sources имеет ровно одну episode-карточку и manifest;
   - нет неизвестных карточек и дубликатов `provenance.source_id`.
3. Проверить manifests и SHA-256 всех YouTube segments.
4. Сверить fingerprints progress с текущими `.txt` и `.meta.json`.

### Модель

- 100% episode/base/segment-карточек имеют `enrichment_model=codex-cli:gpt-5.6-luna@xhigh`.
- Нет карточек с пустой моделью, OpenRouter, NVIDIA, Gemini или прежними API-моделями.
- В логах нет API fallback.

### Язык

- `summary`, key points, topics, theses и events проходят новый русский guard.
- `Корея/25` полностью русская в обязательных полях.
- Утечки из `Китай/18`, `Китай/102` и `Балтийские страны/6` устранены.
- Украинские цитаты сохранены дословно только в разрешённых полях.
- `graph_text` содержит русский нарратив; `search_text` сохраняет русский и оригинальные retrieval-формы.

### Качество

- Нет пустых содержательных карточек.
- Нет `extraction_unstable`, если он связан с языком или структурной ошибкой.
- Все обязательные provenance-поля и проверяемые URL присутствуют.
- Выполнена ручная стратифицированная проверка минимум 10 карточек: короткие посты, длинные посты, украинские источники, смешанные источники и YouTube.

Только успешная Фаза 4 разрешает обновить активные индексы.

## 9. Фаза 5 — производные индексы

### Команды

```powershell
python main.py fts rebuild
python main.py registry rebuild
```

### Проверки

- Card FTS содержит все 129 generic и 4 YouTube episode-карточки согласно своему контракту.
- YouTube segment FTS соответствует опубликованным segment manifests.
- Source registry разрешает каждый `provenance.source_id` в правильный URL и путь.
- Старые, удалённые или переименованные карточки не остаются в индексах.

Не выполнять `python main.py rebuild` и `python main.py load`: LightRAG основан на неизменившемся normalized-корпусе.

## 10. Фаза 6 — Late-Fusion и финальная приёмка

При конфигурации `LATE_FUSION_ENABLED=true`, `RERANKER_ENABLED=false` выполнить те же 10 контрольных запросов, использованные в предыдущем Late-Fusion контуре, но зарегистрировать новый evaluation identity, потому что модель и весь Enriched-корпус изменились.

Для каждого запроса сохранить:

- route и отсутствие legacy fallback;
- latency;
- выбранные Card/YouTube источники;
- citations и их URL;
- ошибки/таймауты;
- автоматические проверки полноты и языковой чистоты ответа.

Старые человеческие A/B-оценки нельзя переносить на новый corpus identity. Новый пользовательский blind A/B не требуется для технического запуска, если пользователь отдельно его не запросит; обязательны автоматические regression и citation gates.

### Финальный DoD

- 133/133 ожидаемых episode/base-карточек присутствуют.
- 100% создано Luna с одной model identity.
- Языковой контракт выполняется.
- Structural validation, полный pytest, Ruff и diff-check зелёные.
- FTS и source registry пересобраны и согласованы с корпусом.
- 10/10 контрольных запросов проходят Late-Fusion без legacy fallback и с валидными citations.
- Run manifest, логи, validation report и backup сохранены.
- Git содержит отдельный коммит hardening и отдельный документированный corpus closeout; generated cards не добавляются в Git, если это противоречит текущей ignore-политике.

## 11. Откат

Откат выполняется при любом незакрытом gate:

1. Остановить query/enrichment процессы.
2. Сохранить неудачный частичный run отдельным каталогом для диагностики.
3. Вернуть из проверенного backup:
   - `output/enriched`;
   - `output/enriched_segments`;
   - progress и YouTube checkpoints;
   - Card FTS и source registry SQLite с sidecar-файлами.
4. Проверить counts, hashes, `python main.py status` и локальный FTS search.
5. Не затрагивать `output/normalized`, `rag_storage`, Wiki DB и sidecars.

Backup не удаляется до завершения нового стабильного retrieval-периода и отдельного решения пользователя.

## 12. Запрещённые сокращения

- Не запускать enrichment до завершения Фазы 1.
- Не генерировать часть карточек Luna поверх смешанного старого корпуса без полного run identity.
- Не считать `language="ru"` доказательством языка без анализа содержательных полей.
- Не переводить оригинальные цитаты и собственные имена ради формальной одноязычности.
- Не удалять украинские retrieval-формы из `search_text`/`search_phrases`.
- Не принимать run с partial/failed карточками.
- Не обновлять активный FTS до приёмки файлового корпуса.
- Не пересобирать LightRAG без изменения normalized-корпуса.
- Не удалять backup автоматически.
-
## 13. Closeout (2026-08-10)

- Run: `artifacts/luna_full_reenrich/20260810T035948Z_f9c5a48/`.
- Corpus acceptance: passed; 131 normalized, 129 generic, 2 YouTube-only, 4 episodes, 22 segments.
- Enriched validation: 133/133 valid; required Russian fields have zero violations; no open YouTube checkpoints.
- Model identity: all base/episode/segment cards use `codex-cli:gpt-5.6-luna@xhigh`; `CODEX_FALLBACK_TO_API=false`.
- Active indexes: Card FTS 133, YouTube segment FTS 22, registry 135 sources / 131 normalized / 133 enriched / 32 references, all references URL-backed.
- Late-Fusion automatic smoke: 10/10 routed through Late-Fusion, zero fallback, zero citation URL errors.
- Full pytest: 587 passed in both `LATE_FUSION_ENABLED=false` and `true` profiles; targeted final tests: 69 passed, 9 subtests; review-queue isolation: 2 passed.
- Known source-quality flags remain only where the source itself lacks transcript/timestamps or mixes topics; they are not generation failures.
