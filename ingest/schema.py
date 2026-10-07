"""Normalised listing record.

Every extractor, no matter which portal the page came from, produces one of
these. The voice agent only ever sees this shape, so adding a new portal never
touches the agent code.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional
import json


@dataclass
class Money:
    amount: Optional[float] = None
    currency: str = "CAD"
    period: Optional[str] = None  # None for a sale price, "month" for rent/fees

    def spoken(self) -> Optional[str]:
        """How a human says it out loud. 899000 -> "eight hundred ninety nine
        thousand dollars" is handled by the TTS, but "$899,000" is not read
        well by every engine, so we hand it a plain phrase."""
        if self.amount is None:
            return None
        a = self.amount
        if a >= 1_000_000 and a % 100_000 == 0:
            head = f"{a / 1_000_000:.2f}".rstrip("0").rstrip(".")
            out = f"{head} million dollars"
        else:
            out = f"{int(round(a)):,} dollars"
        if self.period:
            out += f" per {self.period}"
        return out


@dataclass
class Address:
    unit: Optional[str] = None
    street: Optional[str] = None
    city: Optional[str] = None
    neighbourhood: Optional[str] = None
    province: Optional[str] = None
    postal_code: Optional[str] = None
    country: str = "Canada"

    def one_line(self) -> str:
        bits = []
        if self.unit:
            bits.append(f"Unit {self.unit}")
        if self.street:
            bits.append(self.street)
        if self.city:
            bits.append(self.city)
        if self.province:
            bits.append(self.province)
        return ", ".join(b for b in bits if b)

    def spoken(self) -> str:
        """Addresses get mangled by TTS when they are punctuation heavy."""
        bits = []
        if self.unit:
            bits.append(f"unit {self.unit}")
        if self.street:
            bits.append(self.street)
        if self.neighbourhood:
            bits.append(f"in {self.neighbourhood}")
        elif self.city:
            bits.append(f"in {self.city}")
        return " ".join(bits)


@dataclass
class Facts:
    property_type: Optional[str] = None       # Apartment, Detached, Townhouse
    bedrooms: Optional[float] = None
    bedrooms_plus: Optional[int] = None       # Toronto "2+1" style
    bathrooms: Optional[float] = None
    parking_spaces: Optional[int] = None
    storeys: Optional[float] = None
    size_interior_sqft: Optional[str] = None  # often a range on MLS, keep as text
    year_built: Optional[str] = None
    exposure: Optional[str] = None
    balcony: Optional[str] = None
    heating: Optional[str] = None
    cooling: Optional[str] = None
    locker: Optional[str] = None
    pets: Optional[str] = None


@dataclass
class Neighbourhood:
    """The "#view=neighbourhood" tab, normalised."""
    name: Optional[str] = None
    summary: Optional[str] = None
    walk_score: Optional[int] = None
    transit_score: Optional[int] = None
    bike_score: Optional[int] = None
    demographics: dict = field(default_factory=dict)   # label -> value, as shown
    nearby: dict = field(default_factory=dict)         # "schools" -> [names]


@dataclass
class MarketStats:
    """The "#view=stats" tab, normalised."""
    area_name: Optional[str] = None
    median_sale_price: Optional[float] = None
    average_sale_price: Optional[float] = None
    average_days_on_market: Optional[float] = None
    sale_to_list_ratio: Optional[float] = None
    active_listings: Optional[int] = None
    period: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class Listing:
    listing_id: str
    source: str                                   # realtor.ca, zolo.ca, manual
    source_url: Optional[str] = None
    captured_at: Optional[str] = None
    status: str = "active"
    price: Money = field(default_factory=Money)
    maintenance_fee: Money = field(default_factory=lambda: Money(period="month"))
    taxes_annual: Money = field(default_factory=Money)
    address: Address = field(default_factory=Address)
    facts: Facts = field(default_factory=Facts)
    description: Optional[str] = None
    features: list = field(default_factory=list)
    neighbourhood: Neighbourhood = field(default_factory=Neighbourhood)
    stats: MarketStats = field(default_factory=MarketStats)
    brokerage: Optional[str] = None
    media_count: int = 0
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Listing":
        return cls(
            listing_id=d["listing_id"],
            source=d.get("source", "manual"),
            source_url=d.get("source_url"),
            captured_at=d.get("captured_at"),
            status=d.get("status", "active"),
            price=Money(**d.get("price", {})),
            maintenance_fee=Money(**d.get("maintenance_fee", {})),
            taxes_annual=Money(**d.get("taxes_annual", {})),
            address=Address(**d.get("address", {})),
            facts=Facts(**d.get("facts", {})),
            description=d.get("description"),
            features=d.get("features", []),
            neighbourhood=Neighbourhood(**d.get("neighbourhood", {})),
            stats=MarketStats(**d.get("stats", {})),
            brokerage=d.get("brokerage"),
            media_count=d.get("media_count", 0),
            warnings=d.get("warnings", []),
        )
