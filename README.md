# Source Intelligence Tool

Product research CRM built with Streamlit. Aggregates data from vendor pages, NIH DSLD, PubMed, and FDA CAERS into structured research profiles for supplement and health product analysis.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium  # optional — for JS-rendered pages
```

### Environment Variables

- `ANTHROPIC_API_KEY` — required for AI-powered research phases

### Database

SQLite database stored at `~/.source-intelligence/data/source_intelligence.db`. Created automatically on first run. Current schema version: 2.

## Running

```bash
streamlit run app.py
```

### CLI

```bash
python research_product.py --url "https://example.com/product" --name "Product Name"
python research_product.py --csv products.csv
python research_product.py --label /path/to/supplement-facts.jpg --name "Product Name"
```

## Tests

```bash
pip install pytest
pytest tests/ -v
```

Test coverage:
- `test_url_validation.py` — SSRF protection, private IP blocking, protocol whitelist
- `test_dsld_matching.py` — NIH DSLD false-positive prevention
- `test_scoring.py` — Completeness scoring, human vs animal study weighting, C16-C19 local scoring
- `test_database.py` — Schema migrations, hash-based freshness tracking
