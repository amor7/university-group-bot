import pytest

from bot.db import connect
from bot.repo import Repo

CHAT = -1001


@pytest.fixture
def repo():
    conn = connect(":memory:")
    yield Repo(conn)
    conn.close()
