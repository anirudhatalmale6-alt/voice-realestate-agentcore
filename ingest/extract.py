"""Saved property page -> normalised Listing.

Why the input is a saved HTML file and not a URL
------------------------------------------------
realtor.ca, zolo.ca and zillow.com all sit behind a Cloudflare bot challenge.
A plain request gets HTTP 403, and so does a real headless Chromium with a
normal Windows user agent (verified, "Just a moment..." interstitial). So the
pipeline takes the page you already have open in your own browser:

    Ctrl+S in Chrome  ->  "Webpage, Complete" or "Webpage, Single File"
    python -m ingest.extract "C:\\pages\\55-stewart.html"

That also keeps us on the right side of the portals' terms, because a person
opened the page, not a robot.

The extraction runs four passes, best evidence first:
  1. JSON-LD blocks           (schema.org RealEstateListing / Residence / Product)
  2. Embedded app state       (__NEXT_DATA__, window.__INITIAL_STATE__, dataLayer)
  3. Labelled key/value pairs (the "Bedrooms  2" table every portal renders)
  4. Visible prose            (description, neighbourhood summary)

Each pass only fills fields that are still empty, so a weaker pass can never
overwrite a stronger one. Anything we could not find is recorded in
listing.warnings rather than guessed at, because a voice agent that invents a
square footage on a live sales call is worse than one that says it will check.
"""

from __future__ import annotations

import json
import re
import sys
import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from bs4 import BeautifulSoup

from .schema import Listing, Money, Address, Facts, Neighbourhood, MarketStats


# --------------------------------------------------------------------------
# small parsing helpers
# --------------------------------------------------------------------------

_MONEY_RE = re.compile(r"\$\s*([\d][\d,\s]*(?:\.\d{1,2})?)")
_INT_RE = re.compile(r"-?\d+")


def to_money(text: Any) -> Optional[float]:
    """"$899,000" -> 899000.0 ; "1,234.56" -> 1234.56 ; "Contact" -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = _MONEY_RE.search(str(text))
    raw = m.group(1) if m else str(text)
    raw = re.sub(r"[^\d.]", "", raw)
    if not raw or raw.count(".") > 1:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def to_int(text: Any) -> Optional[int]:
    if text is None:
        return None
    if isinstance(text, int):
        return text
    m = _INT_RE.search(str(text))
    return int(m.group(0)) if m else None


def to_float(text: Any) -> Optional[float]:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"-?\d+(?:\.\d+)?", str(text))
    return float(m.group(0)) if m else None


def clean(text: Optional[str]) -> Optional[str]:
    """Collapse the whitespace a saved page is full of, keep sentence breaks."""
    if text is None:
        return None
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


def beds_from_text(text: str) -> tuple[Optional[float], Optional[int]]:
    """Toronto listings say "2+1". The "+1" is a den, not a bedroom, and the
    agent must not add them together or it oversells the unit."""
    m = re.search(r"(\d+)\s*\+\s*(\d+)", text)
    if m:
        return float(m.group(1)), int(m.group(2))
    v = to_float(text)
    return v, None


# --------------------------------------------------------------------------
# pass 1: JSON-LD
# --------------------------------------------------------------------------

_LD_TYPES = {
    "singlefamilyresidence", "residence", "apartment", "house", "product",
    "realestatelisting", "offer", "accommodation", "place",
}


def _iter_ld_nodes(soup: BeautifulSoup) -> Iterable[dict]:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        payload = tag.string or tag.get_text() or ""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            # some portals emit several objects concatenated
            for chunk in re.findall(r"\{.*?\}(?=\s*\{|\s*$)", payload, re.S):
                try:
                    yield json.loads(chunk)
                except json.JSONDecodeError:
                    continue
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))


def apply_jsonld(listing: Listing, soup: BeautifulSoup) -> None:
    for node in _iter_ld_nodes(soup):
        types = node.get("@type")
        types = [types] if isinstance(types, str) else (types or [])
        if not any(str(t).lower() in _LD_TYPES for t in types):
            continue

        if listing.description is None:
            listing.description = clean(node.get("description"))

        offer = node.get("offers")
        if isinstance(offer, list):
            offer = offer[0] if offer else None
        if isinstance(offer, dict) and listing.price.amount is None:
            listing.price.amount = to_money(offer.get("price"))
            listing.price.currency = offer.get("priceCurrency") or listing.price.currency

        addr = node.get("address")
        if isinstance(addr, dict):
            a = listing.address
            a.street = a.street or clean(addr.get("streetAddress"))
            a.city = a.city or clean(addr.get("addressLocality"))
            a.province = a.province or clean(addr.get("addressRegion"))
            a.postal_code = a.postal_code or clean(addr.get("postalCode"))

        f = listing.facts
        if f.bedrooms is None:
            f.bedrooms = to_float(_scalar(node.get("numberOfBedrooms")))
        if f.bathrooms is None:
            f.bathrooms = to_float(_scalar(node.get("numberOfBathroomsTotal")
                                           or node.get("numberOfBathrooms")))
        if f.size_interior_sqft is None:
            size = node.get("floorSize")
            if isinstance(size, dict):
                val = size.get("value")
                unit = size.get("unitCode") or size.get("unitText") or "sqft"
                if val:
                    f.size_interior_sqft = f"{val} {unit}"

        imgs = node.get("image")
        if isinstance(imgs, list):
            listing.media_count = max(listing.media_count, len(imgs))


def _scalar(v: Any) -> Any:
    """schema.org lets a value be {"value": 2, "unitCode": ...}."""
    if isinstance(v, dict):
        return v.get("value")
    if isinstance(v, list) and v:
        return _scalar(v[0])
    return v


# --------------------------------------------------------------------------
# pass 2: embedded app state
# --------------------------------------------------------------------------

_STATE_PATTERNS = [
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S),
    re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S),
    re.compile(r"__NEXT_DATA__[^>]*>\s*(\{.*?\})\s*</script>", re.S),
]

# realtor.ca names its fields in PascalCase inside the embedded payload.
_STATE_KEYS = {
    "price": ("Price", "price", "listPrice", "ListPrice", "unformattedPrice"),
    "maintenance": ("MaintenanceFee", "maintenanceFee", "condoFee", "hoaFee"),
    "taxes": ("AnnualTaxAmount", "taxes", "annualTaxes", "TaxAmount"),
    "beds": ("BedroomsTotal", "Bedrooms", "beds", "bedrooms"),
    "baths": ("BathroomTotal", "Bathrooms", "baths", "bathrooms"),
    "parking": ("ParkingSpaceTotal", "parkingSpaces", "parking"),
    "type": ("PropertyType", "propertyType", "homeType"),
    "sqft": ("SizeInterior", "sizeInterior", "livingArea", "squareFootage"),
    "year": ("ConstructedDate", "yearBuilt", "BuiltYear"),
    "desc": ("PublicRemarks", "Description", "description", "remarks"),
    "neighbourhood": ("CommunityName", "neighborhood", "neighbourhood", "Community"),
    "city": ("City", "city", "AddressCity"),
    "street": ("StreetAddress", "AddressText", "streetAddress"),
    "postal": ("PostalCode", "postalCode", "zipcode"),
    "province": ("Province", "ProvinceName", "state", "stateCode"),
    "brokerage": ("OrganizationName", "brokerName", "brokerage", "ListOfficeName"),
}


def _walk(node: Any, depth: int = 0):
    if depth > 14:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v, depth + 1)
    elif isinstance(node, list):
        for v in node[:400]:
            yield from _walk(v, depth + 1)


def _find_key(blobs: list, names: tuple) -> Any:
    for blob in blobs:
        for node in _walk(blob):
            for name in names:
                if name in node:
                    v = node[name]
                    if isinstance(v, (str, int, float)) and str(v).strip():
                        return v
                    if isinstance(v, dict):
                        for inner in ("Value", "value", "Text", "text", "Amount"):
                            if inner in v and str(v[inner]).strip():
                                return v[inner]
    return None


def apply_state(listing: Listing, html: str) -> None:
    blobs = []
    for pat in _STATE_PATTERNS:
        for m in pat.finditer(html):
            try:
                blobs.append(json.loads(m.group(1)))
            except json.JSONDecodeError:
                continue
    if not blobs:
        return

    g = lambda k: _find_key(blobs, _STATE_KEYS[k])  # noqa: E731

    if listing.price.amount is None:
        listing.price.amount = to_money(g("price"))
    if listing.maintenance_fee.amount is None:
        listing.maintenance_fee.amount = to_money(g("maintenance"))
    if listing.taxes_annual.amount is None:
        listing.taxes_annual.amount = to_money(g("taxes"))

    f = listing.facts
    if f.bedrooms is None:
        raw = g("beds")
        if raw is not None:
            f.bedrooms, f.bedrooms_plus = beds_from_text(str(raw))
    f.bathrooms = f.bathrooms if f.bathrooms is not None else to_float(g("baths"))
    f.parking_spaces = f.parking_spaces if f.parking_spaces is not None else to_int(g("parking"))
    f.property_type = f.property_type or clean(str(g("type")) if g("type") else None)
    f.size_interior_sqft = f.size_interior_sqft or clean(str(g("sqft")) if g("sqft") else None)
    f.year_built = f.year_built or clean(str(g("year")) if g("year") else None)

    a = listing.address
    a.city = a.city or clean(str(g("city")) if g("city") else None)
    a.street = a.street or clean(str(g("street")) if g("street") else None)
    a.postal_code = a.postal_code or clean(str(g("postal")) if g("postal") else None)
    a.province = a.province or clean(str(g("province")) if g("province") else None)
    a.neighbourhood = a.neighbourhood or clean(str(g("neighbourhood")) if g("neighbourhood") else None)

    listing.description = listing.description or clean(str(g("desc")) if g("desc") else None)
    listing.brokerage = listing.brokerage or clean(str(g("brokerage")) if g("brokerage") else None)


# --------------------------------------------------------------------------
# pass 3: labelled key/value pairs in the rendered DOM
# --------------------------------------------------------------------------

_LABEL_MAP = {
    "bedrooms": "beds", "beds": "beds", "bed": "beds",
    "bathrooms": "baths", "baths": "baths", "bath": "baths",
    "parking": "parking", "parking spaces": "parking", "total parking spaces": "parking",
    "property type": "type", "building type": "type", "home type": "type",
    "square footage": "sqft", "size": "sqft", "interior size": "sqft",
    "living area": "sqft", "floor area": "sqft", "approx. sq ft": "sqft",
    "year built": "year", "built in": "year", "age": "year",
    "maintenance fees": "maintenance", "maintenance fee": "maintenance",
    "condo fee": "maintenance", "hoa fee": "maintenance", "maint. fees": "maintenance",
    "annual property taxes": "taxes", "property taxes": "taxes", "taxes": "taxes",
    "exposure": "exposure", "facing": "exposure",
    # "Balcony" is deliberately absent. On DDF portals it is a *value* of
    # "Exterior Features", and treating it as a label pairs it with whatever
    # heading follows it. _derive_flags() reads it out of the feature list.
    "heating": "heating", "heating type": "heating",
    "cooling": "cooling", "air conditioning": "cooling",
    "locker": "locker", "pets": "pets", "pets allowed": "pets",
    "storeys": "storeys", "stories": "storeys",
    "neighbourhood": "neighbourhood", "neighborhood": "neighbourhood",
    "community": "neighbourhood", "community name": "neighbourhood",
    "walk score": "walk", "transit score": "transit", "bike score": "bike",
    "days on market": "dom", "average days on market": "dom",
    "median sale price": "median_price", "median price": "median_price",
    "average sale price": "avg_price", "average price": "avg_price",
    "active listings": "active", "sale to list ratio": "stl",
    # DDF / MLS field names as the portals render them
    "bedrooms total": "beds", "bedrooms above ground": "beds",
    "bathrooms total": "baths", "bathroom total": "baths",
    "living area range": "sqft", "living area": "sqft", "size interior": "sqft",
    "maintenance fee": "maintenance", "hoa fee": "maintenance",
    "tax annual amount": "taxes", "annual tax amount": "taxes",
    "unit number": "unit", "unit no": "unit",
    "parking total": "parking", "total parking": "parking",
    "construction style attachment": "type", "architectural style": "type",
    "listing office": "brokerage", "listing by": "brokerage",
    "listing brokerage": "brokerage", "office": "brokerage",
    "mls® #": "mls", "mls #": "mls", "mls number": "mls", "mls® number": "mls",
    "stories total": "storeys", "storeys total": "storeys",
    "cooling type": "cooling", "heating fuel": "heating",
    "community name": "neighbourhood", "subdivision name": "neighbourhood",
}

# Portals using the CREA DDF feed emit raw unit codes.
_UNIT_CODES = {"FTK": "sq ft", "SQFT": "sq ft", "SQM": "sq m", "MTK": "sq m"}


def _dom_pairs(soup: BeautifulSoup) -> dict:
    """Harvest label/value pairs from <dl>, two-cell <tr>, and the
    <div><span>Label</span><span>Value</span></div> pattern portals favour."""
    pairs: dict[str, str] = {}

    def record(label: str, value: str) -> None:
        label = clean(label) or ""
        value = clean(value) or ""
        label = label.strip().rstrip(":").lower()
        if not label or not value or len(label) > 40 or len(value) > 160:
            return
        pairs.setdefault(label, value)

    for dl in soup.find_all("dl"):
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            record(dt.get_text(" "), dd.get_text(" "))

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False) or tr.find_all(["td", "th"])
        if len(cells) == 2:
            record(cells[0].get_text(" "), cells[1].get_text(" "))

    for parent in soup.find_all(["li", "div", "p"]):
        kids = [k for k in parent.find_all(["span", "div", "strong", "b"], recursive=False)]
        if len(kids) == 2:
            record(kids[0].get_text(" "), kids[1].get_text(" "))

    # Most portals render the spec table as a flat run of lines, label then
    # value, with no wrapping element tying the two together. RE/MAX does this:
    #     Bathrooms Total
    #     1
    #     Living Area Range
    #     0-499
    # So walk the visible text line by line and pair a known label with the
    # line underneath it. Only known labels are paired, so a run of prose can
    # never be mistaken for a spec row.
    for line, nxt in _line_pairs(soup):
        record(line, nxt)

    return pairs


_INVISIBLE = {"script", "style", "noscript", "template", "head"}


def _visible_lines(soup: BeautifulSoup) -> list:
    """Visible text, one line per text node. Does not mutate the soup, because
    the other passes still need the <script> tags we would otherwise strip."""
    out = []
    for node in (soup.body or soup).find_all(string=True):
        if any(p.name in _INVISIBLE for p in node.parents if p.name):
            continue
        line = re.sub(r"[ \t]+", " ", str(node)).strip()
        if line:
            out.append(line)
    return out


# A value line that is really the next section heading, e.g. "MAINTENANCE
# FEATURES", "CLIMATE RISK FOR M5V 0S2", "Parking Features". Pairing a label
# with one of these silently files a heading as a fact.
_SECTION_TAIL = re.compile(r"\b(features|info|information|details|overview|"
                           r"and schools|risk)\b\s*$", re.I)


def _looks_like_heading(line: str) -> bool:
    if not line:
        return True
    words = line.split()
    if len(words) >= 2 and not any(c.islower() for c in line):
        return True                      # ALL CAPS run of words
    return bool(_SECTION_TAIL.search(line)) and line[:1].isupper()


def _line_pairs(soup: BeautifulSoup) -> Iterable[tuple]:
    lines = _visible_lines(soup)
    for i in range(len(lines) - 1):
        label = lines[i].rstrip(":").strip().lower()
        value = lines[i + 1]
        if label not in _LABEL_MAP or len(value) > 160:
            continue
        if _looks_like_heading(value):
            continue
        yield lines[i], value


def apply_dom_pairs(listing: Listing, soup: BeautifulSoup) -> dict:
    pairs = _dom_pairs(soup)
    hits: dict[str, str] = {}
    for label, value in pairs.items():
        key = _LABEL_MAP.get(label)
        if key:
            hits.setdefault(key, value)

    f, a, n, s = listing.facts, listing.address, listing.neighbourhood, listing.stats

    if f.bedrooms is None and "beds" in hits:
        f.bedrooms, f.bedrooms_plus = beds_from_text(hits["beds"])
    if f.bathrooms is None:
        f.bathrooms = to_float(hits.get("baths"))
    if f.parking_spaces is None:
        f.parking_spaces = to_int(hits.get("parking"))
    if a.unit is None:
        a.unit = hits.get("unit")
    if listing.listing_id.startswith("unknown-") and hits.get("mls"):
        listing.listing_id = hits["mls"]
    listing.brokerage = listing.brokerage or hits.get("brokerage")
    f.property_type = f.property_type or hits.get("type")
    f.size_interior_sqft = f.size_interior_sqft or hits.get("sqft")
    f.year_built = f.year_built or hits.get("year")
    f.exposure = f.exposure or hits.get("exposure")
    f.balcony = f.balcony or hits.get("balcony")
    f.heating = f.heating or hits.get("heating")
    f.cooling = f.cooling or hits.get("cooling")
    f.locker = f.locker or hits.get("locker")
    f.pets = f.pets or hits.get("pets")
    f.storeys = f.storeys if f.storeys is not None else to_float(hits.get("storeys"))

    if listing.maintenance_fee.amount is None:
        listing.maintenance_fee.amount = to_money(hits.get("maintenance"))
    if listing.taxes_annual.amount is None:
        listing.taxes_annual.amount = to_money(hits.get("taxes"))

    # A "Neighbourhood" label on these pages is followed by the area name on
    # some listings and by the opening words of a sentence on others ("The
    # character of"). Only take it if it is shaped like a place name.
    hit_area = hits.get("neighbourhood")
    if a.neighbourhood is None and hit_area and _is_place_name(hit_area):
        a.neighbourhood = hit_area
    n.name = n.name or a.neighbourhood
    n.walk_score = n.walk_score if n.walk_score is not None else to_int(hits.get("walk"))
    n.transit_score = n.transit_score if n.transit_score is not None else to_int(hits.get("transit"))
    n.bike_score = n.bike_score if n.bike_score is not None else to_int(hits.get("bike"))

    s.average_days_on_market = s.average_days_on_market if s.average_days_on_market is not None \
        else to_float(hits.get("dom"))
    s.median_sale_price = s.median_sale_price if s.median_sale_price is not None \
        else to_money(hits.get("median_price"))
    s.average_sale_price = s.average_sale_price if s.average_sale_price is not None \
        else to_money(hits.get("avg_price"))
    s.active_listings = s.active_listings if s.active_listings is not None \
        else to_int(hits.get("active"))
    s.sale_to_list_ratio = s.sale_to_list_ratio if s.sale_to_list_ratio is not None \
        else to_float(hits.get("stl"))

    # Keep everything we saw but did not map. The stats tab in particular is
    # full of portal-specific rows, and the agent can still read them aloud.
    s.raw = {k: v for k, v in pairs.items() if _LABEL_MAP.get(k) is None and len(v) < 60}
    return pairs


# --------------------------------------------------------------------------
# pass 4: visible prose
# --------------------------------------------------------------------------

_NEIGHBOURHOOD_HEADS = re.compile(
    r"(neighbourhood|neighborhood|about the area|about this area|area profile|"
    r"local amenities|community overview)", re.I)

_BOILERPLATE = re.compile(
    r"(cookie|privacy policy|terms of use|all rights reserved|sign in|"
    r"create an account|realtor\.ca is operated|mls.*trademark)", re.I)


def longest_prose(soup: BeautifulSoup, min_len: int = 180) -> Optional[str]:
    best = None
    for tag in soup.find_all(["p", "div", "section", "span"]):
        if tag.find(["p", "div", "section"]):
            continue  # only leaf-ish nodes, otherwise we grab the whole page
        text = clean(tag.get_text(" "))
        if not text or len(text) < min_len or _BOILERPLATE.search(text):
            continue
        if best is None or len(text) > len(best):
            best = text
    return best


def apply_prose(listing: Listing, soup: BeautifulSoup) -> None:
    if listing.description is None:
        listing.description = longest_prose(soup)

    if listing.neighbourhood.summary is None:
        for head in soup.find_all(["h1", "h2", "h3", "h4"]):
            if not _NEIGHBOURHOOD_HEADS.search(head.get_text(" ")):
                continue
            chunks = []
            for sib in head.find_all_next(limit=40):
                if sib.name in {"h1", "h2", "h3"} and sib is not head:
                    break
                if sib.name in {"p", "li"}:
                    t = clean(sib.get_text(" "))
                    if t and len(t) > 30 and not _BOILERPLATE.search(t):
                        chunks.append(t)
                if sum(len(c) for c in chunks) > 1200:
                    break
            if chunks:
                listing.neighbourhood.summary = "\n".join(chunks)
                break

    if not listing.features:
        for ul in soup.find_all("ul"):
            # A <ul> inside the chrome is the site menu, not the amenity list.
            if any(p.name in {"nav", "header", "footer"} for p in ul.parents if p.name):
                continue
            # Breadcrumb trails are also <ul> lists, and they look exactly like
            # an amenity list until you notice every item is a separator or a
            # repeat of the page title.
            if "breadcrumb" in " ".join(ul.get("class", []) + [ul.get("aria-label") or "",
                                                               ul.get("itemtype") or ""]).lower():
                continue
            items = [clean(li.get_text(" ")) for li in ul.find_all("li", recursive=False)]
            items = [i for i in items if i and 3 < len(i) < 70 and not _BOILERPLATE.search(i)]
            if any(i.startswith((">", "/", "›")) for i in items):
                continue
            # Every entry being a single capitalised word or two is how a nav
            # menu looks; a real amenity list has multi-word entries.
            if items and sum(1 for i in items if len(i.split()) >= 2) < len(items) / 2:
                continue
            if 4 <= len(items) <= 30 and len(items) > len(listing.features):
                listing.features = items

    # "Appliances: Washer, Refrigerator, ..." style rows are the real amenity
    # list on DDF-fed portals, and they beat any <ul> we might have found.
    amenity_labels = ("appliances", "features", "amenities", "community features",
                      "maintenance amenities", "building features")
    lines = _visible_lines(soup)
    collected = []
    for label, value in zip(lines, lines[1:]):
        if label.rstrip(":").strip().lower() in amenity_labels and "," in value:
            collected += [clean(v) for v in value.split(",")]
    collected = [c for c in collected if c and 2 < len(c) < 60]
    if len(collected) > len(listing.features):
        listing.features = list(dict.fromkeys(collected))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

_SOURCE_HINTS = [
    ("realtor.ca", "realtor.ca"),
    ("zolo.ca", "zolo.ca"),
    ("zillow.com", "zillow.com"),
    ("realtor.com", "realtor.com"),
    ("housesigma", "housesigma.com"),
    ("condos.ca", "condos.ca"),
    ("royallepage", "royallepage.ca"),
    ("remax.ca", "remax.ca"),
    ("point2homes", "point2homes.com"),
    ("rew.ca", "rew.ca"),
]

_REQUIRED_FOR_A_CALL = [
    ("price.amount", "asking price"),
    ("address.street", "street address"),
    ("facts.bedrooms", "bedroom count"),
    ("facts.bathrooms", "bathroom count"),
    ("description", "listing description"),
]


def _dotted(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def extract_listing(html: str, listing_id: Optional[str] = None,
                    source_url: Optional[str] = None) -> Listing:
    soup = BeautifulSoup(html, "html.parser")

    source = "unknown"
    haystack = (source_url or "") + html[:60000]
    for needle, name in _SOURCE_HINTS:
        if needle in haystack.lower():
            source = name
            break

    if source_url is None:
        canon = soup.find("link", rel="canonical")
        if canon and canon.get("href"):
            source_url = canon["href"]
        else:
            og = soup.find("meta", property="og:url")
            if og and og.get("content"):
                source_url = og["content"]

    if listing_id is None:
        listing_id = _guess_listing_id(soup, source_url, html)

    listing = Listing(
        listing_id=listing_id,
        source=source,
        source_url=source_url,
        captured_at=datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0).isoformat(),
    )

    apply_jsonld(listing, soup)
    apply_state(listing, html)
    # Before the generic label sweep: a page has a "Neighbourhood" heading in
    # the nav and another over the real area profile, and the label sweep
    # cannot tell them apart. This pass finds the one with prose under it.
    apply_neighbourhood_block(listing, soup)
    apply_dom_pairs(listing, soup)
    apply_prose(listing, soup)
    _fill_address_from_title(listing, soup)
    _tidy_address(listing)
    _tidy_size(listing)
    _derive_flags(listing)

    for path, label in _REQUIRED_FOR_A_CALL:
        if _dotted(listing, path) in (None, ""):
            listing.warnings.append(
                f"missing {label} - the agent will say it needs to confirm rather than guess")

    return listing


def _guess_listing_id(soup: BeautifulSoup, source_url: Optional[str], html: str) -> str:
    """Prefer the MLS number: it is the one id a human on the phone can quote
    back, and it is stable across portals. The portal's own URL id is second
    choice, because two portals give the same home two different ids."""
    for pat in (r"MLS\s*(?:&reg;|®)?\s*#?\s*:?\s*</?[^>]*>?\s*([A-Z]{1,2}\d{6,9})\b",
                r"MLS[^\w]{0,6}#?\s*:?\s*([A-Z]{1,2}\d{6,9})\b",
                r'"(?:MlsNumber|mlsNumber|ListingId)"\s*:\s*"([A-Z0-9-]{5,20})"'):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1).upper()
    if source_url:
        for pat in (r"-(\d{6,})-lst", r"/(\d{6,})/", r"/(\d{7,})\b"):
            m = re.search(pat, source_url)
            if m:
                return m.group(1)
    return "unknown-" + str(abs(hash(html[:4000])))[:8]


def _fill_address_from_title(listing: Listing, soup: BeautifulSoup) -> None:
    """Portals put the full address in <h1> or og:title. Only used to fill
    gaps, and the unit number is split out so the agent does not say
    "eight hundred twenty nine fifty five Stewart Street"."""
    a = listing.address
    title = None
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = clean(og["content"])
    if not title:
        h1 = soup.find("h1")
        title = clean(h1.get_text(" ")) if h1 else None
    if not title:
        return

    if a.street is None:
        m = re.match(r"\s*(?:#?\s*([\dA-Za-z-]{1,6})\s*[-,]\s*)?(.+?)(?:,|$)", title)
        if m:
            if m.group(1) and a.unit is None:
                a.unit = m.group(1)
            a.street = clean(m.group(2))
    elif a.unit is None:
        m = re.match(r"\s*#?\s*([\dA-Za-z-]{1,6})\s*-\s", title)
        if m:
            a.unit = m.group(1)

    if a.city is None:
        parts = [clean(p) for p in title.split(",")]
        if len(parts) >= 2:
            a.city = parts[1]


_SCORE_LABELS = {
    "walk score": "walk_score", "walkscore": "walk_score",
    "transit score": "transit_score", "transitscore": "transit_score",
    "bike score": "bike_score", "bikescore": "bike_score",
}


_PLACE_NAME = re.compile(
    r"^[A-Z][\w'’.]*(?:[ \-/][A-Z0-9][\w'’.]*)*$")


def _is_place_name(line: str) -> bool:
    """Every word capitalised, two to five words, no sentence punctuation.
    "Don Valley Village" and "Fort York-Liberty Village" pass; "The character
    of" fails on the lowercase words, which is what stops a prose lead-in
    being filed as the neighbourhood name."""
    line = (line or "").strip()
    # One word is common ("Davisville"); six is the practical ceiling. Four
    # characters keeps stray capitalised fragments out.
    if not 1 <= len(line.split()) <= 6 or not 4 <= len(line) <= 60:
        return False
    if any(p in line for p in ".,;:!?") and not re.search(r"\b[A-Z]\.", line):
        return False
    return bool(_PLACE_NAME.match(line))


def apply_neighbourhood_block(listing: Listing, soup: BeautifulSoup) -> None:
    """The neighbourhood tab renders as

        Neighbourhood
        Fort York-Liberty Village
        <a paragraph about the area>
        Walk Score
        98
        ...

    so once the "Neighbourhood" label is found, read forward: the next short
    line is the area name and the next long line is the summary.
    """
    lines = _visible_lines(soup)
    n = listing.neighbourhood

    for i, line in enumerate(lines):
        if line.strip().rstrip(":").lower() not in {"neighbourhood", "neighborhood"}:
            continue
        window = lines[i + 1:i + 12]
        if not window or _looks_like_heading(window[0]):
            continue

        # The block is split across text nodes, and not always in the same
        # order. On one page it is [name, paragraph]; on another the sentence
        # opens before the name: ["The character of", "Don Valley Village",
        # "is exemplified by ..."]. So find the place name by shape rather
        # than by position, then stitch the sentence back together around it.
        name_at = next((j for j, c in enumerate(window) if _is_place_name(c)), None)
        if name_at is None:
            continue
        if n.name is None:
            n.name = clean(window[name_at])

        if n.summary is None:
            body = next((c for c in window[name_at:]
                         if len(c) > 200 and not _BOILERPLATE.search(c)), None)
            body_text = clean(body) if body else None
            if body_text and body_text[:1].islower():
                # Mid-sentence continuation: stitch the lead-in and the name
                # back on, so it reads "The character of Don Valley Village is
                # exemplified by ..." rather than "is exemplified by ...".
                lead = window[name_at - 1] if name_at else ""
                use_lead = bool(lead) and len(lead) < 40 and lead.split()[-1].islower()
                parts = ([lead] if use_lead else []) + [n.name or "", body_text]
                n.summary = clean(" ".join(p for p in parts if p))
            else:
                n.summary = body_text
        break

    # Walk/transit/bike scores sit as label-then-number pairs anywhere on the
    # page, including inside a third party widget.
    for label, value in zip(lines, lines[1:]):
        key = _SCORE_LABELS.get(label.strip().rstrip(":").lower())
        if key and getattr(n, key) is None:
            score = to_int(value)
            if score is not None and 0 <= score <= 100:
                setattr(n, key, score)


def _derive_flags(listing: Listing) -> None:
    """Fill the yes/no comfort facts a caller always asks about from whatever
    free text we captured, rather than leaving them blank when the page clearly
    says so. Only sets a value when the evidence is explicit."""
    f = listing.facts
    haystack = " | ".join(
        str(x) for x in ([listing.description or ""] + listing.features +
                         [f.exposure or "", f.heating or "", f.cooling or ""])
    ).lower()

    if f.balcony is None:
        if "balcony" in haystack or "terrace" in haystack:
            f.balcony = "Yes"
    if f.locker is None:
        if "locker" in haystack:
            f.locker = "Yes"
    if f.bedrooms is None and re.search(r"\bstudio\b|\bbachelor\b", haystack):
        # A studio genuinely has zero bedrooms. Leaving it blank makes the
        # agent say "let me confirm" about something the listing is explicit on.
        f.bedrooms = 0.0
    if f.exposure is None:
        m = re.search(r"\b(north|south|east|west|northeast|northwest|southeast|southwest)"
                      r"(?:ern)?\s+(?:exposure|facing|view)", haystack)
        if m:
            f.exposure = m.group(1).title()


_STREET_SUFFIX = {
    "st": "Street", "ave": "Avenue", "av": "Avenue", "rd": "Road", "dr": "Drive",
    "blvd": "Boulevard", "cres": "Crescent", "crt": "Court", "ct": "Court",
    "pl": "Place", "ln": "Lane", "ter": "Terrace", "pkwy": "Parkway",
    "hwy": "Highway", "sq": "Square", "trl": "Trail", "gdns": "Gardens",
    "cir": "Circle", "way": "Way", "e": "East", "w": "West", "n": "North",
    "s": "South", "ne": "Northeast", "nw": "Northwest", "se": "Southeast",
    "sw": "Southwest",
}


def _titlecase_street(text: str) -> str:
    """MLS feeds are all caps. "38 IANNUZZI ST" spoken by a TTS engine is fine,
    but "38 IANNUZZI ST" shown in a dashboard is shouting, and the abbreviated
    suffix is read as "saint" by some voices. Expand it."""
    out = []
    for word in text.split():
        bare = word.strip(".,").lower()
        if bare in _STREET_SUFFIX:
            out.append(_STREET_SUFFIX[bare])
        elif word.isupper() and len(word) > 1:
            out.append(word.capitalize())
        else:
            out.append(word)
    return " ".join(out)


def _tidy_address(listing: Listing) -> None:
    a = listing.address

    # Canadian MLS writes "TORONTO (NIAGARA)": city, then the community.
    if a.city:
        m = re.match(r"^\s*(.+?)\s*\(([^)]+)\)\s*$", a.city)
        if m:
            a.city = m.group(1).strip()
            a.neighbourhood = a.neighbourhood or m.group(2).strip()
        a.city = a.city.title() if a.city.isupper() else a.city
    if a.neighbourhood and a.neighbourhood.isupper():
        a.neighbourhood = a.neighbourhood.title()

    if a.street:
        # "1503 - 38 IANNUZZI ST" carries the unit in the street line. Said out
        # loud that becomes "fifteen oh three thirty eight Iannuzzi Street",
        # which no caller can parse.
        m = re.match(r"^\s*#?\s*([\dA-Za-z]{1,6})\s*-\s*(\d.*)$", a.street)
        if m:
            a.unit = a.unit or m.group(1)
            a.street = m.group(2)
        a.street = re.sub(r",\s*[A-Za-z .()]+,?\s*[A-Z]{2}\s*[A-Z]\d[A-Z]\s*\d[A-Z]\d\s*$",
                          "", a.street).strip().rstrip(",")
        a.street = _titlecase_street(a.street)

    if a.postal_code:
        a.postal_code = a.postal_code.upper().replace("  ", " ")

    # The two can disagree: MLS puts the board district in the city brackets
    # ("Toronto (Niagara)") while the neighbourhood tab names the area people
    # actually say out loud ("Fort York-Liberty Village"). Prefer the latter.
    n = listing.neighbourhood
    if n.name:
        a.neighbourhood = n.name
    else:
        n.name = a.neighbourhood


def _tidy_size(listing: Listing) -> None:
    """DDF portals split size across "Living Area Range: 0-499" and
    "Living Area Units: Square Feet", or emit the bare unit code FTK."""
    size = listing.facts.size_interior_sqft
    if not size:
        return
    for code, label in _UNIT_CODES.items():
        size = re.sub(rf"\b{code}\b", label, size, flags=re.I)
    size = re.sub(r"\s+", " ", size).strip()
    if re.fullmatch(r"[\d,.]+(\s*-\s*[\d,.]+)?", size):
        size += " sq ft"
    listing.facts.size_interior_sqft = size


def main(argv: list) -> int:
    if not argv:
        print(__doc__)
        print("usage: python -m ingest.extract <saved-page.html> [more.html ...]")
        return 2

    out_dir = Path("samples/listings")
    out_dir.mkdir(parents=True, exist_ok=True)

    for path in argv:
        p = Path(path)
        html = p.read_text(encoding="utf-8", errors="replace")
        listing = extract_listing(html)
        dest = out_dir / f"{listing.listing_id}.json"
        dest.write_text(listing.to_json(), encoding="utf-8")
        print(f"{p.name} -> {dest}  ({listing.source}, {len(listing.warnings)} warnings)")
        for w in listing.warnings:
            print(f"   ! {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
