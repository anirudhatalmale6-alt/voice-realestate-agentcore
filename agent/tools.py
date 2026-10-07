"""The tools the voice agent can call, and their JSON schemas.

Design rules that come from this being a *phone* agent rather than a chat one:

* Every tool returns something short enough to say out loud. A tool that
  returns twelve listings produces a thirty second monologue the caller will
  talk over, so search caps at three and says how many more there are.

* Every tool takes tenant_id from the session, never from the model. The
  schemas below do not expose tenant_id at all, so there is no argument the
  model could get wrong or a caller could talk it into changing.

* Nothing here writes to a system of record except book_viewing, and that one
  is idempotent on (listing_id, phone, slot) because a caller who repeats
  themselves on a bad line must not create two appointments.
"""

from __future__ import annotations

import datetime
import re
from typing import Optional

from knowledge.pack import build_pack, normalise_type
from .store import Store, Tenant


# --------------------------------------------------------------------------
# schemas, in the shape Bedrock Converse / Nova Sonic toolSpec expects
# --------------------------------------------------------------------------

TOOL_SPECS = [
    {
        "name": "search_listings",
        "description": (
            "Find homes this office has for sale that match what the caller "
            "described. Use it as soon as the caller mentions a budget, a "
            "number of bedrooms, or an area. Returns at most three."),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "max_price": {"type": "number",
                              "description": "Top of the caller's budget in dollars."},
                "min_bedrooms": {"type": "number",
                                 "description": "Fewest bedrooms they will accept. Use 0 for a studio."},
                "neighbourhood": {"type": "string",
                                  "description": "Area or neighbourhood the caller named."},
                "property_type": {"type": "string",
                                  "description": "condo, house, townhouse or loft, if they said."},
            },
            "required": [],
        }},
    },
    {
        "name": "get_listing_details",
        "description": (
            "Everything known about one home: price, rooms, fees, taxes, "
            "amenities and the listing remarks. Call this before answering any "
            "specific question about a property."),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"listing_id": {"type": "string",
                                          "description": "The MLS number from a previous search."}},
            "required": ["listing_id"],
        }},
    },
    {
        "name": "get_neighbourhood",
        "description": (
            "What the area around a home is like, plus walk, transit and bike "
            "scores and local market statistics. Use it when the caller asks "
            "about the area, schools, transit, or what prices are doing."),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"listing_id": {"type": "string"}},
            "required": ["listing_id"],
        }},
    },
    {
        "name": "book_viewing",
        "description": (
            "Book the caller in to see a home. Only call this once you have "
            "read the day, time and their phone number back to them and they "
            "have confirmed."),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "listing_id": {"type": "string"},
                "caller_name": {"type": "string"},
                "callback_number": {"type": "string",
                                    "description": "Digits as the caller said them."},
                "preferred_day": {"type": "string",
                                  "description": "A date like 2026-10-11, or a weekday name."},
                "preferred_time": {"type": "string", "description": "e.g. 2pm"},
            },
            "required": ["listing_id", "caller_name", "callback_number",
                         "preferred_day", "preferred_time"],
        }},
    },
    {
        "name": "transfer_to_human",
        "description": (
            "Hand the call to a person. Call this the moment the caller asks "
            "for a human, sounds frustrated, wants to negotiate a price, or "
            "asks something you were told you do not know."),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        }},
    },
]


# --------------------------------------------------------------------------
# implementations
# --------------------------------------------------------------------------

class ToolBox:
    """Bound to one tenant for the life of one call."""

    def __init__(self, store: Store, tenant: Tenant, bookings: Optional[list] = None,
                 today: Optional[datetime.date] = None):
        self.store = store
        self.tenant = tenant
        self.bookings = bookings if bookings is not None else []
        self.today = today or datetime.date.today()

    # ---- dispatch ----

    def call(self, name: str, args: dict) -> dict:
        fn = getattr(self, name, None)
        if fn is None or name.startswith("_"):
            return {"error": f"no such tool: {name}"}
        try:
            return fn(**args)
        except TypeError as exc:
            # The model passed an argument the tool does not take. Say so in a
            # form it can recover from rather than crashing the call.
            return {"error": f"bad arguments for {name}: {exc}"}

    # ---- tools ----

    def search_listings(self, max_price: Optional[float] = None,
                        min_bedrooms: Optional[float] = None,
                        neighbourhood: Optional[str] = None,
                        property_type: Optional[str] = None) -> dict:
        results = []
        for listing in self.store.list_listings(self.tenant.tenant_id):
            if listing.status != "active":
                continue
            if max_price is not None and listing.price.amount is not None \
                    and listing.price.amount > max_price:
                continue
            if min_bedrooms is not None:
                beds = listing.facts.bedrooms
                # An unknown bedroom count is not a match for "at least 2". Let
                # it through only when the caller set no floor.
                if beds is None or beds < min_bedrooms:
                    continue
            if neighbourhood and not _area_matches(listing, neighbourhood):
                continue
            if property_type and property_type.lower() not in normalise_type(listing):
                continue
            results.append(listing)

        results.sort(key=lambda l: l.price.amount if l.price.amount is not None else 1e12)
        shown = results[:3]
        return {
            "count": len(results),
            "showing": len(shown),
            "more_available": max(0, len(results) - len(shown)),
            "listings": [{
                "listing_id": l.listing_id,
                "summary": build_pack(l)["pitch"],
            } for l in shown],
            "say_if_empty": (
                f"I do not have anything matching that right now at "
                f"{self.tenant.display_name}, but I can take your details and "
                f"call you the moment something comes up."
            ) if not results else None,
        }

    def get_listing_details(self, listing_id: str) -> dict:
        listing = self.store.get_listing(self.tenant.tenant_id, _tidy_id(listing_id))
        if listing is None:
            return _not_ours(listing_id, self.tenant)
        pack = build_pack(listing)
        return {
            "listing_id": pack["listing_id"],
            "spoken_address": pack["spoken_address"],
            "facts": pack["facts"],
            "features": pack["features"],
            "remarks": pack["description"],
            "do_not_know": pack["do_not_know"],
            "listed_by": pack["listed_by"],
        }

    def get_neighbourhood(self, listing_id: str) -> dict:
        listing = self.store.get_listing(self.tenant.tenant_id, _tidy_id(listing_id))
        if listing is None:
            return _not_ours(listing_id, self.tenant)
        pack = build_pack(listing)
        out = {"listing_id": pack["listing_id"], "area": pack["area"],
               "market": pack["market"]}
        if not pack["area"] and not pack["market"]:
            out["say"] = ("I do not have the area profile for that one in front "
                          "of me, but I can have someone send it over.")
        return out

    def book_viewing(self, listing_id: str, caller_name: str, callback_number: str,
                     preferred_day: str, preferred_time: str) -> dict:
        listing = self.store.get_listing(self.tenant.tenant_id, _tidy_id(listing_id))
        if listing is None:
            return _not_ours(listing_id, self.tenant)

        number = _digits_to_e164(callback_number)
        if number is None:
            return {"ok": False,
                    "say": "I did not catch all ten digits of that number. "
                           "Could you give it to me one more time?"}

        day = _resolve_day(preferred_day, self.today)
        if day is None:
            return {"ok": False,
                    "say": f"I did not catch the day. Did you mean this week or next?"}
        if day < self.today:
            return {"ok": False,
                    "say": "That date has already passed. What day this coming week suits you?"}

        key = (listing.listing_id, number, day.isoformat(), preferred_time.lower().strip())
        for existing in self.bookings:
            # A caller on a bad line repeats themselves. Two appointments for
            # one person wastes an agent's afternoon, so booking is idempotent.
            if existing["key"] == key:
                return {"ok": True, "duplicate": True,
                        "reference": existing["reference"],
                        "say": f"You are already down for {_say_date(day)} "
                               f"at {preferred_time}. I have not booked it twice."}

        reference = f"{self.tenant.tenant_id[:3].upper()}-{len(self.bookings) + 1:04d}"
        self.bookings.append({
            "key": key, "reference": reference, "tenant_id": self.tenant.tenant_id,
            "listing_id": listing.listing_id, "caller_name": caller_name.strip(),
            "callback_number": number, "date": day.isoformat(),
            "time": preferred_time.strip(),
            "address": listing.address.one_line(),
        })
        return {
            "ok": True, "reference": reference,
            "say": (f"Booked. {caller_name.strip()}, {_say_date(day)} at "
                    f"{preferred_time} at {listing.address.one_line()}. "
                    f"Your reference is {reference} and we will text "
                    f"{_speak_number(number)} to confirm."),
        }

    def transfer_to_human(self, reason: str) -> dict:
        if not self.tenant.handoff_number:
            return {"transferred": False,
                    "say": (f"Our team is in {self.tenant.business_hours}. "
                            f"Let me take your number and have someone call you back.")}
        return {"transferred": True, "to": self.tenant.handoff_number, "reason": reason,
                "say": "Of course, putting you through to one of our agents now."}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _not_ours(listing_id: str, tenant: Tenant) -> dict:
    """Deliberately does not say whether the listing exists elsewhere. A caller
    probing MLS numbers should learn nothing about another tenant's book."""
    return {"error": "not_found", "listing_id": listing_id,
            "say": f"I do not have that one on our books at {tenant.display_name}. "
                   f"Shall I tell you what we do have?"}


def _tidy_id(listing_id: str) -> str:
    """Speech to text writes an MLS number as "C 13868410" or "c13868410"."""
    return re.sub(r"[\s\-]", "", str(listing_id)).upper()


def _area_matches(listing, query: str) -> bool:
    q = query.strip().lower()
    hay = " ".join(filter(None, [listing.address.neighbourhood, listing.address.city,
                                 listing.neighbourhood.name or ""])).lower()
    if q in hay:
        return True
    # "Liberty Village" should match "Fort York-Liberty Village", and a caller
    # saying "downtown" should not match nothing just because no field says it.
    return any(word in hay for word in q.split() if len(word) > 3)


def _digits_to_e164(spoken: str) -> Optional[str]:
    digits = "".join(c for c in str(spoken) if c.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def _say_date(d: datetime.date) -> str:
    """strftime("%-d") strips the leading zero on Linux and raises ValueError on
    Windows. The client develops on Windows, so build it by hand."""
    return f"{d.strftime('%A %B')} {d.day}"


def _speak_number(e164: str) -> str:
    d = e164.lstrip("+")
    if len(d) == 11:
        d = d[1:]
    return f"{d[0:3]} {d[3:6]} {d[6:]}" if len(d) == 10 else e164


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]


def _resolve_day(text: str, today: datetime.date) -> Optional[datetime.date]:
    """A caller says "Saturday", not "2026-10-11". Resolve to the next such day,
    and treat "today" and "tomorrow" properly, because a viewing booked for last
    Saturday is worse than no booking."""
    t = str(text).strip().lower()
    try:
        return datetime.date.fromisoformat(t)
    except ValueError:
        pass
    if t in {"today", "this afternoon", "this evening", "tonight"}:
        return today
    if t == "tomorrow":
        return today + datetime.timedelta(days=1)
    for i, name in enumerate(_WEEKDAYS):
        if name in t:
            # The next such day strictly in the future. Someone phoning on a
            # Saturday and saying "Saturday" means the coming one, not today,
            # and "next Saturday" means the one after that.
            ahead = (i - today.weekday()) % 7 or 7
            if "next" in t:
                ahead += 7
            return today + datetime.timedelta(days=ahead)
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            candidate = datetime.date(today.year, month, day)
        except ValueError:
            return None
        if candidate < today:
            candidate = candidate.replace(year=today.year + 1)
        return candidate
    return None
