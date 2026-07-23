#!/usr/bin/env python3
"""Compare approved-release entities with the WordPress network inventory.

Outputs an administrative opportunity file only.  It does not publish content.
Recommendations require both an uncovered product and a relevant site niche.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
from datetime import datetime


DEFAULT_INDEX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "approved_release_index.json.gz",
)
DEFAULT_MASTER = "/Users/kevinmahoney/master-publisher-data/master_product_list.json"
DEFAULT_SITES = (
    "/Users/kevinmahoney/Desktop/Code Projects/mbk-recovery/config/wp-sites.json"
)

VERTICAL_CATEGORY_TERMS = {
    "supplement": {
        "supplements", "supplement", "wellness", "consumer_health",
        "mens_health", "natural", "nutrition", "fitness",
    },
    "telehealth": {
        "telehealth", "clinical", "medical", "consumer_health",
        "weight_loss", "mens_health",
    },
    "consumer_electronics": {
        "consumer_products", "consumer", "tech", "gadgets", "home",
        "product_reviews",
    },
    "financial": {
        "financial", "investing", "finance", "business", "consumer_advocacy",
    },
    "info_product": {
        "education", "consumer_products", "consumer", "business",
        "product_reviews",
    },
    "collectible": {
        "collectibles", "consumer_products", "consumer", "politics",
        "product_reviews",
    },
    "general_consumer": {
        "consumer_products", "consumer", "product_reviews", "wellness",
        "lifestyle",
    },
    "gambling": set(),
}

NON_HEALTH_SITE_ALLOWLIST = {
    "financial": {"totalhealthrd", "uspatriotnews", "globalleaderscouncil"},
    "consumer_electronics": {
        "hollyherman", "totalhealthrd", "uspatriotnews",
        "thepreppersblueprint", "sigmedical",
    },
    "collectible": {
        "hollyherman", "totalhealthrd", "uspatriotnews", "thepreppersblueprint",
    },
    "info_product": {
        "hollyherman", "totalhealthrd", "uspatriotnews",
        "maketimeforwellness", "globalleaderscouncil",
    },
    "general_consumer": {
        "hollyherman", "totalhealthrd", "uspatriotnews", "thepreppersblueprint",
    },
    # No current network site has a declared gambling editorial niche.
    "gambling": set(),
}

GENERIC_SUPPLEMENT_SITES = {
    "pvmedcenter", "tutelamedical", "totalhealthrd", "hollyherman",
    "hathawaymd", "mercyiowacityclinics", "piedmontprimarycare",
    "gatewaytocare", "totalcaremedical", "maketimeforwellness",
    "uspatriotnews", "troupcohealth", "sterlingmedicalcenter",
}

TOPICAL_SITE_TERMS = {
    "topshelfmushrooms": {"mushroom", "lion's mane", "cordyceps", "reishi"},
    "vitaminsformen": {"men", "male", "testosterone", "prostate", "vitamin"},
    "utcardiothoracicsurgery": {"heart", "cardio", "blood pressure", "circulation"},
    "londonbridgeurology": {"prostate", "urinary", "bladder", "male", "men"},
    "tricountyurology": {"prostate", "urinary", "bladder", "male", "men"},
    "empireneuro": {"brain", "memory", "nerve", "cognitive", "focus"},
    "globalmhsummit": {"brain", "memory", "mental", "mood", "stress", "sleep"},
    "okcoptometrist": {"eye", "vision", "retina"},
    "affiliatedfootandankleclinics": {"foot", "feet", "fungus", "neuropathy"},
    "healthysteppodiatry": {"foot", "feet", "fungus", "neuropathy"},
    "californiacannabinoids": {"cbd", "cannabis", "hemp", "cannabinoid"},
    "worldcannabiscongress": {"cbd", "cannabis", "hemp", "cannabinoid"},
    "wellspringweightloss": {"weight", "slim", "lean", "metabolic", "glp"},
    "novamedspa": {"skin", "beauty", "wrinkle", "anti-aging", "weight"},
}


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def relevant_sites(entity: dict, sites: dict) -> list[str]:
    verticals = set(entity.get("verticals", ()))
    product_text = " ".join(
        [entity.get("product_or_offering", ""), *entity.get("source_domains", ())]
    ).lower()

    # Non-health verticals use explicit editorial fits. The network is largely
    # medical, so category substring matching would recommend nonsense.
    non_health = verticals & set(NON_HEALTH_SITE_ALLOWLIST)
    if non_health and "supplement" not in verticals and "telehealth" not in verticals:
        allowed = set()
        for vertical in non_health:
            allowed.update(NON_HEALTH_SITE_ALLOWLIST[vertical])
        return sorted(key for key in allowed if key in sites)

    if "supplement" in verticals:
        allowed = set(GENERIC_SUPPLEMENT_SITES)
        for site, terms in TOPICAL_SITE_TERMS.items():
            if any(term in product_text for term in terms):
                allowed.add(site)
        return sorted(site for site in allowed if site in sites)

    if "telehealth" in verticals:
        return sorted(
            key for key, cfg in sites.items()
            if "telehealth" in cfg.get("categories", ())
        )

    desired = set()
    for vertical in entity.get("verticals", ()):
        desired.update(VERTICAL_CATEGORY_TERMS.get(vertical, ()))
    if not desired:
        return []

    matches = []
    for key, cfg in sites.items():
        haystack = " ".join(
            [
                str(cfg.get("archetype", "")),
                str(cfg.get("voice", "")),
                " ".join(cfg.get("categories", ())),
            ]
        ).lower()
        if any(term.replace("_", " ") in haystack or term in haystack for term in desired):
            matches.append(key)
    return sorted(matches)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--master", default=DEFAULT_MASTER)
    parser.add_argument("--sites", default=DEFAULT_SITES)
    parser.add_argument(
        "--output",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "network_opportunity_inventory.csv",
        ),
    )
    args = parser.parse_args()

    with gzip.open(args.index, "rt", encoding="utf-8") as handle:
        corpus = json.load(handle)
    with open(args.master, encoding="utf-8") as handle:
        master_payload = json.load(handle)
    with open(args.sites, encoding="utf-8") as handle:
        site_payload = json.load(handle)

    master = master_payload.get("products", master_payload)
    sites = site_payload.get("sites", site_payload)
    master_by_key = {normalize(name): (name, details) for name, details in master.items()}

    rows = []
    for entity in corpus.get("entity_inventory", ()):
        entity_key = normalize(entity["product_or_offering"])
        master_match = master_by_key.get(entity_key)
        covered_sites = sorted(
            (master_match[1].get("sites", {}) if master_match else {}).keys()
        )
        fitting_sites = relevant_sites(entity, sites)
        opportunities = [site for site in fitting_sites if site not in covered_sites]
        rows.append({
            "product_or_offering": entity["product_or_offering"],
            "brand_candidate": entity["brand_candidate"],
            "company_status": entity["company_status"],
            "source_domains": " | ".join(entity["source_domains"]),
            "verticals": " | ".join(entity["verticals"]),
            "approved_platforms": " | ".join(entity["platforms"]),
            "approved_release_count": entity["release_count"],
            "first_seen": entity["first_seen"],
            "last_seen": entity["last_seen"],
            "network_inventory_match": master_match[0] if master_match else "",
            "currently_covered_sites": " | ".join(covered_sites),
            "relevant_uncovered_sites": " | ".join(opportunities),
            "opportunity_count": len(opportunities),
            "reader_intents": " | ".join(entity["intents"]),
        })

    rows.sort(
        key=lambda row: (
            -row["opportunity_count"],
            -row["approved_release_count"],
            row["product_or_offering"],
        )
    )
    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print(json.dumps({
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "products_or_offerings": len(rows),
        "exact_network_matches": sum(bool(row["network_inventory_match"]) for row in rows),
        "with_relevant_uncovered_sites": sum(row["opportunity_count"] > 0 for row in rows),
        "output": args.output,
    }, indent=2))


if __name__ == "__main__":
    main()
