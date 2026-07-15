"""
Source Intelligence Tool — Configuration

Loads secrets from (in order of priority):
1. Streamlit secrets (st.secrets) — when running as Streamlit app
2. Environment variables — when set in shell
3. Local .env file — when running CLI locally
4. MBK project .env — fallback for dev machine
"""
import os


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

# AccessWire blocklist terms (from MBK compliance rules)
ACCESSWIRE_BLOCKLIST = [
    "erection", "erectile", "libido", "arousal", "sexual function",
    "stamina", "climax", "intercourse", "penetration", "impotence",
    "premature ejaculation", "orgasm", "aphrodisiac", "virility", "potency"
]

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
