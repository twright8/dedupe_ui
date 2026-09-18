# backend/tests/conftest.py
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db import _reset_db, init_db
from tests.rulesets import small_ruleset

@pytest.fixture(autouse=True)
def reset_db_singleton():
    yield
    _reset_db()

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path

@pytest.fixture
def ruleset():
    """A small, valid ruleset each test may edit freely."""
    return small_ruleset()
