# Newswire Compliance Workbench

This local tool preserves the independent two-model workflow used for
AccessNewsWire and Barchart:

1. Source Intelligence record
2. Claude first draft
3. ChatGPT comprehensive compliance review
4. Claude revision
5. ChatGPT sign-off
6. Claude SEO optimization
7. ChatGPT post-SEO regression review
8. Hash-bound submission package

The extra post-SEO review is mandatory because SEO edits change the article
after sign-off. Every article version, review, hash, and stage transition is
stored in an append-only audit history under:

`~/.source-intelligence/newswire-workbench/`

## Start locally

```bash
bash run_newswire_workbench.sh
```

Then open `http://127.0.0.1:8101`.

On Kevin's Mac, two Desktop shortcuts are installed:

- `Configure Newswire Workbench.command` securely saves the OpenAI API key.
- `Start Newswire Workbench.command` starts the app and opens the browser.

## Keys

- `ANTHROPIC_API_KEY`: Claude generation, revision, and SEO.
- `OPENAI_API_KEY`: ChatGPT compliance review and sign-off.
- `ANTHROPIC_GENERATION_MODEL`: optional override.
- `OPENAI_COMPLIANCE_MODEL`: optional override; default is `gpt-5`.

If a key is absent, the Manual fallback tab accepts the corresponding Claude
article or ChatGPT compliance report without losing project history.

## VA operation

The VA selects a platform, uploads/pastes the source record, creates the
project, and clicks **Run entire workflow**. Failed compliance automatically
returns to Claude revision. After three unsuccessful repair rounds, the item
moves to Kevin's review queue and the VA moves to the next project. The
workflow does not ask a VA to select a claim, positioning angle, or legal
interpretation.
