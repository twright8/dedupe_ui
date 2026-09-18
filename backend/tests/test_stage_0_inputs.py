"""Stage 0 input loaders should accept both a raw .csv and a .zip wrapping a .csv.

Regression: a run failed with ``BadZipFile: File is not a zip file`` because a
reviewer selected a raw OCOD .csv where the loader assumed a .zip. The loaders
now branch on file type, and an unsupported extension raises a clear error.
"""

import zipfile

import pandas as pd
import pytest

from app.pipeline.stage_0_preprocess import (
    expand_ocod_proprietors,
    expand_roe_names,
    load_ocod,
    load_roe,
    standardise_dataframe,
)


# ---------------------------------------------------------------------------
# Sample CSV content
# ---------------------------------------------------------------------------

OCOD_CSV = (
    "Title Number,Proprietor Name (1),Company Registration No. (1),"
    "Country Incorporated (1)\n"
    "AB123,ACME HOLDINGS LTD,OE000111,JERSEY\n"
    "AB124,BETA PROPERTIES INC,OE000222,GUERNSEY\n"
)

# A title can carry up to 4 proprietors, each with its own name / registration no. /
# country-incorporated / address block. Here AB123 has TWO proprietors (slot 3 empty),
# AB124 has ONE (slots 2-3 empty). The fan-out must produce one row per non-empty slot.
OCOD_CSV_MULTI = (
    "Title Number,Proprietor Name (1),Company Registration No. (1),Country Incorporated (1),"
    "Proprietor (1) Address (1),"
    "Proprietor Name (2),Company Registration No. (2),Country Incorporated (2),Proprietor (2) Address (1),"
    "Proprietor Name (3),Company Registration No. (3),Country Incorporated (3),Proprietor (3) Address (1)\n"
    "AB123,ACME HOLDINGS LTD,OE000111,JERSEY,1 HIGH ST,"
    "BETA PROPERTIES INC,OE000222,GUERNSEY,2 LOW ST,,,,\n"
    "AB124,GAMMA LTD,OE000333,ISLE OF MAN,3 MID ST,,,,,,,,\n"
)

# CH has the 8 columns load_roe needs; only OE-prefixed rows are kept.
CH_CSV = (
    "CompanyName,CompanyNumber,RegAddress.AddressLine1,RegAddress.PostCode,"
    "RegAddress.PostTown,CompanyCategory,CompanyStatus,CountryOfOrigin\n"
    "ACME HOLDINGS LTD,OE000111,1 HIGH ST,JE2 3AB,ST HELIER,Overseas Entity,Active,JERSEY\n"
    "RANDOM UK CO,12345678,2 LOW ST,SW1A 1AA,LONDON,Ltd,Active,UNITED KINGDOM\n"
    "BETA PROPERTIES INC,OE000222,3 MID ST,GY1 2CD,ST PETER PORT,Overseas Entity,Active,GUERNSEY\n"
)

# Real Companies House extracts ship PreviousName_<n>.CONDATE / .CompanyName
# pairs, several with inconsistent *leading spaces* in the header. The loader
# must read only the .CompanyName slots (dates are ignored) and tolerate the
# spacing. OE000111 has two former names; OE000222 has none.
CH_CSV_WITH_PREV = (
    "CompanyName,CompanyNumber,RegAddress.AddressLine1,RegAddress.PostCode,"
    "RegAddress.PostTown,CompanyCategory,CompanyStatus,CountryOfOrigin,"
    "PreviousName_1.CONDATE, PreviousName_1.CompanyName,"
    " PreviousName_2.CONDATE, PreviousName_2.CompanyName\n"
    "NEWACME LTD,OE000111,1 HIGH ST,JE2 3AB,ST HELIER,Overseas Entity,Active,JERSEY,"
    "27/03/2020,ACME HOLDINGS LTD,05/11/2015,ACME OLD LTD\n"
    "BETA PROPERTIES INC,OE000222,3 MID ST,GY1 2CD,ST PETER PORT,Overseas Entity,Active,GUERNSEY,"
    ",,,\n"
)


def _write_csv(dir_path, name, content):
    p = dir_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _write_zip(dir_path, zip_name, inner_csv_name, content):
    p = dir_path / zip_name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(inner_csv_name, content)
    return p


# ---------------------------------------------------------------------------
# load_ocod
# ---------------------------------------------------------------------------

class TestLoadOcod:
    def test_reads_raw_csv(self, tmp_path):
        p = _write_csv(tmp_path, "OCOD_FULL.csv", OCOD_CSV)
        df = load_ocod(p)
        assert len(df) == 2
        assert set(df["ocod_name_raw"]) == {"ACME HOLDINGS LTD", "BETA PROPERTIES INC"}
        assert "title_number" in df.columns

    def test_reads_zip(self, tmp_path):
        p = _write_zip(tmp_path, "OCOD_FULL.zip", "OCOD_FULL.csv", OCOD_CSV)
        df = load_ocod(p)
        assert len(df) == 2
        assert set(df["ocod_name_raw"]) == {"ACME HOLDINGS LTD", "BETA PROPERTIES INC"}

    def test_csv_and_zip_agree(self, tmp_path):
        csv_df = load_ocod(_write_csv(tmp_path, "a.csv", OCOD_CSV))
        zip_df = load_ocod(_write_zip(tmp_path, "b.zip", "a.csv", OCOD_CSV))
        pd.testing.assert_frame_equal(csv_df, zip_df)

    def test_unsupported_extension_raises(self, tmp_path):
        p = _write_csv(tmp_path, "OCOD_FULL.txt", OCOD_CSV)
        with pytest.raises(RuntimeError, match=r"(?i)\.zip or \.csv"):
            load_ocod(p)

    def test_zip_without_csv_raises(self, tmp_path):
        p = tmp_path / "empty.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("readme.txt", "no csv here")
        with pytest.raises(RuntimeError, match=r"(?i)no .*csv"):
            load_ocod(p)


# ---------------------------------------------------------------------------
# load_roe
# ---------------------------------------------------------------------------

class TestLoadRoe:
    def test_reads_raw_csv_and_filters_to_oe(self, tmp_path):
        p = _write_csv(tmp_path, "CH.csv", CH_CSV)
        df = load_roe(p)
        # Only the two OE-prefixed rows survive.
        assert len(df) == 2
        assert set(df["roe_company_number"]) == {"OE000111", "OE000222"}
        assert all(c.startswith("roe_") for c in df.columns)

    def test_reads_zip(self, tmp_path):
        p = _write_zip(tmp_path, "CH.zip", "BasicCompanyData.csv", CH_CSV)
        df = load_roe(p)
        assert len(df) == 2
        assert set(df["roe_company_number"]) == {"OE000111", "OE000222"}

    def test_csv_and_zip_agree(self, tmp_path):
        csv_df = load_roe(_write_csv(tmp_path, "a.csv", CH_CSV))
        zip_df = load_roe(_write_zip(tmp_path, "b.zip", "a.csv", CH_CSV))
        pd.testing.assert_frame_equal(csv_df, zip_df)

    def test_unsupported_extension_raises(self, tmp_path):
        p = _write_csv(tmp_path, "CH.dat", CH_CSV)
        with pytest.raises(RuntimeError, match=r"(?i)\.zip or \.csv"):
            load_roe(p)

    def test_reads_previous_name_columns(self, tmp_path):
        df = load_roe(_write_csv(tmp_path, "CH.csv", CH_CSV_WITH_PREV))
        assert {"roe_prev_name_1", "roe_prev_name_2"}.issubset(df.columns)
        row = df[df["roe_company_number"] == "OE000111"].iloc[0]
        assert row["roe_prev_name_1"] == "ACME HOLDINGS LTD"
        assert row["roe_prev_name_2"] == "ACME OLD LTD"
        # CONDATE columns are deliberately not read.
        assert not any("CONDATE" in c for c in df.columns)


# ---------------------------------------------------------------------------
# expand_roe_names: fan out current + former names, attribute to the company
# ---------------------------------------------------------------------------

class TestExpandRoeNames:
    def test_fans_out_former_names(self, tmp_path):
        df = load_roe(_write_csv(tmp_path, "CH.csv", CH_CSV_WITH_PREV))
        out = expand_roe_names(df)

        oe1 = out[out["roe_company_number"] == "OE000111"]
        assert set(oe1["roe_name_raw"]) == {"NEWACME LTD", "ACME HOLDINGS LTD", "ACME OLD LTD"}
        assert (oe1["roe_name_type"] == "former").sum() == 2
        assert (oe1["roe_name_type"] == "current").sum() == 1
        # Every variant points back at the company's current name.
        assert (oe1["roe_current_name_raw"] == "NEWACME LTD").all()

        oe2 = out[out["roe_company_number"] == "OE000222"]
        assert len(oe2) == 1 and oe2.iloc[0]["roe_name_type"] == "current"

        # Previous-name source columns are consumed, not leaked downstream.
        assert not any(c.startswith("roe_prev_name_") for c in out.columns)

    def test_no_previous_columns_is_noop_plus_tags(self, tmp_path):
        df = load_roe(_write_csv(tmp_path, "CH.csv", CH_CSV))
        out = expand_roe_names(df)
        assert len(out) == 2
        assert (out["roe_name_type"] == "current").all()
        assert (out["roe_current_name_raw"] == out["roe_name_raw"]).all()

    def test_drops_former_equal_to_current(self, tmp_path):
        csv = (
            "CompanyName,CompanyNumber,RegAddress.AddressLine1,RegAddress.PostCode,"
            "RegAddress.PostTown,CompanyCategory,CompanyStatus,CountryOfOrigin,"
            " PreviousName_1.CompanyName\n"
            "SAME LTD,OE000999,1 ST,AA1 1AA,TOWN,Overseas Entity,Active,JERSEY,SAME LTD\n"
        )
        out = expand_roe_names(load_roe(_write_csv(tmp_path, "CH.csv", csv)))
        oe = out[out["roe_company_number"] == "OE000999"]
        assert len(oe) == 1 and oe.iloc[0]["roe_name_type"] == "current"


# ---------------------------------------------------------------------------
# expand_ocod_proprietors: fan a title's 1-4 proprietors out to one row each
# ---------------------------------------------------------------------------

class TestExpandOcodProprietors:
    def test_single_proprietor_csv_is_one_row_per_title(self, tmp_path):
        # The legacy fixture only fills slot 1 -> one row per title, index 1.
        wide = load_ocod(_write_csv(tmp_path, "OCOD.csv", OCOD_CSV))
        out = expand_ocod_proprietors(wide)
        assert len(out) == 2
        assert set(out["proprietor_index"]) == {1}
        assert set(out["ocod_name_raw"]) == {"ACME HOLDINGS LTD", "BETA PROPERTIES INC"}

    def test_multi_proprietor_title_produces_n_rows(self, tmp_path):
        wide = load_ocod(_write_csv(tmp_path, "OCOD.csv", OCOD_CSV_MULTI))
        out = expand_ocod_proprietors(wide)

        # AB123 -> 2 proprietor rows; AB124 -> 1. Empty slots contribute nothing.
        assert len(out) == 3
        ab123 = out[out["title_number"] == "AB123"].sort_values("proprietor_index")
        assert list(ab123["proprietor_index"]) == [1, 2]
        assert list(ab123["ocod_name_raw"]) == ["ACME HOLDINGS LTD", "BETA PROPERTIES INC"]
        assert list(ab123["ocod_jurisdiction_raw"]) == ["JERSEY", "GUERNSEY"]
        # Each proprietor keeps its own registration no. and address block.
        assert list(ab123["ocod_reg_no"]) == ["OE000111", "OE000222"]
        assert list(ab123["ocod_address_1"]) == ["1 HIGH ST", "2 LOW ST"]

        ab124 = out[out["title_number"] == "AB124"]
        assert len(ab124) == 1
        assert ab124.iloc[0]["proprietor_index"] == 1
        assert ab124.iloc[0]["ocod_name_raw"] == "GAMMA LTD"

    def test_empty_proprietor_slots_are_skipped(self, tmp_path):
        wide = load_ocod(_write_csv(tmp_path, "OCOD.csv", OCOD_CSV_MULTI))
        out = expand_ocod_proprietors(wide)
        # No blank proprietor names survive the fan-out.
        assert (out["ocod_name_raw"].str.strip() != "").all()
        # Slot 3 was present in the header but empty for every title -> no index-3 rows.
        assert 3 not in set(out["proprietor_index"])

    def test_cleaning_applied_per_proprietor_row(self, tmp_path):
        wide = load_ocod(_write_csv(tmp_path, "OCOD.csv", OCOD_CSV_MULTI))
        out = expand_ocod_proprietors(wide)

        jurisdiction_map = {
            ("ocod", "JERSEY"): "JERSEY",
            ("ocod", "GUERNSEY"): "GUERNSEY",
            ("ocod", "ISLE OF MAN"): "ISLE OF MAN",
        }
        cleaned = standardise_dataframe(
            out, "ocod_name_raw", "ocod_jurisdiction_raw", "ocod",
            jurisdiction_map, name_rules=[], entity_tokens={"LTD", "LIMITED", "INC"},
        )
        # Every proprietor row (co-owners included) gets the standard cleaning columns.
        assert len(cleaned) == 3
        for col in ("name_clean", "name_core", "name_digits_sorted", "name_tokens_sorted", "jurisdiction_clean"):
            assert col in cleaned.columns
            assert cleaned[col].notna().all()
        beta = cleaned[cleaned["ocod_reg_no"] == "OE000222"].iloc[0]
        assert beta["jurisdiction_clean"] == "GUERNSEY"
        assert beta["name_core"] == "BETA PROPERTIES"  # trailing INC stripped


# ---------------------------------------------------------------------------
# UnmappedJurisdictionsError carries structured data for the UI
# ---------------------------------------------------------------------------

class TestUnmappedJurisdictionsError:
    def test_carries_lists_and_message(self):
        from app.pipeline.standardise import UnmappedJurisdictionsError

        err = UnmappedJurisdictionsError(["PUERTO RICO"], ["TAJIKISTAN"])
        assert err.unmapped_ocod == ["PUERTO RICO"]
        assert err.unmapped_roe == ["TAJIKISTAN"]
        msg = str(err)
        assert "PUERTO RICO" in msg
        assert "TAJIKISTAN" in msg
        # It is a RuntimeError so existing except-blocks still catch it.
        assert isinstance(err, RuntimeError)
