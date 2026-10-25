# Дополнение к POST_PUSH_REVIEW_REMEDIATION_TZ.md

## Дополнительные замечания, которые также нужно закрыть

### Native current backend

Явно определить поведение `MEDIA_VISION_FALLBACK_ENABLED` для native `current` backend: либо разрешённый fallback provider, либо deterministic unavailable/failed status. Добавить тесты на оба режима.

Failure/unavailable native-video visual branch должна попадать в `quality_flags/issues`; сейчас проверка рискует учитывать только image branch.

### Instagram transcription gating

До сетевого STT-вызова проверять `TRANSCRIPTION_ENABLED`, API key, provider/base URL и модель. При включённом STT без ключа возвращать deterministic `missing_key`, не делать unauthorized request и не превращать это в caption-only success. Добавить tests disabled/missing-key/provider-failure/partial-success.

### Pre-existing image caption context

В `normalizer/pipeline.py::_build_image_sections` baseline теряет caption context для image message, когда classifier уже видит текст (`caption = msg.text if not classified.has_text else ""`). Это не регрессия текущего remediation, но до финального закрытия нужно либо исправить и покрыть тестом, либо внести в accepted-risk ledger.
