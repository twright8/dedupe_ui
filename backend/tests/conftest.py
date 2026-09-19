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


def nulls_as_none(frame):
    """Every missing value as ``None``, whatever dtype it came back on.

    A frame read out of parquet and the same frame in memory hold their nulls
    differently, and pandas 3 widened the gap: its ``str`` dtype uses ``NaN``
    where an object column used ``None``, and ``frame.where(frame.notna(),
    None)`` no longer puts ``None`` back into a float column. Tests that
    compare a written file against an in-memory frame care that the values
    match, not which spelling of null the two ended up with, so they compare
    through this. Tests that compare one *file* against another still compare
    them directly — that identity is the thing being proved.
    """
    import pandas as pd

    frame = frame.reset_index(drop=True)
    return pd.DataFrame(
        {name: pd.Series(frame[name].to_numpy(dtype=object, na_value=None),
                         dtype=object)
         for name in frame.columns},
        columns=list(frame.columns),
    )
