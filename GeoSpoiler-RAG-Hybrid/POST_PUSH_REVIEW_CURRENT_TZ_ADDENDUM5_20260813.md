# Addendum 5 к ТЗ текущего review: STT fallback observability

При primary STT HTTP 400 fallback может выбросить `requests.Timeout`; текущий outer catch возвращает `route=unknown` и теряет историю primary/fallback attempt.

Требуется:

1. Ловить timeout/connection/response errors на каждой route отдельно.
2. Сохранять ordered `route_attempts` с provider/model, error class, HTTP status, latency и финальным route.
3. Не включать токены/полные URL в sidecar/report.
4. Добавить regression test: primary 400 → fallback timeout → terminal failed, сохранились обе попытки и корректный `route`/status.

Gate: каждый failed STT item имеет полную, не потерянную историю попыток.
