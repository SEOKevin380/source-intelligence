# Generation Friction Audit

## Operating rule

VA jobs run unattended. Routine editorial uncertainty must become an automatic
rewrite, omission, quarantine, or documented limitation. It must not become a
question for the operator.

## Stop-condition classification

### Automatic continuation

- Unknown offering subtype: continue with the generic vertical pack.
- Missing optional or normally required facts: emit an evidence-limited pack.
- Missing supplement label facts after all acquisition/OCR attempts: omit
  ingredient-specific statements and continue in limited mode.
- Compliance wording finding with a safe alternative: substitute automatically.
- Compliance finding without a safe alternative: quarantine the statement.
- Conflicting claims: quarantine conflicted claims; retain them in the audit
  ledger but exclude them from publication context.
- Elevated risk score: record it; do not pause an unattended job.
- Platform rewrite requirement: apply it automatically.
- Cumulative elapsed-time budget: do not pause unattended jobs. Per-call limits,
  result caps, and research routing remain the cost controls.
- Sparse VSL or financial record: write an informational publication/product
  review using the established identity and presentation scope; exclude return
  projections, anonymous outcomes, urgency, and transaction recommendations.

### Legitimate hard failures

- Authentication failure.
- Malformed input that cannot identify or locate any offering.
- Complete source-capture failure: no official URL or captured source artifact.
- Corrupt/tampered publication source pack.
- System/dependency outage after retries are exhausted.
- A standing category decline established by MBK policy.

These failures should end with one plain-language reason and a concrete retry or
repair action. They should never open a multi-question editorial form for a VA.

## Generator relationship

Source Intelligence performs the defensive work upstream. It gives the writing
model an affirmative approved scope, publication-safe claims, verified facts,
and documented limitations. The generation brief must not contain jailbreak
language, instructions to hide concerns, internal chain-of-thought disputes, or
aggressive commands to override model judgment.

The writing model's role is to make the approved material clear, useful,
client-positive, searchable, and platform-compliant. It is not asked to repeat
unsubstantiated performance claims or to make a securities recommendation.

## Remaining improvements

1. Add automated refusal detection in API generation and retry once with a
   narrower informational scope. Do not retry prohibited content unchanged.
2. Add source-acquisition retry telemetry visible to administrators, hidden
   from normal VA workflow unless every route fails.
3. Replace the legacy Human Review screen with an administrator-only audit
   screen. Unattended jobs no longer route there; this is UI cleanup only.
4. Add end-to-end fixtures for every offering type and source combination.
5. Add a cloud queue from Source Intelligence directly into Universal Publisher.
