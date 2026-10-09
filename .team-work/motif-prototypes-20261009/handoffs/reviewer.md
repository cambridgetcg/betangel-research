# Independent motif prototype review

Verdict: **READY for the reviewed fixed-configuration offline engineering prototype.** This is a static implementation/design review, not a profitability, predictive-performance or live-execution approval. Final CLI demonstrations and integration-document result claims remain the coordinator's checks.

Reviewer: native `betangel_strategy_evidence`; not an implementation author for these files.

Reviewed at: 2026-10-09T18:28:35.824959+00:00

## Scope and evidence

Read project AGENTS.md; inspected motif policies, CLI addition, ADV sampler, fixed config, synthetic fixtures, motif and microstructure regression tests, catalogue and strategy document. No application tests, CLI demonstrations or raw-data replay were run by the reviewer. The coordinator separately reported passing tests; those are not represented as reviewer executions.

Performed read-only JSON parsing, source-fidelity/card-count checks, local-link checks and protected-file hashes. All 14 original observation IDs are covered by the catalogue's nine grouped motifs. Source URLs, selected dates and participant paraphrases match the original observation records; all profitability flags remain false. The declared observation-source hash matches. All three local document targets resolved. Four protected inputs match their recorded hashes: E01 proposal, strategy cards, stream.py and evaluation.py.

## Findings resolved during review

1. Malformed start/observation clocks and non-object records/context could crash before a controlled validation result. The final candidate validates those structures, config/fixture objects and invalid-reason lists. Regression cases are present.
2. A previous feature could be newer than the current observed packet. The final candidate rejects reversed feature chronology; the test fixture covers it.
3. P03 initially inherited P01's later expiry despite earlier context expiry, and P02 omitted expiry. The final candidate records effective expiry and tests delayed context expiration.
4. Malformed context selection IDs, prior-market IDs and coverage scopes could crash or be treated as usable provenance. Final guards require suitable strings/null and a nonempty list of named scopes. Unknown selection links and provisional prior-race finishes do not establish P02 attention evidence.
5. Effective expiry initially ignored feature-age and context-age deadlines. Final P01 expiry includes feature age; P03 additionally includes context limits; P02 uses its watch requirements. Policy age deadlines are exclusive. Boundary fixtures are present.

No remaining material blocker found in the inspected final code using the declared v1 configuration. This was not exhaustive arbitrary-input fuzzing.

## Correctness and claim boundaries

The frozen cutoff does not advance with injected processing delay. Future context and hidden outcome fields are not decision inputs. Unknown context makes P03 abstain. P03 reuses P01 and can retain or abstain, without changing its direction. P02 remains one market-level activity watch with null odds direction. Supplied content hashes, timestamps and assessments are evidence fields, not independent authentication or actual semantic-model inference.

The sampler uses level-keyed ADV replacement state and unsigned runner-level cumulative volume. Image resets, suspensions, field changes, volume corrections and missing endpoints invalidate the relevant window. Captured decisions are not vetoed by later removals or outcome-time fields. Unusable known runners and market exclusions remain auditable. Existing E01/v1 code and specifications were preserved.

Documentation/integration note for coordinator: distinguish sampler endpoint MARKET-message age <=10 seconds from policy issuance age <10 seconds. Unchanged per-field quote/volume state may carry older observation timestamps; neither quantity establishes customer receipt latency or complete feed continuity. Keep the engineering traces and future ablations distinct from measured forecasting/financial results. The catalogue is a specification, not evidence those later studies ran.

## Exact reviewed SHA-256 values

- `betangel_research/motifs.py`: `a4f69ce887a0fc761f6e544c798ab81e558dff1d426ecb78ee1a4aa7d8a2b3fc`
- `betangel_research/microstructure.py`: `5a171a5ac17a239253071c5a1173a024b08fc2e373ea64d36ffc1ae85f46d4c5`
- `betangel_research/__main__.py`: `43e96e196495a798816c52050bee3b47c645389f5b6990751e8088a7057fadcc`
- `tests/test_motifs.py`: `438c5cfa3ff10a49e22133644ea3d583420897784e1070cf6e22dea91c1a174c`
- `tests/test_microstructure.py`: `76d41dc808ed16f332365e5e6936541e2fe2307caf2465e75a63cd92513eefcb`
- `research/experiments/motif-prototypes-v1.json`: `73b0890f2512c0f1b3a47687ba94198a9f8cb0c051c8ed0d61e529e6f6b89712`
- `research/fixtures/motif-scenarios.json`: `2d215e42c892758ba0d08572824c18ae486e9e9003c90803338e4dc6a0368035`
- `research/motif-catalog.json`: `ffcfbdf8186b8fbfbe65688eab2eb3430664d6147370b220f00bfbcc522f577f`
- `docs/motif-strategies.md`: `ab50bd13f5f05c4752d01267deb2e04b00dbcd35096283bc6e313c419dcdb469`

## Protected-input results

- `research/experiments/e01-hermes-proposal.json`: MATCH (`c3abe4af0632e09ed2ce1d2d867ca6e7cb993637328cd61e85aa2314f30b0d7b`)
- `research/strategy-cards.json`: MATCH (`e82831a9a6239b7b53859b8cea4cf8be60362a580416dbd82b271218b8873183`)
- `betangel_research/stream.py`: MATCH (`084ce9460cdd3d0981e602f8b2c718b2b5a05c1cfe74a8a5ec5c2c27529773f9`)
- `betangel_research/evaluation.py`: MATCH (`7308d604f486dc6828b028152fb12e88797472b40c310f6a0172e84a53454b8d`)

Reviewer wrote only this handoff. No code/data edits, accounts, orders, external messages or commits.
