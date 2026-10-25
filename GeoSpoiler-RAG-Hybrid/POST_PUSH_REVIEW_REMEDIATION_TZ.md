# ТЗ: закрытие замечаний полного post-push review

**Проект:** `D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid`
**Базовый commit review:** `2c3d749eec2526120d651cf6e8d4b910d8c9d4bf`
**Дата:** 2026-08-12
**Статус:** исторический remediation-checklist. Текущий статус и доказательства закрытия ведутся в `POST_PUSH_REVIEW_CLOSEOUT_TZ_FINAL.md` и final artifacts; этот документ не подтверждает готовность к push.

## 1. Цель

Закрыть все замечания полного review после `POST_PUSH_REVIEW_FIX_TZ.md` и довести код, canonical-данные, LightRAG/FTS/registry, безопасность, документацию и финальные проверки до состояния, в котором разрешены единый commit и push.

Текущая база работоспособна: полный pytest зелёный, Enriched-валидация зелёная, graph/FTS/registry согласованы по основным счётчикам. Однако финальный DoD ещё не закрыт из-за неподтверждённых media-данных Ultra/18 и нескольких fail-closed/hardening gaps.

## 2. Жёсткие инварианты

1. Не выполнять `git reset --hard`, force-push, массовое удаление или полную переиндексацию без отдельного разрешения.
2. Перед каждой production-операцией создавать backup с SHA-256 и machine-readable manifest.
3. Не менять настройки reranker: `RERANKER_ENABLED=false`.
4. Сохранять текущую архитектуру: Luna для текста, Gemini для STT, текущий Late-Fusion остаётся включённым.
5. Любая недоказанная транскрипция/OCR/visual-выжимка считается недоверенной и не может попадать в canonical normalized text.
6. Ошибка одной media-ветки не должна ломать остальные ветки сообщения или превращаться в ложный success/cache hit.
7. Любая ошибка manifest/load/backfill должна завершаться fail-closed, с отчётом и без принятия частичного результата.
8. Все проверки должны быть воспроизводимыми из `.venv` проекта; системный `yt-dlp` не считать источником истины.

## 3. Обязательные исправления

### P1-A. Ultra/18: восстановить доказуемый canonical source

**Проблема.** В `output/normalized/Ультра левые и ультра правые/18.txt` остались блоки `[Транскрипция аудио]`, `[Текст с экрана (OCR)]`, `[Описание ролика]`, но `18.meta.json` не содержит media/transcription/visual sidecar, input SHA или identity модели. Предыдущий isolated report пометил это как `CONCERNS`, хотя promotion report позже поставил `PASS` только по semantic-полям.

**Обязательное решение по умолчанию:** обработать Ultra/18 caption-only, как Ultra/16. Если появится подтверждённый backup media с input SHA, моделью, prompt/frame identity и artifact status, допускается восстановить media-блоки только после независимой проверки.

**Шаги:**

1. Создать backup текущих `18.txt`, `18.meta.json`, `18.enriched.json`, `card_fts.sqlite`, `source_registry.sqlite` и SHA-manifest.
2. Удалить из canonical `18.txt` все неподтверждённые transcript/OCR/description-блоки; сохранить caption, URL и provenance.
3. Не изменять canonical meta без доказанной media identity.
4. Запустить штатный Luna enrichment на изолированной копии.
5. Проверить source_id, URL, date, message_id, channel, `source_type=telegram_post`, forwarded fields, language, quality flags и отсутствие технических placeholders в semantic-полях.
6. Атомарно заменить только после PASS; затем штатно rebuild FTS и source registry.
7. Создать отчёты pre/post SHA, promotion и rollback.

**Acceptance:**

- `18.txt` не содержит неподтверждённых `[Транскрипция аудио]`, `[Текст с экрана (OCR)]`, `[Описание ролика]`.
- `18.meta.json` и enriched-card согласованы.
- `quality_flags=[]`, `issues=[]`, provenance exact-match.
- FTS/registry содержат ровно одну актуальную запись Ultra/18.
- Старый backup восстанавливается по manifest.

### P1-B. Защитить каждый yt-dlp subprocess от обхода URL allowlist

Файл: `normalizer/instagram/downloader.py`.

1. Вынести общий `_require_valid_instagram_url()`.
2. Вызывать его непосредственно перед каждым yt-dlp subprocess/API entry point: `_get_info_ytdlp`, `_download_video`, `_download_carousel_images`, `_get_subtitles_from_info`, `_extract_post_id` и будущие wrappers.
3. Разрешать только `instagram.com`, `www.instagram.com`, `kkinstagram.com`, `www.kkinstagram.com`, схемы `http/https`, пути `/reel/<id>` и `/p/<id>`.
4. Отклонять credentials, IP/localhost, произвольные домены, suffix-подобные домены и query/path injection.
5. Добавить regression tests для всех прямых entry points; тесты не должны скачивать сеть.

### P2-A. Instagram cache должен быть строго fail-closed

Файл: `normalizer/instagram/cache.py`.

1. После `json.loads()` проверять `isinstance(payload, dict)`; массив, число, строка и `null` превращать в cache miss.
2. Любой malformed/Unicode/OSError cache read не должен прерывать pipeline.
3. Сохранить cache v6 identity: STT provider/base/model/language, visual provider/model/prompt, fallback policy, frame/batch/max limits, yt-dlp version, Luna profile/model/reasoning и API fallback identity.
4. Добавить tests на non-object JSON, corrupted JSON и смену каждого result-affecting параметра.

### P2-B. Native visual backend и artifacts

Файл: `normalizer/media_vision.py`.

1. Для cached success принимать только `status=success`, непустой text, valid digest/model/prompt identity и `1 <= frame_count <= MEDIA_VISION_MAX_FRAMES`.
2. При `MEDIA_VISION_BACKEND=current` применять `MEDIA_VISION_MAX_FILE_MB` до отправки данных.
3. Обернуть SHA/read/encode ошибки в failed/unavailable `VisualExtractionResult`; исключить падение всей нормализации.
4. Artifact writer и reader должны использовать одну visual-model identity.
5. Добавить тесты на zero/negative/too-large frame_count, oversized file и OSError во время hash/read.

### P2-C. Media frame resource cap

Файл: `normalizer/media_frames.py`.

1. Ограничить ffmpeg реальным `-frames:v`/duration cap, а не только срезом списка после генерации.
2. Соблюдать `MEDIA_VISION_MAX_FRAMES`, interval, batch size и размер временной директории.
3. Добавить тест, доказывающий, что число создаваемых кадров не превышает лимит.

### P2-D. Visual backfill должен быть транзакционным и fail-closed

Файл: `normalizer/visual_backfill.py`.

1. Отклонять parent/item `message_id`, если значение отсутствует, не integer или `<=0`.
2. Перед записью sidecar требовать найденный canonical media item; при отсутствии совпадения не добавлять extraction.
3. При чтении artifact требовать `payload.status=success`, непустой text, корректную identity модели и frame limits.
4. Для failed/unavailable писать audit status, чтобы backfill не повторял бесконечно одну и ту же ошибку без отчёта.
5. Сохранять temp TXT/sidecar в той же директории, делать fsync и atomic replace; при любой ошибке выполнять rollback.
6. Добавить idempotence, malformed-sidecar, unmatched-item и rollback tests.

### P2-E. Manifest и LightRAG fail-closed

Файлы: `loader/ingest.py`, `loader/storage.py`.

1. В `load_from_manifest` проверять, что decoded card — `dict`, до вызова `.get()`.
2. Для каждой записи требовать exact provenance, три SHA-256, allowed roots, readable files, уникальные source/path и отсутствие blocked flags.
3. Load считать успешным только при terminal `processed`; missing/empty/unknown status — failure.
4. При partial insert удалять все связанные LightRAG stores, а не только `doc_status`/`full_docs`; добавить mock-test на неполный rollback.
5. Отчёт `accepted_manifest_load_report.json` обязан отражать `expected`, `inserted`, `skipped`, `failed`, `errors`; N/N — единственный accepted.
6. Старые slash-manifest и двухисточниковый manifest не использовать. Файл `artifacts/remediation_final_20260812/accepted_manifest_remediation_all_ultra_20260812.json` сохранён только как исторический 4-source repair; для release использовать свежий `accepted_manifest_v2` с динамическим `expected == len(sources)`.

### P2-F. Prompt trust boundary

Файл: `llm_backend.py`.

1. Не передавать недоверенный Telegram/Instagram текст в псевдо-system prompt.
2. Разделить инструкцию и source data на настоящие роли/структурированные delimiters, насколько это поддерживает Codex CLI.
3. Ограничить Codex runtime directory только выделенной рабочей директорией; исключить доступ к production secrets, `.env`, backup и unrelated output.
4. Добавить prompt-injection regression tests: source text не должен менять tool policy, читать произвольные файлы или подменять schema/quality flags.
5. Если CLI не даёт надёжной role separation, задокументировать compensating controls и запретить непроверенный production use для hostile sources.

## 4. Данные и безопасность

1. Проверить и redacted/rotate URL с `accessToken` в normalized output и `source_registry` для Китай/103; создать report до/после.
2. Провести аудит всех normalized-текстов с media markers без sidecar. Минимум: Ultra/17 и Ultra/28. Для каждого выбрать `caption-only`, доказанное восстановление или quarantine.
3. Не удалять valid quality flags автоматически. `mixed_topics`, `timestamps_unavailable`, `unclear_source_chain`, `very_short_text` и `transcript_unavailable` должны сохраняться как честные данные, если подтверждены source evidence.
4. Проверить, что `partial_segment_failure` отсутствует в accepted corpus; current target expected `0`.
5. Исправить nesting backup-структуры или добавить canonical restore tool, который явно понимает текущий layout и проверяет SHA.
6. Все команды yt-dlp в runbook выполнять через `.venv\Scripts\python.exe`; системную версию не считать проектной.

## 5. Документация

Обновить:

- `LUNA_GEMINI_MEDIA_PIPELINE_PLAN.md` — пометить историческим или синхронизировать со статусом после repair.
- `LUNA_GEMINI_CANARY_REPORT.md` — отделить исторический canary от текущего final acceptance и убрать формулировку «исправления ещё выполняются» после закрытия.
- `LUNA_GEMINI_MEDIA_EXECUTION_REPORT.md` — пометить historical execution report и сослаться на актуальные reports.
- `README.md` — добавить operator runbook: активные flags, manifest v2, backup/rollback, project yt-dlp, no-reranker policy.
- Этот документ — использовать как master checklist до commit/push.

## 6. Тестовый план

### Unit/regression

Добавить/обновить тесты для:

- hostile Instagram URLs на каждом downloader entry point;
- non-object/corrupt Instagram cache;
- native visual cache invalid frame count and size cap;
- media hash/read failure isolation;
- ffmpeg hard frame cap;
- backfill invalid IDs/unmatched media/rollback/idempotence;
- manifest card root type and strict SHA/provenance;
- LightRAG missing/nonterminal status and full cleanup;
- prompt injection/trust boundary;
- token redaction.

### Required commands

```powershell
Set-Location D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q --basetemp D:\RAG-GeoSpoiler\GeoSpoiler-RAG-Hybrid\.pytest-remediation
.venv\Scripts\python.exe main.py validate enriched --fail-on-error
git diff --check
```

## 7. Live and structural DoD

Работа считается закрытой только если одновременно выполнены все условия:

1. Ultra/18 обработана caption-only либо имеет проверяемые media SHA/model artifacts.
2. Ultra/16 и Ultra/18 имеют exact canonical provenance; FTS/registry — по одной актуальной записи.
3. Accepted manifest validation и load: `expected == len(manifest.sources)`, `inserted == expected`, `skipped=0`, `failed=0`; число источников берётся из свежего manifest, не из исторического 4-source repair.
4. LightRAG: все текущие документы `processed`, `processing=0`, `failed=0`, stale China/6 и China/72 отсутствуют.
5. Enriched validator: 131/131 или новый явно зафиксированный count, 0 errors/warnings.
6. `partial_segment_failure=0` в accepted corpus; технические placeholders отсутствуют в semantic-полях.
7. Fresh Telegram image: one backend call, `visual_status=success`, input SHA/model/artifact записаны.
8. Reel без media: review/failure, no caption-only deep success cache; Reel с одной успешной веткой сохраняет partial success.
9. Backfill повторным запуском не делает LLM/media calls при неизменной identity.
10. Нет необезличенных token-like URLs в publishable output/registry/backups.
11. Все P1/P2 findings из этого ТЗ закрыты либо явно утверждены как accepted risk с владельцем и сроком.
12. Финальный review после исправлений не содержит новых P1/P2.

## 8. Порядок исполнения и откат

1. Зафиксировать baseline: branch, HEAD, flags, counts, SHA и backup manifest.
2. Сначала исправить Ultra/18 и data-security findings.
3. Затем внести code hardening и regression tests.
4. Выполнить isolated tests, затем full suite.
5. Выполнить live/graph/FTS/registry checks и сформировать final report.
6. Только после PASS подготовить один commit; push выполняется отдельным явным действием.
7. При любом failed gate остановиться, сохранить report и восстановиться только из SHA-проверенного backup; не принимать частичный результат.

## 9. Артефакты закрытия

Создать минимум:

- `artifacts/remediation_baseline_20260812.json`;
- `artifacts/ultra18_remediation_report.json`;
- `artifacts/security_redaction_remediation_20260812.json`;
- `artifacts/manifest_load_report_20260812.json`;
- `artifacts/remediation_test_report_20260812.json`;
- `artifacts/remediation_final_review_20260812.md`.

Финальный report должен содержать commit/base, flags, counts, SHA-manifests, список закрытых findings, accepted risks и точный verdict `READY_FOR_COMMIT` или `BLOCKED`.
