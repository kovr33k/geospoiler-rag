# ТЗ: закрытие замечаний ревью после push 2026-08-10 23:51

Статус: готово к исполнению
Базовый коммит: `2c3d749eec2526120d651cf6e8d4b910d8c9d4bf`
Рабочая ветка: `work/rag-v2-snapshot-20260810`

## 1. Цель

Исправить все подтверждённые замечания ревью изменений после последнего push, восстановить корректное происхождение двух уже опубликованных карточек и довести media/manifest-контур до fail-closed состояния.

Итог должен позволять безопасно продолжить загрузку новых материалов без риска:

- фиктивных Telegram URL и `source_id`;
- незаметных caption-only карточек при сбое обработки Reel;
- ложных media-флагов у Telegram-изображений;
- частично успешной manifest-загрузки с нулевым exit-кодом;
- повторного использования устаревшего media-cache;
- зависимости обычной обработки Instagram от доступности GitHub API;
- повторных платных Luna-вызовов при неизменившейся visual identity.

## 2. Зафиксированные проблемы

### P1 — блокируют следующий push и массовую загрузку

1. `Ультра левые и ультра правые/16` и `/18` имеют фиктивные `channel_id=1`, `source_id=telegram:1:*`, дату `2026-06-22` и URL `https://t.me/c/1/*`.
2. Обычный Telegram image-маршрут добавляет Luna-описание в normalized-текст, но не сохраняет `visual_status`, `visual_model`, `visual_artifact_path`, `visual_error` и `visual_frame_count`.
3. Instagram Reel с непустой подписью считается успешным и кэшируется, даже если отсутствуют одновременно audio transcript и OCR/visual result.
4. `load --manifest` не проверяет полный контракт accepted manifest и может завершиться частичным успехом без ненулевого exit-кода.

### P2 — обязательны для полного закрытия кейса

5. Временная недоступность GitHub Releases блокирует Instagram extraction при рабочем локальном `yt-dlp`.
6. Instagram cache signature не включает все параметры, влияющие на результат.
7. Visual backfill повторно обрабатывает уже успешные media и обновляет TXT/sidecar небезопасной парой записей.
8. Новые параметры отсутствуют в `.env.example` и README; отчёты содержат устаревшие статусы.
9. Media-конфигурация не полностью валидируется на старте.

## 3. Границы работы

Входит в работу:

- исправление кода, тестов и документации;
- восстановление production normalized/enriched для `Ультра/16` и `/18`;
- удаление их фиктивных identity из FTS/source registry/LightRAG;
- точечная повторная загрузка только исправленных карточек;
- финальное ревью всего diff после базового коммита.

Не входит:

- новый массовый canary;
- полный `rebuild` LightRAG;
- полное повторное enrichment всего корпуса;
- включение reranker;
- изменение Late-Fusion архитектуры;
- автоматический push без отдельной команды пользователя.

Активные решения сохраняются: Luna — visual и semantic enrichment; Gemini — основной STT, Whisper — STT fallback; `LATE_FUSION_ENABLED=true`; `RERANKER_ENABLED=false`.

## 4. Неподвижные инварианты

1. `source_id`, Telegram URL, channel ID, message ID и дата берутся только из канонического Telegram metadata/export или проверенного backup. Запрещено синтезировать их из порядкового номера файла.
2. Luna не получает аудио. Gemini/Whisper не используются как скрытый visual fallback.
3. LLM не назначает pipeline-owned quality flags.
4. Ошибка одной media-ветви не уничтожает успешный результат другой ветви.
5. При отсутствии обеих содержательных media-ветвей Reel не считается полным успехом и не попадает в обычный success-cache.
6. Accepted manifest является обязательным, полным и fail-closed контрактом.
7. Ни одна операция исправления не начинается без проверенного backup затрагиваемых ignored production-данных.
8. Публикация в граф разрешена только после структурной и ручной смысловой приёмки карточек.

## 5. Фаза 0 — фиксация состояния и документация

### Работа

1. Зафиксировать в run-отчёте:
   - HEAD, ветку и `git status --short`;
   - активные model/backend flags без секретов;
   - число normalized/enriched карточек;
   - число Card FTS, YouTube FTS, source registry и LightRAG документов;
   - текущие записи `Ультра/16` и `/18` во всех представлениях.
2. Создать отдельный timestamped backup:
   - двух normalized TXT и sidecar;
   - двух enriched JSON;
   - Card FTS/source registry;
   - активного `rag_storage/` либо подтвердить актуальный проверенный snapshot.
3. Сформировать SHA-256 manifest backup и проверить его чтением.
4. Не включать `.env`, ключи API и media-файлы в Git.

### Разрешённые существующие контракты

- `VisualExtractionResult` и `analyze_image/analyze_video`: `normalizer/media_vision.py`.
- Каноническая сборка media metadata: `_visual_to_metadata`, `_media_to_metadata` в `normalizer/pipeline.py`.
- Pipeline-owned flags: `_normalize_pipeline_quality_flags`, `_media_quality_flags` в `enricher/pipeline.py`.
- Точечный upsert: `_upsert_text`/`load_texts` в `loader/ingest.py`.
- Media audit: `_audit_media` в `scripts/enriched_v2_pilot.py`.
- Существующие тестовые паттерны: `tests/test_media_vision.py`, `tests/test_manifest_loader.py`, `tests/test_visual_backfill.py`.

### Gate

- Backup существует, SHA совпадают, rollback-порядок записан.
- Production state на этой фазе не изменён.

## 6. Фаза 1 — единый Telegram image-контракт

### Работа

1. Перевести Telegram image-маршрут на `VisualExtractionResult`, не ограничиваясь строкой из `describe_image(...)`.
2. Для каждого скачанного изображения ровно один раз вызвать общий visual backend.
3. Добавить canonical section `[Изображение]` только при `status=success` и непустом тексте.
4. Независимо от успеха записать в media sidecar:
   - `visual_status`;
   - `visual_model`;
   - `visual_artifact_path`;
   - `visual_error`;
   - `visual_frame_count`;
   - identity/input SHA, если поле уже предусмотрено общим артефактом.
5. Передавать тот же результат в `visual_extractions`, чтобы text и metadata не расходились.
6. Не выполнять повторный Luna-вызов ради построения metadata.

### Тесты

- Telegram image success сохраняет текст и полный visual metadata.
- `unavailable` и `failed` сохраняют статус и ошибку, но не создают ложный visual-текст.
- Один image вызывает visual backend ровно один раз.
- Media audit принимает success и корректно диагностирует failed/missing.

### Gate

- На свежем тестовом Telegram image `visual_status=success` появляется без visual backfill.
- Старые text-only сообщения не меняют поведение.

## 7. Фаза 2 — запрет незаметного caption-only для Instagram

### Работа

1. В Reel deep extraction считать содержательным media-успехом наличие хотя бы одного из:
   - непустого audio transcript;
   - непустого OCR/visual result.
2. Если оба результата отсутствуют, вернуть failure в вызывающий pipeline независимо от наличия caption.
3. Направить источник в существующую review queue с детерминированной причиной, содержащей отдельные статусы audio и visual.
4. Не записывать caption-only failure в обычный Instagram success-cache.
5. Сохранить partial success:
   - transcript без OCR — допустим с visual failure metadata;
   - OCR без transcript — допустим с transcription failure metadata.
6. Применить то же правило к carousel: подпись без успешно обработанного изображения не должна маскировать media failure.

### Тесты

- caption + audio failure + visual failure => review queue, success-cache отсутствует.
- caption + transcript => accepted partial success.
- caption + OCR => accepted partial success.
- пустой caption + обе ошибки => review queue.
- повторный запуск failure не возвращает stale success-cache.

### Gate

- Ни один Reel/carousel при включённом deep extraction не становится caption-only карточкой без явной review-причины.

## 8. Фаза 3 — строгий accepted manifest

### Обязательная схема

В корне manifest обязательны:

- `schema_version`;
- `run_id`;
- `pipeline_identity`;
- непустой `sources`.

В каждой записи обязательны:

- `accepted: true`;
- `source_id`;
- `normalized_path`;
- `card_path`;
- `normalized_sha256`;
- `enriched_sha256`;
- `graph_text_sha256`.

### Preflight до первой записи в RAG

1. Разрешить `card_path` только внутри реального `ENRICHED_DIR` после `resolve()`.
2. Разрешить `normalized_path` только внутри реального `NORMALIZED_DIR` после `resolve()`; абсолютные и относительные пути проверяются одинаково.
3. Проверить существование и читаемость обоих файлов.
4. Проверить точное совпадение manifest с `provenance.source_id` и `provenance.normalized_path`.
5. Проверить три SHA-256.
6. Проверить уникальность `source_id`, canonical normalized path и card path.
7. Отклонить карточки с `extraction_unstable` или `partial_segment_failure`.
8. Отклонить отсутствующие поля; optional identity для accepted manifest запрещена.
9. Собрать все ошибки и завершить preflight до вызова `_upsert_text`.

### Поведение загрузки

1. Успех возможен только при `inserted == expected`, `skipped == 0`, `failed == 0`.
2. Любой timeout, skip или terminal failed:
   - завершает CLI с ненулевым exit-кодом;
   - сохраняет machine-readable отчёт;
   - помечает run как `failed`;
   - запрещает последующее обновление FTS/registry как принятого run.
3. До изменения LightRAG должен существовать проверенный backup.
4. При частичном изменении применяется документированный rollback; частичный результат нельзя называть успешной публикацией.

### Тесты

- отсутствует каждое обязательное поле;
- normalized/card path выходит через `..`, absolute path или symlink/junction;
- SHA каждого вида не совпадает;
- provenance mismatch;
- duplicate source/path;
- запрещённый quality flag;
- `load_texts` возвращает меньше ожидаемого;
- timeout/failed даёт ненулевой exit-код;
- валидный manifest загружает ровно N/N.

### Gate

- До полного preflight RAG не мутируется.
- Частичная загрузка никогда не возвращает успешный CLI-код.

## 9. Фаза 4 — yt-dlp без зависимости от доступности GitHub

### Работа

1. Разделить результаты preflight:
   - `failed`: локальная версия не читается или ниже `YTDLP_MIN_VERSION`;
   - `outdated`: GitHub доступен и локальная версия ниже latest;
   - `ok`: версия соответствует политике;
   - `degraded`: локальная версия не ниже minimum, но latest временно проверить нельзя.
2. Обычная normalization разрешает extraction при `degraded`, пишет явный warning и metadata проверки.
3. Управляемый backfill/pilot в strict latest-режиме блокируется при `degraded` или `outdated`.
4. Не выполнять `pip install`, `yt-dlp -U` и другие самообновления автоматически.
5. Не кэшировать сетевой `degraded` навсегда: задать ограниченный TTL либо повторную проверку на следующий run.

### Тесты

- GitHub timeout + установленная версия >= minimum => обычный extractor вызывается.
- То же в strict режиме => запуск блокируется до изменения production state.
- below minimum блокирует оба режима.
- cached degraded перепроверяется после TTL/нового run.

### Gate

- Временный сбой GitHub не превращает все Instagram-источники в failures обычного pipeline.

## 10. Фаза 5 — полная cache identity и валидация конфигурации

### Работа

1. Повысить Instagram cache schema/version.
2. В signature включить все значимые параметры:
   - primary transcription model/provider;
   - fallback STT model/provider;
   - visual backend/model/prompt version;
   - `MEDIA_VISION_FALLBACK_ENABLED`;
   - frame interval, batch size, max frames и max images;
   - yt-dlp installed version;
   - active Luna profile/model/reasoning identity.
3. Stale signature считать cache miss; старый cache не удалять до успешной замены.
4. На старте fail-fast проверять:
   - frame intervals > 0;
   - batch sizes > 0;
   - visual batch size <= visual max images;
   - timeout > 0;
   - допустимый backend;
   - strict yt-dlp policy задана допустимым значением.

### Тесты

- изменение каждого значимого параметра отдельно инвалидирует cache;
- неизменная identity даёт cache hit;
- неправильные числовые параметры завершают запуск понятной ошибкой.

### Gate

- Нельзя получить старый Gemini/Luna/STT результат под новой identity.

## 11. Фаза 6 — идемпотентный и восстанавливаемый visual backfill

### Работа

1. По умолчанию выбирать только media:
   - без `visual_status=success`;
   - с отсутствующим артефактом;
   - с несовпадающей identity/input SHA;
   - явно запрошенные через `--force`.
2. Для совпадающего success использовать существующий artifact/cache без Luna-вызова.
3. Перед изменением создать временные версии TXT и sidecar в том же каталоге.
4. Проверить обе временные версии, затем заменить файлы через `Path.replace/os.replace`.
5. При исключении восстановить исходные файлы; оставить transaction report для следующего recovery-run.
6. Повторный запуск обязан устранить незавершённую пару и не дублировать canonical sections.

### Тесты

- второй запуск с той же identity делает ноль Luna-вызовов;
- `--force` делает повторную обработку;
- stale identity обрабатывается повторно;
- исключение при второй записи восстанавливает первую;
- повтор после имитации прерванной транзакции приводит TXT/sidecar к одному состоянию.

### Gate

- Backfill идемпотентен по тексту, metadata и числу model calls.

## 12. Фаза 7 — документация

### Работа

1. Обновить `.env.example` всеми новыми media/yt-dlp параметрами без секретов.
2. В README описать:
   - Luna visual + Gemini/Whisper audio разделение;
   - yt-dlp normal/strict policy;
   - `load --manifest` и обязательную схему;
   - backup/rollback перед точечной загрузкой;
   - где искать run report.
3. В `LUNA_GEMINI_CANARY_REPORT.md` разделить первый неуспешный и финальный повтор; итоговая таблица должна показывать финальный статус, старый результат остаётся только как история.
4. В `LUNA_GEMINI_MEDIA_PIPELINE_PLAN.md` изменить статус с «не запускалось» на реализовано с замечаниями/ссылкой на это ТЗ.
5. Добавить операторскую пометку: `output/` и `rag_storage/` игнорируются Git и требуют отдельного backup.

### Gate

- README и `.env.example` позволяют воспроизвести безопасный запуск без чтения исходного кода.
- Документы не противоречат текущему состоянию.

## 13. Фаза 8 — восстановление `Ультра/16` и `/18`

Эта фаза выполняется только после прохождения фаз 1–7 и автоматических тестов.

### Канонические значения

`Ультра/16`:

- `source_id=telegram:3299898370:16`;
- `channel_id=3299898370`;
- `message_id=16`;
- `date=2026-03-01T17:10:51+00:00`;
- `post_url=https://t.me/c/3299898370/16`.

`Ультра/18`:

- `source_id=telegram:3299898370:18`;
- `channel_id=3299898370`;
- `message_id=18`;
- `date=2026-03-05T01:16:18+00:00`;
- `post_url=https://t.me/c/3299898370/18`.

### Работа

1. Восстановить Telegram identity в normalized sidecar из проверенного backup.
2. Сохранить уже извлечённые transcript/OCR/visual данные, если их input SHA и model identity подтверждены.
3. Пересобрать normalized canonical text и обе Enriched-карточки через штатный pipeline, а не ручной правкой только финального JSON.
4. Провести ручную проверку четырёх слоёв: caption, audio transcript, OCR/vision, итоговая карточка.
5. Убедиться, что source type соответствует фактическому источнику и не меняется произвольно между sidecar и Enriched.
6. Сформировать строгий accepted manifest с тремя SHA для каждой карточки.
7. Удалить фиктивные identity `telegram:1:16`, `telegram:1:18` и URL `/c/1/*` из Card FTS/source registry.
8. По accepted manifest точечно upsert обеих карточек в LightRAG.
9. Проверить отсутствие stale docs/metadata и ровно одну актуальную запись каждого source.

### Gate

- По всему production state отсутствуют `telegram:1:16`, `telegram:1:18`, `/c/1/16`, `/c/1/18`.
- Обе citations ведут на `https://t.me/c/3299898370/{16|18}`.
- В FTS, registry и graph присутствует ровно одна актуальная версия каждой карточки.
- Manifest load завершён `2/2`, skipped=0, failed=0.

## 14. Фаза 9 — финальная проверка и повторное ревью

### Автоматические проверки

1. `ruff check` для всего изменённого Python-контура.
2. Полный `pytest` из project `.venv` с отдельным абсолютным `--basetemp` вне проекта.
3. `git diff --check`.
4. Secret scan отслеживаемых и новых файлов.
5. Структурная проверка всех Enriched-карточек.
6. Сверка счётчиков normalized/enriched, FTS, registry и graph.
7. Проверка, что China/24, China/66, China/69 и Cuba/10 остались `processed`.
8. Targeted retrieval для `Ультра/16` и `/18`: смысл найден, URL корректен, citations валидны, legacy fallback не скрывает отсутствие source.
9. Регрессионные тесты всех воспроизведённых в ревью ошибок.

Новый массовый canary не требуется. Проверяется исправленный контур и сохранённый корпус.

### Повторное ревью

Провести requirement-by-requirement ревью этого ТЗ:

- каждое P1/P2 замечание имеет кодовое исправление;
- каждое исправление имеет негативный regression test;
- production data repair подтверждён отдельным manifest/report;
- документация соответствует реализации;
- untracked файлы, которые входят в реализацию, добавлены в будущий commit;
- ignored production data сохранены отдельным backup, а не считаются сохранёнными GitHub push.

### Финальный DoD

Кейс закрыт только если одновременно выполнено всё:

- полный test suite зелёный;
- все P1 и P2 устранены;
- `Ультра/16` и `/18` имеют правильную provenance во всех слоях;
- fail-closed manifest доказан тестами и успешной загрузкой 2/2;
- свежий Telegram image получает полный visual metadata без backfill;
- Reel с двумя отказавшими media-ветвями попадает в review, а не в success-cache;
- GitHub API outage не блокирует обычный Instagram pipeline при допустимой локальной версии yt-dlp;
- повторный visual backfill с той же identity делает ноль новых Luna-вызовов;
- финальное ревью не содержит P1/P2;
- перед commit/push создан отдельный итоговый отчёт с командами и результатами.

## 15. Порядок исполнения

Фазы выполняются строго последовательно:

`0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → автоматические тесты → 8 → 9`

Production data нельзя менять до прохождения кодовых фаз 1–7. При провале любого gate работа останавливается на текущей фазе, состояние не объявляется принятым, а production восстанавливается по подготовленному rollback-порядку.
