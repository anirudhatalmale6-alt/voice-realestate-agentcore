"""Runs with no AWS account, no API key and no network.

Everything that could put a wrong number in a caller's ear, or one store's
listing in another store's call, is checked here, against the six real
listings in samples/.

    python -m pytest tests -q
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from ingest.extract import extract_listing, to_money, beds_from_text          # noqa: E402
from ingest.extract import _is_place_name, _looks_like_heading                # noqa: E402
from ingest.schema import Listing                                             # noqa: E402
from knowledge.pack import build_pack, trim_description, one_breath_pitch     # noqa: E402
from agent.store import JsonStore, normalise_e164                             # noqa: E402
from agent.tools import ToolBox, _resolve_day, _digits_to_e164                # noqa: E402
from agent.session import CallSession, UnroutedNumber                         # noqa: E402
from agent.prompts import build_system_prompt                                 # noqa: E402


PAGES = sorted(p for p in (ROOT / "samples" / "pages").glob("remax_*.html")
               if "search" not in p.name)


@pytest.fixture(scope="module")
def store():
    return JsonStore()


@pytest.fixture(scope="module")
def listings():
    return [Listing.from_dict(json.loads(p.read_text(encoding="utf-8")))
            for p in sorted((ROOT / "samples" / "listings").glob("*.json"))]


# ---------------------------------------------------------------- extraction

def test_sample_pages_exist():
    assert len(PAGES) >= 6, "sample pages missing, re-run the capture step"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_page_extracts_without_warnings(page):
    listing = extract_listing(page.read_text(encoding="utf-8", errors="replace"))
    assert listing.warnings == [], listing.warnings
    assert listing.price.amount and listing.price.amount > 50_000
    assert listing.address.street
    assert listing.listing_id and not listing.listing_id.startswith("unknown-")


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_unit_number_is_not_left_in_the_street(page):
    """"1503 - 38 Iannuzzi Street" read aloud is unintelligible."""
    listing = extract_listing(page.read_text(encoding="utf-8", errors="replace"))
    assert " - " not in listing.address.street


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_neighbourhood_name_is_a_place_not_a_sentence(page):
    listing = extract_listing(page.read_text(encoding="utf-8", errors="replace"))
    name = listing.neighbourhood.name
    assert name, "no neighbourhood found"
    assert _is_place_name(name), f"{name!r} is prose, not a place name"
    # The bug this guards: a "Neighbourhood" heading immediately followed by
    # the opening words of the area paragraph.
    assert not name.lower().startswith(("the character", "this neighbourhood"))


def test_money_parsing():
    assert to_money("$899,000") == 899000.0
    assert to_money("1,234.56") == 1234.56
    assert to_money("328.04") == 328.04
    assert to_money("Contact agent") is None
    assert to_money(None) is None
    assert to_money("$2,344.60") == 2344.60


def test_den_is_never_counted_as_a_bedroom():
    """A 2+1 is a two bedroom with a den. Selling it as a three bedroom is how
    you lose the buyer at the door."""
    assert beds_from_text("2+1") == (2.0, 1)
    assert beds_from_text("3") == (3.0, None)
    pack = build_pack(_fake_listing(bedrooms=2, bedrooms_plus=1))
    assert "plus a den" in pack["facts"]["bedrooms"]
    assert "3 bedroom" not in pack["facts"]["bedrooms"]


def test_section_headings_are_not_mistaken_for_values():
    assert _looks_like_heading("CLIMATE RISK FOR M5V 0S2")
    assert _looks_like_heading("Parking Features")
    assert _looks_like_heading("MAINTENANCE FEATURES")
    assert not _looks_like_heading("Central Air")
    assert not _looks_like_heading("328.04")


# ------------------------------------------------------------ knowledge pack

@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_pack_never_contains_a_null(page):
    """A null in the prompt is an invitation to make something up."""
    pack = build_pack(extract_listing(
        page.read_text(encoding="utf-8", errors="replace")))
    blob = json.dumps(pack)
    assert "null" not in blob
    assert ": None" not in blob
    for value in pack["facts"].values():
        assert value not in (None, "", "None", "null")


def test_unknowns_are_declared_rather_than_filled():
    listing = _fake_listing(bedrooms=2, bathrooms=1)
    pack = build_pack(listing)
    assert "the annual property tax" in pack["do_not_know"]
    assert "taxes" not in pack["facts"]


def test_studio_pitch_is_grammatical():
    """"It is a a studio, so no separate bedroom and 1 bathroom condo" is what
    the generic phrase builder produced before studios got their own path."""
    pitch = one_breath_pitch(_fake_listing(bedrooms=0, bathrooms=1,
                                           maintenance=328.04))
    assert pitch.startswith("It is a studio condo")
    assert " a a " not in pitch
    assert "bedrooms" not in pitch


def test_pitch_uses_singular_adjectives():
    pitch = one_breath_pitch(_fake_listing(bedrooms=2, bathrooms=2))
    assert "2 bedroom, 2 bathroom" in pitch
    assert "bedrooms and" not in pitch


def test_run_together_words_in_remarks_are_split():
    assert "Hour Concierge" in trim_description("24-HourConcierge/Security services.")


def test_remarks_keep_their_proper_nouns():
    """An earlier version lowercased MLS Title Case and turned "Fort York"
    into "fort york", which changes how a speech model pronounces it."""
    out = trim_description("Sun-Filled Studio At Fortune Condos In Fort York.")
    assert "Fort York" in out
    assert "Fortune Condos" in out


def test_long_remarks_are_cut_at_a_sentence():
    text = ("First sentence here. " * 80).strip()
    out = trim_description(text, max_chars=200)
    assert len(out) <= 205
    assert out.endswith(".")


# ------------------------------------------------------------- multi-tenancy

def test_e164_normalisation():
    assert normalise_e164("416 555 0101") == "+14165550101"
    assert normalise_e164("+1 (416) 555-0101") == "+14165550101"
    assert normalise_e164("14165550101") == "+14165550101"
    assert normalise_e164("") == ""


def test_each_number_routes_to_its_own_tenant(store):
    assert store.tenant_for_number("+14165550101").tenant_id == "harbourfront"
    assert store.tenant_for_number("4165550102").tenant_id == "northyork"
    assert store.tenant_for_number("+14165559999") is None


def test_a_tenant_cannot_read_another_tenants_listing(store):
    """The whole point of the store layer. "C13870448" belongs to North York;
    a caller on Harbourfront's line must not be able to reach it by reading
    the MLS number out loud."""
    assert store.get_listing("northyork", "C13870448") is not None
    assert store.get_listing("harbourfront", "C13870448") is None


def test_duplicate_phone_numbers_fail_at_load(tmp_path):
    config = {"tenants": [
        {"tenant_id": "a", "display_name": "A", "phone_number": "+14165550101",
         "listing_ids": []},
        {"tenant_id": "b", "display_name": "B", "phone_number": "416-555-0101",
         "listing_ids": []},
    ]}
    path = tmp_path / "tenants.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="claimed by both"):
        JsonStore(tenants_path=str(path))


def test_unrouted_number_refuses_the_call(store):
    with pytest.raises(UnroutedNumber):
        CallSession(store, "+14165559999")


def test_prompt_contains_only_this_tenants_listings(store):
    session = CallSession(store, "+14165550101")
    prompt = session.system_prompt
    assert "C13868410" in prompt                 # Harbourfront's
    assert "C13870448" not in prompt             # North York's
    assert "Harbourfront Realty" in prompt
    assert "North York Home Group" not in prompt


def test_prompt_forbids_markdown_and_caps_length(store):
    prompt = CallSession(store, "+14165550101").system_prompt
    assert "markdown" in prompt.lower()
    assert "one or two sentences" in prompt.lower()


def test_tenant_voice_and_name_are_per_tenant(store):
    a = store.tenant_for_number("+14165550101")
    b = store.tenant_for_number("+14165550102")
    assert (a.agent_name, a.voice_id) != (b.agent_name, b.voice_id)


# -------------------------------------------------------------------- tools

@pytest.fixture
def box(store):
    tenant = store.tenant_for_number("+14165550101")
    return ToolBox(store, tenant, bookings=[], today=datetime.date(2026, 10, 7))


def test_search_respects_the_budget(box):
    out = box.search_listings(max_price=400_000)
    assert out["count"] == 1
    assert out["listings"][0]["listing_id"] == "C13868410"


def test_search_never_returns_more_than_three(box, store):
    tenant = store.tenant_for_number("+14165550101")
    tenant.listing_ids = tenant.listing_ids * 3      # pretend a bigger book
    out = ToolBox(store, tenant).search_listings()
    assert out["showing"] <= 3
    assert out["more_available"] == out["count"] - out["showing"]
    tenant.listing_ids = tenant.listing_ids[:3]


def test_search_with_unknown_bedrooms_does_not_match_a_minimum(box):
    """A listing whose bedroom count we failed to extract must not be offered
    to a caller who said "at least two bedrooms"."""
    out = box.search_listings(min_bedrooms=2)
    ids = [l["listing_id"] for l in out["listings"]]
    assert "C13868410" not in ids                    # the studio


def test_search_matches_a_partial_neighbourhood(box):
    out = box.search_listings(neighbourhood="Liberty Village")
    assert any(l["listing_id"] == "C13868410" for l in out["listings"])


def test_empty_search_carries_the_sentence_to_say(box):
    out = box.search_listings(max_price=1000)
    assert out["count"] == 0
    assert "Harbourfront Realty" in out["say_if_empty"]


def test_details_for_another_tenants_listing_leaks_nothing(box):
    out = box.get_listing_details("C13870448")
    assert out["error"] == "not_found"
    # Must not confirm the listing exists somewhere else.
    assert "North York" not in json.dumps(out)


def test_mls_number_from_speech_is_tolerated(box):
    """Speech to text writes "C 13868410" or "c-13868410"."""
    for spoken in ["C 13868410", "c13868410", "C-13868410", " C13868410 "]:
        assert box.get_listing_details(spoken)["listing_id"] == "C13868410"


def test_booking_resolves_a_weekday_not_a_date(box):
    out = box.book_viewing(listing_id="C13868410", caller_name="Sam",
                           callback_number="four one six five five five one two one two",
                           preferred_day="Saturday", preferred_time="2pm")
    assert out["ok"] is False          # the number was words, not digits
    out = box.book_viewing(listing_id="C13868410", caller_name="Sam",
                           callback_number="416 555 1212",
                           preferred_day="Saturday", preferred_time="2pm")
    assert out["ok"] is True
    assert "Saturday October 10" in out["say"]
    assert out["reference"].startswith("HAR-")


def test_booking_is_idempotent(box):
    args = dict(listing_id="C13868410", caller_name="Sam",
                callback_number="416 555 1212", preferred_day="Saturday",
                preferred_time="2pm")
    first = box.book_viewing(**args)
    second = box.book_viewing(**args)
    assert second["duplicate"] is True
    assert second["reference"] == first["reference"]
    assert len(box.bookings) == 1


def test_booking_rejects_a_past_date(box):
    out = box.book_viewing(listing_id="C13868410", caller_name="Sam",
                           callback_number="4165551212",
                           preferred_day="2026-10-01", preferred_time="2pm")
    assert out["ok"] is False
    assert "passed" in out["say"]


def test_day_resolution():
    monday = datetime.date(2026, 10, 5)              # a Monday
    assert _resolve_day("today", monday) == monday
    assert _resolve_day("tomorrow", monday) == datetime.date(2026, 10, 6)
    assert _resolve_day("Monday", monday) == datetime.date(2026, 10, 12)
    assert _resolve_day("next Monday", monday) == datetime.date(2026, 10, 19)
    assert _resolve_day("Friday", monday) == datetime.date(2026, 10, 9)
    assert _resolve_day("2026-12-24", monday) == datetime.date(2026, 12, 24)
    assert _resolve_day("whenever", monday) is None


def test_callback_number_parsing():
    assert _digits_to_e164("(416) 555-1212") == "+14165551212"
    assert _digits_to_e164("1 416 555 1212") == "+14165551212"
    assert _digits_to_e164("555-1212") is None       # not ten digits


def test_transfer_without_a_handoff_number_still_says_something(store):
    tenant = store.tenant_for_number("+14165550101")
    saved, tenant.handoff_number = tenant.handoff_number, None
    out = ToolBox(store, tenant).transfer_to_human(reason="wants to negotiate")
    assert out["transferred"] is False
    assert "call you back" in out["say"]
    tenant.handoff_number = saved


def test_unknown_tool_name_does_not_crash_the_call(box):
    assert "no such tool" in box.call("get_mortgage_rate", {})["error"]


def test_bad_arguments_do_not_crash_the_call(box):
    assert "bad arguments" in box.call("get_listing_details", {"mls": "x"})["error"]


# ------------------------------------------------------------------ helpers

def _fake_listing(bedrooms=None, bedrooms_plus=None, bathrooms=None,
                  maintenance=None) -> Listing:
    return Listing.from_dict({
        "listing_id": "TEST1", "source": "manual",
        "price": {"amount": 500_000.0, "currency": "CAD"},
        "maintenance_fee": {"amount": maintenance, "currency": "CAD",
                            "period": "month"},
        "address": {"street": "1 Test Street", "city": "Toronto",
                    "neighbourhood": "Testville", "province": "ON"},
        "facts": {"bedrooms": bedrooms, "bedrooms_plus": bedrooms_plus,
                  "bathrooms": bathrooms},
        "description": "A home.",
    })
