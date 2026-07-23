# Newswire Workbench Full Audit

Audit scope: source intake through Claude generation, OpenAI compliance,
automatic repair, SEO regression, final packaging, local startup, team use, and
continuous-learning behavior.

## Executive result

The local workflow is suitable for controlled production on Kevin's Mac after
the fixes below. It is not yet suitable for unattended multi-user remote access
until authenticated hosting, durable service operation, and backups are added.

The first live financial project proved the central design: SEO changes created
new compliance defects, and the mandatory post-SEO review caught them. It also
exposed malformed model JSON, CTA presentation drift, duplicate-call risk, and
artifact-history gaps that ordinary happy-path tests did not catch.

## Fixed during this audit

1. **Duplicate paid runs:** per-project locks now stop two VA browser sessions
   from launching the same model stage. Locks recover after a bounded stale
   interval if the Mac or app is killed.
2. **Silent provider failures:** exceptions now create immutable
   `workflow_error` events without saving partial output.
3. **Malformed compliance JSON:** OpenAI reviews now use a strict JSON schema,
   followed by consistency validation. “Approved” reports cannot contain
   mandatory edits.
4. **False manual approvals:** unstructured pasted reports can never approve.
   Manual JSON must contain the exact current article SHA-256 hash.
5. **Truncated Claude output:** a max-token stop is rejected; partial articles
   are not advanced.
6. **Network resilience:** provider timeout and retry limits are explicit and
   configurable.
7. **Artifact overwrites:** replaced canonical files are archived with timestamp
   and content hash under each project's `history/` directory.
8. **Prompt injection from source pages/VSLs:** source and article inputs are
   delimited as evidence; embedded commands are explicitly ignored.
9. **Credential isolation:** the launcher no longer imports the entire publisher
   environment. Only dedicated workbench API keys are loaded.
10. **Affiliate presentation:** raw pretty-link URLs, intermediary-routing
    explanations, late first CTAs, and under-distributed CTAs now trigger
    deterministic failures.
11. **Prior-release SERP stacking:** prompts now require a distinct intent,
    headline, opening, and section architecture without naming prior publishers.
12. **VA packaging:** approved projects produce a one-click ZIP containing the
    source record, final article, manifest, and compliance reports.

## Remaining production dependencies

### P0 before team-wide remote use

1. **Authenticated hosting.** The Streamlit app binds to localhost and has no
   user authentication. Do not expose port 8101 directly. Put it behind
   Cloudflare Access or an equivalent identity gate.
2. **Durable service.** A Mac sleep, logout, restart, or lost network connection
   stops team access. Run the app as a launchd service or deploy it to a managed
   host with health checks and automatic restart.
3. **Backups.** The SQLite database and project artifacts need automated,
   encrypted daily backup plus a restore test. Cloud sync alone is not a tested
   database backup strategy.
4. **Authorization roles.** VAs should create/run/download; only Kevin/admin
   should reopen approved projects, change source records, or override a gate.

### P1 quality and cost controls

1. **Direct Source Intelligence handoff.** Projects still accept pasted/uploaded
   source records. Add a signed pack import or API handoff so the workbench can
   verify source hash, retrieval time, artifact list, and offering identity.
2. **Factual diff gate.** Add deterministic extraction of price, guarantee,
   phone, URL, ingredient/feature list, and priority code, then compare those
   fields against every final article. Model review should be a second line,
   not the only factual checker.
3. **Usage telemetry and budgets.** Persist provider, model, latency, input/output
   tokens, estimated cost, retry count, and failure reason per call. Enforce a
   per-project ceiling before another repair call.
4. **Idempotent provider jobs.** A response received immediately before a crash
   can still be billed without being stored. Persist a call intent and request
   fingerprint before sending, then reconcile incomplete calls on restart.
5. **Prompt compiler by vertical.** The whole master instruction file is still
   supplied with “relevant portions only.” Compile a smaller platform + vertical
   rule bundle to reduce token cost and contradictory instructions.
6. **Learning governance.** Recurring issue memory is now limited to completed
   projects, but it still needs admin accept/dismiss labels. Only validated fixes
   should graduate into prompts; reviewer false positives must not become policy.
7. **Regression corpus.** Maintain representative health, financial, gaming,
   collectible, device, telehealth, political merchandise, and general consumer
   cases. Run them before prompt/model changes and compare approvals, defects,
   cost, and length.
8. **CTA profiles.** Move CTA count, location, wording, and styling into explicit
   per-platform configuration instead of one global long-form threshold.
9. **HTML validation.** Add parser checks for one H1, valid heading order,
   non-empty anchors, duplicate IDs, broken markup, and prohibited wrappers.
10. **Article efficiency limits.** Set target length bands by platform/vertical.
    The first financial article reached roughly 2,600 words; longer is not
    automatically better and raises model cost and editorial burden.

### P2 integration and continuous improvement

1. Connect the approved package to Universal Publisher as a draft-only handoff;
   preserve human final submission for higher-compliance newswire platforms.
2. Store previous-release fingerprints (title, intent, headings, entities) and
   calculate similarity before generation and before approval.
3. Add source snapshot files and retrieval metadata to the final package.
4. Add cryptographic manifest signing. Hashes detect changes only when the
   trusted manifest itself cannot be silently replaced.
5. Add an admin dashboard for failure rate, average rounds, cost, time, recurring
   issues, false-positive dismissals, and model/prompt version comparisons.
6. Add project export/import and disaster-recovery exercises.

## Operational rule

An article is ready only when the current article hash matches an approved
structured report, deterministic gates return zero findings, the post-SEO review
is approved, and the submission manifest is generated from that same hash.

