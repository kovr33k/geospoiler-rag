# Прямое ревью структурной миграции — 2026-09-10

Результат: **0 открытых P0 / 0 открытых P1 / 0 открытых P2 в области структурной миграции**.

Проверка проведена отдельным проходом после реализации: сопоставлены требования ТЗ, финальный diff, import graph, path/config contract, миграционный journal, package contents, data snapshots, Wiki routes, UI и документация. Это не ревью готовности release-evidence R24.

## Исправленные findings

| Приоритет | Finding | Исправление и доказательство |
|---|---|---|
| P1 | `run_pipeline.ps1` при старте LightRAG UI безусловно перезаписывал process env значениями `.env` | `.env` заполняет только отсутствующие process keys; PowerShell parser и status launcher проходят |
| P1 | Rollback мог восстановить исходный `.env` поверх новых ручных настроек, сделанных после activation | Перед восстановлением принимается только исходный или зафиксированный active hash; любое другое содержимое требует ручного согласования |
| P2 | URL reviewer отображался текстом и не выполнял UI-критерий открытия источника | HTTP(S) URL стал ссылкой с `noopener noreferrer`; href и остальные поля экранируются; unit и браузерная проверка прошли |
| P2 | Operations/README/Data Contracts содержали активные инструкции со старыми физическими путями | Текущие инструкции переведены на `data/`; старые адреса оставлены только там, где описывают logical identity или исторические backup paths |

## Проверенные риски без открытого finding

- Старые импорты и entrypoints указывают на канонические модули, а не на вторую копию состояния.
- Release producer wrappers входят в effective implementation inventory, поэтому перенос не ослабляет fingerprint/security boundary.
- Старые абсолютные source paths сохраняют document IDs и YouTube fingerprints через layout marker; внешние test roots не алиасируются в рабочий `data/`.
- Миграция не следует symlink/junction, не переключает непроверенную копию, не теряет target-записи при rollback и не переносит live SQLite sidecars.
- Wiki master switch проверяется до DB/service работы. Существующее Wiki-состояние сохраняется, отсутствующее не создаётся.
- Wheel не выбирает site-packages как data root: без абсолютного `GEOSPOILER_PROJECT_ROOT` он завершается с понятной ошибкой.
- Канонический `retrieval` не зависит от `answering` или CLI. Wiki имеет отдельный слой и не включена по умолчанию при отсутствующем master flag.

## Внешние blockers

Открытые R24 P1/P2 относятся к prepared publication и SQLite recovery release-evidence. Они существовали до миграции и не исправлялись этим ТЗ. На момент этого ревью они блокировали release/cold/live execution; 2026-09-10 владелец проекта принял остаточный риск и снял обязательный gate. Точка продолжения hardening сохранена в R24.
