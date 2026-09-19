# backend/tests/test_psc_profile.py
"""The PSC profile: the loader, the ruleset, entity IDs, features and the export."""

import json
import os
import sys
import zipfile

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("SITE_PASSWORD", "testpass123")

from app.profiles import get_profile
from app.profiles.base import LoadOptions
from app.profiles.psc import (
    PscProfile,
    RAW_COLUMNS,
    choose_survivors,
    events_from_records,
    extract_to_parquet,
    mint_entity_ids,
    stable_psc_id,
)
from app.profiles import psc as psc_module
from app.rules import engine, keys, linkage

DEFAULTS = os.path.join(os.path.dirname(__file__), "..", "app", "profiles", "defaults", "psc")


def default_ruleset() -> dict:
    with open(os.path.join(DEFAULTS, "ruleset.json"), encoding="utf-8") as handle:
        return json.load(handle)


def default_linkage() -> dict:
    with open(os.path.join(DEFAULTS, "linkage_settings.json"), encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Synthetic snapshot
# ---------------------------------------------------------------------------


def _psc(company, kind, name=None, elements=None, dob=None, **data):
    item = {"kind": kind, "links": {"self": f"/company/{company}/persons-with-significant-control/"
                                            f"{kind.split('-')[0]}/{company}KEY"}}
    if name is not None:
        item["name"] = name
    if elements is not None:
        item["name_elements"] = elements
    if dob is not None:
        item["date_of_birth"] = dob
    item.update(data)
    return {"company_number": company, "data": item}


LINES = [
    # A plain individual.
    _psc("01000001", "individual-person-with-significant-control",
         "Mr John Alan Smith",
         {"title": "Mr", "forename": "John", "middle_name": "Alan", "surname": "Smith"},
         {"month": 4, "year": 1975}, nationality="British",
         country_of_residence="England",
         address={"address_line_1": "1 High Street", "locality": "Leeds",
                  "postal_code": "ls1 1aa"},
         natures_of_control=["ownership-of-shares-75-to-100-percent"],
         notified_on="2016-04-06"),
    # The same person on another company, with a nickname and a different case.
    _psc("01000002", "individual-person-with-significant-control",
         "Mr Johnny A Smith",
         {"title": "Mr", "forename": "Johnny", "middle_name": "A", "surname": "SMITH"},
         {"month": 4, "year": 1975}, nationality="English",
         country_of_residence="Wales",
         address={"address_line_1": "1 High Street", "locality": "Leeds",
                  "postal_code": "LS1 1AA"},
         notified_on="2017-01-01"),
    # Junk birth year — the redaction sentinel.
    _psc("01000003", "individual-person-with-significant-control",
         "Ms Jane Doe", {"forename": "Jane", "surname": "Doe"},
         {"month": 1, "year": 9999}, nationality="British"),
    # No name_elements at all.
    _psc("01000004", "individual-person-with-significant-control", "Anon Person"),
    # A corporate entity with a placeholder registration number.
    _psc("01000005", "corporate-entity-person-with-significant-control",
         "Placeholder Holdings Limited",
         identification={"registration_number": "00000001",
                         "country_registered": "England & Wales",
                         "legal_form": "Private Limited Company"},
         address={"postal_code": "EC1V 2NX"}),
    _psc("01000006", "corporate-entity-person-with-significant-control",
         "Other Placeholder Ltd",
         identification={"registration_number": "12345678",
                         "country_registered": "United Kingdom"}),
    # Two statements about one real company, numbers written differently.
    _psc("01000007", "corporate-entity-person-with-significant-control",
         "Robert Hitchins Limited",
         identification={"registration_number": "00686734",
                         "country_registered": "England & Wales"}),
    _psc("01000008", "corporate-entity-person-with-significant-control",
         "Robert Hitchins Ltd",
         identification={"registration_number": "686734",
                         "country_registered": "England and Wales"}),
    # A trust, which must outrank its own legal form.
    _psc("01000009", "legal-person-person-with-significant-control",
         "The Smith Family Trust Limited"),
    # Dropped: super-secure, and a record with no name at all.
    _psc("01000010", "super-secure-person-with-significant-control", "Hidden"),
    {"company_number": "01000011",
     "data": {"kind": "individual-person-with-significant-control",
              "links": {"self": "/company/01000011/persons-with-significant-control/x/K11"}}},
    # A ceased record, which stays in (D16).
    _psc("01000012", "individual-person-with-significant-control",
         "Mr Gone Away", {"forename": "Gone", "surname": "Away"},
         {"month": 2, "year": 1960}, ceased_on="2020-01-01", ceased=True),
]


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "psc-snapshot.zip"
    body = "\n".join(json.dumps(line) for line in LINES) + "\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("psc-snapshot-2026-09-18.txt", body)
    return path


@pytest.fixture
def records(snapshot):
    """The loader hands back a PATH, so the test reads it like a stage would."""
    path, _ = PscProfile().load_records(snapshot)
    return pd.read_parquet(path)


def test_the_loader_returns_a_path_not_a_frame(snapshot):
    """A 13 GB snapshot must never be assembled in pandas, so the contract is
    the parquet the loader already wrote."""
    from pathlib import Path as _Path

    path, stats = PscProfile().load_records(snapshot)
    assert isinstance(path, _Path)
    assert path.suffix == ".parquet" and path.is_file()
    assert stats["records_total"] == len(LINES) - 2


def test_stage_0_accepts_a_path_and_a_frame_alike(snapshot, tmp_path, monkeypatch):
    """Whichever form a profile returns, the run folder ends up the same and
    the stats are identical."""
    import app.pipeline.dedupe.stage_0_load as stage
    from app.pipeline.dedupe.stage_0_load import run_stage_0_load

    monkeypatch.setattr(stage, "get_profile", PscProfile)
    run = tmp_path / "run"
    stats = run_stage_0_load(run_dir=str(run), input_path=str(snapshot))
    assert (run / "records_raw.parquet").is_file()
    assert (run / "events.parquet").is_file()
    written = pd.read_parquet(run / "records_raw.parquet")
    assert len(written) == stats["records_total"] == len(LINES) - 2
    assert list(written.columns) == RAW_COLUMNS
    assert stats["event_rows"] == len(written)


def test_the_events_are_projected_not_re_read(snapshot):
    path = PscProfile().load_events(snapshot)
    events = pd.read_parquet(path)
    assert list(events.columns)[0] == "record_id"
    assert "company_number" in events.columns
    assert len(events) == len(LINES) - 2


# ---------------------------------------------------------------------------
# Record identity
# ---------------------------------------------------------------------------


def test_stable_psc_id_matches_the_dedupings_rule():
    """An Elasticsearch index is keyed on this, so it is pinned, not chosen."""
    assert stable_psc_id(
        "07434180",
        "/company/07434180/persons-with-significant-control/corporate-entity/4VX8NR7hszDG1wJAzQS9Oi7Z5c4",
    ) == "07434180_4VX8NR7hszDG1wJAzQS9Oi7Z5c4"
    # A trailing slash is stripped before the last segment is taken.
    assert stable_psc_id("123", "/company/123/psc/ABC/") == "123_ABC"


def test_stable_psc_id_falls_back_to_a_hash_of_the_record():
    import hashlib

    expected = hashlib.sha256(b"123|A Name|a-kind|2016-04-06").hexdigest()[:16]
    assert stable_psc_id("123", None, "A Name", "a-kind", "2016-04-06") == f"123_{expected}"
    assert stable_psc_id("123", "") == f"123_{hashlib.sha256(b'123|||').hexdigest()[:16]}"


def test_the_loader_builds_the_same_ids_in_sql(records):
    """The SQL branch and the Python one have to agree, or a rerun renames
    every record in the index."""
    ids = set(records["record_id"])
    assert "01000001_01000001KEY" in ids
    assert all("_" in value for value in ids)
    assert records["record_id"].is_unique


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


def test_the_loader_reads_the_zip_without_unpacking_it(records):
    assert len(records) == len(LINES) - 2  # super-secure and the nameless one
    assert list(records.columns) == RAW_COLUMNS


def test_super_secure_and_nameless_records_are_dropped_and_counted(snapshot):
    _, stats = PscProfile().load_records(snapshot)  # noqa: F841 — stats only
    assert stats["input_rows"] == len(LINES)
    assert stats["records_dropped_super_secure"] == 1
    assert stats["records_dropped_no_name"] == 1
    assert stats["records_total"] == len(LINES) - 2


def test_the_schema_survives_a_missing_key_in_the_first_rows(records):
    """A record with no name_elements must not change how the rest is read."""
    anon = records[records["record_id"] == "01000004_01000004KEY"].iloc[0]
    assert pd.isna(anon["surname"])
    smith = records[records["record_id"] == "01000001_01000001KEY"].iloc[0]
    assert smith["surname"] == "Smith"
    assert smith["dob_year"] == 1975


def test_the_columns_the_design_asks_for_are_all_there(records):
    smith = records[records["record_id"] == "01000001_01000001KEY"].iloc[0]
    assert smith["title"] == "Mr"
    assert smith["middle_name"] == "Alan"
    assert smith["nationality"] == "British"
    assert smith["country_of_residence"] == "England"
    assert smith["postcode"] == "ls1 1aa"
    assert smith["locality"] == "Leeds"
    assert smith["natures_of_control"] == "ownership-of-shares-75-to-100-percent"
    assert smith["notified_on"] == "2016-04-06"
    assert smith["review_state"] == "unreviewed"
    assert pd.isna(smith["existing_entity_id"])
    assert smith["n_companies"] == 1


def test_a_ceased_record_stays_in(records):
    gone = records[records["name"] == "Mr Gone Away"].iloc[0]
    assert bool(gone["is_ceased"]) is True
    assert gone["ceased_on"] == "2020-01-01"


def test_the_identification_block_is_carried(records):
    company = records[records["record_id"] == "01000007_01000007KEY"].iloc[0]
    assert company["registration_number"] == "00686734"
    assert company["country_registered"] == "England & Wales"


def test_quick_mode_reads_only_the_first_rows(snapshot, tmp_path):
    out = tmp_path / "quick.parquet"
    stats = extract_to_parquet(snapshot, out, LoadOptions(quick_mode=True, quick_rows=3))
    assert stats["input_rows"] == 3
    assert stats["quick_mode"] is True
    full = extract_to_parquet(snapshot, tmp_path / "full.parquet", LoadOptions())
    assert full["input_rows"] == len(LINES)
    assert full["quick_mode"] is False


def test_a_chunked_read_gives_the_same_records(snapshot, tmp_path, monkeypatch):
    """One line per chunk must produce exactly what one chunk for the lot does.

    The loader decompresses the member a chunk at a time and extracts each chunk
    on its own, so the chunk size is the one knob that could silently change
    what is loaded — a boundary in the wrong place, a part file missed, a row
    counted twice.
    """
    whole = extract_to_parquet(snapshot, tmp_path / "whole.parquet", LoadOptions())
    monkeypatch.setenv(psc_module.CHUNK_LINES_ENV, "1")
    chunked = extract_to_parquet(snapshot, tmp_path / "chunked.parquet", LoadOptions())
    assert chunked == whole
    left = pd.read_parquet(tmp_path / "whole.parquet")
    right = pd.read_parquet(tmp_path / "chunked.parquet")
    assert left.sort_values("record_id").reset_index(drop=True).equals(
        right.sort_values("record_id").reset_index(drop=True)
    )


def test_quick_mode_stops_inside_a_chunk(snapshot, tmp_path, monkeypatch):
    """A row limit smaller than the chunk must still stop at the limit."""
    monkeypatch.setenv(psc_module.CHUNK_LINES_ENV, "2")
    stats = extract_to_parquet(snapshot, tmp_path / "quick.parquet",
                               LoadOptions(quick_mode=True, quick_rows=3))
    assert stats["input_rows"] == 3


def test_the_staging_files_are_cleaned_up(snapshot, tmp_path):
    """Nothing of the extract survives the call.

    A 16-million-row load stages about a gigabyte, and a loader that leaves it
    behind fills the disk one run at a time.
    """
    out = tmp_path / "records.parquet"
    extract_to_parquet(snapshot, out, LoadOptions())
    assert not (out.parent / psc_module.STAGING_DIRNAME).exists()


def test_the_chunk_size_falls_back_on_nonsense(monkeypatch):
    monkeypatch.setenv(psc_module.CHUNK_LINES_ENV, "not a number")
    assert psc_module.chunk_size() == psc_module.DEFAULT_CHUNK_LINES
    monkeypatch.setenv(psc_module.CHUNK_LINES_ENV, "0")
    assert psc_module.chunk_size() == psc_module.DEFAULT_CHUNK_LINES
    monkeypatch.setenv(psc_module.CHUNK_LINES_ENV, "250")
    assert psc_module.chunk_size() == 250


def test_a_file_that_is_not_a_snapshot_is_refused(tmp_path):
    path = tmp_path / "notes.docx"
    path.write_bytes(b"nope")
    with pytest.raises(ValueError, match="must be the snapshot"):
        extract_to_parquet(path, tmp_path / "out.parquet")


def test_a_zip_with_no_json_member_is_refused(tmp_path):
    path = tmp_path / "wrong.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.md", "nothing here")
    with pytest.raises(ValueError, match="no .txt"):
        extract_to_parquet(path, tmp_path / "out.parquet")


def test_events_are_one_row_per_record(records):
    events = events_from_records(records)
    assert len(events) == len(records)
    assert list(events.columns)[0] == "record_id"
    assert "company_number" in events.columns
    assert "natures_of_control" in events.columns


# ---------------------------------------------------------------------------
# The ruleset
# ---------------------------------------------------------------------------


def test_the_shipped_ruleset_and_settings_validate():
    ruleset = default_ruleset()
    assert engine.validate_ruleset(ruleset, RAW_COLUMNS) == []
    assert linkage.validate_linkage_settings(default_linkage(), ruleset, RAW_COLUMNS) == []


@pytest.fixture
def cleaned(records):
    from app.pipeline.dedupe.stage_1_clean import clean_records

    return clean_records(records, default_ruleset())


def test_tracks_come_from_the_kind(cleaned):
    by_id = cleaned.set_index("record_id")
    assert by_id.loc["01000001_01000001KEY", "track"] == "person"
    assert by_id.loc["01000005_01000005KEY", "track"] == "organisation"
    assert by_id.loc["01000009_01000009KEY", "track"] == "organisation"


def test_person_cleaning_uses_the_parsed_parts_not_the_display_name(cleaned):
    """The name box carries the title; the parsed parts do not, which is why
    they are what the rules read."""
    smith = cleaned[cleaned["record_id"] == "01000001_01000001KEY"].iloc[0]
    assert smith["surname_clean"] == "SMITH"
    assert smith["forename_clean"] == "JOHN"
    assert smith["middle_clean"] == "ALAN"
    assert smith["forename_canon"] == "JOHN"
    assert smith["surname_metaphone"]
    assert smith["forename_initial"] == "J"


def test_a_nickname_reaches_the_canonical_forename(cleaned):
    johnny = cleaned[cleaned["record_id"] == "01000002_01000002KEY"].iloc[0]
    assert johnny["forename_clean"] == "JOHNNY"
    # JOHNNY is not in the ported table; the passthrough keeps it upper-cased.
    assert johnny["forename_canon"] == "JOHNNY"
    # ...but the fingerprint still puts the two Smiths together on route 3.
    assert johnny["surname_clean"] == "SMITH"


def test_postcodes_are_normalised_and_junk_ones_dropped(cleaned):
    smith = cleaned[cleaned["record_id"] == "01000001_01000001KEY"].iloc[0]
    assert smith["postcode_clean"] == "LS1 1AA"
    assert smith["postcode_district"] == "LS1"
    # EC1V 2NX is in the derived junk list: a formation agent's office.
    agent = cleaned[cleaned["record_id"] == "01000005_01000005KEY"].iloc[0]
    assert agent["postcode_clean"] is None


def test_a_junk_birth_year_is_nulled(cleaned):
    jane = cleaned[cleaned["record_id"] == "01000003_01000003KEY"].iloc[0]
    assert jane["dob_year"] == 9999          # the raw value is kept
    assert jane["dob_year_clean"] is None    # ...and never used as one


def test_nationality_and_residence_are_mapped(cleaned):
    by_id = cleaned.set_index("record_id")
    assert by_id.loc["01000001_01000001KEY", "nationality_norm"] == "BRITISH"
    assert by_id.loc["01000002_01000002KEY", "nationality_norm"] == "BRITISH"  # English
    assert by_id.loc["01000001_01000001KEY", "residence_norm"] == "UK"
    assert by_id.loc["01000002_01000002KEY", "residence_norm"] == "UK"         # Wales


def test_the_address_key_joins_the_line_and_the_town(cleaned):
    smith = cleaned[cleaned["record_id"] == "01000001_01000001KEY"].iloc[0]
    assert smith["address_key"] == "1 HIGH STREET | Leeds"
    jane = cleaned[cleaned["record_id"] == "01000003_01000003KEY"].iloc[0]
    assert jane["address_key"] is None       # no address line, so no key


def test_organisation_cleaning_splits_the_legal_form(cleaned):
    trust = cleaned[cleaned["record_id"] == "01000009_01000009KEY"].iloc[0]
    assert trust["name_clean"] == "SMITH FAMILY TRUST LIMITED"   # THE dropped
    assert trust["name_core"] == "SMITH FAMILY TRUST"
    assert trust["legal_form_clean"] == "LIMITED"


def test_the_type_bucket_puts_a_trust_above_its_legal_form(cleaned):
    """deduping's order: SMITH FAMILY TRUST LIMITED is a TRUST, not an LTD."""
    by_id = cleaned.set_index("record_id")
    assert by_id.loc["01000009_01000009KEY", "type_bucket"] == "TRUST"
    assert by_id.loc["01000009_01000009KEY", "type_bucket_rule"] == "d1r1"
    assert by_id.loc["01000007_01000007KEY", "type_bucket"] == "LTD"


def test_registration_numbers_are_padded_and_placeholders_dropped(cleaned):
    by_id = cleaned.set_index("record_id")
    # The same company written two ways becomes one number.
    assert by_id.loc["01000007_01000007KEY", "regnum_clean"] == "00686734"
    assert by_id.loc["01000008_01000008KEY", "regnum_clean"] == "00686734"
    # Placeholders name no company.
    assert by_id.loc["01000005_01000005KEY", "regnum_clean"] is None
    assert by_id.loc["01000006_01000006KEY", "regnum_clean"] is None


def test_the_country_is_canonical(cleaned):
    by_id = cleaned.set_index("record_id")
    assert by_id.loc["01000007_01000007KEY", "country_canonical"] == "UK"
    assert by_id.loc["01000008_01000008KEY", "country_canonical"] == "UK"


# ---------------------------------------------------------------------------
# Match keys
# ---------------------------------------------------------------------------


def test_the_company_number_key_merges_the_two_spellings(cleaned):
    groups, stats = keys.apply_match_keys(cleaned, default_ruleset())
    merged = groups[groups["status"] == "merged"]
    together = merged.groupby("group_id")["record_id"].apply(set)
    assert any(
        {"01000007_01000007KEY", "01000008_01000008KEY"} <= members
        for members in together
    )


def test_a_placeholder_number_never_forms_a_group(cleaned):
    groups, _ = keys.apply_match_keys(cleaned, default_ruleset())
    merged = set(groups[groups["status"] == "merged"]["record_id"])
    assert "01000005_01000005KEY" not in merged
    assert "01000006_01000006KEY" not in merged


def test_the_person_key_needs_every_part_of_the_name_and_date(cleaned):
    """Jane Doe has a junk year, so she is not eligible for the person key at
    all — nulls never match."""
    _, stats = keys.apply_match_keys(cleaned, default_ruleset())
    person_key = next(k for k in stats["keys"] if k["id"] == "k1")
    eligible = person_key["eligible_records"]
    assert eligible == 3   # the two Smiths and Gone Away; not Jane, not Anon


# ---------------------------------------------------------------------------
# Entity IDs
# ---------------------------------------------------------------------------


def test_an_organisation_with_a_gb_number_takes_it_as_its_id():
    profile = PscProfile()
    members = pd.DataFrame({
        "entity_key": ["a", "a", "b"],
        "track": ["organisation", "organisation", "person"],
        "company_number_padded": ["00686734", "00686734", None],
    })
    minted = mint_entity_ids(members, profile)
    assert minted["a"] == "00686734"
    assert minted["b"].startswith("PSCP-")


def test_minted_ids_are_zero_padded_and_never_reused():
    profile = PscProfile()
    first = mint_entity_ids(
        pd.DataFrame({"entity_key": ["a", "b"], "track": ["person", "person"]}), profile
    )
    second = mint_entity_ids(
        pd.DataFrame({"entity_key": ["c"], "track": ["organisation"]}), profile
    )
    assert list(first) == ["PSCP-0000000001", "PSCP-0000000002"]
    assert list(second) == ["PSCO-0000000003"]
    assert not set(first) & set(second)


def test_a_company_number_outranks_a_minted_id_and_the_older_one_wins():
    claims = pd.DataFrame({
        "entity_key": ["a", "a", "b", "b"],
        "registry_entity": ["PSCP-0000000009", "00686734",
                            "PSCP-0000000004", "PSCP-0000000002"],
    })
    survivors = choose_survivors(claims)
    assert survivors["a"] == "00686734"          # the number names the company
    assert survivors["b"] == "PSCP-0000000002"   # the older entity


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


def _units(rows):
    return pd.DataFrame(rows)


def test_person_features_read_the_name_the_dob_and_the_companies():
    from app.profiles import psc_features

    units = _units([
        {"unit_id": "1", "unit_size": 1, "surname_clean": "SMITH",
         "forename_canon": "JOHN", "dob_year": 1975, "dob_month": 4,
         "nationality_norm": "BRITISH", "residence_norm": "UK",
         "postcode_clean": "LS1 1AA", "postcode_district": "LS1",
         "address_line_1": "1 HIGH STREET", "notified_on": "2016-04-06"},
        {"unit_id": "2", "unit_size": 3, "surname_clean": "SMITH",
         "forename_canon": "JOHN", "dob_year": 1975, "dob_month": 4,
         "nationality_norm": "BRITISH", "residence_norm": "UK",
         "postcode_clean": "LS1 1AA", "postcode_district": "LS1",
         "address_line_1": "1 HIGH STREET", "notified_on": "2017-01-01"},
    ])
    pairs = pd.DataFrame({"unit_id_l": ["1"], "unit_id_r": ["2"]})
    events = pd.DataFrame({
        "unit_id": ["1", "2"], "company_number": ["01000001", "01000001"],
    })
    out = psc_features.build(pairs, units, events, {}, "person")
    row = out.iloc[0]
    assert row["surname_similarity"] == 1.0
    assert row["forename_similarity"] == 1.0
    assert row["dob_month_equal"] == 1.0
    assert row["dob_year_gap"] == 0.0
    assert row["nationality_equal"] == 1.0
    assert row["postcode_equal"] == 1.0
    assert row["same_company"] == 1.0
    assert row["shared_companies"] == 1.0
    assert row["unit_size_min"] == 1 and row["unit_size_max"] == 3


def test_organisation_features_carry_the_six_discriminators():
    from app.profiles import psc_features

    units = _units([
        {"unit_id": "1", "unit_size": 1, "name_core": "ACME", "name_clean": "ACME HOLDINGS",
         "regnum_clean": "00686734", "country_canonical": "UK",
         "legal_form_clean": "LIMITED", "numeric_suffix": "II",
         "subject_phrase": None, "address_line_1": "1 HIGH STREET"},
        {"unit_id": "2", "unit_size": 1, "name_core": "ACME", "name_clean": "ACME",
         "regnum_clean": "00999999", "country_canonical": "US",
         "legal_form_clean": "PLC", "numeric_suffix": "III",
         "subject_phrase": None, "address_line_1": "9 HIGH STREET"},
    ])
    pairs = pd.DataFrame({"unit_id_l": ["1"], "unit_id_r": ["2"]})
    out = psc_features.build(pairs, units, None, {}, "organisation")
    row = out.iloc[0]
    assert row["regnum_conflict"] == 1.0
    assert row["country_differs"] == 1.0
    assert row["legal_form_differs"] == 1.0
    assert row["numeric_suffix_differs"] == 1.0
    assert row["house_number_differs"] == 1.0
    assert row["holdings_asymmetry"] == 1.0   # near-identical core, one says HOLDINGS


def test_every_declared_feature_is_built_for_both_tracks():
    from app.profiles import psc_features

    for track in ("person", "organisation"):
        names = [f.name for f in psc_features.metadata(track)]
        assert names, track
        units = _units([{"unit_id": "1", "unit_size": 1}, {"unit_id": "2", "unit_size": 1}])
        pairs = pd.DataFrame({"unit_id_l": ["1"], "unit_id_r": ["2"]})
        out = psc_features.build(pairs, units, None, {}, track)
        assert list(out.columns) == names


def test_features_of_an_empty_pairs_frame_are_empty_not_broken():
    from app.profiles import psc_features

    pairs = pd.DataFrame({"unit_id_l": [], "unit_id_r": []})
    out = psc_features.build(pairs, _units([]), None, {}, "person")
    assert len(out) == 0
    assert list(out.columns) == [f.name for f in psc_features.metadata("person")]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_the_export_writes_a_table_and_an_elasticsearch_bulk_file(tmp_path, cleaned):
    from app.profiles import psc_export

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cleaned.to_parquet(run_dir / "records.parquet", index=False)
    entities = pd.DataFrame({
        "record_id": list(cleaned["record_id"])[:3],
        "entity_id": ["00686734", "PSCP-0000000001", None],
        "entity_basis": ["exact key", "score", None],
    })

    bundle = psc_export.export(run_dir, "proposed", "csv", {
        "entities": entities, "aliases": [], "run_id": "run_x",
        "config_version": 1, "counts": {}, "scope": "proposed",
    })

    assert bundle.exists()
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        assert names == {"psc_entities.csv", "elasticsearch_bulk.jsonl",
                         "aliases.csv", "README.txt"}
        bulk = archive.read("elasticsearch_bulk.jsonl").decode().strip().splitlines()

    # Two lines per updated record, and the record with no entity is left out.
    assert len(bulk) == 4
    action = json.loads(bulk[0])
    assert action["update"]["_index"] == "ch-cred-pscs-v1"
    assert action["update"]["_id"] == entities.iloc[0]["record_id"]
    assert json.loads(bulk[1])["doc"]["psc_entity_id"] == "00686734"


def test_the_export_never_talks_to_elasticsearch(tmp_path, cleaned):
    """D16: the write-back is a separate step, started by hand."""
    import inspect

    from app.profiles import psc_export

    source = inspect.getsource(psc_export)
    for forbidden in ("requests", "urllib", "http", "elasticsearch("):
        assert forbidden not in source.lower().replace("_bulk endpoint", "")


# ---------------------------------------------------------------------------
# The profile as the UI reads it
# ---------------------------------------------------------------------------


def test_the_psc_profile_declares_what_the_ui_needs(monkeypatch):
    monkeypatch.setenv("PROFILE", "psc")
    body = get_profile("psc").as_dict()

    assert body["key"] == "psc"
    assert body["nouns"] == {
        "record": "PSC record", "record_plural": "PSC records",
        "unit_evidence": "companies controlled",
        "evidence_row": "company", "evidence_row_plural": "companies",
    }
    assert body["priority_columns"] == ["n_companies"]
    assert body["existing_label_name"] is None      # no imported grouping
    assert "Elasticsearch" in body["export_description"]
    assert body["pattern_summary"]
    assert "{n_companies}" in body["pattern_summary"][0]["template"]
    assert {f["id"] for f in body["evidence_focus"]} == {"person", "organisation", "other"}


def test_the_donations_profile_still_declares_its_own(monkeypatch):
    monkeypatch.setenv("PROFILE", "donations")
    body = get_profile("donations").as_dict()

    assert body["nouns"]["record"] == "donor"
    assert body["nouns"]["unit_evidence"] == "donation history"
    assert body["nouns"]["evidence_row_plural"] == "donations"
    assert body["existing_label_name"] == "earlier manual grouping"
    template = body["pattern_summary"][0]["template"]
    assert "{n_donations} donations" in template
    assert "{modal_value:money}" in template
    assert "{share_round_1000:percent}" in template
    assert "{first_year}–{last_year}" in template


def test_a_profile_that_declares_no_nouns_still_reads():
    from app.profiles.base import DEFAULT_NOUNS, Profile

    body = Profile(key="bare").as_dict()
    assert body["nouns"] == DEFAULT_NOUNS
    assert body["pattern_summary"] == []
    assert body["existing_label_name"] is None
    assert body["export_description"]


def test_the_psc_unit_aggregate_counts_distinct_companies():
    """Summing n_companies would count one company twice when two statements
    about it land in the same unit."""
    profile = PscProfile()
    members = pd.DataFrame({
        "unit_id": ["u1", "u1", "u1", "u2"],
        "company_number": ["01", "01", "02", "03"],
        "n_companies": [1, 1, 1, 1],
    })
    out = profile.aggregate_unit_columns(members)
    assert out.loc["u1", "n_companies"] == 2
    assert out.loc["u2", "n_companies"] == 1
