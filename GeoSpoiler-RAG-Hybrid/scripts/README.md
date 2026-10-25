# scripts

`run_offline_tests.ps1` запускает pytest с сетевым запретом и уводит все
изменяемые runtime-пути во внешний каталог. Остальные файлы — совместимые
операционные/release entrypoints; их реализация находится в
`src/geospoiler/operations/`. Используйте действующие аргументы `--help`;
release/cold-команды не являются обычными offline-тестами.
