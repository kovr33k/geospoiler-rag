# Luna media canary — 2026-08-11

Статус документа: historical canary evidence; не является current production acceptance.

## Текущий статус — 2026-08-12

Актуальное закрытие ведётся по `POST_PUSH_REVIEW_CLOSEOUT_TZ_FINAL.md`. Canonical remediation manifest принят `4/4`; текущий load report — `artifacts/accepted_manifest_load_report.json`. Canary ниже остаётся доказательством только проверенного набора и конфигурации запуска.

Изолированный historical canary выполнен на 10 новых разнородных материалах. Производственные
`output/`, FTS и registry не изменялись.

Артефакты запуска:

- [pilot report](D:/RAG-GeoSpoiler/GeoSpoiler-RAG-Hybrid/artifacts/canary_20260811_fast/report.md)
- [pilot JSON](D:/RAG-GeoSpoiler/GeoSpoiler-RAG-Hybrid/artifacts/canary_20260811_fast/report.json)
- [Late-Fusion smoke](D:/RAG-GeoSpoiler/GeoSpoiler-RAG-Hybrid/artifacts/canary_20260811_fast/late_fusion_smoke.json)
- [query latency](D:/RAG-GeoSpoiler/GeoSpoiler-RAG-Hybrid/artifacts/canary_20260811_fast/late_fusion_latency.json)

## Повторная проверка после исправлений — 2026-08-12

Первый запуск показал stale metadata: старый sidecar сохранял прошлый HTTP 400,
а pilot только копировал его и не проверял media-state. После controlled
backfill и исправления контракта контроля состояния повторный запуск на том же
наборе из 10 материалов прошёл полностью:

- [повторный pilot report](D:/RAG-GeoSpoiler/GeoSpoiler-RAG-Hybrid/artifacts/canary_20260812_media/report.md)
- [повторный pilot JSON](D:/RAG-GeoSpoiler/GeoSpoiler-RAG-Hybrid/artifacts/canary_20260812_media/report.json)
- [повторный Late-Fusion smoke](D:/RAG-GeoSpoiler/GeoSpoiler-RAG-Hybrid/artifacts/canary_20260812_media/late_fusion_smoke.json)
- [повторная latency-выборка](D:/RAG-GeoSpoiler/GeoSpoiler-RAG-Hybrid/artifacts/canary_20260812_media/late_fusion_latency.json)

Результат: `passed=True`, 10/10 валидных карточек, 0 failed/partial, media gate
`passed=True` (1 native video, 4 images, 1 YouTube), self-recall 10/10,
Late-Fusion smoke 4/4 без fallback и с валидными citations/URL. Production
enriched validation после синхронизации: 131/131 карточек валидны.

 Средняя latency четырёх query smoke — 25,4 с (16,0–32,1 с); это небольшая
выборка, не p95.

Этот повторный результат supersedes amber-решение первого запуска именно для
проверенного media-пути. Он не является статистическим доказательством для всех
будущих форматов, поэтому при массовой загрузке сохраняются те же metadata,
media-gate и fallback-проверки.

## Исторический результат первого запуска

Следующая таблица относится только к первому запуску до controlled backfill.
Она сохранена как история диагностики и не является текущим итогом: финальный
результат повторной проверки указан выше, а оставшиеся production-контрактные
исправления выполняются по `POST_PUSH_REVIEW_FIX_TZ.md`.

| Критерий | Результат | Вывод |
|---|---:|---|
| Смысл | 10/10 карточек валидны; ручная сверка без явных неподтверждённых смысловых добавок | зелёный, с одним замечанием по трассировке evidence |
| Статическая схема | 10/10, по 11/11 проверок на карточку | зелёный |
| Media | YouTube обработан; 3 изображения скачаны без visual-derived результата; 1 видео скачано, но transcription получил HTTP 400 | янтарный, media-gate не закрыт |
| Флаги | `mixed_topics` у YouTube; `very_short_text` у `Китай/56`; media-ошибка `Куба/10` не попала в card quality_flags | схема работает, но media-флагирование неполное |
| Latency | enrichment: 17:06 на 10 материалов, 15 номинальных model calls, 2 repair; RAG smoke: 14.8–45.4 с, среднее 26.1 с | измерено, но выборка мала для p95/SLO |
| Citations | 4/4 Late-Fusion smoke-кейса прошли: валидные URL/IDs, неизвестных citations нет | зелёный для smoke |
| Fallback | RAG fallback: 0/4; enrichment failures/partials: 0/10; media fallback: `Куба/10` → caption/text-only | RAG зелёный, media fallback нужно сделать явным |

## Что именно вошло в выборку

1. `Балтийские страны/7` — YouTube transcript.
2. `Китай/93` — короткий Telegram forward.
3. `Китай/69` — средний Telegram forward.
4. `Китай/66` — длинный Telegram forward с изображениями.
5. `Китай/24` — Telegram forward с изображением.
6. `Ультра левые и ультра правые/28` — Instagram-текст с transcript-блоком.
7. `Китай/103` — Telegram forward с web-ссылкой и ignored media.
8. `Куба/10` — Telegram forward с видео.
9. `Ультра левые и ультра правые/25` — короткий Telegram post.
10. `Китай/56` — короткий Instagram-текст.

## Ручная проверка смысла

Саммари и key points сверены с началом/телом исходных normalized-материалов и
YouTube transcript. Существенных hallucination/contradiction не обнаружено:
модель в основном сохраняет атрибуцию (`утверждает`, `сообщает`, `по данным
источника`) и числовые значения.

Небольшое замечание: у третьего key point карточки `Ультра/28` поле `evidence`
пустое, хотя соответствующий фрагмент есть в transcript. Это не сломало текущую
схему, но ухудшает аудируемость и требует отдельной проверки контракта evidence.

## Media и fallback

- YouTube `Балтийские страны/7`: transcript найден и обогащён; флаг
  `mixed_topics` оправдан широкой тематикой эпизода.
- `Китай/24`, `Китай/66`, `Китай/69`: изображения скачаны, но в карточках не
  появилось visual-derived содержание. Если изображения должны участвовать в
  обогащении, это незакрытый media-путь; если нет — это должно быть явно
  зафиксировано политикой pipeline.
- `Куба/10`: видео скачано, но
  `POST https://openrouter.ai/api/v1/audio/transcriptions` вернул HTTP 400.
  Карточка корректно сохранила текстовый контекст, однако результат фактически
  caption/text-only и не получил quality flag. Молчаливый fallback нельзя считать
  зелёным media-результатом.
- Instagram-карточки `Ультра/28` и `Китай/56` содержат URL/текст без локального
  media-файла; транскрибация для них невозможна по входным данным.

## Решение

Canary подтверждает готовность Luna для текстового enrichment и Late-Fusion
retrieval smoke. Полное массовое принятие именно media-пайплайна пока не
рекомендуется. Перед загрузкой оставшегося массива нужно:

1. Исправить/проверить формат OpenRouter audio-transcriptions и добавить
   детерминированный `transcript_unavailable`/`partial_segment_failure` по
   metadata, чтобы HTTP 400 не превращался в незаметную caption-only карточку.
2. Зафиксировать контракт для скачанных изображений: либо выполнять visual
   extraction, либо явно помечать `visual_unavailable` и не считать media-gate
   пройденным.
3. Повторить тот же 10–20-material canary и закрывать media-gate только при
   отсутствии неявных fallback.


## Remediation status — 2026-08-12

"
По результатам remediation по `POST_PUSH_REVIEW_REMEDIATION_TZ.md`:

- Ultra/18 переведена на caption-only: неподтверждённые transcript/OCR/description-блоки удалены из canonical normalized text.
- Enriched Ultra/18 пересобрана штатным Luna pipeline; provenance соответствует `telegram:3299898370:18`.
- Допустимый quality flag: `very_short_text`; технические flags/issues отсутствуют.
- FTS и source registry пересобраны после promotion; итоговые счётчики записаны в `artifacts/remediation_ultra18_20260812_v2/ultra18_remediation_report.json`.
- Token-подобные `accessToken` значения в Китай/103 проверены: используется только `%5BREDACTED%5D`.
- Этот документ является текущим status update; первоначальный canary/execution текст ниже сохраняется как исторический evidence.
