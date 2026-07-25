# Source Intelligence to Universal Publisher Audit

## Governing objective

Every generated article must begin with captured, traceable source material.
Routine gaps change what the article says; they do not send a VA into a review
loop. Unsupported or altered facts must not enter publication automation.

## Corrected in this integration

- Source Intelligence now emits a versioned publication source pack with a
  SHA-256 integrity seal and MBK Master Content Generation System v3.8 marker.
- Readiness is plain and machine-readable: `complete`, `limited`, or `blocked`.
- `limited` packs publish automatically using verified facts and documented
  omissions. `blocked` is reserved for missing identity, official URL, or
  captured source material.
- Publication-safe claims are separated from the complete audit ledger.
  Conflicted, inferred, manual-unverified, and needs-verification claims remain
  traceable but are excluded from publisher context.
- Universal Publisher requires a verified pack for normal automation. The old
  URL/model-memory workflow requires the explicit `--legacy-source-fetch` flag.
- Pack identity, version, readiness, product name, and integrity hash are
  validated before an API generation call, avoiding wasted model cost.
- Verified jobs use a vertical-aware template. Financial, device, software,
  telehealth, service, information-product, and physical-product jobs can no
  longer inherit supplement or equipment-finance fields by accident.
- The publisher receives a compact research representation (maximum five
  studies per ingredient), limiting token cost without losing the full sealed
  source record.
- Research and update jobs are persisted before entering a lease-fenced worker
  queue. Closing the browser cannot interrupt acquisition, OCR, research,
  compliance, or final source-pack persistence.
- Newswire drafting, independent review, bounded repair, final sign-off, and
  optional WordPress delivery are also worker-owned. Provider request intent
  is stored before network I/O, preventing unsafe replay after a crash.
- Platform behavior is selected from executable contracts for AccessNewsWire,
  Barchart, and Globe. Unsupported destinations stop before model spend.

## Remaining high-priority work

1. Add PubMed relevance scoring at the study-to-ingredient level. A PubMed hit
   proves retrieval, not that the paper supports a product outcome.
2. Version and provenance-tag the shared ingredient knowledge cache. Cached
   research should never appear indistinguishable from research run for the
   current product and ingredient form.
3. Replace supplement-weighted CRM completeness scoring with per-offering-type
   scorecards from the intelligence packs.
4. Add acquisition caching keyed by submitted-source manifest hash so unchanged
   reports skip browser, OCR, and research calls while still checking freshness
   for price and policy facts.
5. Add an automation dashboard showing pack hash, source count, freshness,
   readiness, generation status, destination site, and publication URL in plain
   language.

## Operator workflow

1. Submit URLs/files once in Source Intelligence. The durable worker owns the
   run, so the browser can be closed.
2. Wait for `Ready for automated publishing` or `Ready ... with documented gaps`.
3. Download the Publication Source Pack, or export it locally with
   `export_source_packs.py`.
4. Put its path in the Universal Publisher CSV `source_pack` column.
5. Run Universal Publisher normally. No factual research is repeated during
   article generation.

Legacy URL-only publishing remains available solely through the explicit
`--legacy-source-fetch` switch for controlled migration work.
