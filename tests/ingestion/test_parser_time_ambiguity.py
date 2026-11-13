"""Source-grounded date ambiguity regressions with injected model replies."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import socket
from urllib import request as urllib_request

import pytest

from common.contracts import schemas
from tests.ingestion.test_guest_parser import assert_contract, llm, parser


# Captured from the cold ministral-3:8b workers=1 pilot, date_de_competing_days.
# Original response SHA256: a888f78ca0dc78b66b95cd98aa9df8aa8f5a537a4bbe7f7640b3be79e3c496a1.
CAPTURED_COMPETING_DAYS_RESPONSE = """{
  "intents": [
    {
      "intent": "early_checkin",
      "evidence_quote": "morgen oder übermorgen um 13:00 früher einchecken?",
      "requested_time_text": "morgen um 13:00",
      "requested_time_utc": "2026-01-06T12:00:00Z",
      "details": {
        "assistance_requested": "früher einchecken"
      },
      "needs_review": false
    },
    {
      "intent": "early_checkin",
      "evidence_quote": "morgen oder übermorgen um 13:00 früher einchecken?",
      "requested_time_text": "übermorgen um 13:00",
      "requested_time_utc": "2026-01-07T12:00:00Z",
      "details": {
        "assistance_requested": "früher einchecken"
      },
      "needs_review": false
    }
  ]
}"""


@pytest.fixture(autouse=True)
def local_module_and_no_network(parser, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    assert Path(parser.__file__).resolve() == root / "ingestion" / "guest_parser.py"
    assert Path(schemas.__file__).resolve() == root / "common" / "contracts" / "schemas.py"
    assert Path(assert_contract.__code__.co_filename).resolve() == root / "tests" / "ingestion" / "test_guest_parser.py"
    attempted = []

    def block_network(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("Date ambiguity regressions must not use the network")

    monkeypatch.setattr(urllib_request, "urlopen", block_network)
    monkeypatch.setattr(socket, "create_connection", block_network)
    monkeypatch.setattr(socket.socket, "connect", block_network)
    monkeypatch.setattr(socket.socket, "connect_ex", block_network)
    yield
    assert not attempted


@pytest.fixture
def request_case():
    record = {
        "request_id": "synthetic:date-ambiguity", "timestamp_utc": "2026-01-05T10:00:00Z",
        "room_id": "R101", "booking_id": "BK-SYNTHETIC", "channel": "app_chat",
        "language": "en", "request_type": "early_checkin", "text": "",
        "priority": "medium", "response_due_utc": "2026-01-05T13:00:00Z",
        "requires_guest_consent": "not_required",
    }
    context = {
        "timezone": "Europe/Vienna", "as_of_utc": record["timestamp_utc"],
        "booking": {
            "booking_id": record["booking_id"], "created_utc": "2026-01-04T10:00:00Z",
            "arrival_utc": "2026-01-06T14:00:00Z", "departure_utc": "2026-01-09T10:00:00Z",
        },
    }
    return record, context


def proposal(time_text, evidence, model_time, *, kind="early_checkin"):
    return {
        "intent": kind, "requested_time_text": time_text, "evidence_quote": evidence,
        "requested_time_utc": model_time, "details": {}, "needs_review": False,
    }


def extract(parser, llm, record, context, response):
    before = copy.deepcopy((record, context))
    calls = llm(response)
    items, meta = parser.parse_request_full(record, context)
    assert len(calls) == 1
    assert meta.method == "llm", "The host must validate the injected response, not hide it with fallback"
    assert_contract(record, items, meta)
    assert (record, context) == before
    return items, meta


def assert_unknown_date(items, meta):
    assert items and all(item["requested_time_utc"] is None for item in items)
    assert all(item["needs_review"] for item in items)
    assert meta.needs_review
    assert "requested_time_utc" in meta.uncertain_fields


def test_captured_two_intent_response_cannot_select_competing_dates(parser, llm):
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "parser_benchmark_cases.jsonl"
    case = next(case for case in map(json.loads, fixture.read_text(encoding="utf-8").splitlines())
                if case["case_id"] == "date_de_competing_days")

    items, meta = extract(parser, llm, case["record"], case["context"], CAPTURED_COMPETING_DAYS_RESPONSE)

    assert [item["intent"] for item in items] == ["early_checkin", "early_checkin"]
    assert_unknown_date(items, meta)


def test_suffix_quote_inside_longer_relative_word_cannot_ground_tomorrow(parser, llm, request_case):
    record, context = request_case
    record.update(language="de", text="Können wir übermorgen um 13:00 früher einchecken?")
    # The apparent quote occurs only as a suffix of the different date word.
    response = {"intents": [proposal("morgen um 13:00", record["text"], "2026-01-06T12:00:00Z")]}

    items, meta = extract(parser, llm, record, context, response)

    assert items[0]["requested_time_utc"] is None
    assert items[0]["needs_review"] and meta.needs_review
    assert {"requested_time_text", "requested_time_utc"} & set(meta.uncertain_fields)


@pytest.mark.parametrize("language,long_text,long_quote,short_quote,standalone_text", [
    ("en", "Can we check in the day after tomorrow at 13:00?", "day after tomorrow at 13:00",
     "tomorrow at 13:00", "Can we check in tomorrow at 13:00?"),
    ("fr", "Pouvons-nous faire le check-in après-demain à 13:00 ?", "après-demain à 13:00",
     "demain à 13:00", "Pouvons-nous faire le check-in demain à 13:00 ?"),
    ("es", "¿Podemos hacer el check-in pasado mañana a las 13:00?", "pasado mañana a las 13:00",
     "mañana a las 13:00", "¿Podemos hacer el check-in mañana a las 13:00?"),
], ids=["en", "fr", "es"])
@pytest.mark.parametrize("quote_mode", ["clipped", "complete", "standalone"])
def test_supported_relative_phrase_cannot_be_shortened_to_another_day(
    parser, llm, request_case, language, long_text, long_quote, short_quote, standalone_text, quote_mode,
):
    record, context = request_case
    record.update(language=language, text=standalone_text if quote_mode == "standalone" else long_text)
    quoted = long_quote if quote_mode == "complete" else short_quote
    model_time = "2026-01-07T12:00:00Z" if quote_mode == "complete" else "2026-01-06T12:00:00Z"
    response = {"intents": [proposal(quoted, record["text"], model_time)]}

    items, meta = extract(parser, llm, record, context, response)

    if quote_mode == "clipped":
        assert items[0]["requested_time_utc"] is None
        assert items[0]["needs_review"] and meta.needs_review
        assert {"requested_time_text", "requested_time_utc"} & set(meta.uncertain_fields)
    else:
        assert items[0]["requested_time_utc"] == model_time
        assert not items[0]["needs_review"] and not meta.needs_review


@pytest.mark.parametrize("language,text,selected_quote", [
    ("en", "Can we check in tomorrow or the day after tomorrow at 13:00?", "day after tomorrow at 13:00"),
    ("de", "Können wir morgen oder übermorgen um 13:00 früher einchecken?", "übermorgen um 13:00"),
    ("it", "Possiamo fare il check-in domani o dopodomani alle 13:00?", "dopodomani alle 13:00"),
    ("fr", "Pouvons-nous faire le check-in demain ou après-demain à 13:00 ?", "après-demain à 13:00"),
    ("es", "¿Podemos hacer el check-in mañana o pasado mañana a las 13:00?", "pasado mañana a las 13:00"),
    ("nl", "Kunnen we morgen of overmorgen om 13:00 inchecken?", "overmorgen om 13:00"),
    ("de", "Können wir am 06.01.2026 oder 07.01.2026 um 13:00 früher einchecken?", "07.01.2026 um 13:00"),
], ids=["en", "de", "it", "fr", "es", "nl", "numeric-dates"])
def test_narrow_quote_cannot_hide_supported_date_alternatives(
    parser, llm, request_case, language, text, selected_quote,
):
    record, context = request_case
    record.update(language=language, text=text)
    response = {"intents": [proposal(selected_quote, selected_quote, "2026-01-07T12:00:00Z")]}

    items, meta = extract(parser, llm, record, context, response)

    assert_unknown_date(items, meta)


@pytest.mark.parametrize("with_second_intent", [False, True], ids=["single", "multiple"])
def test_clock_only_quote_near_date_alternatives_does_not_use_booking_day(
    parser, llm, request_case, with_second_intent,
):
    record, context = request_case
    record["text"] = "Can we check in tomorrow or the day after tomorrow, at 13:00? I also need Wi-Fi help."
    response = {"intents": [proposal("13:00", "13:00", "2026-01-06T12:00:00Z")]}
    if with_second_intent:
        response["intents"].append(proposal(None, "Wi-Fi help", None, kind="wifi_help"))

    items, meta = extract(parser, llm, record, context, response)

    assert_unknown_date(items[:1], meta)
    if with_second_intent:
        assert items[1]["intent"] == "wifi_help" and items[1]["needs_review"] is False


def test_explanatory_clock_clause_cannot_hide_prior_date_alternatives(parser, llm, request_case):
    record, context = request_case
    record.update(language="de", text=(
        "Können wir morgen oder übermorgen früher einchecken, und zwar um 13:00? "
        "Wir brauchen WLAN."
    ))
    response = {"intents": [
        proposal("13:00", "13:00", "2026-01-06T12:00:00Z"),
        proposal(None, "WLAN", None, kind="wifi_help"),
    ]}

    items, meta = extract(parser, llm, record, context, response)

    assert_unknown_date(items[:1], meta)
    assert items[1]["intent"] == "wifi_help" and items[1]["needs_review"] is False


@pytest.mark.parametrize("language,text,first_evidence,first_quote,second_evidence,second_quote", [
    ("en", "Can we check in tomorrow at 13:00 and check out the day after tomorrow at 13:00?",
     "check in tomorrow at 13:00", "tomorrow at 13:00",
     "check out the day after tomorrow at 13:00", "day after tomorrow at 13:00"),
    ("de", "Können wir morgen um 13:00 früher einchecken und übermorgen um 13:00 später auschecken?",
     "morgen um 13:00 früher einchecken", "morgen um 13:00",
     "übermorgen um 13:00 später auschecken", "übermorgen um 13:00"),
], ids=["en-and", "de-und"])
def test_independently_dated_intents_in_one_sentence_remain_resolved(
    parser, llm, request_case, language, text, first_evidence, first_quote, second_evidence, second_quote,
):
    record, context = request_case
    record.update(language=language, text=text)
    response = {"intents": [
        proposal(first_quote, first_evidence, "2026-01-06T12:00:00Z"),
        proposal(second_quote, second_evidence, "2026-01-07T12:00:00Z", kind="late_checkout"),
    ]}

    items, meta = extract(parser, llm, record, context, response)

    assert [item["requested_time_utc"] for item in items] == ["2026-01-06T12:00:00Z", "2026-01-07T12:00:00Z"]
    assert not any(item["needs_review"] for item in items)
    assert not meta.needs_review and not meta.uncertain_fields


def test_full_source_evidence_preserves_complete_quote_before_later_suffix(parser, llm, request_case):
    record, context = request_case
    record.update(language="de", text=(
        "Können wir morgen um 13:00 früher einchecken "
        "und übermorgen um 13:00 später auschecken?"
    ))
    response = {"intents": [
        proposal("morgen um 13:00", record["text"], "2026-01-06T12:00:00Z"),
        proposal("übermorgen um 13:00", record["text"], "2026-01-07T12:00:00Z", kind="late_checkout"),
    ]}

    items, meta = extract(parser, llm, record, context, response)

    assert [item["requested_time_utc"] for item in items] == ["2026-01-06T12:00:00Z", "2026-01-07T12:00:00Z"]
    assert not any(item["needs_review"] for item in items)
    assert not meta.needs_review and not meta.uncertain_fields


def test_repeated_clock_without_date_evidence_cannot_select_one_request_day(parser, llm, request_case):
    record, context = request_case
    record.update(language="de", text=(
        "Können wir morgen um 13:00 früher einchecken "
        "und übermorgen um 13:00 später auschecken?"
    ))
    response = {"intents": [
        proposal("13:00", "13:00", "2026-01-06T12:00:00Z"),
        proposal("übermorgen um 13:00", "übermorgen um 13:00 später auschecken",
                 "2026-01-07T12:00:00Z", kind="late_checkout"),
    ]}

    items, meta = extract(parser, llm, record, context, response)

    assert_unknown_date(items[:1], meta)
    assert items[1]["requested_time_utc"] == "2026-01-07T12:00:00Z"
    assert items[1]["needs_review"] is False


def test_ambiguous_request_does_not_suppress_separate_explicit_checkout(parser, llm, request_case):
    record, context = request_case
    record["text"] = (
        "Can we check in tomorrow or the day after tomorrow at 13:00? "
        "We will check out on 09.01.2026 at 16:00."
    )
    response = {"intents": [
        proposal("day after tomorrow at 13:00", "day after tomorrow at 13:00", "2026-01-07T12:00:00Z"),
        proposal("09.01.2026 at 16:00", "check out on 09.01.2026 at 16:00", "2026-01-09T15:00:00Z", kind="late_checkout"),
    ]}

    items, meta = extract(parser, llm, record, context, response)

    assert_unknown_date(items[:1], meta)
    assert items[1]["requested_time_utc"] == "2026-01-09T15:00:00Z"
    assert items[1]["needs_review"] is False


@pytest.mark.parametrize("model_time,review", [
    (None, False), ("2026-10-25T00:30:00Z", True), ("2026-10-25T01:30:00Z", False),
], ids=["missing-model-time", "wrong-model-fold", "correct-model-fold"])
def test_explicit_cet_resolves_autumn_fold_without_date_ambiguity(
    parser, llm, request_case, model_time, review,
):
    record, context = request_case
    record.update(language="nl", request_type="arrival_delay", timestamp_utc="2026-10-24T10:00:00Z",
                  text="We komen morgen om 02:30 CET aan.")
    context["as_of_utc"] = record["timestamp_utc"]
    response = {"intents": [proposal("morgen om 02:30 CET", record["text"], model_time, kind="arrival_delay")]}

    items, meta = extract(parser, llm, record, context, response)

    assert items[0]["requested_time_utc"] == "2026-10-25T01:30:00Z"
    assert items[0]["needs_review"] is review and meta.needs_review is review
    assert "requested_time_utc" not in meta.uncertain_fields
    if review:
        assert "model_time_mismatch" in meta.uncertain_fields


@pytest.mark.parametrize("message_time,model_time", [
    ("2026-03-28T10:00:00Z", "2026-03-29T01:30:00Z"),
    ("2026-10-24T10:00:00Z", "2026-10-25T00:30:00Z"),
], ids=["spring-gap", "autumn-fold-without-zone"])
def test_genuine_gap_or_unqualified_fold_stays_unknown(parser, llm, request_case, message_time, model_time):
    record, context = request_case
    record.update(language="de", request_type="arrival_delay", timestamp_utc=message_time,
                  text="Wir kommen morgen um 02:30 an.")
    context["as_of_utc"] = message_time
    response = {"intents": [proposal("morgen um 02:30", record["text"], model_time, kind="arrival_delay")]}

    items, meta = extract(parser, llm, record, context, response)

    assert_unknown_date(items, meta)
