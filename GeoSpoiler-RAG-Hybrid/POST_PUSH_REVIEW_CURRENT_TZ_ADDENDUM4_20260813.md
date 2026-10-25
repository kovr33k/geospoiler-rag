# Addendum 4 к ТЗ текущего review: path security

## P1/P2 — containment для media paths

`portable_media_path` и downstream resolvers должны быть fail-closed для crafted sidecar values. Сейчас `..` и относительные значения могут после re-anchor выйти за пределы `MEDIA_CACHE_DIR`/`PROJECT_ROOT`.

Требуется:

1. Нормализовать путь через `resolve(strict=False)` и проверять `relative_to` разрешённого media/cache root.
2. Отклонять traversal (`..`), absolute path вне разрешённого legacy re-anchor, symlink component и junction escape.
3. Не читать и не отправлять во внешний LLM файл, который не прошёл containment check.
4. Возвращать machine-readable `failed|unavailable` media status вместо исключения всего pipeline.
5. Добавить regression tests для `media_cache/../../secret`, absolute path другого диска, symlink/junction escape и валидного stale `C:\\WikiRag\\...\\media_cache\\...` re-anchor.

Gate: все media paths в corpus и backfill либо containment-valid, либо quarantine; arbitrary local file read/send = 0.
