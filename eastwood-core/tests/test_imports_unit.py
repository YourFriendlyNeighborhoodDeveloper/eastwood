"""Bulk import building blocks: states, column mapping, readers, authorization."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from domain.imports.classify import ConsentEvidence, classify
from domain.imports.mapping import build_column_map, normalize_row, parse_datetime
from domain.imports.readers import detect_format, file_sha256, open_rows
from domain.imports.states import normalize_state
from domain.normalization import normalize_lead_payload
from domain.tcpa import supported_states

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)
STATES = supported_states()
OURS = "Eastwood Insurance Agency"
GOOD_TEXT = (f"By submitting I agree that {OURS} may contact me at the number provided, including by "
             "automated calls, artificial or prerecorded voice, AI voice and text messages. Consent is not "
             "a condition of purchase.")


# ── States ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, code", [
    ("MICHIGAN", "MI"), ("New York", "NY"), ("TEXAS", "TX"), ("tx", "TX"), ("Mich.", "MI"),
    ("District of Columbia", "DC"), (" wisconsin ", "WI"), ("N. Carolina", "NC"), ("Puerto Rico", "PR"),
])
def test_states_normalize(raw, code):
    assert normalize_state(raw) == code


@pytest.mark.parametrize("raw", ["", None, "Ontario", "XX", "NEWY"])
def test_unrecognized_states_are_none_never_a_guess(raw):
    assert normalize_state(raw) is None


def test_api_payload_state_is_normalized_not_truncated():
    """The old importer turned "NEW YORK" into "NE" (Nebraska)."""
    assert normalize_lead_payload({"phone": "313-555-0101", "state": "NEW YORK"})["state"] == "NY"
    assert normalize_lead_payload({"phone": "313-555-0101", "state": "Texas"})["state"] == "TX"


# ── Mapping ──────────────────────────────────────────────────────────────────

HEADERS = ["Lead ID", "First Name", "Last Name", "Mobile", "Other Phone 1", "State", "DOB", "Date Created",
           "Product", "Agent", "TCPA Consent", "Consent Date", "TrustedForm Cert URL", "DNC", "Favorite Color"]


def test_vendor_headers_map_to_canonical_fields():
    m = build_column_map(HEADERS)
    named = {HEADERS[i]: f for i, f in m.items()}
    assert named == {
        "Lead ID": "external_id", "First Name": "first_name", "Last Name": "last_name", "Mobile": "phone",
        "Other Phone 1": "phone", "State": "state", "DOB": "dob", "Date Created": "acquired_at",
        "Product": "product_line", "Agent": "lead_owner", "TCPA Consent": "consent_text",
        "Consent Date": "consent_captured_at", "TrustedForm Cert URL": "consent_certificate", "DNC": "dnc_flag",
    }


def test_operator_override_wins_and_can_unmap():
    m = build_column_map(["Phone A", "Mobile"], {"Phone A": "phone", "Mobile": "ignore"})
    assert m == {0: "phone"}


def _row(**over):
    base = {"Lead ID": "X-1", "First Name": "Pat", "Last Name": "Doe", "Mobile": "", "Other Phone 1": "3135550101",
            "State": "MICHIGAN", "DOB": "03/04/1955", "Date Created": "2026-10-01", "Product": "Final Expense",
            "Agent": "Former Agent", "TCPA Consent": GOOD_TEXT, "Consent Date": "2026-10-01 14:03:00",
            "TrustedForm Cert URL": "https://cert.example/abc", "DNC": "", "Favorite Color": "blue"}
    base.update(over)
    return [base[h] for h in HEADERS]


def test_first_valid_phone_wins_and_fields_normalize():
    r = normalize_row(2, HEADERS, _row(), build_column_map(HEADERS), today=date(2026, 11, 24))
    assert not r.rejected
    assert r.phone_e164 == "+13135550101"
    f = r.fields
    assert (f["state"], f["product_line"], f["age"], f["external_id"]) == ("MI", "final_expense", 71, "X-1")
    assert f["acquired_at"] == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert f["consent_captured_at"] == datetime(2026, 10, 1, 14, 3, tzinfo=timezone.utc)
    assert r.raw["Favorite Color"] == "blue", "unmapped columns are preserved"


def test_spreadsheet_float_phone_is_handled():
    r = normalize_row(2, HEADERS, _row(**{"Other Phone 1": 3135550101.0}), build_column_map(HEADERS))
    assert r.phone_e164 == "+13135550101"


@pytest.mark.parametrize("mobile, other, code", [("", "", "missing_phone"), ("12", "555-555-0100", "invalid_phone")])
def test_bad_phones_reject_the_row(mobile, other, code):
    r = normalize_row(7, HEADERS, _row(**{"Mobile": mobile, "Other Phone 1": other}), build_column_map(HEADERS))
    assert r.rejected
    assert [i.code for i in r.issues if i.severity == "error"] == [code]


def test_unknown_state_is_a_warning_not_a_rejection():
    r = normalize_row(3, HEADERS, _row(State="Ontario"), build_column_map(HEADERS))
    assert not r.rejected and r.fields["state"] is None
    assert "unknown_state" in [i.code for i in r.issues]


def test_dnc_flag_is_read():
    r = normalize_row(3, HEADERS, _row(DNC="Yes"), build_column_map(HEADERS))
    assert r.fields["source_dnc"] is True


@pytest.mark.parametrize("raw", ["2026-10-01", "10/01/2026", "10/1/26", "2026-10-01T09:30:00Z", "Oct 01, 2026"])
def test_dates_parse(raw):
    assert parse_datetime(raw).date() == date(2026, 10, 1)


# ── Readers ──────────────────────────────────────────────────────────────────

def test_csv_and_xlsx_read_identically(tmp_path):
    from openpyxl import Workbook

    csv_path = tmp_path / "leads.csv"
    csv_path.write_text("First Name,Phone,State\nPat,313-555-0101,MI\n,,\nLee,(313) 555-0102,Texas\n", encoding="utf-8")
    wb = Workbook()
    ws = wb.active
    ws.append(["First Name", "Phone", "State"])
    ws.append(["Pat", 3135550101, "MI"])
    ws.append([None, None, None])
    ws.append(["Lee", "(313) 555-0102", "Texas"])
    xlsx_path = tmp_path / "leads.xlsx"
    wb.save(xlsx_path)

    for path, fmt in ((csv_path, "csv"), (xlsx_path, "xlsx")):
        assert detect_format(path) == fmt
        got_fmt, headers, rows = open_rows(path)
        rows = list(rows)
        assert got_fmt == fmt and headers == ["First Name", "Phone", "State"]
        assert [n for n, _ in rows] == [2, 4], "blank rows skipped, spreadsheet row numbers kept"
        m = build_column_map(headers)
        phones = [normalize_row(n, headers, v, m).phone_e164 for n, v in rows]
        assert phones == ["+13135550101", "+13135550102"]


def test_semicolon_csv_is_sniffed(tmp_path):
    p = tmp_path / "eu.csv"
    p.write_text("Name;Phone\nPat;3135550101\n", encoding="utf-8")
    _, headers, rows = open_rows(p)
    assert headers == ["Name", "Phone"] and list(rows)[0][1] == ["Pat", "3135550101"]


def test_file_hash_is_stable(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("x\n1\n")
    assert file_sha256(p) == file_sha256(p) and len(file_sha256(p)) == 64


# ── Authorization ────────────────────────────────────────────────────────────

def _ev(text=GOOD_TEXT, at=NOW - timedelta(days=30), url="https://form.example", cert=None, source=None):
    return ConsentEvidence(text=text, captured_at=at, url=url, certificate=cert, source=source)


def _cls(**kw):
    args = dict(use_authorized=True, consent=_ev(), sellers=[OURS], state="MI", supported_states=STATES, now=NOW)
    args.update(kw)
    return classify(**args)


def test_documented_consent_naming_us_authorizes_everything():
    a = _cls()
    assert (a.ai_voice, a.sms, a.manual_call, a.reasons, a.consent_status) == (True, True, True, [], "documented")


def test_consent_given_to_another_seller_is_quarantined():
    """The former-employer case: valid consent, wrong seller."""
    a = _cls(consent=_ev(text=GOOD_TEXT.replace(OURS, "Acme Final Expense LLC")))
    assert not a.ai_voice and not a.sms and a.manual_call
    assert "consent_other_seller" in a.reasons and a.consent_status == "other_seller"


def test_no_written_lead_use_authorization_blocks_every_channel():
    a = _cls(use_authorized=False)
    assert (a.ai_voice, a.sms, a.manual_call) == (False, False, False)
    assert "lead_use_unauthorized" in a.reasons


@pytest.mark.parametrize("kw, reason", [
    ({"consent": None}, "consent_missing"),
    ({"consent": _ev(at=None)}, "consent_undocumented"),
    ({"consent": _ev(url=None)}, "consent_undocumented"),
    ({"sellers": []}, "consent_seller_unverifiable"),
    ({"state": None}, "state_missing"),
    ({"state": "PR"}, "state_unsupported"),
    ({"source_dnc": True}, "source_dnc_flag"),
    ({"max_age_days": 10}, "consent_stale"),
])
def test_each_missing_element_quarantines_ai_voice(kw, reason):
    a = _cls(**kw)
    assert not a.ai_voice and reason in a.reasons


def test_certificate_or_vendor_source_documents_consent_without_url():
    assert _cls(consent=_ev(url=None, cert="https://cert.trustedform.com/x")).ai_voice
    assert _cls(consent=_ev(url=None, source="Vendor X")).ai_voice


def test_channel_scope_is_respected():
    calls_only = f"I agree {OURS} may call me using an autodialer or artificial voice."
    texts_only = f"I agree {OURS} may send me text messages."
    a, b = _cls(consent=_ev(text=calls_only)), _cls(consent=_ev(text=texts_only))
    assert (a.ai_voice, a.sms) == (True, False) and "consent_lacks_sms" in a.reasons
    assert (b.ai_voice, b.sms) == (False, True) and "consent_lacks_ai_voice" in b.reasons


def test_a_bare_yes_is_not_consent():
    a = _cls(consent=_ev(text="Yes"))
    assert not a.ai_voice and not a.sms


def test_seller_match_is_case_and_whitespace_insensitive():
    assert _cls(consent=_ev(text=GOOD_TEXT.replace(OURS, "EASTWOOD   insurance agency"))).ai_voice


def test_canadian_and_caribbean_numbers_are_quarantined():
    from domain.imports.mapping import phone_region_of

    assert phone_region_of("+13135550101") == "US"
    assert phone_region_of("+16135550101") == "CA"  # Ottawa: valid +1, not US
    a = _cls(phone_region="CA")
    assert not a.ai_voice and not a.manual_call and "non_us_number" in a.reasons
    r = normalize_row(2, HEADERS, _row(**{"Other Phone 1": "613-555-0101"}), build_column_map(HEADERS))
    assert r.fields["phone_region"] == "CA"


@pytest.mark.parametrize("raw,product", [
    ("Life Insurance", "life"), ("Term Life", "life"), ("life", "life"), ("Final Expense", "final_expense"),
    ("FE", "final_expense"), ("Burial insurance", "final_expense"), ("Whole Life", "final_expense"),
    ("Med Supp", "medicare_supplement"), ("MAPD", "medicare_advantage"), ("Annuity", "annuity"),
    ("Safe life plan", "life"), ("Coffee", None), ("Vacation", None), ("", None),
])
def test_products_match_whole_words_only(raw, product):
    from domain.normalization import normalize_product

    assert normalize_product(raw) == product
