# Model Routing Architecture

## Objective

Optimize for cost per approved, publishable package. No provider or model owns
the whole workflow. Mechanical rules are code; models receive bounded jobs.

## Production assembly line

1. Source Intelligence captures and seals the evidence pack.
2. The same sealed pack creates an idempotent workbench project.
3. A pinned editorial model creates one evidence-bound draft.
4. Deterministic code normalizes HTML, headings, CTA links, emphasis limits,
   disclosures, hashes, and provenance.
5. An independent structured reviewer returns a fixed-schema compliance report.
6. Repairs are bounded to two calls. Repeated or conflicting findings enter the
   admin queue; VAs are never asked to resolve them.
7. SEO gets one bounded pass followed by an independent regression review.
8. Approved content is packaged from the reviewed article hash and optionally
   saved to ZingFast as a WordPress draft. Publishing remains a separate action.

## Current pinned routes

| Job | Production route | Reason |
|---|---|---|
| First draft | Claude Sonnet 4.5 direct | Incumbent editorial route; remains until corpus benchmark proves a replacement |
| Targeted repair | Claude Haiku 4.5 direct | Cheaper bounded language changes; maximum two calls |
| SEO pass | Claude Haiku 4.5 direct | One bounded pass; deterministic rules own formatting |
| Initial compliance | GPT-5.4 Mini direct | Structured JSON and independent model family |
| Final, routine products | GPT-5.4 Mini direct | Lower risk and lower cost |
| Final, health/finance/political | GPT-5.4 direct | Stronger adjudication reserved for material-risk outputs |

Environment variables can override a route, but changes must first pass the
regression corpus. `latest` aliases and silent model upgrades are prohibited.

## OpenRouter policy

OpenRouter is the benchmark arena and controlled fallback layer. It is not an
unbounded production auto-router. Production requests must pin model and data
policy; sensitive final approval should use direct providers. Provider/model
catalog entries do not count as proof that an endpoint returns usable output.

The July 22, 2026 routing audit demonstrated this directly: two catalog-listed
models initially had no compatible endpoint, and Claude Sonnet 5 later billed a
full response while returning no usable text. Gemini 3.6 Flash and DeepSeek V4
Pro returned useful architecture reviews cheaply. Those observations justify
benchmarking them; they do not promote them automatically.

## Budget and telemetry

Every workbench call records project, stage, provider, pinned model, token use,
estimated cost, status, error, and time. The default per-project ceiling is
`$1.50` (`NEWSWIRE_PROJECT_BUDGET_USD`). Stage call limits are enforced before
the request. The primary KPI is total cost to a stable approval, including
failures and repair rounds.

## Promotion benchmark

A challenger must run on a locked corpus spanning supplements, telehealth,
finance, devices, software, collectibles/political merchandise, gaming, and
general consumer products. Score:

- factual precision and recall against the sealed source pack;
- compliance false-negative and false-positive rates;
- structured-output validity;
- preservation of facts, disclosures, links, and approved language;
- approval stability across repeated runs;
- repair rounds, latency, and total cost to approval.

A challenger replaces the incumbent only when it meets all safety floors and
either lowers total approval cost by at least 20% at equal quality or improves
quality materially without exceeding the stage budget. Run a shadow challenge
on 5% of eligible jobs; never change production behavior automatically.

