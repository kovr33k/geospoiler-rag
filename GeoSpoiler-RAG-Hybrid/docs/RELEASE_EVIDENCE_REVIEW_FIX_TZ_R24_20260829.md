# RELEASE EVIDENCE REVIEW FIX TZ R24

Дата: 2026-08-29  
Статус: **РИСК ПРИНЯТ ВЛАДЕЛЬЦЕМ 2026-09-10; ОБЯЗАТЕЛЬНЫЙ GATE СНЯТ; HARDENING ОТЛОЖЕН**
Основание: независимое прямое ревью исполнения R23, без субагентов.

Решение от 2026-09-10: перечисленные ниже `P1-1`, `P1-2` и `P2-1` не
исправлены и остаются техническим долгом release-evidence контура. Владелец
проекта снял их блокирующий статус, поскольку они не участвуют в обычных
`run`, `enrich`, `query` и `search`. Live QA, cold/release execution и выпуск
отчётов больше не блокируются этим документом. Это принятие риска, а не
подтверждение выполнения Definition of Done R24 и не гарантия защищённости от
конкурентной модификации prepared tree или неоднозначного SQLite recovery.

При возвращении к hardening продолжить с раздела 3: frozen prepared
publication, marker schema v2 для SQLite и реальные platform/runtime
regressions. Исходная точка остановки сохранена в разделах 2 и 6.

## 1. Цель

Закрыть оставшиеся окна неоднозначной публикации prepared-tree и SQLite family,
а также сделать regression-доказательства эквивалентными заявленному production
контракту.

На момент ревью 2026-08-29 до выполнения этого ТЗ были запрещены
cold/live/release execution, создание pass receipt и трактовка зелёного R23
targeted suite как разрешения релиза. Решением владельца от 2026-09-10 этот
обязательный запрет снят с принятием описанного выше остаточного риска.

## 2. Подтверждённые дефекты

### P1-1. Sealed publication остаётся изменяемой во время commit

`PreparedPublicationTree` удерживает только handle/FD корневой директории
(`scripts/release_identity.py:178-188`, `447-492`). `seal()` вычисляет digest и
закрывает все file descriptors, открытые внутренним snapshot reader. До конца
commit не удерживаются handles всех sealed regular files и не доказывается
private/exclusive staging namespace.

На Windows root handle открыт с `FILE_SHARE_WRITE` (`release_identity.py:2030-2057`).
На POSIX проверяются root FD и один post-rename inventory, но owner/mode private
staging не проверяются. Поэтому конкурентный writer может менять содержимое или
namespace между seal, pre-rename check и post-rename check. Одиночная сверка в
`commit()` (`release_identity.py:240-294`) обнаруживает только состояние во время
этой сверки и не закрывает ABA: временно возвращённый sealed state может дать
обычный success, после чего изменённый canonical tree останется без quarantine.

Это не выполняет обязательный контракт R23 section 3.2: удержание sealed child
handles, private staging container и fail-closed при недоказуемом threat model.

### P1-2. SQLite recovery принимает повреждённый или отсутствующий main за original

Transition marker хранит `redacted_sha256`, но не хранит hash исходного main
(`scripts/security_artifact_redaction.py:699-715`). Recovery классифицирует любое
состояние, не совпавшее с `redacted_sha256`, как `original_family_restored`
(`security_artifact_redaction.py:543-553`).

При повреждённом, подменённом или отсутствующем main helper восстанавливает retired
sidecars и удаляет marker (`security_artifact_redaction.py:581-592`). Тем самым он:

- соединяет sidecars с main, принадлежность которого не доказана;
- теряет durable recovery evidence;
- сообщает успешное восстановление вместо stable recovery-required outcome.

Это нарушает R23 section 3.3.5: неоднозначное состояние нельзя угадывать или
очищать.

### P2-1. R23 regressions не доказывают production contract

`tests/test_release_evidence_r23.py:181-190` подменяет `release_identity.os.name`
на `posix`, хотя R23 section 3.4 прямо запрещает использовать monkeypatch
`os.name` как доказательство платформенной безопасности.

Static test `tests/test_release_evidence_r23.py:354-379`:

- проверяет только два файла и только direct `os.replace`;
- не запрещает `Path.replace` и directory-вызов общего `durable_replace` без
  preflight digest;
- не доказывает AST-порядок `snapshot -> scan/preflight -> seal(expected) ->
  commit -> receipt`;
- не содержит runtime integration для bundle/final/failure publication с
  мутацией после seal и до возврата success.

Поэтому `11 passed` подтверждает существующие assertions, но не закрытие P1-1 и
P1-2.

## 3. Требуемая реализация

### 3.1 Frozen prepared publication

1. Ввести `SealedPublicationSnapshot`, содержащий canonical inventory digest,
   root identity, полный список lexical entries, node types, identities, sizes и
   content hashes.
2. `seal(expected_inventory_sha256)` обязан принимать только digest успешного
   analyzed snapshot. В release producers запрещён разрешающий `seal()` без
   expected digest.
3. На Windows до завершения commit удерживать handles каждого sealed regular file
   без `FILE_SHARE_WRITE | FILE_SHARE_DELETE`; root и nested directory handles
   удерживать без delete sharing. Staging root должен быть отдельным private
   container с проверенной ACL/owner boundary.
4. На POSIX создавать prepared tree в private staging container, проверять owner и
   mode, удерживать root/nested directory FDs и FDs sealed files. Выполнять
   descriptor-relative namespace/content reconciliation непосредственно до и
   после `renameat2(RENAME_NOREPLACE)`.
5. Новые, удалённые, заменённые или изменённые entries после seal должны давать
   fail-closed outcome. После rename canonical name переносится в уникальный
   quarantine, pass receipt не создаётся.
6. Если платформа не позволяет исключить конкурентного writer в заявленном threat
   model, не возвращать более сильную гарантию. Либо fail closed, либо явно сузить
   threat model в contract и receipts. Молчаливый path-based fallback запрещён.
7. `durable_replace()` для directory не должен обходить analyzed snapshot contract:
   разделить generic filesystem replace и release publication либо запретить
   directory branch для release artifacts.

### 3.2 Доказуемая SQLite state machine

1. Выпустить marker schema v2. До первого retirement durably записывать:
   `main_name`, `original_main_sha256`, `redacted_main_sha256`, hashes всех active
   и retired members, уникальный transition id и текущую phase.
2. Recovery обязан классифицировать main ровно в одно из состояний:
   `original_verified`, `redacted_verified`, `missing`, `unknown_or_tampered`.
3. Только `original_verified` разрешает восстановить original sidecars. Только
   `redacted_verified` разрешает удалить retired sidecars.
4. Для `missing` и `unknown_or_tampered` сохранить marker и все recoverable bytes,
   не выполнять restore/delete, вернуть stable
   `sqlite_transition_recovery_required` и блокировать дальнейшую remediation.
5. Проверять hash каждого active/retired member до rename/unlink. Mixed или
   конфликтующий family также переводить в recovery-required без cleanup.
6. Marker удалять только после durable completion однозначно доказанной ветки.
   Повторный запуск каждой ветки должен быть идемпотентным.
7. CLI/report/committed receipt обязаны сохранять исходный committed status,
   recovery action, transition id и классифицированный family state без секретных
   bytes или абсолютных чувствительных путей.

### 3.3 Усилить tests и static contracts

Обязательные regressions:

1. Windows: mutation/add/remove после seal и до handle rename не возвращает
   success; canonical name отсутствует либо quarantined.
2. Windows: ABA во время postcommit reconciliation не создаёт pass receipt.
3. POSIX: те же namespace/content cases проверяются на реальном POSIX CI. В
   platform-independent unit tests инжектировать низкоуровневые helpers, не
   подменять `os.name`.
4. Bundle, final verification и обе failure-publication ветки проходят runtime
   integration: один analyzed snapshot связан с `seal(expected_digest)`, а
   post-seal mutation блокирует publication.
5. SQLite original main + retired sidecars восстанавливается и удаляет marker.
6. SQLite redacted main финализирует cleanup и удаляет marker.
7. SQLite tampered main сохраняет marker/retired members и возвращает
   recovery-required.
8. SQLite missing main сохраняет marker/retired members и возвращает
   recovery-required.
9. Повтор каждого recovery case идемпотентен, включая interruption после каждого
   durable transition.
10. AST guard строится из registry/явного allowlist всех directory producers,
    запрещает direct `os.replace`, `Path.replace` и release-directory
    `durable_replace`, проверяет порядок вызовов и наличие expected digest.

## 4. Порядок проверок

После реализации выполнять строго по порядку:

1. Новый `tests/test_release_evidence_r24.py`.
2. R20-R24 release-evidence matrix и затронутые identity/security/contracts tests.
3. Ruff для изменённых Python-файлов.
4. `compileall` для изменённых production/test modules.
5. `git diff --check`.
6. Один общий unit/regression suite только после зелёных пунктов 1-5.
7. Независимое прямое ревью: `0 P0 / 0 P1 / 0 P2`.

Не запускать в рамках этого ТЗ cold/live/release execution, полный corpus или все
карточки. Они допускаются только отдельным финальным решением после чистого ревью.

## 5. Definition of Done

R24 выполнено только если одновременно:

- после seal нет разрешающего окна изменения prepared tree в поддерживаемом
  threat model;
- недоказуемая платформа или mutation даёт fail-closed/quarantine, а не success;
- SQLite recovery никогда не трактует неизвестный main как original;
- ambiguous SQLite state сохраняет все recovery evidence;
- tests используют реальные platform branches или helper injection без `os.name`
  monkeypatch;
- runtime integration покрывает bundle/final/failure publication;
- все проверки section 4 зелёные;
- независимое ревью не находит P0/P1/P2.

## 6. Фактическое evidence ревью на момент остановки

```text
R23 targeted:             11 passed
production code changed: no
cold/live/release:        not run
full corpus/all cards:    not run
review result:            0 P0 / 2 P1 / 1 P2
release gate:             BLOCKED — DO NOT RUN
```

Эта таблица сохраняет исторический результат ревью 2026-08-29. Актуальное
решение о снятии обязательного gate приведено в начале документа; сами findings
не закрыты.
