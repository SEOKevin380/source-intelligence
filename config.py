"""
Source Intelligence Tool — Configuration

Loads secrets from (in order of priority):
1. Streamlit secrets (st.secrets) — when running as Streamlit app
2. Environment variables — when set in shell
3. Local .env file — when running CLI locally
4. MBK project .env — fallback for dev machine
"""
import os
import re

# ── Path Constants (CRM infrastructure) ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Database lives OUTSIDE iCloud to prevent SQLite WAL corruption from sync.
# Production may point this at a mounted Railway volume (for example
# /data/source-intelligence).  Keeping every authoritative SQLite artifact
# below one explicit root prevents the CRM and newswire ledger from silently
# landing on a disposable container filesystem.
_LOCAL_DATA_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get(
        "SOURCE_INTELLIGENCE_DATA_DIR",
        "~/.source-intelligence/data",
    )
))
os.makedirs(_LOCAL_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_LOCAL_DATA_DIR, "source_intelligence.db")
NEWSWIRE_WORKBENCH_PATH = os.path.abspath(os.path.expanduser(
    os.environ.get(
        "NEWSWIRE_WORKBENCH_HOME",
        os.path.join(_LOCAL_DATA_DIR, "newswire-workbench"),
    )
))

# One-time migration from old iCloud location
_OLD_DB_PATH = os.path.join(BASE_DIR, "source_intelligence.db")
if os.path.exists(_OLD_DB_PATH) and not os.path.exists(DB_PATH):
    import shutil
    shutil.copy2(_OLD_DB_PATH, DB_PATH)
    print(f"[DB MIGRATION] Copied database from iCloud to {DB_PATH}")

OUTPUT_DIR = os.path.join(BASE_DIR, "output")
WP_SITES_JSON_PATH = os.path.expanduser(
    "~/Desktop/Code Projects/mbk-recovery/config/wp-sites.json"
)
MASTER_PRODUCT_LIST_PATH = os.path.expanduser(
    "~/master-publisher-data/master_product_list.json"
)
SLUG_DIVERSIFIER_DIR = os.path.expanduser("~/master-publisher-data")


def _get_secret(key, default=""):
    """Get a secret from Streamlit secrets, env vars, or .env files."""
    # 1. Try Streamlit secrets first (when running as web app)
    try:
        import streamlit as st
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except (ImportError, Exception):
        pass

    # 2. Try environment variables
    val = os.environ.get(key)
    if val:
        return val

    # 3. Fall through to .env loading (handled below)
    return default


def _load_env():
    """Load environment variables from .env files."""
    env_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.expanduser("~/Desktop/Code Projects/mbk-recovery/.env"),
    ]
    for path in env_paths:
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key and value and key not in os.environ:
                            os.environ[key] = value

_load_env()

# Claude API (reuse from publisher scripts)
ANTHROPIC_API_KEY = _get_secret("ANTHROPIC_API_KEY")

# PubMed API (free, no key required — but key increases rate limit from 3/sec to 10/sec)
PUBMED_API_KEY = _get_secret("NCBI_API_KEY")
PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_DELAY = 0.35  # 350ms between requests (safe for keyless access)
PUBMED_MAX_RESULTS = 8  # per ingredient
PUBMED_MAX_INGREDIENTS = 12  # cap ingredients to research per product

# Google Drive source material folder
GDRIVE_SOURCE_FOLDER_ID = "1WpM3JTQnT1NGVZMtANK1--B7jrSwwlfH"

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INGREDIENT_DB_PATH = os.path.join(BASE_DIR, "ingredient_db.json")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# User agent for web fetching
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# AccessWire/Barchart R12 blocklist (synced with production system v2.16)
# ABSOLUTE BLOCKLIST — never use these terms or variants in any publishable content
ACCESSWIRE_BLOCKLIST = [
    # Core sexual terms (from production R12)
    "male enhancement", "erection", "erectile", "libido", "arousal",
    "sex", "sexual", "sexually", "sexuality", "sexual function",
    "sexual side effects", "stamina", "climax", "orgasm", "bedroom",
    "intercourse", "penetration", "genital", "genitalia",
    "ejaculation", "ejaculatory", "impotence", "impotent",
    "premature ejaculation", "aphrodisiac", "virility", "virile",
    "potency", "manhood",
    # Anatomical terms
    "penis", "penile", "phallus", "phallic",
    # Size/enlargement claims
    "enlargement", "inches", "girth",
    # Suggestive / euphemistic variants
    "performance booster", "performance enhancer",
    "sex drive", "sex life", "in the bedroom", "between the sheets",
    "rise to the occasion", "firmer", "harder", "rock hard",
    "longer lasting", "lasting longer", "staying power", "natural lift",
]

# R12 Safe Alternatives — non-explicit clinical synonyms for writing around R12 terms
# The production system uses these to WRITE THE ARTICLE using safe language.
# R12 FAIL does NOT mean "decline to write" — it means "write around these terms."
# These mappings tell the production system exactly what language to use instead.
R12_SAFE_ALTERNATIVES = {
    # Core sexual terms → clinical/functional alternatives
    "male enhancement": "men's wellness formula",
    "erection": "physiological response",
    "erectile": "vascular function",
    "libido": "men's wellness",
    "arousal": "responsiveness",
    "sex": "intimacy",
    "sexual": "intimate",
    "sexually": "in intimate contexts",
    "sexuality": "intimate wellness",
    "sexual function": "intimate function",
    "sexual side effects": "intimate wellness effects",
    "stamina": "sustained energy",
    "climax": "peak satisfaction",
    "orgasm": "peak experience",
    "bedroom": "private moments",
    "intercourse": "intimate activity",
    "penetration": "physical intimacy",
    "genital": "anatomical",
    "genitalia": "anatomy",
    "ejaculation": "release timing",
    "ejaculatory": "release-related",
    "impotence": "performance difficulty",
    "impotent": "experiencing difficulty",
    "premature ejaculation": "timing concerns",
    "aphrodisiac": "men's vitality",
    "virility": "men's vitality",
    "virile": "vital",
    "potency": "efficacy",
    "manhood": "masculine confidence",
    # Anatomical terms → clinical alternatives
    "penis": "male anatomy",
    "penile": "anatomical",
    "phallus": "anatomy",
    "phallic": "anatomical",
    # Size/enlargement → vascular/blood flow framing
    "enlargement": "vascular support",
    "inches": "measurable improvement",
    "girth": "vascular fullness",
    # Suggestive variants → clinical alternatives
    "performance booster": "wellness support formula",
    "performance enhancer": "function-supporting blend",
    "sex drive": "men's wellness",
    "sex life": "intimate wellness",
    "in the bedroom": "in intimate settings",
    "between the sheets": "in private moments",
    "rise to the occasion": "respond when it matters",
    "firmer": "improved vascular tone",
    "harder": "enhanced blood flow",
    "rock hard": "optimal vascular response",
    "longer lasting": "sustained duration",
    "lasting longer": "extended duration",
    "staying power": "sustained performance",
    "natural lift": "natural support",
}

# Deceptive/impossible claim patterns (regex) — auto-block on match
# These represent physically impossible outcomes that no legitimate product can deliver
DECEPTIVE_CLAIM_PATTERNS = [
    re.compile(r"increase.{0,20}(penis|member|size).{0,20}\d+.{0,10}(inch|cm|centimeter)", re.IGNORECASE),
    re.compile(r"grow.{0,20}\d+.{0,10}(inch|cm)", re.IGNORECASE),
    re.compile(r"(permanent|guaranteed).{0,20}(enlargement|growth|increase)", re.IGNORECASE),
    re.compile(r"\d+.{0,5}(inch|cm).{0,20}(bigger|larger|longer|growth)", re.IGNORECASE),
]

# Globe Newswire phrase blocklist (Categories A-K from Globe v1.12)
# These are confirmed rejection triggers — zero tolerance on Globe platform.
# Organized by category for compliance reporting.
GLOBE_BLOCKLIST = {
    "A_affiliate": [
        "affiliate link", "commission may be earned", "referral fee",
        "this compensation does not influence", "if you register through the links",
        "sponsored",
    ],
    "B_self_reference": [
        "this article", "this overview", "this report", "in this report",
        "at the time of this report", "this report examines",
        "this article compiles",
    ],
    "C_publisher": [
        "independent content publisher", "the publisher is not affiliated",
        "the publisher makes no independent verification",
        "all claims are attributed to", "publisher responsibility disclaimer",
        "platform and legal disclaimer", "non-affiliation disclaimer",
        "editorial position", "editorial independence",
    ],
    "D_observer": [
        "according to the company", "according to the brand",
        "according to product materials", "according to the platform",
        "the brand states", "the company states that",
        "the brand describes", "the company describes",
        "as described on the official product page",
        "per the official website", "per the supplement facts panel",
        "is presented as", "is described as", "is positioned as",
        "is characterized as", "is framed as", "is marketed as",
        "is portrayed as", "drawn directly from", "sourced from",
        "based on information from", "documented on",
        "details confirmed at", "information available at",
        "available sources", "the available record shows",
        "source materials identify", "source materials show",
    ],
    "E_consumer_guide": [
        "before signing up", "what to know before",
        "what to confirm before", "for readers evaluating",
        "this is the consumer report", "you came here first",
    ],
    "F_external_validation": [
        "per third-party review", "reportedly include",
        "at the time of this report", "this report will be updated",
    ],
    "G_cta": [
        "click here", "buy now", "view the current offer",
        "order now", "get yours today", "shop now",
    ],
    "H_entity_risk": [
        # Category H: Never name operating company unless verified across ALL of
        # ToS + footer + contact page. These phrases signal unverified entity naming.
        # Note: H is primarily a procedural rule (omit-if-unverified), but these
        # phrases are structural triggers when they appear in source data.
    ],
    "I_bare_outcome_verbs": [
        # Category I: Bare outcome verbs stated as clinical fact.
        # These must be rewritten to mechanism-forward phrasing.
        "boosts collagen", "reduces inflammation", "stimulates",
        "improves circulation", "permanent hair reduction",
        "visible results from the first session",
        "clinically developed", "clinically proven",
        "dermatologist-recommended", "recommended and used by",
    ],
    "J_telehealth_absolutes": [
        # Category J: Telehealth/YMYL absolute claims that need qualification.
        "fastest-growing category", "strict quality and safety protocols",
        "reported to improve as the body adjusts",
        "all at no additional cost",
        "insurance is not accepted or required",
        "pharmacy network covering all 50 states",
        "success stories prove",
    ],
    "K_urgency": [
        "act now", "don't miss out", "limited time only",
        "while supplies last", "hurry", "doctors hate this",
        "shocking truth", "hidden secret",
    ],
}

# YMYL categories and their risk levels
YMYL_CATEGORIES = {
    "weight_loss": "High",
    "blood_sugar": "High",
    "brain_health": "High",
    "heart_health": "High",
    "male_enhancement": "Very High",
    "anti_aging": "High",
    "sleep": "Moderate",
    "joint_health": "Moderate",
    "vision": "Moderate",
    "dental": "Moderate",
    "skin_care": "Moderate",
    "immune_health": "Moderate",
    "gut_health": "Moderate",
    "nerve_health": "High",
    "respiratory": "Moderate",
    "pain_relief": "High",
    "telehealth": "High",
    "financial": "High",
    "device": "Low",
    "info_product": "Low",
    "cannabis": "High",
}

# Disease/treatment claim words that MUST be hedged in YMYL content
CLAIM_RED_FLAGS = [
    "cures", "cure", "treats", "treat", "heals", "healing",
    "prevents", "prevent", "eliminates", "eliminate", "reverses", "reverse",
    "guaranteed", "100% safe", "miracle", "breakthrough",
    "clinically proven", "doctor recommended", "FDA approved",
    "no side effects", "risk-free", "permanent results"
]

# Safe hedging alternatives
HEDGE_ALTERNATIVES = {
    "cures": "may help support",
    "treats": "is designed to help manage",
    "heals": "may help support recovery",
    "prevents": "may help reduce the risk of",
    "eliminates": "may help reduce",
    "reverses": "may help support improvement in",
    "guaranteed": "backed by a money-back guarantee",
    "clinically proven": "supported by some clinical research",
    "FDA approved": "manufactured in an FDA-registered facility (note: dietary supplements are not FDA-approved)",
}

# CVD-9: Disease-reversal claim detection
# When a red flag verb (cures, fixes, reverses, etc.) appears alongside a disease/condition
# term, the claim is a CVD-9 BLOCK — cannot be attributed, hedged, or softened.
# The production system must exclude these claims entirely and write around them.
CVD9_DISEASE_TERMS = [
    "diabetes", "type 2 diabetes", "type 1 diabetes", "blood pressure",
    "hypertension", "cancer", "tumor", "heart disease", "cardiovascular disease",
    "stroke", "alzheimer", "dementia", "parkinson", "epilepsy", "seizure",
    "hiv", "aids", "hepatitis", "kidney disease", "liver disease",
    "arthritis", "lupus", "multiple sclerosis", "crohn", "colitis",
    "asthma", "copd", "pneumonia", "tuberculosis",
    "depression", "anxiety disorder", "bipolar", "schizophrenia",
    "obesity", "anorexia", "bulimia",
    "glaucoma", "macular degeneration", "cataracts",
    "infertility", "endometriosis", "fibroids",
    "osteoporosis", "scoliosis",
]
CVD9_REVERSAL_VERBS = [
    "fixes", "fix", "cures", "cure", "reverses", "reverse",
    "eliminates", "eliminate", "eradicates", "eradicate",
    "heals", "heal", "repairs", "repair", "restores", "restore",
    "gets rid of", "wipes out", "destroys", "kills",
]

# CVD-9: Standing Decline Categories
# These are automatic HARD STOPS at the category level — the production system
# will refuse to write regardless of data quality. Only the telehealth exception
# (confirmed three-entity structure) can override the multi_pde5 decline.
CVD9_STANDING_DECLINES = {
    "drug_test_defeat": {
        "description": "Products designed to defeat, cheat, or pass drug tests",
        "keywords": ["pass drug test", "beat drug test", "drug test defeat", "clean urine",
                     "synthetic urine", "detox for drug test", "pass a ua", "flush thc"],
        "action": "HARD DECLINE — cannot publish under any circumstances",
    },
    "multi_pde5_no_physician": {
        "description": "Multiple PDE5 inhibitors stacked without physician supervision",
        "keywords": ["sildenafil and tadalafil", "tadalafil and sildenafil",
                     "dual pde5", "stacked ed", "combined erectile"],
        "action": "DECLINE unless telehealth three-entity structure confirmed (R25 exception)",
        "exception": "telehealth_three_entity",
    },
    "escharotic_formulation": {
        "description": "Caustic/escharotic chemical formulations (black salve, etc.)",
        "keywords": ["black salve", "escharotic", "bloodroot paste", "caustic treatment",
                     "burn off moles", "burn off skin tags"],
        "action": "HARD DECLINE — dangerous unregulated treatments",
    },
    "weapon_enhancement": {
        "description": "Products designed to enhance weapon lethality or evade regulations",
        "keywords": ["silencer", "suppressor kit", "ghost gun", "untraceable firearm",
                     "bump stock", "convert to auto"],
        "action": "HARD DECLINE — liability and legal risk",
    },
}

# CVD-12: Product Type Routing
# Different product types have different compliance paths, risk levels, and
# platform eligibility. The production system uses this to route content correctly.
PRODUCT_TYPE_ROUTES = {
    "supplement": {
        "compliance_level": "standard",
        "fda_disclaimer_required": True,
        "platforms": ["accesswire", "newswire", "barchart", "globe"],
        "r12_applies": True,
        "globe_allowed": True,
        "notes": "Standard supplement review path. All R-rules apply.",
    },
    "telehealth": {
        "compliance_level": "elevated",
        "fda_disclaimer_required": True,
        "platforms": ["accesswire", "newswire", "barchart", "globe"],
        "r12_applies": True,
        "globe_allowed": True,
        "requires_c17": True,
        "notes": "Telehealth/prescription path. Requires R25 three-entity structure in C17. "
                 "Multi-PDE5 combinations allowed ONLY with confirmed three-entity structure.",
    },
    "research_peptide": {
        "compliance_level": "elevated",
        "fda_disclaimer_required": True,
        "platforms": ["accesswire", "newswire", "barchart"],
        "r12_applies": True,
        "globe_allowed": False,
        "notes": "Research peptide path. Globe typically rejects. Requires 'for research purposes' framing.",
    },
    "device": {
        "compliance_level": "light",
        "fda_disclaimer_required": False,
        "platforms": ["accesswire", "newswire", "barchart", "globe"],
        "r12_applies": True,
        "globe_allowed": True,
        "notes": "Device/gadget path. Lower YMYL risk. No FDA supplement disclaimer needed.",
    },
    "info_product": {
        "compliance_level": "minimal",
        "fda_disclaimer_required": False,
        "platforms": ["accesswire", "newswire", "barchart", "globe"],
        "r12_applies": False,
        "globe_allowed": True,
        "notes": "Info product path (newsletters, courses, ebooks). Minimal health compliance.",
    },
    "food": {
        "compliance_level": "standard",
        "fda_disclaimer_required": True,
        "platforms": ["accesswire", "newswire", "barchart", "globe"],
        "r12_applies": True,
        "globe_allowed": True,
        "notes": "Food/beverage path. Standard compliance with allergen awareness.",
    },
    "topical": {
        "compliance_level": "standard",
        "fda_disclaimer_required": True,
        "platforms": ["accesswire", "newswire", "barchart", "globe"],
        "r12_applies": True,
        "globe_allowed": True,
        "notes": "Topical product path (creams, serums, patches). Standard compliance.",
    },
    "financial": {
        "compliance_level": "elevated",
        "fda_disclaimer_required": False,
        "platforms": ["accesswire", "newswire", "barchart"],
        "r12_applies": False,
        "globe_allowed": False,
        "notes": "Financial product path. Different YMYL rules (investment disclaimers).",
    },
    "cannabis": {
        "compliance_level": "elevated",
        "fda_disclaimer_required": True,
        "platforms": ["accesswire", "newswire", "barchart"],
        "r12_applies": True,
        "globe_allowed": False,
        "notes": "Cannabis/hemp/THCA product path. Elevated compliance — legal gray area. "
                 "Requires state legality disclaimer, 2018 Farm Bill reference for hemp-derived, "
                 "age restriction notice. Globe typically rejects. No disease-treatment claims.",
    },
}

# Category-Claim Alignment Keywords (for C15 Path A conflict detection)
# Each product category expects certain types of claims. If a product is categorized
# as X but ALL its claims are about Y, that's a Path A conflict that needs resolution.
CATEGORY_CLAIM_KEYWORDS = {
    "male_enhancement": {
        "expected": ["erectile", "erection", "libido", "sexual performance",
                     "testosterone", "virility", "bedroom", "dysfunction",
                     "nitric oxide", "blood flow", "male vitality", "intimate"],
        "conflicts_with": ["weight loss", "fat burn", "metabolism", "cognitive",
                           "memory", "focus", "brain", "joint", "vision",
                           "mental fatigue", "concentration", "recall"],
    },
    "weight_loss": {
        "expected": ["weight", "fat", "metabolism", "appetite", "calorie",
                     "slim", "lean", "thermogenic", "ketosis", "metabolic"],
        "conflicts_with": ["erectile", "libido", "erection", "brain health",
                           "joint", "vision", "dental"],
    },
    "brain_health": {
        "expected": ["cognitive", "memory", "focus", "brain", "mental clarity",
                     "concentration", "neural", "nootropic", "recall"],
        "conflicts_with": ["erectile", "libido", "weight loss", "fat burn",
                           "joint", "dental", "vision"],
    },
    "blood_sugar": {
        "expected": ["blood sugar", "glucose", "insulin", "glycemic", "a1c",
                     "diabetic", "pancreatic", "metabolic"],
        "conflicts_with": ["erectile", "libido", "brain", "cognitive",
                           "joint", "vision", "dental"],
    },
    "joint_health": {
        "expected": ["joint", "cartilage", "flexibility", "mobility", "inflammation",
                     "arthritis", "collagen", "connective tissue"],
        "conflicts_with": ["erectile", "libido", "weight loss", "brain",
                           "blood sugar", "vision"],
    },
    "skin_care": {
        "expected": ["skin", "wrinkle", "collagen", "complexion", "dermal",
                     "moisture", "aging", "elasticity", "glow"],
        "conflicts_with": ["erectile", "libido", "weight loss", "blood sugar",
                           "joint", "brain"],
    },
    "nerve_health": {
        "expected": ["nerve", "neuropathy", "tingling", "numbness", "peripheral",
                     "neural", "nerve damage", "nerve pain"],
        "conflicts_with": ["erectile", "libido", "weight loss", "dental",
                           "vision", "skin"],
    },
    "heart_health": {
        "expected": ["heart", "cardiovascular", "cholesterol", "blood pressure",
                     "CoQ10", "omega-3", "arterial", "cardiac", "triglycerides"],
        "conflicts_with": ["erectile", "libido", "weight loss", "brain",
                           "dental", "vision"],
    },
    "anti_aging": {
        "expected": ["aging", "longevity", "NAD+", "NMN", "telomere", "cellular",
                     "rejuvenation", "resveratrol", "senescent", "mitochondrial"],
        "conflicts_with": ["erectile", "libido", "weight loss", "blood sugar",
                           "joint", "dental"],
    },
    "sleep": {
        "expected": ["sleep", "insomnia", "melatonin", "circadian", "restful",
                     "relaxation", "GABA", "magnesium", "nighttime"],
        "conflicts_with": ["erectile", "libido", "weight loss", "blood sugar",
                           "brain", "joint"],
    },
    "vision": {
        "expected": ["vision", "eye", "macular", "lutein", "retina", "ocular",
                     "zeaxanthin", "eye health", "sight"],
        "conflicts_with": ["erectile", "libido", "weight loss", "blood sugar",
                           "brain", "joint"],
    },
    "gut_health": {
        "expected": ["gut", "probiotic", "microbiome", "digestive", "bloating",
                     "prebiotic", "flora", "intestinal", "fiber"],
        "conflicts_with": ["erectile", "libido", "brain", "vision",
                           "joint", "dental"],
    },
    "immune_health": {
        "expected": ["immune", "immunity", "elderberry", "vitamin C", "zinc",
                     "antioxidant", "defense", "pathogen", "white blood cell"],
        "conflicts_with": ["erectile", "libido", "weight loss", "brain",
                           "joint", "vision"],
    },
    "dental_health": {
        "expected": ["dental", "teeth", "gum", "oral", "cavity", "enamel",
                     "periodontal", "probiotic", "mouth"],
        "conflicts_with": ["erectile", "libido", "weight loss", "brain",
                           "joint", "vision"],
    },
    "telehealth": {
        "expected": ["physician", "prescription", "consultation", "licensed",
                     "medical", "doctor", "provider", "compounding"],
        "conflicts_with": [],  # Telehealth is a delivery mechanism, not a health category
    },
    "cannabis": {
        "expected": ["thca", "thc", "cbd", "cannabinoid", "hemp", "flower",
                     "strain", "terpene", "indica", "sativa", "hybrid",
                     "delta-8", "delta-9", "concentrate", "extract",
                     "calming", "uplifting", "relaxation", "lab tested",
                     "full spectrum", "broad spectrum", "isolate"],
        "conflicts_with": ["blood sugar", "glucose", "dental", "vision",
                           "cardiovascular", "cholesterol"],
    },
}

# Platform-specific compliance rules summary
# Used by the prompt builder to output correct platform context
PLATFORM_RULES = {
    "accesswire": {
        "blocklists": ["R12"],
        "format": "Standard press release",
        "globe_rules_apply": False,
        "notes": "R12 blocklist in full effect. Standard ACW format.",
    },
    "newswire": {
        "blocklists": ["R12"],
        "format": "Standard press release",
        "globe_rules_apply": False,
        "notes": "Same as AccessWire. R12 in full effect.",
    },
    "barchart": {
        "blocklists": ["R12", "B1-B4"],
        "format": "ACW format + B1-B4 overlay",
        "globe_rules_apply": False,
        "notes": "R12 + B1: Zero schema, B2: Zero platform furniture, "
                 "B3: 'Fake Testimonial Hype' approved, B4: R12 full.",
    },
    "globe": {
        "blocklists": ["Globe_A-K"],
        "format": "Format C (default)",
        "globe_rules_apply": True,
        "r12_applies": False,
        "notes": "Globe Categories A-K in effect. R12 does NOT apply to Globe. "
                 "Brand-as-subject voice. Mechanism-forward claims only.",
    },
}

# Safe category display labels for production system output
# Internal codes → human-readable labels that won't trigger content policy filters
# The production system sees these labels, not the internal category codes.
CATEGORY_DISPLAY_LABELS = {
    "male_enhancement": "Men's Vitality & Vascular Health",
    "weight_loss": "Weight Management & Metabolic Health",
    "blood_sugar": "Blood Sugar & Glycemic Support",
    "brain_health": "Cognitive & Brain Health",
    "heart_health": "Cardiovascular Health",
    "anti_aging": "Longevity & Anti-Aging",
    "sleep": "Sleep & Recovery",
    "joint_health": "Joint & Mobility Support",
    "vision": "Vision & Eye Health",
    "dental": "Oral Health",
    "skin_care": "Skin Health & Dermatology",
    "immune_health": "Immune Support",
    "gut_health": "Digestive & Gut Health",
    "nerve_health": "Nerve Health & Neuropathy Support",
    "respiratory": "Respiratory Health",
    "pain_relief": "Pain Management",
    "telehealth": "Telehealth & Prescription Wellness",
    "financial": "Financial Products",
    "device": "Health Devices & Gadgets",
    "info_product": "Information Products",
    "cannabis": "Hemp & Cannabis Products",
}

# Risk level display mapping — production-safe framing
# "Very High" can trigger over-cautious behavior in AI systems.
# These convey the same compliance information without alarm-word framing.
RISK_DISPLAY_LABELS = {
    "Very High": "Elevated — R12 + hedging mandatory",
    "High": "Standard-Plus — hedging required",
    "Moderate": "Standard — normal compliance",
    "Low": "Minimal — light compliance",
}

# Site category mappings for publishing recommendations
SITE_CATEGORIES = {
    "pvmedcenter": {
        "weight_loss": [12], "brain_health": [13], "telehealth": [14],
        "male_enhancement": [17], "nerve_health": [19], "supplement_reviews": [20],
    },
    "tutelamedical": {
        "weight_loss": [100], "skin_care": [102], "male_enhancement": [103],
        "blood_sugar": [105], "brain_health": [106], "dental": [107],
        "nerve_health": [111], "supplement_reviews": [112], "gut_health": [120],
        "joint_health": [126], "respiratory": [127],
    },
    "totalhealthrd": {
        "weight_loss": [23, 38], "blood_sugar": [24], "supplement_reviews": [27],
        "gut_health": [31], "brain_health": [32], "joint_health": [33],
        "skin_care": [34], "pain_relief": [37], "dental": [40],
        "vision": [46], "nerve_health": [22],
    },
    "hathawaymd": {
        "weight_loss": [33, 78], "blood_sugar": [36], "brain_health": [49],
        "male_enhancement": [62], "joint_health": [71], "vision": [72],
        "pain_relief": [81], "anti_aging": [89], "supplement_reviews": [98],
    },
    "hollyherman": {
        "weight_loss": [11], "skin_care": [12], "male_enhancement": [15],
        "brain_health": [16], "blood_sugar": [18], "sleep": [46],
        "telehealth": [67], "anti_aging": [70], "supplement_reviews": [92],
        "immune_health": [94], "respiratory": [95],
    },
    "utcts": {
        "weight_loss": [14], "anti_aging": [15], "nerve_health": [16],
        "heart_health": [18], "blood_sugar": [24], "supplement_reviews": [22],
        "brain_health": [23],
    },
    "micc": {
        "weight_loss": [12], "skin_care": [13], "brain_health": [44],
        "telehealth": [45], "male_enhancement": [57], "joint_health": [65],
        "vision": [66], "supplement_reviews": [72],
    },
}
