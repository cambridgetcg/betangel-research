# Integration review

The source-research worker independently reviewed coordinator collector code and the feed author's adapter. Findings addressed: future-dated quarantined items must not pin conditional validators; oversized Retry-After must not poison pending recovery; generic dc:date is not publication; Atom text titles are not HTML; error backoff must not shorten a long configured interval. Regression checks were added by each owner.

The feed author independently reviewed the backup author's script/tests, Docker/Fly config and deployment documentation. Verdict READY for static scope, with exact hashes in handoffs/deployment-review.md. No claim of live deployment from that review.

Coordinator: 142 tests passed under local Homebrew Python 3.14. Strict Fly config validation passed. A real pre-review source probe returned 20 GOV.UK and 1 Met Office metadata entries; all retained outside Git. Two-database local backup and fresh-directory restore integrity checks passed. Test log, source hashes, actual cloud resources and later live checks are recorded in reports/direct-collection-2026-10-10.json. No CI result or profitability result is implied.

The first build exposed Fly's config-relative Dockerfile path; coordinator corrected `deploy/fly.toml` to `../Dockerfile` in fd8317e. The subsequent real build/deploy passed and used only the allowlisted 136.91kB build context. This path-only correction did not alter Python code/tests; the earlier static review did not establish build success.

Actual deployment: one 1GB worker in lhr, encrypted 10GB mounted volume, no ingress services. Remote code/config hashes match the reviewed local artifacts. Both feeds returned HTTP200, creating 21 versions and two receipts; a controlled machine restart retained these counts and due times, with a new zero-poll heartbeat. Initial Tigris backup was verified by complete GET SHA256 readback; anonymous HEAD returned403. Fresh-directory archive extraction verified both database hashes/integrity on deployed Python3.12.15. This is an initial functional and persistence check, not proof of future uptime, scheduled backup execution or racing-news completeness.
