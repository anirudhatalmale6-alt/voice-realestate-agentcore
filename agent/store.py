"""Tenant registry and listing store.

Multi-tenancy here means: one agent build, N stores, and a caller who dials
store 7's number must never be able to reach store 3's listings. The isolation
boundary is the *dialed number*, decided before the model is given anything,
not a instruction in the prompt asking it to behave.

    inbound call -> Connect contact flow reads the dialed number
                 -> tenant_id looked up here
                 -> only that tenant's listings are loaded into the session

Two backends, same interface:

  JsonStore      local files, what the demo and the tests run against
  DynamoDbStore  one table, partition key tenant_id, sort key sk

They are deliberately the same shape so moving from one to the other is a
one line change in the agent, not a rewrite. The DynamoDB keys are chosen so
that a single query returns a tenant and all of its listings, and so that no
query can be written that spans two tenants by accident.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from ingest.schema import Listing


@dataclass
class Tenant:
    tenant_id: str
    display_name: str                      # "Harbourfront Realty"
    phone_number: str                      # E.164, +1416...
    agent_name: str = "Alex"               # what the voice calls itself
    language: str = "en-CA"
    voice_id: str = "matthew"              # Nova Sonic voice
    greeting: Optional[str] = None
    business_hours: str = "9am to 7pm Eastern, seven days a week"
    handoff_number: Optional[str] = None   # a real person, for "get me a human"
    booking_url: Optional[str] = None
    disclaimers: list = field(default_factory=list)
    listing_ids: list = field(default_factory=list)
    active: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Tenant":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class Store(Protocol):
    def get_tenant(self, tenant_id: str) -> Optional[Tenant]: ...
    def tenant_for_number(self, e164: str) -> Optional[Tenant]: ...
    def get_listing(self, tenant_id: str, listing_id: str) -> Optional[Listing]: ...
    def list_listings(self, tenant_id: str) -> list: ...


def normalise_e164(number: str) -> str:
    """Connect hands the dialed number as +14165551234. Humans type it fifteen
    other ways. Everything is compared in E.164 or the lookup silently misses
    and the caller gets the wrong store's listings, which is the one failure
    this whole module exists to prevent."""
    digits = "".join(c for c in str(number) if c.isdigit())
    if not digits:
        return ""
    if len(digits) == 10:              # 416 555 1234
        digits = "1" + digits
    return "+" + digits


class JsonStore:
    """Local files. config/tenants.json plus samples/listings/<id>.json."""

    def __init__(self, tenants_path: str = "config/tenants.json",
                 listings_dir: str = "samples/listings"):
        self.tenants_path = Path(tenants_path)
        self.listings_dir = Path(listings_dir)
        self._tenants: dict[str, Tenant] = {}
        self._by_number: dict[str, str] = {}
        self._listings: dict[str, Listing] = {}
        self.reload()

    def reload(self) -> None:
        self._tenants.clear()
        self._by_number.clear()
        raw = json.loads(self.tenants_path.read_text(encoding="utf-8"))
        for entry in raw["tenants"]:
            t = Tenant.from_dict(entry)
            t.phone_number = normalise_e164(t.phone_number)
            if t.phone_number in self._by_number:
                # Two tenants on one number means calls land on whichever row
                # was read first. Fail loudly at load, not quietly on a call.
                raise ValueError(
                    f"phone number {t.phone_number} is claimed by both "
                    f"{self._by_number[t.phone_number]} and {t.tenant_id}")
            self._tenants[t.tenant_id] = t
            self._by_number[t.phone_number] = t.tenant_id

        self._listings.clear()
        for path in self.listings_dir.glob("*.json"):
            listing = Listing.from_dict(json.loads(path.read_text(encoding="utf-8")))
            self._listings[listing.listing_id] = listing

    def get_tenant(self, tenant_id: str) -> Optional[Tenant]:
        return self._tenants.get(tenant_id)

    def tenant_for_number(self, e164: str) -> Optional[Tenant]:
        tid = self._by_number.get(normalise_e164(e164))
        return self._tenants.get(tid) if tid else None

    def get_listing(self, tenant_id: str, listing_id: str) -> Optional[Listing]:
        tenant = self._tenants.get(tenant_id)
        # The tenant check is the whole point. Without it, a caller who reads
        # out an MLS number they found online reaches another store's listing.
        if not tenant or listing_id not in tenant.listing_ids:
            return None
        return self._listings.get(listing_id)

    def list_listings(self, tenant_id: str) -> list:
        tenant = self._tenants.get(tenant_id)
        if not tenant:
            return []
        return [self._listings[i] for i in tenant.listing_ids if i in self._listings]


class DynamoDbStore:
    """One table for every tenant. Partition key keeps tenants apart at the
    storage layer, so an IAM policy with a dynamodb:LeadingKeys condition can
    pin a session to exactly one tenant even if the agent code is wrong.

        PK  tenant_id   "tenant#harbourfront"
        SK  sk          "meta"              -> the tenant record
                        "listing#C13868410" -> one listing
                        "number#+14165551234" (in the GSI) -> routing
    """

    def __init__(self, table_name: str, region_name: str = "ca-central-1",
                 number_index: str = "number-index"):
        import boto3  # imported here so the local demo needs no AWS SDK config
        self.table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)
        self.number_index = number_index

    @staticmethod
    def _pk(tenant_id: str) -> str:
        return f"tenant#{tenant_id}"

    def get_tenant(self, tenant_id: str) -> Optional[Tenant]:
        item = self.table.get_item(
            Key={"tenant_id": self._pk(tenant_id), "sk": "meta"}).get("Item")
        return Tenant.from_dict(item["payload"]) if item else None

    def tenant_for_number(self, e164: str) -> Optional[Tenant]:
        from boto3.dynamodb.conditions import Key
        res = self.table.query(
            IndexName=self.number_index,
            KeyConditionExpression=Key("phone_number").eq(normalise_e164(e164)),
            Limit=1)
        items = res.get("Items") or []
        return Tenant.from_dict(items[0]["payload"]) if items else None

    def get_listing(self, tenant_id: str, listing_id: str) -> Optional[Listing]:
        item = self.table.get_item(
            Key={"tenant_id": self._pk(tenant_id),
                 "sk": f"listing#{listing_id}"}).get("Item")
        return Listing.from_dict(_undecimal(item["payload"])) if item else None

    def list_listings(self, tenant_id: str) -> list:
        from boto3.dynamodb.conditions import Key
        res = self.table.query(
            KeyConditionExpression=Key("tenant_id").eq(self._pk(tenant_id))
            & Key("sk").begins_with("listing#"))
        return [Listing.from_dict(_undecimal(i["payload"])) for i in res.get("Items", [])]


def _undecimal(obj):
    """DynamoDB returns every number as a Decimal, which json.dumps refuses and
    which compares oddly against ints. Normalise on the way out."""
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _undecimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_undecimal(v) for v in obj]
    return obj
