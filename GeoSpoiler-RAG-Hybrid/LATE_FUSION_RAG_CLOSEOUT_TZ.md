# Late-Fusion RAG V1 — ТЗ на исправление findings и полное закрытие

Дата: 2026-08-09
Статус: обязательный closeout-контракт
Базовый документ: `LATE_FUSION_RAG_TZ.md`
Режим до приёмки: `LATE_FUSION_ENABLED=false`

---

## 1. Назначение

Этот документ определяет единственный обязательный набор доработок, проверок и
артефактов, необходимый для полного закрытия Late-Fusion RAG V1 после ревью
текущей реализации.

Документ не заменяет базовое ТЗ и не меняет его архитектурные решения. Все
требования `LATE_FUSION_RAG_TZ.md` сохраняются. При конфликте этот документ
уточняет способ исправления найденного дефекта, но не расширяет продуктовый
scope V1.

Работа считается завершённой только после выполнения всех требований этого
документа и всех AC-1..AC-8 базового ТЗ.

## 2. Зафиксированные архитектурные ограничения

Следующие решения неизменны:

1. Enriched-first остаётся основным доказательным контуром.
2. LightRAG используется через `aquery_data()`, а не `aquery_llm()`.
3. Три retrieval-канала — LightRAG, Enriched Card FTS и YouTube Segment FTS —
   запускаются независимо.
4. LightRAG chunks создают source rank; entities и relationships только
   дополняют уже выбранный источник.
5. Wiki полностью исключена из retrieval, prompt, references и trace.
6. LightRAG закреплён на версии 1.5.4.
7. Финальный ответ генерируется одним Luna synthesis-вызовом.
8. Legacy query path сохраняется как прямой и нерекурсивный fallback.
9. Source mapping не использует basename-only resolution.
10. До полной приёмки production-флаг остаётся выключенным.

## 3. Исходное состояние и недействительность прежней приёмки

Сохранённый прогон `artifacts/late_fusion_ab_verified` допускается использовать
только как диагностический материал. Он не является acceptance-доказательством
после выполнения этого ТЗ, потому что:

- identity не хешировала содержимое всех untracked implementation-файлов;
- текущий RAG storage manifest уже отличается от сохранённой identity;
- blind packet не позволял полноценно проверить source links;
- scorer не применял все обязательные автоматические гейты;
- в принятом LF06 обнаружен выбор YouTube-сегментов не по исходному FTS-рангу.

Запрещено переносить прежние оценки, `accepted=true`, blind mapping или итоговый
score в новый acceptance run. После исправлений выполняется новый run с новым
`run_id`, `started_at`, identity и blind mapping.

## 4. Scope изменений

### 4.1. Обязательные production-файлы

- `loader/late_fusion.py`;
- `loader/query.py` — только если требуется для routing/fallback/response shape;
- `retrieval/card_fts.py`;
- `retrieval/source_registry.py` — только для scoped mapping/performance fixes;
- `config.py` и `.env.example` — только для timeout/token/feature configuration.

### 4.2. Обязательные verification-файлы

- `scripts/late_fusion_ab.py`;
- `tests/test_late_fusion.py`;
- `tests/test_late_fusion_ab.py`;
- существующие затронутые loader/source-registry/card-FTS/CLI tests;
- `README.md` и `ARCHITECTURE.md` при изменении фактического поведения.

### 4.3. Вне scope

- изменение Enriched v2 schema;
- изменение ingest pipeline;
- включение Wiki;
- новый reranker;
- отдельный citation repair LLM;
- изменение frozen набора LF01..LF10;
- удаление legacy path;
- перестройка корпуса или индексов, если она не нужна для исправления
  обнаруженного query-time DDL.

## 5. P1 — целостность Enriched source attribution

### 5.1. Обязательное поведение

После загрузки и schema-валидации `EnrichedCardV2` код обязан сравнить:

```text
card.provenance.source_id == candidate.source_id
```

Сравнение выполняется всегда, когда candidate source ID известен.

При несовпадении:

1. карточка не попадает в evidence;
2. candidate не получает content, URL или passport этой карточки;
3. trace фиксирует `card_source_id_mismatch` с candidate key, ожидаемым и
   фактическим source ID, но без полного содержимого карточки;
4. selection продолжает hydration следующего кандидата;
5. выполняется обычный backfill до `LATE_FUSION_MAX_SOURCES`.

Нельзя исправлять mismatch подменой candidate source ID на значение карточки:
это скроет повреждение FTS/registry identity.

### 5.2. Дополнительная проверка пути

До чтения карточки canonical path обязан:

- находиться под `ENRICHED_DIR`;
- указывать на обычный файл ожидаемого типа;
- не разрешаться только по basename;
- не проходить через path traversal или symlink/junction за пределы разрешённого
  корня.

## 6. P1 — корректный выбор YouTube-сегментов

### 6.1. Rank contract

Для каждого FTS hit сохраняется исходный 1-based rank до любой группировки или
сортировки. Parent rank равен минимальному rank его валидного сегмента.

Сегменты внутри одного `parent_source_id` выбираются строго по:

```text
1. original_fts_rank ascending
2. segment_index ascending, если rank равен
3. segment_id ascending
```

Сортировка по timeline не может заменять FTS rank.

### 6.2. Validation и backfill

Алгоритм для каждого видео:

1. пройти его сегменты в исходном FTS-порядке;
2. загрузить JSON;
3. проверить `YouTubeSegmentCardV2`;
4. проверить совпадение `parent_source_id`;
5. проверить наличие допустимого содержательного evidence;
6. добавить валидный сегмент;
7. продолжать до трёх валидных сегментов либо исчерпания списка.

Срез `[:3]` до валидации запрещён. Broken или mismatched segment не занимает
slot и должен быть заменён следующим FTS hit того же видео.

`start_url`, `start_seconds` и `end_seconds` берутся только из валидированной
segment card/канонического metadata. Таймкоды нельзя восстанавливать догадкой.

### 6.3. Trace

Для каждого выбранного YouTube-сегмента сохраняются:

- `segment_id`;
- `parent_source_id`;
- `original_fts_rank`;
- `segment_index`;
- validation status;
- причина skip, если сегмент отклонён;
- факт backfill.

## 7. P1 — graph context не является самостоятельным evidence

Только первое появление источника в ordered LightRAG chunks создаёт rank канала
`lightrag`.

Entities и relationships:

- не создают candidate;
- не создают или улучшают RRF rank;
- не резервируют source slot;
- не позволяют источнику пройти hydration без card/chunk/segment evidence;
- прикрепляются только к уже финально выбранному source, если canonical linkage
  однозначно совпадает;
- удаляются, если не удалось связать их с выбранным source;
- маркируются в prompt как вспомогательный контекст, недостаточный для
  подтверждения факта без `[S#]` source evidence.

Graph-only retrieval при отсутствии chunks/cards/segments считается пустым
evidence, а не успешным источником.

## 8. P2 — независимость retrieval-каналов и read-only FTS

### 8.1. Timeout каждого канала

Каждый из трёх retrieval-каналов обязан иметь отдельный timeout. Timeout FTS
должен покрывать весь `asyncio.to_thread()` вызов и не отменять результаты двух
остальных каналов.

Результат каждого канала приводится к `RetrievalChannelResult` со следующими
обязательными полями:

```text
name
status = success | empty | error | timeout
duration_ms
result_count
error_type
error_message_safe
```

`duration_ms` записывается для всех статусов. Полный traceback, credentials и
source contents в trace не записываются.

### 8.2. Query-time FTS только читает

`search_card_index()` и `search_youtube_segments()` не должны выполнять:

- `CREATE TABLE`;
- `CREATE VIRTUAL TABLE`;
- `ALTER`;
- rebuild/migration;
- иные DDL/DML операции.

Schema creation/migration переносится в явный ingest/init/preflight path.
Query-time поиск открывает существующий index для чтения и возвращает типовую
ошибку/empty degradation, если схема отсутствует или несовместима.

Два параллельных FTS-поиска по существующему SQLite index не должны получать
write lock или изменять mtime/содержимое базы.

## 9. P2 — canonical URL validation

Source считается citable только если после parsing:

1. scheme равен `http` или `https`;
2. hostname непустой;
3. URL не содержит credentials;
4. значение не является control-character/prompt payload;
5. URL взят из валидированного passport/card/segment metadata, а не из
   сгенерированного ответа;
6. после нормализации URL остаётся непустым и детерминированным.

`javascript:`, `data:`, `file:`, bare paths и произвольные непустые строки
отклоняются.

Если основной URL кандидата невалиден, разрешён следующий валидный canonical URL
этого же source. Если валидного URL нет, source исключается и выполняется
backfill по исходной candidate queue.

References и prompt используют один и тот же нормализованный набор URL.

## 10. P2 — полный Enriched formatter contract

Formatter обязан передавать все разрешённые базовым ТЗ поля, когда они доступны
и помещаются в budget:

- title, date, canonical URLs, `content_type`;
- summary;
- key points с type, importance и evidence;
- topics с явной пометкой metadata-only;
- theses со speaker, stance и evidence;
- quotes со speaker и context;
- events с type, dates, location, actors и description;
- relevant source chain: original source, forwarded from, external links;
- выбранные LightRAG chunks;
- валидные YouTube segment evidence и timestamps;
- только связанный с выбранным source graph context.

Дата берётся детерминированно: canonical passport value, затем разрешённый
fallback из валидированной provenance карточки. Выбранное происхождение даты
фиксируется в trace.

Запрещённые retrieval-only и служебные поля из §13.2 базового ТЗ не передаются.

## 11. P2 — детерминированный token budget

### 11.1. Реальный hard limit

До Luna вычисляется полная стоимость:

```text
system prompt + question + instructions + evidence context
```

Используется один зафиксированный tokenizer/estimator. Identity сохраняет его
имя и версию. `OUTPUT_TOKEN_RESERVE` применяется при проверке доступного context
window и не остаётся неиспользуемой конфигурацией.

Финальный запрос запрещено отправлять, если рассчитанный input превышает меньшее
из configured hard cap и доступного runtime limit.

### 11.2. Reduction algorithm

Запрещено обрезать целиком сериализованный XML source block по token prefix.

Для oversized reserved/единственного сильного source применяется строго:

1. удаление exact duplicate strings;
2. удаление вторичных source-chain details;
3. удаление topics;
4. удаление повторяющихся low-importance key points;
5. удаление повторяющихся medium-importance key points;
6. удаление второстепенных дублирующих theses;
7. усечение отдельного текстового поля на границе предложения;
8. добавление `[TRUNCATED_BY_BUDGET]` только в реально усечённое поле.

При сохранении evidence приоритет имеют matched YouTube fragments, числа,
numeric claims, события/даты, high-importance points, quotes и прямо релевантные
theses.

### 11.3. Token trace

Trace обязан содержать:

- tokenizer identity;
- immutable prompt cost;
- стоимость каждого полного и итогового source block;
- `dropped_source_ids` с причиной;
- `truncated_fields` как `{source_id, field_path, before_tokens, after_tokens}`;
- `estimated_input_tokens`;
- `max_input_tokens`;
- `output_token_reserve`;
- использованный runtime context limit, если известен.

## 12. P1 — доказательная A/B identity

### 12.1. Content-based implementation identity

Identity не может зависеть только от `git diff`. До первого case создаётся
детерминированный manifest всех scoped-файлов:

```text
relative_path
file_status = tracked | modified | untracked
size_bytes
sha256(content)
```

Manifest обязательно включает:

- production-файлы Late-Fusion;
- `scripts/late_fusion_ab.py`;
- оба специальных test-файла;
- базовое ТЗ и этот closeout-документ;
- config/requirements/docs, влияющие на runtime;
- frozen query definition.

Совокупный `implementation_manifest_sha256` рассчитывается по canonical JSON
manifest. Содержимое untracked-файлов хешируется наравне с tracked.

Git commit, scoped diff hash и dirty-tree manifest сохраняются дополнительно, но
не заменяют content hashes.

### 12.2. Полная run identity

Identity обязана содержать:

- `run_id`;
- `started_at` в UTC;
- run seed;
- implementation manifest и его hash;
- git commit/diff/dirty manifest;
- hash обоих ТЗ;
- LightRAG version;
- active `LLM_PROFILE`;
- query-role и fallback-synth model/provider identity;
- effective query mode/profile;
- все relevant Late-Fusion config values и timeouts;
- tokenizer identity;
- source registry fingerprint;
- Card FTS и YouTube FTS fingerprints;
- RAG storage manifest fingerprint;
- Enriched corpus manifest fingerprint;
- frozen query-set hash;
- Python/runtime version и platform encoding metadata.

Секреты, API keys и полные credentials не сохраняются.

### 12.3. Immutable run

После создания identity перед каждым case и перед scoring повторно вычисляются
дешёвые content/config fingerprints. Любое изменение переводит run в
`invalid_identity`; продолжение и acceptance запрещены.

Для live acceptance предпочтительно чистое scoped состояние. Если используется
dirty tree, content manifest является immutable snapshot: изменять scoped-файлы
до завершения run нельзя.

## 13. P1/P2 — checkpoint, resume и Windows portability

### 13.1. Atomic writes

Все JSON/Markdown state artifacts записываются атомарно:

1. создать временный файл в том же каталоге;
2. записать UTF-8 без BOM;
3. flush и `fsync`, где поддерживается;
4. выполнить atomic replace;
5. только после replace отметить case completed.

После чтения checkpoint выполняется schema validation. Повреждённый или
неполный JSON нельзя молча перезаписывать.

### 13.2. Resume validation

Resume разрешён только при полном совпадении:

- run identity;
- seed;
- frozen query set;
- implementation/corpus/config fingerprints;
- completed case artifact hashes;
- blind mapping hash.

Смена seed требует нового run directory. Existing blind mapping нельзя
переиспользовать или частично перегенерировать.

### 13.3. Encoding

Все subprocess-вызовы явно используют UTF-8 decoding с контролируемой обработкой
ошибок. Harness обязан работать в стандартном Windows PowerShell без требования
вручную задавать `PYTHONUTF8=1`.

## 14. P1 — blind review package

Для каждого ответа A/B reviewer видит:

- question;
- полный answer;
- использованные citation IDs;
- для каждого `[S#]`: title, canonical URL, content type и timestamp/start URL,
  если применимо;
- предупреждение, если citation ID не встречается в ответе;
- никаких признаков, раскрывающих A/B pipeline до завершения review.

URLs должны быть кликабельными и взяты из immutable case artifact. Blind packet
не должен раскрывать, какой ответ является Late-Fusion, через имена файлов,
trace, порядок или metadata.

Reviewer выставляет пять сохранённых criterion values. Если пользователь явно
выбирает действие «применить эту оценку ко всем пяти критериям», harness обязан:

- записать все пять значений;
- сохранить `rating_entry_mode="apply_to_all"`;
- сохранить исходное одно пользовательское решение;
- не представлять его как пять независимо введённых решений.

После изменения реализации прежний blind review не переиспользуется.

## 15. P1 — acceptance scorer как fail-closed validator

До расчёта итогового score scorer обязан проверить:

1. identity текущего run совпадает с immutable snapshot;
2. присутствуют ровно LF01..LF10 без дублей;
3. каждый case имеет legacy и Late-Fusion result;
4. каждый case имеет статус completed;
5. Late-Fusion result имеет `pipeline="late_fusion"`;
6. ни один принятый case не имеет fallback;
7. references соответствуют реально переданным source blocks;
8. все использованные citation IDs известны;
9. при непустом evidence есть хотя бы одна citation;
10. каждый reference имеет валидный canonical HTTP(S) URL;
11. Wiki отсутствует в retrieval, prompt, trace и references;
12. token и channel trace заполнены;
13. blind mapping и rating artifacts проходят hash validation;
14. все пять критериев заполнены допустимыми значениями;
15. LF10 не содержит придуманного установленного финансирования;
16. полный automatic gate report зелёный.

При любой ошибке итог:

```text
accepted=false
acceptance_status=invalid_run | incomplete_run | failed_gate
```

Scorer не должен вычислять положительный acceptance только из ручной арифметики.

Специальные правила базового ТЗ сохраняются:

- non-worse минимум 9/10;
- materially better минимум 5/10;
- LF10 с выдуманным финансированием — автоматический fail независимо от score.

Автоматический LF10 gate может быть консервативным rule-based check плюс явный
reviewer checkbox `unsupported_financing_claim=false`. Неоднозначность требует
ручного fail-closed решения, а не автоматического pass.

## 16. Обязательная автоматическая тестовая матрица

Количество тестов не является критерием приёмки. Должен присутствовать и
проходить каждый перечисленный сценарий.

### 16.1. Source/card integrity

- совпадающий `provenance.source_id` принимается;
- mismatch отклоняется и вызывает backfill;
- mismatch не смешивает content и passport разных sources;
- card path за пределами `ENRICHED_DIR` отклоняется;
- basename-only mapping отсутствует;
- stale/missing/ambiguous registry metadata обрабатывается детерминированно.

### 16.2. YouTube

- best-3 выбираются по original FTS rank, а не timeline;
- известный regression fixture `ranks 3,0,5` не превращается в timeline `0,1,2`;
- broken top segment заменяется следующим ranked segment;
- parent mismatch заменяется следующим segment;
- максимум три валидных сегмента;
- parent rank равен лучшему валидному segment rank;
- segment-only source работает только с evidence и валидным URL;
- timestamps/start URLs не выдумываются.

### 16.3. Graph semantics

- entity без chunk/card/segment не создаёт candidate;
- relationship не создаёт rank;
- graph metadata прикрепляется только к выбранному связанному source;
- graph-only retrieval даёт empty evidence;
- graph-only statement не может получить самостоятельную citation.

### 16.4. Parallel retrieval

- все три канала стартуют до завершения любого;
- timeout Card FTS не отменяет LightRAG/YouTube;
- timeout YouTube FTS не отменяет LightRAG/Card;
- LightRAG timeout не отменяет FTS;
- error и empty различаются;
- status и `duration_ms` заполняются;
- query-time FTS не выполняет DDL/DML;
- параллельные FTS reads не изменяют SQLite file.

### 16.5. URL и references

- HTTP и HTTPS принимаются;
- `javascript:`, `data:`, `file:`, bare path, empty host и URL с credentials
  отклоняются;
- invalid primary URL использует следующий canonical URL;
- source без валидного URL исключается с backfill;
- prompt URL и reference URL совпадают;
- invented response URL вызывает fallback;
- unknown/missing citation вызывает fallback.

### 16.6. Formatter и token budget

- все разрешённые Enriched fields включаются;
- все запрещённые retrieval/service fields исключаются;
- duplicate reduction работает до truncation;
- reduction order соблюдается;
- high-priority numeric/date/event/quote evidence сохраняется раньше низкого;
- truncation происходит на границе предложения;
- marker ставится только в усечённое поле;
- trace называет конкретный `field_path`;
- immutable prompt cost включён в limit;
- output reserve участвует в context-window check;
- запрос сверх hard limit не отправляется;
- 30–40 sources и увеличенный budget работают без изменения кода.

### 16.7. Harness identity/resume

- tracked, modified и untracked content входят в manifest;
- изменение одного байта implementation-файла меняет identity;
- изменение harness/test/TЗ меняет identity;
- другой seed отклоняет resume;
- изменение corpus/config/model/tokenizer отклоняет resume;
- partial case нельзя объявить completed;
- повреждённый checkpoint отклоняется;
- atomic replace не оставляет accepted partial artifact;
- Windows locale без `PYTHONUTF8` поддерживается;
- старый blind mapping нельзя смешать с новым seed.

### 16.8. Scorer

- отсутствующий case вызывает `accepted=false`;
- duplicate case вызывает fail;
- pipeline, отличный от точного `late_fusion`, вызывает fail;
- любой fallback вызывает fail соответствующего accepted case;
- identity drift вызывает fail;
- неизвестная citation/невалидный URL/Wiki reference вызывает fail;
- незаполненный критерий вызывает fail;
- LF10 unsupported financing flag вызывает автоматический fail;
- валидный synthetic fixture правильно считает non-worse/materially-better;
- scorer повторно проверяет automatic gate report.

### 16.9. Routing и regression

- feature flag false сохраняет точное legacy behavior и public shape;
- feature flag true маршрутизирует в Late-Fusion;
- typed fallback вызывает legacy ровно один раз;
- fallback не рекурсирует;
- CLI defaults одинаково корректны при flag false и true;
- существующие loader/source-registry/card-FTS/CLI tests проходят;
- полный suite проходит при изолированном legacy profile;
- полный suite проходит при активном Late-Fusion profile.

## 17. Порядок выполнения

### Этап 0. Freeze

1. Установить `LATE_FUSION_ENABLED=false`.
2. Сохранить текущий dirty-tree manifest без изменения чужих файлов.
3. Зафиксировать baseline targeted и full tests.
4. Зафиксировать scoped file list.
5. Не изменять frozen LF01..LF10.

### Этап 1. Evidence correctness

1. Исправить card/source mismatch.
2. Исправить YouTube rank/validation/backfill.
3. Удалить самостоятельный graph rank/evidence.
4. Добавить строгую URL validation/backfill.
5. Добавить тесты разделов 16.1–16.3 и 16.5.

### Этап 2. Retrieval и formatting

1. Добавить per-channel timeout/status/duration.
2. Убрать query-time FTS DDL.
3. Довести Enriched formatter до полного контракта.
4. Реализовать field-aware token reduction и полный trace.
5. Добавить тесты разделов 16.4 и 16.6.

### Этап 3. A/B harness

1. Реализовать content-based identity.
2. Реализовать atomic checkpoint/resume validation.
3. Исправить Windows encoding.
4. Добавить links в blind packet.
5. Сделать scorer fail-closed.
6. Добавить тесты разделов 16.7–16.8.

### Этап 4. Regression gates

1. Запустить специальные Late-Fusion tests.
2. Запустить затронутый regression contour.
3. Запустить полный suite с `LATE_FUSION_ENABLED=false`.
4. Запустить полный suite с `LATE_FUSION_ENABLED=true` и изолированной тестовой
   конфигурацией без внешнего live вызова.
5. Запустить Ruff, compile/import smoke и `git diff --check`.
6. Исправить все failures, включая CLI regression.
7. Повторить весь этап после последней правки production/harness-кода.

### Этап 5. Immutable preflight

1. Сформировать новый content manifest.
2. Проверить corpus/index/config/model/tokenizer identity.
3. Проверить, что Wiki flags false.
4. Проверить доступность Luna и retrieval indexes.
5. Проверить rollback с flag false.
6. Создать новый empty run directory только после зелёного preflight.

### Этап 6. Новый live A/B

1. Выполнить LF01..LF10 на одной immutable identity.
2. Сохранять atomic checkpoint после каждого завершённого pair.
3. Не продолжать run после identity drift.
4. Сформировать новый blind packet с citations и links.
5. Получить оценки пользователя по всем пяти критериям; явное
   `apply_to_all` допускается только с audit metadata из §14.
6. Раскрыть mapping только после фиксации оценок.
7. Запустить fail-closed scorer и automatic validation.

### Этап 7. Rollout

1. Сформировать итоговый AC-1..AC-8 report со ссылками на артефакты.
2. Получить явное подтверждение пользователя именно нового run.
3. Только после подтверждения установить active `LATE_FUSION_ENABLED=true`.
4. Выполнить production smoke без изменения corpus/index.
5. Проверить один-command rollback через flag false и restart.
6. Сохранить итоговую runtime identity и результат smoke.

## 18. Обязательные команды и evidence report

Конкретные команды определяются существующим окружением проекта, но итоговый
отчёт обязан сохранять для каждой команды:

- полную командную строку без секретов;
- cwd;
- effective non-secret environment/profile;
- started/finished timestamps;
- exit code;
- summary passed/failed/skipped;
- путь к полному локальному логу;
- implementation identity, на которой команда выполнялась.

Минимальный набор evidence:

1. специальные Late-Fusion tests;
2. затронутые loader/source-registry/card-FTS/CLI tests;
3. полный suite с flag false;
4. полный suite с flag true;
5. Ruff;
6. import/compile smoke;
7. `git diff --check`;
8. automatic A/B validator;
9. blind review report;
10. rollback smoke;
11. active-runtime smoke после разрешённого включения.

Число вроде «99 тестов» без manifest конкретных тестов, команды, identity и
полного suite не является доказательством AC-2.

## 19. Финальные acceptance gates

### Gate A — correctness

- нет card/source mismatch attribution;
- YouTube best-3 соответствует original FTS rank и backfill;
- graph metadata не создаёт rank/evidence;
- URL validation fail-closed;
- token budget соответствует полному prompt contract.

### Gate B — automated verification

- вся матрица §16 реализована и зелёная;
- оба полных suite-профиля зелёные;
- Ruff/import/compile/diff checks зелёные;
- ни одного known P1/P2 finding не осталось.

### Gate C — immutable A/B

- новый run содержит полную content-based identity;
- ровно 10 completed cases;
- нет identity drift, partial case, fallback или Wiki;
- все citations и URLs валидны;
- non-worse не менее 9/10;
- materially better не менее 5/10;
- LF10 прошёл automatic и human unsupported-claim gates.

### Gate D — human approval

- blind review выполнен по новому run;
- reviewer видел citation/source links;
- mapping раскрыт после фиксации оценок;
- пользователь явно одобрил новый результат.

### Gate E — rollout/rollback

- flag включён только после Gate D;
- active-runtime smoke зелёный;
- flag false после restart возвращает legacy behavior;
- rollback не требует rebuild corpus/index;
- документация соответствует фактическому поведению.

Провал любого Gate A–E означает `accepted=false`.

## 20. Definition of Done

Кейс полностью закрыт только если одновременно:

1. выполнены все исправления §§5–15;
2. реализована и проходит вся тестовая матрица §16;
3. полный test suite зелёный и с выключенным, и с активным Late-Fusion routing;
4. новый live A/B выполнен на immutable content-based identity;
5. старые acceptance scores не переиспользованы;
6. все AC-1..AC-8 базового ТЗ подтверждены артефактами;
7. все Gate A–E зелёные;
8. пользователь явно одобрил новый blind report;
9. production flag включён только после одобрения;
10. rollback проверен;
11. итоговый отчёт содержит команды, identity, логи и ссылки на артефакты;
12. scoped implementation-файлы находятся под auditable version control либо
    их content hashes однозначно включены в сохранённый release manifest.

Timeout, частичный run, прежний `accepted=true`, скрытый fallback, только unit
tests, ручная арифметика без automatic gates или совпадение URL только по
непустой строке не являются доказательством завершения.
