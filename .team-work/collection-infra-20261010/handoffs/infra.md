# Infrastructure research handoff

Date: 2026-10-10. Author: `/root/vps_research_costs`. Scope: public official infrastructure research and the two owned artifacts below. No accounts, subscriptions, provisioning, deployment or live source connection; no commits.

- `docs/infra-options.md`: recommendation, comparisons and directly linked sources; 1,024 whitespace-delimited words.
- `research/infra-options.json`: workload, region/currency/exclusion assumptions, source/access dates, quoted/derived rates, scenario components and null unknowns.

Recommendation: Mac + SQLite/outbox first; one continuous Linux collector for continuity. Fly + volume + object archive and Lightsail + S3 are alternatives. Add Supabase for a concrete managed-Postgres/concurrent-writer need; do not require AWS + Fly + Supabase together. Supabase Storage can hold the raw archive within that two-provider option, but its DB backup does not back up object contents. Windows Bet Angel is separate.

Illustrative 30-day workload: one worker, 86,400 polls, 2GB metadata DB, 10GB local volume, 50GB average archive, 10GB billable snapshot versions, 20GB egress. Known resource subtotals: Fly/volume/Tigris **USD11.397875**; Fly/Supabase **USD35.297875**; Lightsail 2GB/snapshots **USD12.50** before unresolved AWS S3/secrets/logs. Complete invoice totals remain null. Tax, FX, data/news/Windows/model costs excluded. Promotional/free-tier allowances ignored; normal purchased Supabase plan inclusions retained.

Notable source detail: Fly's 2026-10-01 pricing update changes RAM pricing. London 1 shared vCPU/1GB derived cost is `(2.194128 + .75*6.003072)*1.134615385 = USD7.59787477` per 720h month. These values come from the official public calculator HTML and its multiplication logic; not a checkout quote. Old cached 1GB estimates should not be reused. Tigris zero-egress does not erase Fly-side outbound transfer.

Operational distinctions: proxy autostart is incoming-request driven; a continuous outbound worker needs explicit ongoing runtime. Ordinary Lambda is bounded at 900 seconds (new Managed Instances asynchronous path 5,400 seconds); Supabase Edge is a bounded Deno runtime, not a Python daemon. Fly volumes are single-host and not automatically replicated. Monitor collection gaps separately from uptime; no latency advantage was measured.

Checks actually completed: JSON parsed; unique rate IDs; all rate/architecture source references resolved; every component and subtotal arithmetic recomputed; poll count and GB-second derivation checked; every external Markdown source URL matched the JSON source registry. No code tests, cloud benchmarks, failover or restore tests were run for this documentation-only task. Existing/concurrent files were not changed by this author.

SHA-256 at handoff:

```text
e775ffc0f3834b8652c8374828ca42482b4787d6ebbec92e83d7369e445b5861  docs/infra-options.md
1f12c4bdcfb3cfd992648ed74b296372ad1bfd0c199ae6a44e5e04dc69a6f9be  research/infra-options.json
```

Reviewer focus: distinguish known-line subtotals from full costs, read-only research from future deployment, provider allowances from promotional credits, and collection coverage from measured source latency. London AWS usage rates intentionally remain null where region-specific evidence was not confirmed.
