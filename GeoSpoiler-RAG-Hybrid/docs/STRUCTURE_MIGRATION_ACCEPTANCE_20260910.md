# Матрица приёмки структурной миграции — 2026-09-10

`PASS` означает проверенный результат текущей миграции. Первоначальные `BLOCKED` по R24 сняты владельцем 2026-09-10 с принятием остаточного риска; непроведённые сценарии обозначены `NOT_RUN`.

## Функциональность

| ID | Статус | Доказательство |
|---|---|---|
| F01 | PASS | Editable и изолированный wheel; legacy/canonical module identity и entrypoints |
| F02 | PASS | Root/другой CWD/кириллица+пробелы; установленный пакет требует явный absolute root; нет runtime DB в package |
| F03 | PASS offline / NOT_RUN external ingestion | Текущие handler/media/YouTube/Instagram fixtures проходят; новый внешний ingestion mini-corpus не запускался |
| F04 | PASS | Resume/idempotency/checkpoint и interrupted-migration regressions |
| F05 | PASS | 100% файлов и SHA; 133 IDs и четыре generation fingerprints равны baseline |
| F06 | PASS | Card/YouTube FTS и registry counts; exact top-k snapshot равен baseline |
| F07 | PASS | Graph bytes/IDs/metadata и query-mode regressions сохранены; текущий graph участвовал в успешном live smoke 10/10 |
| F08 | PASS | RRF/dedupe/budget/fallback tests и официальный 10-case live smoke прошли |
| F09 | PASS | Controlled timeout, retry/fallback и reference-preservation tests |
| F10 | PASS с наблюдением | Все live citation/reference/Telegram/YouTube timestamp contracts прошли; ручная оценка нашла один P3-дефект имени «Ванков» вместо «Ланьков» |
| F11 | PASS | Реальный Streamlit browser: очередь, фильтр, ссылки, Wiki отсутствует |
| F12 | PASS | Полная копия, SQLite backup/integrity, stage/activate/rollback rehearsal |
| F13 | PASS | Карта 147 переносов, README по слоям, operations/data/eval/docs исправлены |
| F14 | PASS | Full pytest, Ruff, compileall, diff check, wheel inventory и secret scan |
| F15 | PASS | 5 повторов: import +1.8%, search +1.7%, результаты равны; лимит +20% |

## Wiki

| ID | Статус | Доказательство |
|---|---|---|
| W01–W03 | PASS | Fresh-process matrix: absent flags, false/true, false/false → effective false |
| W04 | PASS | Main/module/Windows/UI используют один root и disabled status |
| W05–W06 | PASS | Pipeline/query spies и regression suite: 0 Wiki service/search/DB при master=false |
| W07 | PASS | Все Wiki CLI подкоманды явно blocked при disabled flag |
| W08 | PASS | В браузере Wiki-вкладки нет, обычная очередь работает |
| W09–W10 | PASS | SQLite traps для существующей/отсутствующей DB: 0 open/create/write, нет новых projections/sidecars |
| W11 | PASS | Изолированные Wiki-on service/query/pipeline regressions проходят |
| W12 | PASS | Финальный свежий процесс: оба effective flags false; тестовый Wiki-on процесс не оставлен |

## Итог

Код, данные, совместимость, offline RAG, UI, упаковка, Wiki OFF и live answer smoke приняты. R24 больше не блокирует выполнение, но два P1 и один P2 сохранены как осознанно отложенный hardening. Внешний ingestion mini-corpus и cold/release publication в этой приёмке не запускались.
