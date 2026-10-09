# Hermes edge research batch

Objective: use the user-requested Hermes/Astra/ultra configuration for research and strategy development, building on the existing offline framework. No profitability claim or execution deployment is an acceptance criterion.

Requested model: `gpt-6-astra`; provider: `openai-codex`; requested reasoning: `ultra`. Installed Hermes maps this to API `max`, as disclosed before launch. Global model/routing/permissions are unchanged.

Ownership: coordinator owns this brief, final synthesis and review record. Data researcher owns its returned handoff text; strategy researcher owns its returned handoff text; a fresh Hermes reviewer will inspect both after they exist. Workers receive curated evidence and web tools only; coordinator saves results. Raw prompts/events stay under ignored `outputs/hermes-edge-20261009-r1/`.

Acceptance: two bounded sourced reports, one concrete proposed primary experiment, a separate review, preserved local evidence, and honest model/runtime status. No accounts, orders, paid data purchases or public messages.

Status: completed. Both researchers and the subsequent fresh reviewer returned final text with exit 0. Researchers were configured for 12 tool iterations / 480 seconds; reviewer for 8 / 360 seconds. Actual reported runtimes were 276.497, 313.683 and 323.139 seconds. No retries or persistent background workers were started. Existing repo tests were not repeated for research-only additions.

Runtime session IDs (final result events and completed shell processes verified):

- data-edge: `20261009_181607_21379d`, initialized model `gpt-6-astra`.
- strategy-design: `20261009_181607_6c19ce`, initialized model `gpt-6-astra`.
- independent-review: `20261009_182206_eee9a6`, initialized model `gpt-6-astra`.

## Outcome

The reviewer returned CHANGES NEEDED on the candidate designs. The coordinator reconciled the population, target, feature groups, trial budget and missing-label/inference rules in `research/experiments/e01-hermes-proposal.json`; it remains proposed, unvalidated and not preregistered. The integrated version has not had a second independent review. Required future evidence is explicit, rather than being marked passed.

The coordinator verified key official sources, recorded retrieval limitations, and reconciled the 126 defined / 112 settled / 113 replay identities. One replay runner was removed after its label; 13 other removed identities did not enter those snapshots. This is an identity audit, not a new predictive evaluation.

See [synthesis](../../reports/hermes-edge-2026-10-09.md), [source checks](source-checks.md), [runtime and hashes](runtime.json), and [review](handoffs/independent-review.md). The review-input hash of this brief records its original pending state; this completion update intentionally changes that hash. Worker handoffs remain verbatim and retain their original hashes.

Verification: seven research/source/batch JSON inputs parsed; 39 source IDs and URLs were unique; all three completed-session records, returned-report hashes and prompt hashes matched. Five review inputs were unchanged (the completion brief intentionally changed). Proposal status/trial arithmetic and 34 local Markdown targets checked. The targeted denominator audit passed. [Verification record](verification.json). No application tests or predictive fits were run in this round.
