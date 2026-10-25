from __future__ import annotations

from collections.abc import Iterator

import pytest

from retrieval.wiki.schema import connect_database


@pytest.fixture
def wiki_db(tmp_path) -> Iterator:
    connection = connect_database(tmp_path / "wiki-v2.sqlite")
    try:
        yield connection
    finally:
        connection.close()
