"""
Source Intelligence — Vertical Intelligence Packs
===================================================
Per-offering-type definitions of what facts to gather, what sources to consult,
what compliance rules apply, and what content opportunities exist.

Each pack replaces the hardcoded supplement path with type-specific intelligence.
Unknown types fail closed — they cannot proceed without human classification.
"""

from entities import OfferingType


INTELLIGENCE_PACKS = {
    OfferingType.SUPPLEMENT: {
        "required_facts": [
            "ingredients_with_amounts", "serving_size", "servings_per_container",
            "proprietary_blend_flag", "other_ingredients", "allergens",
            "manufacturer", "country_of_manufacture",
        ],
        "mandatory_facts": [
            "ingredients_with_amounts", "serving_size",
        ],
        "authoritative_sources": [
            {"type": "dsld", "priority": 1, "description": "NIH DSLD label records"},
            {"type": "fda_caers", "priority": 2, "description": "FDA adverse events"},
            {"type": "pubmed", "priority": 1, "description": "PubMed studies per ingredient"},
            {"type": "vendor_page", "priority": 3, "description": "Official product page"},
        ],
        "compliance_rules": [
            "fda_disclaimer", "structure_function_claims_only",
            "no_disease_claims", "no_cure_claims",
        ],
        "evidence_requirements": {
            "ingredients": "required",
            "pubmed_research": "required",
            "safety_profile": "required",
            "pricing": "recommended",
        },
        "content_opportunities": [
            "L1_ingredient_profile", "L3_safety_guide", "L6_product_review",
            "comparison", "hub_page", "ingredient_guide", "condition_guide",
        ],
    },

    OfferingType.TOPICAL: {
        "required_facts": [
            "active_ingredients", "inactive_ingredients", "application_method",
            "warnings", "manufacturer", "net_weight",
        ],
        "mandatory_facts": ["active_ingredients"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "pubmed", "priority": 2, "description": "Studies on active ingredients"},
            {"type": "fda_otc", "priority": 2, "description": "FDA OTC monograph if applicable"},
        ],
        "compliance_rules": [
            "fda_disclaimer", "no_disease_claims", "cosmetic_vs_drug_distinction",
        ],
        "evidence_requirements": {
            "ingredients": "required",
            "pubmed_research": "recommended",
            "safety_profile": "required",
        },
        "content_opportunities": ["L6_product_review", "comparison", "ingredient_guide"],
    },

    OfferingType.DEVICE: {
        "required_facts": [
            "key_features", "specifications", "warranty", "manufacturer",
            "fda_clearance_status", "certifications", "power_source",
        ],
        "mandatory_facts": ["key_features"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "fda_510k", "priority": 2, "description": "FDA 510(k) database"},
        ],
        "compliance_rules": ["no_medical_claims_without_clearance"],
        "evidence_requirements": {
            "features": "required",
            "pubmed_research": "optional",
            "safety_profile": "optional",
        },
        "content_opportunities": ["L6_product_review", "comparison"],
    },

    OfferingType.TELEHEALTH: {
        "required_facts": [
            "services_offered", "pricing_tiers", "prescriber_credentials",
            "states_available", "medications_offered", "consultation_process",
        ],
        "mandatory_facts": ["services_offered", "prescriber_credentials"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "state_licensing", "priority": 2},
        ],
        "compliance_rules": [
            "three_entity_structure", "prescriber_verification",
            "state_availability_disclosure",
        ],
        "evidence_requirements": {
            "service_details": "required",
            "pubmed_research": "required_for_medications",
        },
        "content_opportunities": [
            "L6_product_review", "comparison", "educational_guide",
        ],
    },

    OfferingType.INFO_PRODUCT: {
        "required_facts": [
            "whats_included", "format", "author_credentials",
            "access_method", "pricing",
        ],
        "mandatory_facts": ["whats_included"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
        ],
        "compliance_rules": [
            "income_claims_substantiation", "results_typicality",
        ],
        "evidence_requirements": {
            "product_details": "required",
            "author_verification": "recommended",
        },
        "content_opportunities": ["L6_product_review", "comparison"],
    },

    OfferingType.FINANCIAL: {
        "required_facts": [
            "service_type", "pricing", "topics_covered",
            "track_record_claims", "regulatory_registrations",
        ],
        "mandatory_facts": ["service_type", "regulatory_registrations"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "sec_edgar", "priority": 2, "description": "SEC filings"},
            {"type": "finra", "priority": 2, "description": "FINRA broker check"},
        ],
        "compliance_rules": [
            "investment_disclaimer", "no_guaranteed_returns",
            "past_performance_disclaimer",
        ],
        "evidence_requirements": {
            "service_details": "required",
            "regulatory_check": "required",
        },
        "content_opportunities": ["L6_product_review"],
    },

    OfferingType.SOFTWARE: {
        "required_facts": [
            "key_features", "pricing_tiers", "platform_support",
            "integrations", "data_security", "support_options",
        ],
        "mandatory_facts": ["key_features"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "g2_reviews", "priority": 3},
            {"type": "capterra", "priority": 3},
        ],
        "compliance_rules": ["privacy_policy_required"],
        "evidence_requirements": {
            "features": "required",
            "pricing": "required",
        },
        "content_opportunities": ["L6_product_review", "comparison", "tutorial"],
    },

    OfferingType.SERVICE: {
        "required_facts": [
            "service_description", "pricing", "service_area",
            "credentials", "guarantees",
        ],
        "mandatory_facts": ["service_description"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "bbb", "priority": 2},
        ],
        "compliance_rules": ["service_guarantee_substantiation"],
        "evidence_requirements": {
            "service_details": "required",
        },
        "content_opportunities": ["L6_product_review", "comparison"],
    },

    OfferingType.FOOD: {
        "required_facts": [
            "nutrition_facts", "ingredients", "allergens",
            "serving_size", "manufacturer", "certifications",
        ],
        "mandatory_facts": ["ingredients", "allergens"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "fda_food", "priority": 2},
            {"type": "pubmed", "priority": 2},
        ],
        "compliance_rules": [
            "nutrition_labeling", "allergen_disclosure", "no_disease_claims",
        ],
        "evidence_requirements": {
            "ingredients": "required",
            "nutrition_info": "required",
        },
        "content_opportunities": ["L6_product_review", "comparison", "recipe_guide"],
    },

    OfferingType.CANNABIS: {
        "required_facts": [
            "cannabinoid_profile", "terpene_profile", "thc_content",
            "cbd_content", "lab_results", "strain_type",
            "consumption_method", "state_availability",
        ],
        "mandatory_facts": ["cannabinoid_profile", "thc_content", "lab_results"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "state_registry", "priority": 2},
            {"type": "lab_coa", "priority": 1, "description": "Certificate of analysis"},
        ],
        "compliance_rules": [
            "state_legality_check", "no_medical_claims",
            "age_restriction_disclosure", "thc_limit_compliance",
        ],
        "evidence_requirements": {
            "cannabinoid_profile": "required",
            "lab_results": "required",
        },
        "content_opportunities": ["L6_product_review", "strain_guide", "comparison"],
    },

    OfferingType.RESEARCH_PEPTIDE: {
        "required_facts": [
            "peptide_sequence", "purity_percentage", "molecular_weight",
            "cas_number", "form", "amount_per_vial",
            "storage_requirements", "research_use_only_disclaimer",
        ],
        "mandatory_facts": [
            "peptide_sequence", "purity_percentage",
            "research_use_only_disclaimer",
        ],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "pubmed", "priority": 1},
            {"type": "pubchem", "priority": 2, "description": "PubChem compound database"},
        ],
        "compliance_rules": [
            "research_use_only", "not_for_human_consumption",
            "no_clinical_claims",
        ],
        "evidence_requirements": {
            "compound_identity": "required",
            "pubmed_research": "required",
            "purity_verification": "recommended",
        },
        "content_opportunities": ["L6_product_review", "mechanism_guide", "comparison"],
    },

    OfferingType.PROGRAM: {
        "required_facts": [
            "program_structure", "duration", "pricing",
            "credentials_earned", "instructor_credentials",
        ],
        "mandatory_facts": ["program_structure"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
        ],
        "compliance_rules": ["results_substantiation"],
        "evidence_requirements": {
            "program_details": "required",
        },
        "content_opportunities": ["L6_product_review"],
    },

    OfferingType.SUBSCRIPTION: {
        "required_facts": [
            "included_items", "pricing_tiers", "billing_frequency",
            "cancellation_policy", "trial_period",
        ],
        "mandatory_facts": ["included_items", "pricing_tiers"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
        ],
        "compliance_rules": ["auto_renewal_disclosure", "cancellation_ease"],
        "evidence_requirements": {
            "subscription_details": "required",
        },
        "content_opportunities": ["L6_product_review", "comparison"],
    },

    OfferingType.PROFESSIONAL: {
        "required_facts": [
            "services_offered", "credentials", "experience",
            "pricing_structure", "service_area",
        ],
        "mandatory_facts": ["services_offered", "credentials"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "licensing_board", "priority": 2},
        ],
        "compliance_rules": ["credential_verification"],
        "evidence_requirements": {
            "credentials": "required",
        },
        "content_opportunities": ["profile", "comparison"],
    },

    OfferingType.GAMING: {
        "required_facts": [
            "product_description", "how_it_works", "access_method",
            "pricing", "billing_terms", "refund_policy", "eligibility",
            "jurisdiction_limits", "odds_or_randomness_disclosure",
        ],
        "mandatory_facts": ["product_description", "how_it_works"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "terms_page", "priority": 1},
            {"type": "refund_page", "priority": 2},
        ],
        "compliance_rules": [
            "no_guaranteed_wins", "no_improved_odds_without_substantiation",
            "eligibility_and_jurisdiction_disclosure", "pricing_and_renewal_disclosure",
        ],
        "evidence_requirements": {
            "product_details": "required",
            "terms": "recommended",
        },
        "content_opportunities": [
            "L6_product_review", "how_it_works", "comparison", "buyer_guide",
        ],
    },

    OfferingType.COLLECTIBLE: {
        "required_facts": [
            "item_description", "materials", "dimensions", "weight",
            "finish_or_plating", "denomination", "legal_tender_status",
            "edition_or_mintage", "manufacturer", "seller_affiliation",
            "pricing", "shipping_cost", "billing_terms", "refund_policy",
        ],
        "mandatory_facts": ["item_description"],
        "authoritative_sources": [
            {"type": "vendor_page", "priority": 1},
            {"type": "terms_page", "priority": 1},
            {"type": "refund_page", "priority": 2},
        ],
        "compliance_rules": [
            "material_composition_accuracy", "no_unverified_rarity_or_value",
            "no_unverified_endorsement", "total_price_and_continuity_disclosure",
        ],
        "evidence_requirements": {
            "item_identity": "required",
            "materials": "recommended",
            "offer_terms": "recommended",
        },
        "content_opportunities": [
            "L6_product_review", "collector_guide", "comparison", "offer_explainer",
        ],
    },
}


def get_pack(offering_type: OfferingType) -> dict:
    """Get intelligence pack for an offering type. Fails closed for UNKNOWN.

    Raises ValueError if no pack exists (including UNKNOWN type).
    This prevents the system from proceeding with uncategorized entities.
    """
    pack = INTELLIGENCE_PACKS.get(offering_type)
    if pack is None:
        raise ValueError(
            f"No intelligence pack for offering type '{offering_type.value}'. "
            f"Cannot research unknown entity types — classify first. "
            f"Known types: {', '.join(t.value for t in INTELLIGENCE_PACKS)}"
        )
    return pack


def get_required_facts(offering_type: OfferingType) -> list:
    """Get the list of required facts for an offering type."""
    return get_pack(offering_type)["required_facts"]


def get_mandatory_facts(offering_type: OfferingType) -> list:
    """Get the mandatory facts — subset that MUST be present to generate output.

    Missing mandatory facts block the SOURCE_PACK stage. Missing non-mandatory
    required facts produce a warning but do not block.
    """
    return get_pack(offering_type).get("mandatory_facts", [])


def get_evidence_requirements(offering_type: OfferingType) -> dict:
    """Get evidence requirements for an offering type."""
    return get_pack(offering_type)["evidence_requirements"]


def get_content_opportunities(offering_type: OfferingType) -> list:
    """Get available content types for an offering type."""
    return get_pack(offering_type)["content_opportunities"]
