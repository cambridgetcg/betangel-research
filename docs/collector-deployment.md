# Collector deployment and recovery

Prepared **2026-10-10**. This is an operating procedure, not evidence that a Machine is running or a remote backup exists. The coordinator records actual provisioning, collection and backup checks in the [collection receipt](../reports/direct-collection-2026-10-10.json). Fly authentication succeeded during setup; AWS infrastructure and Supabase remain deferred.

## One small worker

[fly.toml](../deploy/fly.toml) requests one worker process in London (`lhr`), shared CPU, 1 CPU, 1GB memory, a 10GB `collector_data` volume mounted at `/data`, and restart policy `always`. There is no HTTP service or public listening port. [Fly configuration reference](https://fly.io/docs/reference/configuration/).

Machine count must be controlled separately. Deploy from the repository root with `--ha=false` and check that exactly one worker exists; a second Machine gets separate storage rather than a shared SQLite database. This is a small collection service with downtime during some failures and updates, not a redundant database setup. [Fly deployment options](https://fly.io/docs/flyctl/deploy/), [scaling and volumes](https://fly.io/docs/launch/scale-count/).

One-time provisioning commands, only for resources that do not already exist:

```sh
fly apps create betangel-research-collector --org personal
fly volumes create collector_data --app betangel-research-collector --region lhr --size 10
fly storage create --app betangel-research-collector
```

`fly storage create` uses a private bucket by default and stages app secrets. Its output can contain credentials; do not copy it into Git, handoffs or reports. The backup script uses `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3` and `BUCKET_NAME` from the environment. Both `https://t3.storage.dev` and the older `https://fly.storage.tigris.dev` endpoint are supported. [Official Tigris integration](https://fly.io/docs/tigris/).

After local review/tests, deploy and inspect:

```sh
fly config validate --config deploy/fly.toml --strict
fly deploy . --config deploy/fly.toml --ha=false --no-public-ips
fly scale count worker=1 --app betangel-research-collector
fly status --app betangel-research-collector
fly volumes list --app betangel-research-collector
fly logs --app betangel-research-collector --no-tail
```

[Dockerfile](../Dockerfile) copies only package code, the source registry and backup script. It uses Python 3.12 and a pinned AWS-maintained boto3 dependency set for optional backups; collection itself uses the standard library. The Python base tag is not digest-pinned. An optional `--build-arg GIT_REVISION=REVISION_OR_UNKNOWN` labels the image; this alone does not identify uncommitted source changes. Record the actual image and source hashes with deployment evidence. [boto3 package](https://pypi.org/project/boto3/).

The worker command is:

```sh
python3 -m betangel_research.collector --config /app/research/collector-sources.json --state-dir /data
```

`metadata.sqlite` contains append-only news metadata receipts; `scheduler.sqlite` contains scheduling and recovery state. `.collector.lock` prevents a second worker using the same directory. Every tick holds `.snapshot.lock` through both databases' changes. Hourly feed polling, restart recovery and a configured storage cap belong to the scheduler. The worker stops on the cap instead of deleting evidence. Inspect `--status`, database receipt timestamps, source failures, gaps and disk usage; a running process is not proof of complete or current source coverage.

## Manual backup, optional upload

Run on the Machine, or against a local collector directory. Use a new output directory outside the state directory each time:

```sh
python3 /app/scripts/backup_metadata.py create \
  --state-dir /data --config /app/research/collector-sources.json \
  --out /tmp/metadata-backup-20261010T120000Z
python3 /app/scripts/backup_metadata.py verify \
  --backup-dir /tmp/metadata-backup-20261010T120000Z
python3 /app/scripts/backup_metadata.py upload \
  --backup-dir /tmp/metadata-backup-20261010T120000Z
```

`create --upload` combines the first and last operations. Exit 2 means failure; save successful nonsecret JSON receipts outside the Machine. A busy snapshot lock fails immediately: retry after the active tick. The script takes SQLite online backups of every top-level `*.sqlite`, checks integrity, and records sizes, hashes, schema user versions, UTC snapshot times and **the source configuration hash, not its contents**. Preserve the matching source registry and code separately in Git. No separate feed response files, credentials or article pages are copied; database contents themselves are not redacted. [SQLite backup API](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup).

Cross-database consistency depends on **all writers honoring `.snapshot.lock`**. The script holds it across all copies, then releases it before compression/upload. SQLite-copy progress checks a 60-second deadline; integrity checks, hashing, compression and upload are not covered by a single overall time limit. Total extracted bundle size and compressed upload are each capped at 4GiB. A future dataset exceeding these limits needs another reviewed backup method.

Uploads use an immutable `metadata-backups/SHA256/bundle.tar.gz` key and a conditional write. Success requires downloading the object again and comparing every byte's SHA-256; a successful PUT alone is insufficient. Failed readback leaves no success receipt and an exact retry verifies the existing object. `backup.json` records local creation; `upload.json` records the first successful remote verification. These checks establish an intact copy at that time, not future availability. [Tigris conditional writes](https://www.tigrisdata.com/features/).

No automatic backup schedule or deletion is installed. Before enabling a recurring schedule, choose and verify bounded retention. Example **proposed, not applied** rule: expire only `metadata-backups/` objects after 14 days, using the bucket's lifecycle management. It must leave enough independently verified restore points and account for any separately enabled version/soft-delete retention. [Tigris lifecycle support](https://github.com/tigrisdata/terraform-provider-tigris#tigris_bucket_lifecycle). Periodic manual snapshots also accumulate until deliberately removed.

## Restore rehearsal, then deliberate recovery

Retrieve the private object using the Tigris console or an authenticated S3 client. Retain the separately recorded `upload.json` archive hash; an archive and a hash supplied together by an untrusted party do not authenticate each other. Run:

```sh
python3 scripts/backup_metadata.py restore-check \
  --archive /path/to/bundle.tar.gz --sha256 EXPECTED_ARCHIVE_SHA256 \
  --out /path/to/new-restored-directory
```

This verifies the archive hash, rejects traversal/links/duplicates, checks every database hash and `PRAGMA integrity_check`, and **never replaces live state**. Compare the manifest config hash with the intended source registry; check code/schema compatibility and the restored scheduler's pending receipts. Integrity alone does not establish semantic correctness or restore freshness.

For actual recovery, stop the worker, preserve the current complete state directory, and restore **both databases together into a fresh state directory**, with no old WAL/SHM/journal files. Keep all writers stopped until the entire replacement is complete. Point the sole worker at that directory, restart, inspect pending-receipt recovery and new receipts, and record the lost interval relative to the snapshot. Never copy an old scheduler over a newer metadata database, or vice versa. A full production recovery is only verified after this restart-and-receipt check; local extraction alone is a rehearsal.

Official references accessed **2026-10-10**. Local tests use synthetic SQLite databases and a fake object client; real deployment/upload/recovery evidence belongs in the dated coordinator receipt.
