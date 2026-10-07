"""Listing -> knowledge pack.

A voice agent cannot be handed the raw listing JSON. Two reasons:

1. Nulls. If `year_built` is None and the model sees `"year_built": null`, it
   fills the gap, because that is what language models do. A caller asking
   "how old is the building?" then gets a confident wrong answer on a sales
   call. So the pack never contains an empty field: a fact is either stated or
   listed under "things you do not know", with the exact sentence to say.

2. Speech. "$1,095.27" and "38 IANNUZZI ST" are read badly by every TTS engine
   I have tested. The pack carries a spoken form next to the written one.

The pack is deliberately small. Nova Sonic holds the whole conversation in one
bidirectional session, and every token in the system prompt is latency on the
first word the caller hears.
"""

from __future__ import annotations

import re
from typing import Optional

from ingest.schema import Listing


# What a buyer asks in the first ninety seconds, in the order they ask it.
_HEADLINE_ORDER = [
    "address", "price", "property_type", "bedrooms", "bathrooms",
    "size", "parking", "maintenance", "taxes", "exposure",
]


def _spoken_number(n: float) -> str:
    """Nova Sonic and Polly both read bare digits well, but a trailing ".00"
    becomes "point zero zero". Round money that is whole."""
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.2f}"


def _beds_phrase(listing: Listing) -> Optional[str]:
    f = listing.facts
    if f.bedrooms is None:
        return None
    if f.bedrooms == 0:
        return "a studio, so no separate bedroom"
    n = int(f.bedrooms) if f.bedrooms == int(f.bedrooms) else f.bedrooms
    base = f"{n} bedroom" + ("s" if n != 1 else "")
    if f.bedrooms_plus:
        # Never add the den to the bedroom count. A "2+1" is a two bedroom with
        # a den, and selling it as a three bedroom is the single fastest way to
        # lose a buyer at the viewing.
        base += f" plus a den ({n}+{f.bedrooms_plus} on the listing)"
    return base


def _baths_phrase(listing: Listing) -> Optional[str]:
    b = listing.facts.bathrooms
    if b is None:
        return None
    n = int(b) if b == int(b) else b
    return f"{n} bathroom" + ("s" if n != 1 else "")


def build_facts(listing: Listing) -> dict:
    """Every fact we are willing to state, keyed for the headline order."""
    a, f = listing.address, listing.facts
    facts: dict[str, str] = {}

    if a.street:
        facts["address"] = a.one_line()
    if listing.price.amount is not None:
        facts["price"] = f"${_spoken_number(listing.price.amount)}"
    facts["property_type"] = normalise_type(listing)
    beds = _beds_phrase(listing)
    if beds:
        facts["bedrooms"] = beds
    baths = _baths_phrase(listing)
    if baths:
        facts["bathrooms"] = baths
    if f.size_interior_sqft:
        facts["size"] = f.size_interior_sqft
    if f.parking_spaces is not None:
        facts["parking"] = (f"{f.parking_spaces} parking space"
                            + ("s" if f.parking_spaces != 1 else "")
                            if f.parking_spaces else "no parking included")
    if listing.maintenance_fee.amount is not None:
        facts["maintenance"] = f"${_spoken_number(listing.maintenance_fee.amount)} a month"
    if listing.taxes_annual.amount is not None:
        facts["taxes"] = f"${_spoken_number(listing.taxes_annual.amount)} a year"
    if f.exposure:
        facts["exposure"] = f"{f.exposure} facing"
    if f.locker:
        facts["locker"] = f.locker
    if f.balcony:
        facts["balcony"] = f.balcony
    if f.heating:
        facts["heating"] = f.heating
    if f.cooling:
        facts["cooling"] = f.cooling
    if f.year_built:
        facts["year_built"] = f.year_built
    if f.pets:
        facts["pets"] = f.pets
    return facts


# Caller questions we must be able to answer, and what to say when we cannot.
_EXPECTED = {
    "size": "the exact square footage",
    "maintenance": "the monthly maintenance fee",
    "taxes": "the annual property tax",
    "parking": "whether parking is included",
    "year_built": "the year the building went up",
    "pets": "the pet policy",
    "exposure": "which way the unit faces",
}


def build_unknowns(facts: dict) -> list:
    return [label for key, label in _EXPECTED.items() if key not in facts]


def spoken_address(listing: Listing) -> str:
    """Strip what a TTS engine mispronounces. Unit numbers are read digit by
    digit because "unit one thousand five hundred and three" is not how anyone
    says 1503."""
    a = listing.address
    bits = []
    if a.unit:
        bits.append("unit " + " ".join(a.unit))
    if a.street:
        bits.append(re.sub(r"\bON\b", "Ontario", a.street))
    if a.neighbourhood:
        bits.append(f"in {a.neighbourhood}")
    elif a.city:
        bits.append(f"in {a.city}")
    return ", ".join(bits)


def one_breath_pitch(listing: Listing) -> str:
    """The answer to "tell me about it". One sentence, said in one breath,
    because a caller interrupts anything longer."""
    facts = build_facts(listing)
    f = listing.facts
    kind = normalise_type(listing)
    where = listing.address.neighbourhood or listing.address.city or "the area"
    price = facts.get("price")
    tail = f", asking {price}" if price else ""
    baths = _baths_phrase(listing)

    if f.bedrooms == 0:
        # "a a studio, so no separate bedroom and 1 bathroom condo" is what you
        # get from gluing the generic phrases together. Studios need their own.
        head = f"It is a studio {kind} in {where}"
        if baths:
            head += f" with {baths}"
        return head + tail + "."

    # Used as an adjective, so singular: "a 2 bedroom, 2 bathroom loft", not
    # "a 2 bedrooms and 2 bathrooms loft".
    parts = []
    if f.bedrooms is not None:
        n = int(f.bedrooms) if f.bedrooms == int(f.bedrooms) else f.bedrooms
        parts.append(f"{n}+{f.bedrooms_plus}" if f.bedrooms_plus else f"{n} bedroom")
    if f.bathrooms is not None:
        b = int(f.bathrooms) if f.bathrooms == int(f.bathrooms) else f.bathrooms
        parts.append(f"{b} bathroom")
    body = ", ".join(parts)
    return f"It is a {body} {kind} in {where}{tail}." if body \
        else f"It is a {kind} in {where}{tail}."


# MLS property types are coarse ("Residential") and say nothing a caller cares
# about. The maintenance fee is the reliable tell for a condo.
_GENERIC_TYPES = {"residential", "unknown", "other", "freehold", "", "none"}


def normalise_type(listing: Listing) -> str:
    raw = (listing.facts.property_type or "").strip().lower()
    if raw in _GENERIC_TYPES:
        return "condo" if listing.maintenance_fee.amount else "home"
    return raw


def trim_description(text: Optional[str], max_chars: int = 900) -> Optional[str]:
    """MLS remarks are written in Title Case With Every Word Capitalised and run
    to 2000 characters. Spoken verbatim they sound like a robot reading a
    brochure, so the pack passes them as reference material the agent
    paraphrases, trimmed at a sentence boundary."""
    if not text:
        return None
    text = re.sub(r"\(id:\d+\)\s*$", "", text).strip()
    # MLS remarks come in Title Case. Leave it: a speech model does not
    # pronounce "Sun-Filled" any differently from "sun-filled", and every
    # de-capitalising rule I tried also flattened the proper nouns
    # ("fortune condos in fort york"), which does change the pronunciation.
    # What does need fixing is the run-together words the feed produces when
    # a field is concatenated without a separator.
    text = re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    return cut[:stop + 1] if stop > max_chars * 0.5 else cut.rstrip() + "..."


def build_pack(listing: Listing) -> dict:
    facts = build_facts(listing)
    n = listing.neighbourhood
    s = listing.stats

    area = {}
    if n.name:
        area["name"] = n.name
    if n.summary:
        area["summary"] = trim_description(n.summary, 700)
    for label, value in (("walk score", n.walk_score),
                         ("transit score", n.transit_score),
                         ("bike score", n.bike_score)):
        if value is not None:
            area[label] = f"{value} out of 100"

    market = {}
    if s.median_sale_price is not None:
        market["median sale price in the area"] = f"${_spoken_number(s.median_sale_price)}"
    if s.average_sale_price is not None:
        market["average sale price in the area"] = f"${_spoken_number(s.average_sale_price)}"
    if s.average_days_on_market is not None:
        market["average days on market"] = f"{s.average_days_on_market:g} days"
    if s.active_listings is not None:
        market["active listings in the area"] = str(s.active_listings)
    if s.sale_to_list_ratio is not None:
        market["sale to list ratio"] = f"{s.sale_to_list_ratio:g}%"
    if s.period:
        market["figures cover"] = s.period

    return {
        "listing_id": listing.listing_id,
        "status": listing.status,
        "spoken_address": spoken_address(listing),
        "pitch": one_breath_pitch(listing),
        "facts": {k: facts[k] for k in _HEADLINE_ORDER if k in facts}
        | {k: v for k, v in facts.items() if k not in _HEADLINE_ORDER},
        "features": listing.features[:14],
        "description": trim_description(listing.description),
        "area": area,
        "market": market,
        "do_not_know": build_unknowns(facts),
        "listed_by": listing.brokerage,
        "source": listing.source,
    }


def pack_as_prompt_block(pack: dict) -> str:
    """Rendered into the system prompt. Plain lines beat JSON here: the model
    copies the phrasing it sees, and we want it copying spoken English."""
    lines = [f"LISTING {pack['listing_id']} ({pack['status']})",
             f"Address, say it like this: {pack['spoken_address']}",
             f"One line pitch: {pack['pitch']}", "", "FACTS YOU MAY STATE:"]
    for k, v in pack["facts"].items():
        lines.append(f"  {k.replace('_', ' ')}: {v}")
    if pack["features"]:
        lines += ["", "FEATURES AND AMENITIES:", "  " + ", ".join(pack["features"])]
    if pack["description"]:
        lines += ["", "LISTING REMARKS (paraphrase, never read out verbatim):",
                  "  " + pack["description"]]
    if pack["area"]:
        lines += ["", "NEIGHBOURHOOD:"]
        lines += [f"  {k}: {v}" for k, v in pack["area"].items()]
    if pack["market"]:
        lines += ["", "AREA MARKET STATISTICS:"]
        lines += [f"  {k}: {v}" for k, v in pack["market"].items()]
    if pack["do_not_know"]:
        lines += ["", "YOU DO NOT HAVE THESE. If asked, say you will confirm and "
                      "follow up, and do not estimate:",
                  "  " + "; ".join(pack["do_not_know"])]
    if pack["listed_by"]:
        lines += ["", f"Listed by {pack['listed_by']}."]
    return "\n".join(lines)
