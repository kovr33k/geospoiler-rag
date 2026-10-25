# GeoSpoiler RAG: ускорение заливки и порядок в GitHub

Дата: 25 октября 2026 года.

Статус: спецификация опубликована по `$to-spec` в [issue №1](https://github.com/kovr33k/geospoiler-rag/issues/1) с меткой `ready-for-agent`. Шесть продуктовых решений по заливке и границы тестирования согласованы пользователем 25 октября 2026 года. Настройка agent skills завершена с подтверждённым корневым `AGENTS.md`; трекер — приватный `kovr33k/geospoiler-rag`. Публикация не означает начала или завершения реализации.

Источник договорённостей: [журнал решений](INGESTION_SPEED_DECISIONS_20261025.md). Эта спецификация не переоткрывает отклонённые предложения о сокращении содержания постов и видео.

## Problem Statement

Пополнение личной базы GeoSpoiler RAG занимает лишнее время и расходует модельные вызовы на операции, которые не всегда нужны: перевод русского текста, обработку необязательных вложений и повторную обработку готовых данных. Последовательные этапы заставляют уже подготовленный материал ждать завершения всей партии. Смена модели может неожиданно запускать переработку пригодных карточек.

Пользователь хочет быстрее заливать новые материалы, сохраняя их содержимое, происхождение и возможность продолжить обработку после сбоя. Точность приёмки определяется наблюдаемым результатом, числом необходимых обращений к внешним системам и отсутствием потерь при возобновлении, а не обещанным процентом ускорения.

История проекта на GitHub распределена между старым публичным репозиторием и приватным снимком с актуальной рабочей веткой. Основная ветка приватного снимка имеет другую историю. Это затрудняет выбор исходной версии и интерпретацию CI. Пользователь хочет понятный новый старт именно для RAG и явно запрещает затрагивать остальные проекты.

## Solution

Реализовать согласованные изменения обычной заливки:

- Пропускать модельный перевод уверенно русского текста.
- Разрешить до трёх одновременных обращений к одному LLM-backend на весь конвейер.
- Отправлять каждый полностью сохранённый нормализованный материал в обогащение сразу, с ограниченной очередью и корректным возобновлением.
- Обрабатывать вложения выборочно, исходя из зависимости текста поста от медиа.
- При явном `load` добавлять новые документы, заменять изменённые и переиспользовать совпадающие успешно обработанные документы.
- Сохранять валидные карточки при смене модели, профиля или уровня рассуждений; обновлять их по изменениям материала и правил обработки либо по явному запросу.

Для GitHub подготовить новый приватный репозиторий RAG с одной понятной основной веткой `main`, актуальным снимком исходного кода и документации и полноценным CI. Новый репозиторий уже создан для трекера, но перенос кода, CI и архивация прежних репозиториев ещё не выполнены. После проверки переноса старые репозитории сохранить архивами. Изменения ограничить RAG.

Текущую модель Luna и профиль `luna` сохранить. Уровень рассуждений остаётся `xhigh` во всех задачах, где он сейчас используется. Не подбирать другую модель, не снижать и не разделять effort для перевода, извлечения или ответа. Ускорение достигается согласованными изменениями конвейера, а не сменой этих настроек.

Сборку контекста при вопросах, содержимое постов и видео, правила формирования ответа в рамках этой спецификации не менять. Большой контекст сам по себе не является основанием отбрасывать сведения.

## User Stories

1. As a RAG user, I want confidently Russian text to bypass model translation, so that an unnecessary model call does not delay ingestion.
2. As a RAG user, I want Ukrainian text to remain distinguishable from Russian, so that Cyrillic characters alone do not cause an incorrect translation bypass.
3. As a RAG user, I want foreign, mixed, and uncertain text to retain the necessary translation path, so that ingestion speed does not replace language correctness.
4. As a RAG user, I want translation changes to preserve source meaning and provenance, so that saved facts remain traceable to the submitted material.
5. As a RAG user, I want up to three independent model requests to overlap, so that a batch does not wait for one request at a time.
6. As a RAG operator, I want translation and enrichment to share one concurrency limit, so that each stage cannot create three additional requests of its own.
7. As a RAG operator, I want synchronous and asynchronous callers to respect the same backend limit, so that mixed execution cannot exceed the configured maximum.
8. As a RAG operator, I want failures and cancellation to release occupied request slots, so that the remaining batch can continue.
9. As a RAG user, I want the first ready normalized material to enter enrichment immediately, so that later materials do not delay it.
10. As a RAG operator, I want enrichment to consume only complete saved text and metadata, so that partially written input cannot produce a published card.
11. As a RAG operator, I want a bounded handoff queue, so that a slow model does not cause unbounded pending work.
12. As a RAG user, I want out-of-order completion to preserve earlier failed items, so that a later success cannot cause a checkpoint to skip missing work.
13. As a RAG user, I want interrupted ingestion to resume from saved progress, so that completed valid results do not need to be recreated.
14. As a RAG user, I want local indexes to reflect successfully saved results, so that completed materials become searchable even when another item fails.
15. As a RAG operator, I want ordinary run and explicit graph load to remain separate operations, so that ingestion does not silently rebuild or update the graph.
16. As a RAG user, I want self-contained post text to be enriched without analyzing its attachments, so that decorative or irrelevant media does not incur automatic processing.
17. As a RAG user, I want an attachment to be analyzed when the post explicitly depends on its contents, so that necessary media information enters the card.
18. As a RAG user, I want only the necessary media operations to run, so that speech, screenshots, and visual evidence do not all trigger the same expensive processing bundle.
19. As a RAG user, I want the media decision to avoid a separate classifier model call for every post, so that the decision itself does not erase the intended savings.
20. As a RAG user, I want media-only posts and vague captions to remain pending without automatic analysis, so that uncertain attachment value does not trigger automatic spending.
21. As a RAG user, I want skipped attachments to be marked as not analyzed, so that unseen content is not presented as proven unimportant or as a processing failure.
22. As a RAG user, I want source links and available attachment identifiers to be retained, so that the original material can be located later if it remains available.
23. As a RAG user, I want useful post text to remain searchable when media is skipped, so that the media policy does not hide the post itself.
24. As a RAG user, I want an actual failure of requested media processing to remain distinguishable from a policy skip, so that an unavailable attachment is not reported as successfully understood.
25. As a RAG user, I want the limitations of unprocessed media to be explicit, so that I understand that information found only inside it cannot be retrieved through the text index.
26. As a RAG user, I want explicit graph load to insert new source documents, so that newly enriched materials become available to graph retrieval.
27. As a RAG user, I want changed source documents to replace their prior graph representation, so that updated information does not create duplicate logical documents.
28. As a RAG user, I want unchanged successfully processed documents to be reused, so that ordinary graph loading does not delete and insert them again.
29. As a RAG user, I want incomplete or failed graph documents to be retried, so that an existing document identifier does not falsely count as completed work.
30. As a RAG operator, I want reusable graph documents to retain required source metadata updates, so that skipping content processing does not break source resolution.
31. As a RAG operator, I want stable logical source identity during graph updates, so that editing a source does not create an unrelated duplicate.
32. As a RAG operator, I want explicit force and rebuild operations to remain available, so that changed extraction rules or recovery can intentionally reprocess documents.
33. As a RAG operator, I want a missing local card to avoid silently triggering a new graph deletion policy, so that ordinary incremental loading stays within the agreed add, update, and reuse behavior.
34. As a RAG user, I want changing the model alone to preserve a valid card, so that switching providers does not unexpectedly reprocess the corpus.
35. As a RAG user, I want changing reasoning effort alone to preserve a valid card, so that a runtime preference does not invalidate completed work.
36. As a RAG user, I want changing the backend profile alone to preserve a valid card, so that the reuse rule applies consistently across model configurations.
37. As a RAG user, I want existing results to retain their actual model provenance, so that reused work is not relabeled as generated by the current model.
38. As a RAG user, I want changed source content, significant metadata, and processing rules to invalidate affected results, so that reuse does not preserve outdated cards.
39. As a RAG user, I want missing, invalid, corrupted, and unsuccessful results to be processed again, so that a saved progress record cannot hide a broken output.
40. As a RAG user, I want completed valid YouTube generations to follow the same model-independent reuse policy, so that ordinary and segmented sources behave consistently.
41. As a RAG operator, I want YouTube publication and resume to preserve valid generation identity and atomicity, so that parent and segment artifacts remain consistent.
42. As a RAG owner, I want one clearly identified current repository and main branch, so that repository status and CI refer to the code I actually use.
43. As a RAG owner, I want a fresh private repository based on the current source snapshot, so that unrelated old branch histories do not complicate future work.
44. As a RAG owner, I want the accepted snapshot to include intended current source and documentation changes, so that a fresh start does not silently discard local work.
45. As a RAG owner, I want the old RAG repositories retained as archives after transfer verification, so that their history remains recoverable without competing with the active repository.
46. As a RAG owner, I want this checkout's remote configuration to identify the new active repository, so that future work goes to the intended project.
47. As an owner of multiple projects, I want all repository and CI changes to be restricted to RAG, so that my other projects remain untouched.
48. As an owner of multiple projects, I want shared account, billing, and Git configuration to remain outside this task, so that RAG cleanup cannot alter other projects indirectly.
49. As a RAG owner, I want CI to collect and run the complete offline pytest suite, so that standalone pytest tests are not silently omitted.
50. As a RAG owner, I want lint violations to fail CI, so that a successful workflow means the configured checks passed.
51. As a RAG owner, I want meaningful Windows and Linux checks, so that CI covers the working platform as well as the other supported environment.
52. As a RAG owner, I want an account-level Actions block reported separately from a test failure, so that an unstarted workflow is not mistaken for code verification.
53. As a RAG user, I want this ingestion work to leave answer context selection and content budgets unchanged, so that speed work does not discard evidence from posts and videos.
54. As a RAG user, I want answers to keep their existing source-grounding rules without a new per-answer verifier, so that this work does not add another model request to every question.
55. As a RAG operator, I want existing timing data to be used before new instrumentation is introduced, so that this task does not expand into an unnecessary monitoring project.
56. As a RAG owner, I want documentation to distinguish agreement, implementation, local verification, and actual CI execution, so that readiness claims match the evidence.

## Implementation Decisions

1. **Область и исходная версия.** Работать с фактическим текущим деревом GeoSpoiler RAG. Не смешивать изменения с другими проектами. Сохранять существующие локальные правки; публикация нового снимка требует явного учёта его состава. Эта спецификация описывает продуктовые изменения и перенос, а не разрешает скрытое удаление данных или незапрошенный полный прогон корпуса.

2. **Существующая архитектура.** Сохранить разделение ingestion, retrieval и answering, канонические модули и совместимые точки входа. Нормализованный материал, карточка Enriched v2, поколение YouTube, локальные индексы и документ LightRAG сохраняют свои роли. Не вводить новый универсальный движок конвейеров ради этой задачи.

3. **Локальный языковой допуск.** Перед модельным переводом определить, можно ли уверенно пропустить русский текст. Одна кириллица не является достаточным признаком. Для иностранного, смешанного и неопределённого текста сохраняется необходимый путь перевода. Конкретная библиотека определения языка этой спецификацией не выбирается.

4. **Общий предел обращений.** Начальный предел — три одновременных LLM-вызова на один конвейер, использующий данный backend. Перевод, обогащение, необходимые медиа-вызовы и допустимые повторы используют этот общий ресурс. Синхронные и асинхронные пути не должны независимо получать дополнительные квоты. Предел не означает трёх одновременных вызовов для каждого этапа и не меняет самостоятельную политику embeddings.

5. **Количество работников.** Рабочие задания обогащения согласованы с общим пределом. Ранее согласованные начальные значения — `ENRICHMENT_CONCURRENCY=3` и `CODEX_LLM_MAX_CONCURRENCY=3`. Наличие работников само по себе не разрешает превысить backend-лимит.

6. **Передача готового материала.** Материал допускается к обогащению после полного сохранения и проверки текста и метаданных. Не ждать завершения нормализации всей партии. Использовать ограниченную очередь или существующий эквивалент; завершение этапа и остановка не должны оставлять потерянные задания или занятые слоты.

7. **Возобновление и запись состояния.** Один координатор сохраняет прогресс. Более поздний успешный результат не скрывает раннюю незавершённую обработку. Не публиковать неполные карточки. Сохранённые пригодные результаты переживают сбой другого материала; локальные индексы обновляются согласованно по готовым результатам.

8. **Граница `run` и `load`.** Обычный `run` завершает заливку обновлением локальных индексов и не вызывает загрузку графа автоматически. Отдельные команды нормализации, обогащения и загрузки сохраняют работоспособность и действующие контракты ошибок.

9. **Выборочный режим медиа.** Наличие фотографии, видео, аудио или документа не является достаточной причиной для скачивания и анализа. Самодостаточный текст обогащается самостоятельно. Явная зависимость от вложения запускает необходимые операции. Посты без содержательного текста и с неопределённой подписью сохраняют идентификацию источника и статус необработанного вложения без автоматического анализа.

10. **Решение о необходимости вложения.** Предпочесть локальные правила либо уже необходимый вызов текстового обогащения; отдельный LLM-классификатор для каждого поста не добавляется. Не считать, что существующий строгий семантический payload уже поддерживает маршрутизацию медиа. Если требуются новые сведения о маршрутизации, их контракт и владелец должны быть явными и совместимыми с сохранёнными данными. Не поручать модели создание служебных quality flags или происхождения результата.

11. **Скачивание после решения.** Проверить все фактические пути, включая отдельный маршрут фотографий. Нельзя реализовать выборочный анализ, оставив безусловную предварительную загрузку всех вложений. Использовать доступную идентификацию вложения до получения его содержимого.

12. **Минимально необходимая операция.** Для речи использовать транскрипцию, для текста на изображении — подходящее извлечение, для визуального смысла — необходимый визуальный анализ. Выбранное вложение может потребовать последующего дополнения карточки. Не обещать, что его полноценная обработка всегда укладывается в один модельный вызов.

13. **Честное состояние вложения.** Намеренный пропуск отличается от ошибки, успешного анализа и отсутствия медиа. Сохранять ссылку на пост, тип вложения и доступные идентификаторы. Не обозначать невиденное содержимое как доказанно несущественное. Полезный текст поста остаётся пригодным для поиска. Реальные ошибки выбранного анализа сохраняют диагностику и возможность предусмотренного повторения.

14. **Ограничение полноты медиа.** По подписи нельзя гарантировать обнаружение всех важных вложений. Информация только внутри необработанного медиа не участвует в текстовом поиске. Ссылка не гарантирует будущую доступность оригинала. Эти ограничения приняты для экономного режима; новый анализ на каждом поисковом запросе не добавляется.

15. **Инкрементальный обычный `load`.** Сопоставлять стабильную идентичность, подготовленное содержимое и успешность обработки до удаления документа. Совпадающий успешно обработанный документ переиспользуется, новый добавляется, изменённый заменяется. Неуспешные и неполные документы повторяются. Не терять обязательные обновления метаданных источника при пропуске обработки содержимого.

16. **Изменение правил графа.** Пропуск совпадающего текста не должен блокировать явно запрошенную переработку после изменения правил извлечения или восстановления. Сохранить принудительный режим. Не добавлять автоматическое удаление графовых документов только из-за отсутствия карточки в очередном обходе.

17. **Актуальность карточки и происхождение.** Разделить валидность результата и информацию о модели, которая его создала. Смена модели, backend-профиля или уровня рассуждений сама по себе не инвалидирует готовую валидную карточку. Не переписывать её происхождение на текущие настройки. Это политика на случай самостоятельной будущей смены настроек пользователем, а не задание сменить Luna или `xhigh` в этой работе.

18. **Разрешённая повторная обработка.** Основания — изменение исходного материала или значимых входных метаданных, правил извлечения, обязательной схемы либо контракта; отсутствие или невалидность результата; неуспешное завершение; явный force. Не объявлять потерянную карточку готовой только по прогрессу.

19. **YouTube.** Применить ту же политику к пригодной опубликованной YouTube-генерации, сохранив согласованность родителя, сегментов, manifest и checkpoint. Не подменять реальное происхождение. Проверки целостности, атомарной публикации и восстановления не ослабляются ради переиспользования.

20. **Новый репозиторий.** План переноса — новый приватный репозиторий RAG с основной веткой `main` и новым исходным снимком без переноса старой Git-истории. Переносить актуальный код и документацию, а рабочие базы, локальные секреты и runtime-данные сохранять в их существующих локальных местах. Не сбрасывать текущую рабочую копию ради создания снимка.

21. **Порядок переноса.** Сначала проверить состав и соответствие нового снимка рабочей версии; затем настроить CI нового дерева и рабочий remote этой локальной копии. Старые репозитории `kovr33k/RAG-GeoSpoiler` и `kovr33k/geospoiler-rag-hybrid-backup` архивировать после подтверждённого переноса. Сохранить доступ к истории. Удаление репозиториев не является необходимым или согласованным этапом.

22. **Строго только RAG.** Разрешены изменения новой копии RAG, двух названных старых репозиториев RAG и их проектных настроек. Запрещены изменения других проектов, их веток, CI, remotes и локальных файлов. Не менять глобальный Git config, общие политики организации, общие GitHub Actions или настройки оплаты: они могут влиять на остальные проекты.

23. **CI актуального дерева.** Использовать полный offline pytest, который собирает также отдельные pytest-функции; Ruff должен завершать проверку ошибкой при нарушениях. Нужны проверки Windows и Linux для поддерживаемых сред. Конкретные версии runner и Python выбираются при реализации по текущим требованиям проекта, без изменения продуктового поведения ради удобства CI.

24. **Достоверность статуса CI.** Успешный локальный прогон, старт Actions и успешное завершение CI — разные факты. Последний известный запуск приватного репозитория не начал выполнять шаги из-за ограничения оплаты или расходов. Новый репозиторий не устраняет такое ограничение автоматически; общие платёжные настройки вне области работ. Не обозначать unstarted CI как успешную проверку или как доказанную ошибку кода.

25. **Сохранение модельных настроек и поведения вопросов.** Текущая модель Luna, профиль `luna` и effort `xhigh` сохраняются для существующих задач. Отдельные более низкие или разные уровни рассуждений для перевода, извлечения и ответа не вводятся. Действующий контракт Late Fusion и Enriched v2 сохраняется. Не менять бюджеты контекста, число источников, полноту выбранных карточек и сегментов или prompt ответа для этой оптимизации. Базовая дедупликация уже существует. Не добавлять внешний фактчекинг или новую модельную проверку каждого ответа. Имеющиеся тайминги использовать до появления конкретной необходимости расширения.

## Testing Decisions

**Границы подтверждены пользователем 25 октября 2026 года: «Да, эти границы подходят».** Основной уровень — существующий командный интерфейс `run` и отдельный `load`, с реальной внутренней обработкой в изолированных временных данных и управляемыми внешними границами Telegram, LLM и LightRAG. Это две операции одного существующего интерфейса; объединять их в новую команду ради одного тестового шва нельзя, поскольку их раздельность является требованием.

Хороший тест проверяет опубликованные файлы, валидность контрактов, состояние возобновления, поисковую доступность и видимые обращения к внешним границам. Он не закрепляет названия новых внутренних классов, число вспомогательных методов или конкретную структуру очереди. Для проверки отсутствия лишнего перевода наблюдать именно обращения роли перевода, а не требовать отсутствия необходимого вызова обогащения.

Сетевые и модельные ответы задаются на существующих внешних границах. Не подменять саму проверяемую нормализацию, маршрутизацию медиа или загрузчик простым успешным результатом. Новые сервисные обёртки и тестовые API без необходимости не вводить.

Сценарии смены модели, профиля и effort проверяются только в изолированных тестовых настройках с управляемыми ответами. Они не требуют менять рабочую Luna или `xhigh` и не являются экспериментом по выбору другого уровня рассуждений.

| Сценарий | Наблюдаемое условие приёмки |
| --- | --- |
| Русский, украинский, иностранный и смешанный текст | Для уверенно русского материала нет вызова перевода; остальные случаи проходят необходимую обработку и дают валидный результат |
| Не менее четырёх готовых заданий | Независимые обращения действительно пересекаются по времени; одновременно активно не более трёх на общем backend |
| Смешение нормализации, обогащения и обоих способов вызова backend | Общий предел не увеличивается из-за отдельных этапов, потоков или асинхронных путей |
| Сбой и отмена обращения | Слоты освобождаются; оставшиеся задания не зависают и корректно завершаются либо сохраняют состояние повторения |
| Один готовый материал при заблокированной дальнейшей нормализации | Его обогащение и сохранение могут завершиться до окончания нормализации остальных материалов |
| Неполная пара текста и метаданных | До готовности входного контракта карточка не публикуется |
| Ранний сбой и поздний успех | Возобновление повторяет незавершённый материал; checkpoint не скрывает пробел; готовый валидный результат не теряется |
| Больше материалов, чем вместимость очереди | Очередь создаёт обратное давление; партия завершается без потерь и неограниченного накопления |
| Самодостаточная подпись с фотографией, видео или аудио | Текст сохраняется и индексируется; ненужные скачивание и анализ не вызываются, включая отдельный маршрут фотографий |
| Явная зависимость от скриншота, карты или записи | Вызываются необходимые операции; извлечённые сведения входят в валидную карточку с происхождением |
| Медиа без содержательной подписи | Нет автоматического анализа; сохраняются источник и честный статус необработанного вложения |
| Пропуск по политике и реальная ошибка медиа | Состояния различимы; намеренный пропуск не получает диагностику неуспешного анализа |
| Повторный обычный `load` совпадающего успешного документа | Нет его удаления, повторной вставки и повторного извлечения; нужные метаданные источника сохраняются |
| Новый, изменённый, неуспешный и неполный графовый документ | Каждый проходит соответствующий путь добавления, замены или повторения со стабильной логической идентичностью |
| Явный force и значимые изменения правил | Нужная повторная обработка возможна; инкрементальность не блокирует намеренный rebuild |
| Смена модели, профиля и effort при неизменном валидном результате | Карточка или опубликованная YouTube-генерация переиспользуется; сохранено фактическое происхождение |
| Изменение материала, метаданных, правил либо повреждение результата | Затронутый результат не считается пригодным по одной записи прогресса и корректно перерабатывается |
| YouTube: публикация, прерывание и возобновление | Родитель, сегменты и manifest согласованы; повреждённая или частичная генерация не выдаётся за завершённую |
| Обычный `run` | Не вызывает явную загрузку графа; локальные индексы отражают сохранённые пригодные результаты |
| Перенос GitHub | Новый снимок соответствует согласованному актуальному дереву; сохранён доступ к старой истории; изменены только объекты RAG |
| CI | Собраны самостоятельные pytest-тесты, нарушения Ruff блокируют результат, проверки Windows и Linux относятся к перенесённой версии |

Для конкурентных сценариев использовать управляемые события и наблюдение внешних обращений; не строить приёмку на случайных задержках или обещанном ускорении в секундах. Сопоставление скорости до и после выполнять на одинаковых материалах и одинаковом backend, отдельно от доказательства корректности.

**Существующие образцы тестирования.** В проекте уже есть командные проверки обновления индексов и явной загрузки, временные нормализованные и Enriched v2-данные, тесты ограниченной конкурентности обогащения, fake LightRAG с наблюдением вставок и удалений, проверки повторного использования обработанных документов, состояния native media и целостности YouTube-поколений. Расширить эти подходы на наблюдаемое поведение. Старые проверки, требующие регенерации только по смене модели или профиля, обновить под новое утверждённое требование, сохранив проверки схемы, входных изменений и повреждений.

После реализации — целевые регрессии, полный штатный offline-набор и проверки нового CI. Повторять уже пригодную проверку только при новых изменениях или незакрытом основании для повторения. До итоговой готовности не запускать массовую платную переработку корпуса. Выборочный режим медиа дополнительно проверить на небольшой реальной выборке пересылаемых постов; документировать пропуски важных вложений, а не скрывать их за технически зелёными тестами.

## Out of Scope

- Любые изменения остальных проектов, репозиториев, рабочих копий, настроек CI и общих настроек учётной записи GitHub.
- Удаление двух старых репозиториев RAG, переписывание их истории и перенос актуального приватного кода в публичный доступ.
- Автоматическое обновление текущего тестового графа или удаление документов из него после локального удаления поста.
- Сокращение содержания постов и видео, новый отбор коротких фрагментов, уменьшение числа источников и лимит контекста 20–30 тысяч токенов.
- Новый фактчекинг, внешний поиск истины исходного поста, дополнительный LLM-валидатор на каждый ответ и отдельная кампания оценки цитирования.
- Смена текущей Luna, профиля `luna` или effort `xhigh`, подбор иной модели, снижение либо разделение уровня рассуждений по задачам и обещание конкретного численного ускорения без сопоставимых замеров.
- Автоматический анализ вложений всех найденных карточек при каждом вопросе, новый пользовательский режим анализа медиа по запросу и гарантированная поздняя доступность оригиналов.
- Универсальная политика срока хранения медиабинарников и кеша; текущая задача не требует хранить все вложения постоянно.
- Большая система мониторинга, новые тайминги всех стадий без конкретной необходимости, кеширование финальных ответов и новый постоянно работающий сервер LightRAG.
- Переработка опциональной Wiki, общей архитектуры и ранее принятых рисков служебного контура вне затронутых контрактов.
- Массовая регенерация существующего корпуса, live-вызовы модели или изменение рабочих баз на этапе подготовки этой спецификации.

## Further Notes

Основные термины соответствуют существующим архитектурным и информационным контрактам проекта: нормализованный материал, карточка Enriched v2, происхождение источника, опубликованное поколение YouTube, локальные индексы и явный документ LightRAG. Отдельный доменный словарь или ADR в рабочем дереве при подготовке не обнаружен; новые термины вместо существующих не вводились.

По подтверждённому выбору пользователя создан [новый приватный репозиторий](https://github.com/kovr33k/geospoiler-rag) `kovr33k/geospoiler-rag`, первоначально пустой и предназначенный для трекера. Его идентификатор задаётся в командах tracker явно; существующие локальные remotes пока сохранены. Перенос кода и создание рабочей ветки с исходным снимком относятся к отдельному этапу. Состав актуального снимка включает осознанный учёт имеющихся локальных правок и новой документации, без их незаметного сброса или автоматического включения неподтверждённых изменений в commit.

Границы тестирования подтверждены. `/setup-matt-pocock-skills` завершён: выбран корневой `AGENTS.md`, записаны правила GitHub-трекера и доменных документов, создана метка `ready-for-agent`. Полный набор triage-ролей не вводится, поскольку отдельный triage-skill не установлен. Спецификацию публиковать именно в выбранном RAG-трекере с меткой `ready-for-agent`; это не означает начала реализации или её завершения.
