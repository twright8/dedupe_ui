"""scripts/adopt_linkage_defaults.py: a used instance picks up new default settings."""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

import adopt_linkage_defaults as script  # noqa: E402
from app.db import init_db  # noqa: E402
from app.services import config_manager  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "linkage.db")
    init_db(path)
    return path


def _old_settings():
    settings = script.default_settings("donations")
    settings["tracks"]["person"]["em_blocking_rules"] = [
        "l.surname = r.surname", "l.forename_canon = r.forename_canon"]
    settings.pop("match_probability_threshold_high_by_track", None)
    return settings


def _ruleset():
    path = script.DEFAULTS_DIR / "donations" / "ruleset.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _ruleset_without(veto_id):
    ruleset = _ruleset()
    ruleset["vetoes"] = [v for v in ruleset["vetoes"] if v["id"] != veto_id]
    return ruleset


def test_a_missing_default_veto_rule_is_added_and_the_rest_left_alone(db_path):
    config_manager.save_version(db_path, created_by="tom", note="first",
                                ruleset=_ruleset_without("dv3"),
                                linkage_settings=script.default_settings("donations"))
    out = io.StringIO()
    assert script.adopt(db_path, "donations", out=out) == 2
    current = config_manager.get_current(db_path)
    ids = [v["id"] for v in current["ruleset"]["vetoes"]]
    assert ids == [v["id"] for v in _ruleset_without("dv3")["vetoes"]] + ["dv3"]
    assert "default rule dv3 added" in out.getvalue()
    assert "dv3" in current["note"]
    # Everything else in the rules is exactly as it was.
    stripped = dict(current["ruleset"]); stripped.pop("vetoes")
    expected = _ruleset_without("dv3"); expected.pop("vetoes")
    assert stripped == expected


def test_never_used_instance_is_left_alone(db_path):
    out = io.StringIO()
    assert script.adopt(db_path, "donations", out=out) is None
    assert "Never used" in out.getvalue()
    assert config_manager.list_versions(db_path) == []


def test_old_settings_get_a_new_version_with_the_defaults(db_path):
    config_manager.save_version(db_path, created_by="tom", note="first",
                                ruleset=_ruleset(), linkage_settings=_old_settings())
    out = io.StringIO()
    version = script.adopt(db_path, "donations", out=out)
    assert version == 2
    current = config_manager.get_current(db_path)
    assert current["version"] == 2
    assert json.loads(current["linkage_settings"]) == script.default_settings("donations")
    assert current["ruleset"] == _ruleset()
    text = out.getvalue()
    assert "tracks.person.em_blocking_rules: changed" in text
    assert "match_probability_threshold_high_by_track" in text
    assert "Saved config version 2" in text


def test_dry_run_saves_nothing(db_path):
    config_manager.save_version(db_path, created_by="tom", note="first",
                                ruleset=_ruleset(), linkage_settings=_old_settings())
    out = io.StringIO()
    assert script.adopt(db_path, "donations", dry_run=True, out=out) is None
    assert "Dry run" in out.getvalue()
    assert config_manager.get_current(db_path)["version"] == 1


def test_current_defaults_are_not_saved_again(db_path):
    config_manager.save_version(db_path, created_by="tom", note="first",
                                ruleset=_ruleset(),
                                linkage_settings=script.default_settings("donations"))
    out = io.StringIO()
    assert script.adopt(db_path, "donations", out=out) is None
    assert "already holds" in out.getvalue()
    assert config_manager.get_current(db_path)["version"] == 1


def test_main_needs_a_profile(db_path, monkeypatch):
    monkeypatch.delenv("PROFILE", raising=False)
    with pytest.raises(SystemExit):
        script.main(["--db", db_path])
