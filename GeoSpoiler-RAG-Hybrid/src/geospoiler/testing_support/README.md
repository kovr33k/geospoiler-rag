# testing_support

Явно включаемый GEOSPOILER_NO_NETWORK guard для тестов и subprocess. Не устанавливает глобальный sitecustomize в Python. На Windows разрешает только внутренний socketpair для asyncio, сохраняя запрет обычных connect.
