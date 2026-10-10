# Independent deployment and backup review

Verdict: **READY** within the inspected static code, configuration and operating-documentation scope. No material findings remain in that scope. This verdict does not establish live service health, remote backup availability or successful production recovery.

Reviewer: independent native agent `/root/betangel_strategy_evidence`; not the author of the reviewed deployment/backup artifacts.

Recorded at: 2026-10-10T08:42:10.483009+00:00. This record saves the already completed review; no additional review, tests or code changes were performed while recording it.

## Scope and findings

Read `scripts/backup_metadata.py`, its 14 focused tests, `Dockerfile`, `.dockerignore`, `deploy/fly.toml` and `docs/collector-deployment.md`. Also inspected the collector integration showing the same exclusive `.snapshot.lock` held around the complete collection tick. Checked the deployment document's three local Markdown targets; all resolved.

- SQLite online backup handles committed WAL data. Both databases are copied under the cooperative snapshot lock; correctness depends on all writers honoring that lock.
- Restore rehearsal checks the separately recorded archive hash, permits only bounded regular-file members with safe names, rejects traversal/links/duplicates, and verifies each database's hash, size, schema version and SQLite integrity. It uses a fresh destination and does not replace live state.
- Remote backup success requires a conditional immutable-key PUT followed by a complete GET byte-count and SHA-256 comparison. A successful PUT alone is not reported as verification; retries can verify an existing matching object without overwriting it.
- The Docker build context is restricted to intended application/configuration/backup files. Fly configuration defines an outbound worker with persistent storage and no HTTP service. The document correctly requires explicit single-Machine control and explains the nonredundant storage model.
- Documentation distinguishes procedures from actual provisioning evidence, manual backups from automatic schedules, a configuration hash from archived configuration contents, and restore rehearsal from production recovery. It requires both databases to be restored together with matching configuration/code and subsequent receipt checks.

A documentation precision point raised during review was resolved: the 60-second deadline is checked during SQLite-copy progress. Integrity checks, hashing, compression and upload do not share one enforced overall 60-second deadline. The final inspected document states this limitation explicitly.

## Verification limits

This was read-only review. No application tests, backup/restore commands, builds, deployment commands, authenticated calls or other cloud operations were executed by this reviewer for this review. The coordinator's reported full-suite and local restore results are separate evidence. External-link availability, current provider behavior, running Machine state, uploaded-object existence, future availability, costs and complete production recovery were not independently verified here.

## Exact inspected SHA-256

| Artifact | SHA-256 |
| --- | --- |
| `scripts/backup_metadata.py` | `d01827233c3a4055a2dbfbff921f13a9b3af1431f29478bed147ba96d1e76e8c` |
| `tests/test_backup_metadata.py` | `87487fd3367d6eb1550271a1542e29b2714be4cdceffe49084c0cafdcd40d571` |
| `Dockerfile` | `a4d28fc5163b99b67841fa8d1082e7185729b3daffa95afad6c82f1d59bb8d52` |
| `.dockerignore` | `365bd989c757ed4b104e02cd02930b32cc6597d625193f475ac57e41c4c64729` |
| `deploy/fly.toml` | `b3d249a117d41522529e835dfaab1c74b344362e2e1f34ab0bbc99f81bf1019a` |
| `docs/collector-deployment.md` | `a1c64f474f5d0b368e9d54b040e397dd0a3c54be4d469ff32e636e927238459b` |
