# Отчёт выполнения плана Luna Vision + Gemini Audio

Дата исторического запуска: 2026-08-11
Статус документа: historical execution report; не является current acceptance.

## Текущий статус — 2026-08-12

Текущий canonical remediation run принят через четырёхисточниковый manifest (`expected=4`, `inserted=4`, `skipped=0`, `failed=0`). Актуальные доказательства: `artifacts/accepted_manifest_load_report.json` и `POST_PUSH_REVIEW_CLOSEOUT_TZ_FINAL.md`. Описание пилота ниже сохранено как история исполнения и может включать источники, позднее удалённые или переобработанные.


## Исторически выполнено

- Добавлен отдельный read-only Luna media backend для изображений и кадров видео.
- Telegram video теперь получает независимые визуальный и аудио-результаты; один сбой не маскируется как полный успех.
- Instagram Reel/carousel направлены в тот же Luna visual backend.
- Audio остаётся на Gemini через OpenRouter; для HTTP 400/404/415/422 включается Whisper fallback.
- Добавлены SHA-256/model/prompt artifacts и cache identity.
- Добавлен контролируемый `load --manifest` для точечной загрузки только принятых Enriched v2 карточек.
- Перед Instagram deep extraction проверяется версия `yt-dlp` через активный Python: установленная `2026.07.04`, stable `2026.07.04`.

## Пилот и финальное решение

Исходный пилот содержал пять контрольных материалов:

- `Балтийские страны/4` — visual success + Gemini transcript; Enriched v2 валиден.
- `Китай/6` — visual success + Gemini transcript; Enriched v2 валиден.
- `Китай/72` — visual success + Gemini transcript; Enriched v2 валиден.
- `Ультра левые и ультра правые/16` — Instagram visual + audio transcript; Enriched v2 валиден.
- `Ультра левые и ультра правые/18` — visual/audio sections и Enriched v2 успешно завершены.

После решения пользователя источники `Китай/6` и `Китай/72` удалены из активного корпуса: сообщения удалены в Telegram. Их production/pilot-артефакты сохранены только в `artifacts/retired_deleted_telegram_20260811` для обратимого аудита и исключены из FTS/registry.

В production оставлены и приняты три источника:

- `Балтийские страны/4`;
- `Ультра левые и ультра правые/16`;
- `Ультра левые и ультра правые/18`.

Для них исправлен `graph_text`: иностранные verbatim-цитаты удаляются из Russian-only графового слоя целиком вместе с кавычками, русские смысловые формулировки сохраняются. Ложные технические флаги (`contains_legacy_media_placeholders`, `possible_duplicate`, `timestamps_unavailable` и другие runtime-owned flags) удалены из generic LLM-пути и этих трёх карточек.

## Проверки

- Targeted suite: 152 passed; после финального media fix: 41 passed.
- Full suite после финального media fix: 604 passed, 30 subtests passed.
- Manifest loader tests: 2 passed.
- Luna live smoke: `codex-cli:gpt-5.6-luna@xhigh`.
- `yt-dlp` preflight: installed equals latest stable.
- Enriched validation: 131/131 valid, 0 errors, 0 warnings.
- Card FTS rebuild: 131/131 indexed; source registry rebuilt without the deleted China sources.
- Targeted LightRAG load: 3/3 graph texts processed; all three source doc IDs are `processed`.
- Cards-only retrieval smoke: all three promoted cards are first direct matches for characteristic queries and return their Telegram URLs.

## Gate

Точечный rollout для трёх оставшихся карточек завершён. Полный backfill остальных материалов не запускался: для него нужен отдельный canary на 10–20 разнородных источниках, затем пакетная обработка с checkpoint/resume.


## Remediation status — 2026-08-12

"
По результатам remediation по `POST_PUSH_REVIEW_REMEDIATION_TZ.md`:

- Ultra/18 переведена на caption-only: неподтверждённые transcript/OCR/description-блоки удалены из canonical normalized text.
- Enriched Ultra/18 пересобрана штатным Luna pipeline; provenance соответствует `telegram:3299898370:18`.
- Допустимый quality flag: `very_short_text`; технические flags/issues отсутствуют.
- FTS и source registry пересобраны после promotion; итоговые счётчики записаны в `artifacts/remediation_ultra18_20260812_v2/ultra18_remediation_report.json`.
- Token-подобные `accessToken` значения в Китай/103 проверены: используется только `%5BREDACTED%5D`.
- Этот документ является текущим status update; первоначальный canary/execution текст ниже сохраняется как исторический evidence.
