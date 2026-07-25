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
- Queue leases fence expired workers, and cancelled jobs are finalized after a
  dead worker's lease expires.

## Zero-cost printer self-test

Run:

```bash
python3 scripts/audit_pipeline_contract.py
```

The audit performs no model calls. It currently checks 16 exact product types
and 32 product-type/platform contracts. Any new taxonomy entry must make this
test pass before deployment.

## Verification

- Full automated suite: `625 passed`
- Pipeline contract audit: `passed`, `model_calls=0`
- Runtime revision: `product-first-blueprint-owner-20260725-r24`

External provider, network, credential, and publisher outages cannot be made
impossible. The reliability contract is therefore: no silent jam, no hidden
approved artifact, no blind retry, and one typed recovery owner for every
failure family.

## Remaining architectural upgrade

The durable `RunJobRepository` now has safe lease/cancellation primitives, but
the production Streamlit request still executes the research and workbench
loops synchronously. A dedicated worker service should become the sole owner of
provider calls so a browser disconnect or deployment restart resumes without
an operator click. This is the next infrastructure milestone; the current
engine still uses persisted stage checkpoints and stale-lease recovery.
