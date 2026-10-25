# ТЗ: исправление ошибок полного Luna-прогона R25

Дата: 2026-08-29  
Run: `20260829T232000Z_full_recheck`  
Статус: **выполнено; targeted corpus-gate пройден 2026-08-30**

## 1. Контекст и фактический результат

Полный прогон был возобновлён после отключения питания по сохранённому
checkpoint. Уже завершённые 68 карточек повторно не обрабатывались.

Входной набор:

- 129 нормализованных документов;
- 127 generic-задач;
- 2 YouTube-only документа;
- 4 YouTube-источника, 22 ожидаемых сегмента;
- модель: `codex-cli:gpt-5.6-luna@xhigh`;
- `CODEX_FALLBACK_TO_API=false`, `WIKI_ENABLED=false`.

Результат:

- 124 из 127 generic-карточек опубликованы;
- 3 generic-карточки не опубликованы;
- 4 YouTube episode-карточки опубликованы, 22 сегмента сохранены;
- один YouTube episode опубликован как partial;
- `main.py validate enriched --fail-on-error`: 128/128 созданных карточек валидны,
  но есть одно предупреждение о пустом содержимом;
- acceptance-аудит: **FAIL** из-за 3 пропущенных generic-карточек и partial
  YouTube episode;
- профильные регрессионные тесты: **92 passed**, 2 предупреждения;
- FTS и source registry пересобраны автоматически, счётчики соответствуют
  128 карточкам и 22 сегментам.

Сравнение с backup показало: для 128 общих файлов нет расхождений по пути,
`source_id`, схеме или модели. Пропали только три входа, перечисленные ниже.

## 2. Найденные ошибки

### P1 — `Китай/15`: таймаут оставляет вход без карточки

Источник содержит около 15 тыс. символов. Вызов Codex завершился ошибкой
`Codex CLI timed out after 300s`; после этого итоговая проверка получила пустой
payload и выбросила:

`UnusableExtractionError: Semantic extraction is invalid and contains neither summary nor key_points`.

Файл карточки и запись в `state/enrichment_progress.json` отсутствуют. Ошибка
воспроизводимо означает «нет результата», а не валидную пустую карточку.

Требуется:

1. ограниченный retry только для timeout/транспортной ошибки с явной причиной;
2. для длинного текста — bounded chunk/compact-путь, не увеличивающий запрос до
   нового неконтролируемого таймаута;
3. при окончательном отказе — сохранять failure/review-запись с ключом входа,
   причиной и fingerprint, чтобы источник не выглядел «не существующим»;
4. не публиковать пустую карточку как успешную.

### P1 — `Ультра левые и ультра правые/2`: русскоязычный источник отклонён
после repair

Нормализованный текст содержательный и преимущественно на русском (около
6,5 тыс. символов), но итоговый payload после одной попытки repair нарушил
русскоязычный контракт:

`Semantic extraction still violates the Russian-language contract after repair`.

Карточка и progress-запись отсутствуют.

Требуется:

1. сохранить диагностический результат failure/review с нарушениями контракта;
2. проверить prompt/repair на этом конкретном тексте и добавить regression-тест;
3. не ослаблять русский контракт молча и не считать отказ успешным обогащением;
4. после исправления повторить только эту карточку.

### P2 — `Ультра левые и ультра правые/18`: неподходящий язык входа не
маршрутизируется явно

Вход — короткий Instagram Reel (около 283 символов) на украинском и немецком.
Он закономерно не прошёл обязательный русский контракт, но сейчас результатом
является обычная generic-ошибка без явного статуса «неподдерживаемый язык».

Требуется выбрать и зафиксировать один продуктовый вариант:

- определить язык до enrichment и отправлять нерусскоязычные источники в review;
  либо
- официально поддержать мультиязычную карточку с отдельным языковым полем и
  соответствующим validator-контрактом.

До такого решения русский-only контракт сохраняется: нельзя выдавать
непроверенную русскую выжимку за корректную.

### P1 — YouTube `wXyMfwS5ZYs`: partial merge публикует пустой episode

Ожидалось 6 сегментов. Пять сегментов успешно обогащены, сегмент `0001`
получил timeout и остался с флагом `extraction_unstable`. Episode-файл создан,
но содержит:

- пустые `summary` и `key_points`;
- `extraction_issues`: `Payload is empty but source text has content...`;
- флаги `extraction_unstable`, `partial_segment_failure`;
- manifest со статусом `partial`.

Причина в ветвлении `enricher/youtube_pipeline.py`: deterministic fallback
применяется только когда `len(segment_payloads) >= 6` (строки 278–296). После
одного сбоя остаётся 5 успешных payload, поэтому выполняется ветка для 2–5
сегментов (строки 297–338), где ошибка merge возвращает пустой unstable payload
вместо сборки из валидных сегментов.

Требуется:

1. при partial failure и хотя бы одном валидном сегменте применять bounded
   compact-merge, а при его неуспехе — deterministic union/concatenation для
   **любого** числа успешных сегментов, а не только для `>= 6`;
2. запрещать публикацию episode, если нет ни `summary`, ни `key_points` при
   наличии содержательного transcript;
3. сохранять `partial_segment_failure` и список failed-сегментов;
4. добавить regression-тест на 5 успешных + 1 timeout и на merge timeout.

### P2 — `Куба/10`: необязательная поисковая фраза отброшена

Карточка валидна. Validator удалил фразу `прямой эфир`, отсутствующую в
исходном тексте, и сохранил это как `extraction_issues`. Это не блокирует
использование карточки, но должно остаться наблюдаемым и покрытым тестом.

## 3. Критерии приёмки

После реализации:

1. Все 129 входов имеют ровно один из статусов `published`, `review` или
   `failed` с причиной; «тихих» пропусков нет.
2. Для substantive-текста нет опубликованной карточки без `summary` и
   `key_points`.
3. Partial YouTube episode имеет непустую deterministic/compact выжимку,
   корректный `partial_segment_failure` и полный набор сегментных файлов.
4. Для `/2` результат проходит русский validator; для `/18` выбранный
   мультиязычный/review-маршрут явно отражён в контракте.
5. `main.py validate enriched --fail-on-error` завершается с нулевыми ошибками.
6. Acceptance-аудит проходит: нет пропущенных generic-карт, unstable episode,
   открытых checkpoint, рассинхрона FTS/registry или duplicate source IDs.
7. Профильные тесты сохраняют `92 passed` и включают новые regression-тесты
   для timeout, language routing и partial YouTube merge.

## 4. Порядок повторной проверки

1. Внести исправления и unit/regression-тесты.
2. Повторить только `/Китай/15`, `/Ультра левые и ультра правые/2`,
   `/Ультра левые и ультра правые/18` и episode `wXyMfwS5ZYs`.
3. Запустить validation, acceptance-аудит и пересборку FTS/registry.
4. Полный прогон всех карточек выполнять только после чистого targeted retry;
   повторно прогонять весь corpus из-за этих четырёх случаев не требуется.

## 5. Не входит в это ТЗ

Security/release-evidence hardening, Trusted Access, публикационные seal/lock
механизмы и включение новых feature flags. Этот документ касается только
практической полноты и корректности enrichment-прогона.

## 6. Выполненная реализация и фактическая приёмка

В `enricher/llm_enricher.py` добавлен bounded Codex retry: для timeout и
транспортных ошибок выполняются максимум две попытки одного backend-вызова.
Другие backend-ошибки не маскируются под успешный payload. Для содержательной
карточки, которая после repair всё ещё нарушает русский контракт, pipeline
делает одну повторную попытку job; окончательный отказ сохраняется в
`state/enrichment_progress.json` с `source_fingerprint`, типом исключения и
причиной и ставится в review queue как `enrichment_failure`.

Для `/18` выбран русский-only маршрут: если перевод/extraction проходит
validator, публикуется русская карточка; если нет — источник явно уходит в
review, без ослабления контракта и без фиктивной мультиязычной карточки.

В YouTube merge deterministic fallback теперь применяется при неуспешном
merge для любого количества валидных сегментов. Публикация пустого episode при
содержательном transcript запрещена. Добавлен regression-тест на пять
успешных сегментов и merge timeout.

Targeted recovery:

- `Ультра левые и ультра правые/18`: переобогащена, опубликована, русский
  summary, `quality_flags=[]`, `extraction_issues=[]`;
- `Ультра левые и ультра правые/2`: переобогащена, опубликована, 13 key points,
  без flags/issues;
- `Китай/15`: Codex повторно исчерпал bounded timeout. Восстановлена
  проверенная last-known-good карточка из backup; её `source_id`, текущий
  fingerprint и модель совпадают, а progress помечен
  `recovery_note=last_known_good_backup_after_bounded_codex_timeout`;
- `wXyMfw5ZYs`: повторён только нестабильный сегмент `0001`; получены все 6
  сегментов, manifest стал `complete`, episode содержит summary и 67 key points,
  unstable/partial flags отсутствуют.

Фактическая приёмка после recovery:

- `main.py validate enriched --fail-on-error`: **131/131 valid, 0 errors, 0 warnings**;
- acceptance audit `20260830T000235Z_r25_targeted_fix`: **passed=true, errors=[]**;
- generic cards: **127/127**; YouTube episodes: **4/4**; segments: **22/22**;
- progress entries: **127**; open checkpoints: **0**;
- card FTS: **131/131**; YouTube segment FTS: **22/22**;
- source registry: **133 sources, 129 normalized docs, 131 enriched cards, 32 references**;
- профильный suite после правок: **106 passed, 2 warnings, 9 subtests passed**.

Осталась одна наблюдаемая, неблокирующая `extraction_issue` у карточки
`Куба/10`: validator отбросил необязательную фразу, которой нет в исходном
тексте. Ошибок валидации, пропущенных входов и unstable episode больше нет.
