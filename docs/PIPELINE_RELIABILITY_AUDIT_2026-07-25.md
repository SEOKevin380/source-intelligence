# Source Intelligence Reliability Audit — 2026-07-25

## Governing posture

Source Intelligence is a compliant performance-marketing system. It makes the
strongest accurate client case supported by the captured record, preserves
commercial intent through attribution and qualification, and states material
limitations once in measured language. Compliance is the performance boundary;
it is not an instruction to write a watchdog or prosecution brief.

## Enforced invariants

- Every canonical `OfferingType` owns an intelligence pack, exact workbench
  route, prompt coverage contract, risk tier, policy scope, exemplar boundary,
  depth profile, and bounded execution budget.
- Explicit product types are never flattened to `health` or
  `general_consumer`. Unknown explicit types stop before model spend.
- Adding a future enum without every downstream owner fails startup/tests.
- Keyword and reputation research is product-specific. Gaming, collectibles,
  software, and services no longer inherit supplement/FDA/scam query templates.
- Hard compliance matches are physically excluded from publication claims.
- Structured claims preserve the real source class; operator/context artifacts
  are never promoted to official seller evidence.
- A blocked source pack cannot be persisted as completed research.
- Limited but valid packs remain draftable; unavailable facts are omitted.
- A valid exact-hash package remains authoritative over a later failed
  duplicate, independent of a retryable WordPress delivery.
- Invalid, truncated, stale, rejected, or ambiguous paid responses own a fresh
  corrected transaction instead of offering an impossible resume.
- Provider request intent is committed to the ledger before network I/O. An
  ambiguous or stranded request is quarantined and is never silently replayed.
- Queue leases fence expired workers, and cancelled jobs are finalized after a
  dead worker's lease expires.
- Both research/update pipelines and newswire provider workflows are owned by
  durable background workers. Browser tabs submit and observe; they do not own
  long-running research or paid calls.
- Platform rules are executable contracts. AccessNewsWire, Barchart, and Globe
  each own their CTA, disclosure, FAQ, link, voice, and formatting rules.
  Newswire.com is rejected before project creation because it has no approved
  automated contract.

## Zero-cost printer self-test

Run:

```bash
python3 scripts/audit_pipeline_contract.py
```

The audit performs no model calls. It currently checks 16 exact product types,
64 declared product-type/platform outcomes, 48 automated platform contracts,
and 192 complete prompt builds. Any new taxonomy or platform entry must make
this test pass before deployment.

## Verification

- Full automated suite: `701 passed`
- Pipeline contract audit: `passed`, `prompt_contracts_checked=192`,
  `model_calls=0`
- Runtime revision: `publisher-contract-durability-audit-20260725-r34`
- Production editorial-truth inventory before this deployment: 25 stored
  article artifacts, 9 deterministic passes, and 16 historical failures. Every
  failure is already isolated in `admin_review`; the only `package_ready`
  project passed the exact stored-artifact audit.

External provider, network, credential, and publisher outages cannot be made
impossible. The reliability contract is therefore: no silent jam, no hidden
approved artifact, no blind retry, and one typed recovery owner for every
failure family.

## Remaining external boundaries

No software can guarantee that a model provider, source website, network,
credential, WordPress destination, or newswire publisher will never fail. The
system contract is that those failures cannot become silent duplicate spend,
an invisible approved artifact, or an unowned browser jam. Each failure stops
at a typed boundary with a preserved ledger, saved checkpoints, and one safe
recovery path.
