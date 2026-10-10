# Integration review

The source-research worker independently reviewed coordinator collector code and the feed author's adapter. Findings addressed: future-dated quarantined items must not pin conditional validators; oversized Retry-After must not poison pending recovery; generic dc:date is not publication; Atom text titles are not HTML; error backoff must not shorten a long configured interval. Regression checks were added by each owner.

The feed author independently reviewed the backup author's script/tests, Docker/Fly config and deployment documentation. Verdict READY for static scope, with exact hashes in handoffs/deployment-review.md. No claim of live deployment from that review.

Coordinator: 142 tests passed under local Homebrew Python 3.14. Strict Fly config validation passed. A real pre-review source probe returned 20 GOV.UK and 1 Met Office metadata entries; all retained outside Git. Two-database local backup and fresh-directory restore integrity checks passed. Test log, source hashes, actual cloud resources and later live checks are recorded in reports/direct-collection-2026-10-10.json. No CI result or profitability result is implied.

Final runtime deployment/backup evidence will be recorded after candidate publication and cloud checks; this note is not a cloud-health assertion.
