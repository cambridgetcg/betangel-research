# Deployment and backup handoff — 2026-10-10

**READY for coordinator integration/deployment, scoped to the artifacts below.** This author created no cloud resources, read no credentials, made no authenticated cloud calls, and did not commit. Actual deployment, remote backup and live collection remain coordinator evidence, not inferred from these local checks.

## Artifacts and final SHA-256

| Artifact | SHA-256 |
| --- | --- |
| Dockerfile | `a4d28fc5163b99b67841fa8d1082e7185729b3daffa95afad6c82f1d59bb8d52` |
| .dockerignore | `365bd989c757ed4b104e02cd02930b32cc6597d625193f475ac57e41c4c64729` |
| deploy/fly.toml | `b3d249a117d41522529e835dfaab1c74b344362e2e1f34ab0bbc99f81bf1019a` |
| scripts/backup_metadata.py | `d01827233c3a4055a2dbfbff921f13a9b3af1431f29478bed147ba96d1e76e8c` |
| tests/test_backup_metadata.py | `87487fd3367d6eb1550271a1542e29b2714be4cdceffe49084c0cafdcd40d571` |
| docs/collector-deployment.md | `a1c64f474f5d0b368e9d54b040e397dd0a3c54be4d469ff32e636e927238459b` |

App `betangel-research-collector`, worker group, London `lhr`, shared 1 CPU/1GB, 10GB `collector_data` mounted `/data`, restart always, no services or public ingress. Deploy from repository root with `--config deploy/fly.toml --ha=false --no-public-ips`, then verify exactly one worker. Docker build context is allowlisted. Optional `GIT_REVISION` build argument defaults to `unknown`; full code/image hashes are needed for uncommitted builds.

Backup commands: `create --state-dir DIR --config FILE --out NEW_DIR [--upload]`, `upload --backup-dir DIR`, `verify --backup-dir DIR`, `restore-check --archive FILE --sha256 EXPECTED_SHA256 --out NEW_DIR`. All are explicit/manual. Local functions run without SDK; container pins boto3 plus transitive dependencies for optional upload.

The cooperative `.snapshot.lock` spans all SQLite copies and matches the root worker tick lock. SQLite online backup includes committed WAL data; each database is integrity-checked. The archive contains SQLite files and a manifest with hashes, versions, snapshot times and source-config hash only. It does not copy config contents, separate feed responses or credential files. Complete database contents are not redacted. Any writers bypassing the lock invalidate the cross-database consistency guarantee.

Upload uses a content-addressed key and `IfNoneMatch="*"`; a full GET and SHA comparison are required for `upload.json`. A conditional-write conflict on retry goes through the same GET verification. Failed remote verification leaves no success receipt. Exact successful retries preserve the first receipt. Errors suppress provider response/credential text. Restore extraction rejects unsafe paths, links, duplicates and files outside the declared SQLite/manifest scope, checks hashes and integrity, and never replaces live state.

## Checks and review

- **Author executed:** `python3 -m unittest discover -s tests -p test_backup_metadata.py -v` — **14 passed** on local Python 3.9. Last run after final test assertion adjustment. Covers WAL data, both databases, lock contention, partial-output cleanup, size expansion, hashes/sidecars, unsafe extraction, remote-byte corruption, failed PUT/GET, and immutable retries using a fake S3 client.
- **Author executed:** Python 3.14 `tomllib` parse and field inspection — valid TOML, correct app/region/VM/mount/restart fields, no service definitions. This is not cloud validation.
- **Coordinator reported:** complete 142-test suite passed with Python 3.14; separate local two-database backup and restore rehearsal passed; authenticated Fly config validation passed. Author did not rerun these checks.
- **Independent reviewer `/root/betangel_strategy_evidence`:** read-only code/config/lock integration review found no material blocker. Reviewer requested precise deadline wording; docs now say SQLite-copy progress checks a 60-second deadline, with no overall integrity/hash/compression/upload deadline. Reviewer ran no additional tests or live calls.
- No local Docker executable; image build/install and Python 3.12 runtime are left to coordinator deployment evidence. No remote upload or production recovery verified by this author.

Manual backup only: no cron, lifecycle mutation or automatic deletion. Proposed 14-day prefix retention is documented as unapplied; configure and verify bounded retention before recurring uploads. Each extracted/compressed bundle is capped at 4GiB. Preserve receipt hashes independently and restore the metadata/scheduler pair together with all writers stopped. Do not reuse old WAL/SHM files. Source config and compatible code must be recovered separately from Git.

## Official source evidence

All accessed **2026-10-10**; publication dates not asserted for living documentation.

- https://fly.io/docs/reference/configuration/ — process, VM, mount, restart, termination and build syntax.
- https://fly.io/docs/flyctl/deploy/ — `--ha`, `--no-public-ips`, build arguments and deployment options.
- https://fly.io/docs/launch/scale-count/ — per-process counts and separate volumes on added Machines.
- https://fly.io/docs/tigris/ — private bucket default, app environment secret names, current and legacy endpoints.
- https://www.tigrisdata.com/features/ — S3 compatibility and conditional writes; linked lifecycle documentation failed web retrieval, so no specific lifecycle deployment claimed.
- https://github.com/tigrisdata/terraform-provider-tigris#tigris_bucket_lifecycle — provider-authored lifecycle support and prefix/day expiry limitations; used only for the proposed retention procedure.
- https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup — online SQLite backup API.
- https://pypi.org/project/boto3/ and version-specific official PyPI JSON metadata — pinned `boto3==1.43.111`, `botocore==1.43.111`, `s3transfer==0.19.0`, `jmespath==1.1.0`, `python-dateutil==2.9.0.post0`, `six==1.17.0`, `urllib3==2.8.0`; declared dependency ranges inspected, not installed locally by this author. Container Python 3.12 satisfies the packages' Python requirements; base image is not digest-pinned.
