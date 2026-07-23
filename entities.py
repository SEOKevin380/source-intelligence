"""
Source Intelligence — Universal Entity Model
=============================================
Replaces the implicit "everything is a supplement with supplement_facts"
assumption with a proper entity hierarchy that supports any product type.

Organization → Brand → Offering (Product/Service/Software/Program/...)
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, List, Any


class OfferingType(Enum):
    """All entity types the system can research and evaluate."""
    SUPPLEMENT = "supplement"
    TOPICAL = "topical"
    DEVICE = "device"
    FOOD = "food"
    CANNABIS = "cannabis"
    TELEHEALTH = "telehealth"
    INFO_PRODUCT = "info_product"
    FINANCIAL = "financial"
    SOFTWARE = "software"
    SERVICE = "service"
    PROGRAM = "program"
    SUBSCRIPTION = "subscription"
    PROFESSIONAL = "professional"
    GAMING = "gaming"
    COLLECTIBLE = "collectible"
    RESEARCH_PEPTIDE = "research_peptide"
    UNKNOWN = "unknown"


@dataclass
class Organization:
    """A company or entity that owns brands and offerings."""
    name: str
    url: Optional[str] = None
    address: Optional[str] = None
    contact: Optional[Dict[str, str]] = None  # email, phone, etc.
    identifiers: Dict[str, str] = field(default_factory=dict)  # BBB ID, DUNS, etc.


@dataclass
class Brand:
    """A brand under an organization."""
    name: str
    organization: Optional[Organization] = None
    url: Optional[str] = None


@dataclass
class Offering:
    """Universal entity representing any product/service/program being researched.

    This is the center of the entity graph. Every research job targets an Offering.
    The `composition` field replaces `supplement_facts` — it holds whatever the
    offering is made of (ingredients for supplements, features for devices, etc.).
    """
    name: str
    offering_type: OfferingType
    offering_id: str = ""  # Stable ID — auto-generated from name+url if empty
    brand: Optional[Brand] = None
    url: Optional[str] = None
    category: str = ""
    description: str = ""
    variants: List[Dict[str, Any]] = field(default_factory=list)
    offers: List[Dict[str, Any]] = field(default_factory=list)  # Pricing tiers
    market: str = "US"
    version: str = ""
    claims: List[Dict[str, Any]] = field(default_factory=list)
    composition: Dict[str, Any] = field(default_factory=dict)
    policies: Dict[str, Any] = field(default_factory=dict)  # Refund, shipping, etc.
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Generate stable offering_id if not provided."""
        if not self.offering_id:
            raw = f"{self.name}:{self.url or ''}:{self.offering_type.value}"
            self.offering_id = hashlib.sha256(raw.encode()).hexdigest()[:24]

    def is_ingestible(self) -> bool:
        """Does this offering involve physical consumption?"""
        return self.offering_type in (
            OfferingType.SUPPLEMENT, OfferingType.FOOD,
            OfferingType.CANNABIS, OfferingType.RESEARCH_PEPTIDE,
        )

    def requires_fda_disclaimer(self) -> bool:
        """Does this offering need an FDA structure/function disclaimer?"""
        return self.offering_type in (
            OfferingType.SUPPLEMENT, OfferingType.FOOD,
            OfferingType.TOPICAL, OfferingType.CANNABIS,
            OfferingType.RESEARCH_PEPTIDE, OfferingType.TELEHEALTH,
        )

    def requires_ingredient_research(self) -> bool:
        """Should PubMed/safety research be run for this offering?"""
        return self.offering_type in (
            OfferingType.SUPPLEMENT, OfferingType.FOOD,
            OfferingType.TOPICAL, OfferingType.CANNABIS,
            OfferingType.RESEARCH_PEPTIDE,
        )

    @classmethod
    def from_legacy_product_data(cls, product_data: dict) -> "Offering":
        """Convert existing research_product.py product_data dict to Offering.

        This is the bridge between old and new systems. Existing phase1 output
        flows through here to become a proper entity.
        """
        type_str = product_data.get("product_type", "")
        if not type_str:
            offering_type = OfferingType.UNKNOWN
        else:
            try:
                offering_type = OfferingType(type_str)
            except ValueError:
                offering_type = OfferingType.UNKNOWN

        org = None
        company = product_data.get("company", {})
        if company and company.get("name"):
            org = Organization(
                name=company["name"],
                url=company.get("website", ""),
                address=company.get("address", ""),
                contact={
                    k: v for k, v in {
                        "email": company.get("email", ""),
                        "phone": company.get("phone", ""),
                    }.items() if v
                },
            )

        brand = None
        if product_data.get("brand_name"):
            brand = Brand(
                name=product_data["brand_name"],
                organization=org,
                url=product_data.get("official_url", ""),
            )

        return cls(
            name=product_data.get("product_name", ""),
            offering_type=offering_type,
            brand=brand,
            url=product_data.get("official_url", ""),
            category=product_data.get("category", ""),
            description=product_data.get("description", ""),
            composition=product_data.get("supplement_facts", {}),
            offers=product_data.get("pricing", []),
            claims=product_data.get("claims", []),
            policies={
                "refund": product_data.get("refund_policy", {}),
                "shipping": product_data.get("shipping_policy", {}),
                "warranty": product_data.get("warranty", ""),
            },
            raw_metadata={
                "testimonials": product_data.get("testimonials", []),
                "brand_faqs": product_data.get("brand_faqs", []),
                "payment_processor": product_data.get("payment_processor", ""),
                "subscription_available": product_data.get("subscription_available", False),
            },
        )

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "offering_id": self.offering_id,
            "name": self.name,
            "offering_type": self.offering_type.value,
            "brand": {
                "name": self.brand.name,
                "url": self.brand.url,
                "organization": {
                    "name": self.brand.organization.name,
                    "url": self.brand.organization.url,
                    "address": self.brand.organization.address,
                } if self.brand.organization else None,
            } if self.brand else None,
            "url": self.url,
            "category": self.category,
            "description": self.description,
            "market": self.market,
            "composition": self.composition,
            "offers": self.offers,
            "claims": self.claims,
            "policies": self.policies,
        }

    def save(self, db_path: str = None):
        """Persist this offering to the offerings table."""
        import json
        import sqlite3
        if db_path is None:
            from config import DB_PATH
            db_path = DB_PATH
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db_path)
        conn.execute("""
            INSERT OR REPLACE INTO offerings (
                offering_id, offering_type, name, url,
                category, brand_name, organization_name,
                composition_json, policies_json,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            self.offering_id,
            self.offering_type.value,
            self.name,
            self.url or "",
            self.category,
            self.brand.name if self.brand else "",
            self.brand.organization.name if self.brand and self.brand.organization else "",
            json.dumps(self.composition),
            json.dumps(self.policies),
            now, now,
        ))
        conn.commit()
        conn.close()

    @classmethod
    def load(cls, offering_id: str, db_path: str = None) -> Optional["Offering"]:
        """Load an offering from the offerings table."""
        import json
        import sqlite3
        if db_path is None:
            from config import DB_PATH
            db_path = DB_PATH
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM offerings WHERE offering_id = ?", (offering_id,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        d = dict(row)
        brand = None
        if d.get("brand_name"):
            org = None
            if d.get("organization_name"):
                org = Organization(name=d["organization_name"])
            brand = Brand(name=d["brand_name"], organization=org)
        return cls(
            name=d["name"],
            offering_type=OfferingType(d["offering_type"]),
            offering_id=d["offering_id"],
            brand=brand,
            url=d.get("url", ""),
            category=d.get("category", ""),
            composition=json.loads(d.get("composition_json", "{}")),
            policies=json.loads(d.get("policies_json", "{}")),
        )
