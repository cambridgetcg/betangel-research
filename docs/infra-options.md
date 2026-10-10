# Collection infrastructure options — 2026-10-10

**建議：先 Mac + SQLite 驗證 collection；需要持續收集先搬一部 Linux worker。** Container workflow 選 Fly + persistent volume；想同一供應商處理 VM、archive、backup，可選 AWS Lightsail + S3。多個 writer、remote dashboard 或 managed Postgres 真係有需要先加 Supabase。以下係設計建議，未 provision、未連接 live feed，亦未證明任何 latency／預測優勢。

All linked official sources were accessed **2026-10-10**. Machine-readable assumptions, derivations and unresolved prices are in [infra-options.json](../research/infra-options.json).

## Workload and cost boundary

Illustrative month: 30 days / 720 hours, one continuous worker; 10 news sources polled every 5 minutes = **86,400 polls**. Assume 2 seconds and 512MB per separately invoked poller = 86,400 GB-seconds if implemented as functions. This is a sizing example, not measured usage or permission to poll any provider at that frequency.

Storage assumptions: 2GB metadata DB within a 10GB persistent local allocation; 50GB average compressed object archive, 10GB average billable snapshot storage, 10,000 object writes, 100,000 reads, 20GB internet egress, 1GB logs. Archive size is retained average, not daily growth. Retention and snapshot churn can change the bill substantially.

Amounts are **USD**, no FX conversion, tax/VAT, promotional credits, free-tier credits, Windows/Bet Angel licence, market/news subscriptions, model inference, support or labour. Normal purchased-plan inclusions remain included, specifically Supabase's recurring compute allowance. Fly/Tigris free allowances are ignored for conservative gross metered estimates. Unknown service charges remain `null`; subtotals below are **not complete monthly invoices**.

| Setup | Modelled monthly lines | What is still outside that subtotal? |
|---|---:|---|
| Existing Mac + SQLite | $0 new cloud hosting | Electricity, hardware, internet, independent backup; sleep/network gaps |
| Fly London + 10GB volume + Tigris archive | **$11.40** | External log retention/alerting, extra transfer/storage, tax |
| Fly London + volume + Supabase Pro/Storage | **$35.30** | Independent backup of Storage objects, external logs/alerts, overages, tax |
| AWS London Lightsail 2GB + snapshots + S3 | **$12.50 known subtotal** | S3, Secrets Manager, CloudWatch, any excess transfer, tax; total unknown |

Fly calculation: London shared-cpu-1x / 1GB **$7.597875**, volume $1.50, snapshots $0.80, egress $0.40. Tigris adds $1.10; the Supabase option instead adds $25. Fly's **1 October 2026** revision matters: the official page's base 256MB monthly price is 2.194128, extra RAM 6.003072/GB and London multiplier 1.134615385. Thus `(2.194128 + 0.75×6.003072)×1.134615385`, derived from its public calculator, rounds to $7.60. Volumes are $0.15/GB-month, snapshots $0.08, Europe egress $0.02/GB. These are shared CPU resources. [Fly update](https://fly.io/pricing-update/), [billing details](https://docs.fly.io/about/pricing).

Tigris Standard is $0.02/GB-month, $0.005/1,000 Class A and $0.0005/1,000 Class B requests: `50×.02 + 10×.005 + 100×.0005 = $1.10` before allowances. Its egress is free; **Fly's outgoing transfer remains separate**. Global/default object placement is not a London-only residency promise. [Tigris pricing](https://www.tigrisdata.com/pricing/), [Fly extension billing](https://docs.fly.io/about/pricing).

Lightsail's public-IPv4 Linux bundle is $7/month for 1GB or **$12 for 2GB**, with 2 vCPU, 60GB SSD and 3TB transfer on the latter; snapshots cost $0.05/GB-month. We prefer 2GB as engineering headroom, not a benchmark result. London `eu-west-2` is available. S3 London unit prices were not verified in the readable regional pricing table, so no invented all-in total. [Bundles](https://aws.amazon.com/lightsail/pricing/), [regions](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-regions-and-availability-zones-in-amazon-lightsail.html), [S3 pricing](https://aws.amazon.com/s3/pricing/).

## Which service does which job?

**Continuous collector:** keep an outbound stream process on the Mac, a Linux VM, Fly Machine, or ECS/Fargate service. Add reconnect/backoff, durable checkpoints, bounded buffers, idempotent writes, heartbeat and explicit gap records. A cloud server does not grant data rights; source eligibility is a separate gate in [data-provider-options.md](data-provider-options.md).

Fly has London `lhr`. Keep the collector running explicitly: proxy autostart responds to **incoming requests**, so it is not an outbound-stream watchdog. Put SQLite and the outbox on a volume, not the ephemeral root filesystem. Volumes live on one physical host and do not replicate automatically; one Machine is not high availability. [Regions](https://docs.fly.io/reference/regions), [autostop/autostart](https://docs.fly.io/launch/autostop-autostart), [volume limitations](https://docs.fly.io/volumes/overview/).

**Scheduled news polling:** initially use a timer on the same worker. EventBridge Scheduler + ordinary Lambda suits independent bounded jobs; ordinary Lambda has a 900-second limit. The newer Managed Instances asynchronous path allows up to 5,400 seconds, still not an indefinite stream. ECS services maintain a desired task count; Fargate is an alternative when independent container deployment justifies more configuration. London Fargate/Lambda rates were not confirmed here; do not substitute US-region examples. [Lambda limits](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html), [Scheduler pricing](https://aws.amazon.com/eventbridge/pricing/), [ECS services](https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_CreateService.html), [Fargate pricing](https://aws.amazon.com/fargate/pricing/).

**Database and queue:** SQLite + a durable outbox is sufficient for the initial single-writer design. Supabase supplies managed Postgres; Cron uses `pg_cron`, and Queues uses `pgmq`. This can replace a separate queue vendor once a database is already justified. Its Deno/TypeScript Edge runtime is not a persistent Python host: 256MB, 150-second free / 400-second paid wall clock, 2 seconds CPU per request. [Cron](https://supabase.com/docs/guides/cron), [Queues](https://supabase.com/docs/guides/queues), [Edge runtime](https://supabase.com/docs/guides/functions), [limits](https://supabase.com/docs/guides/functions/limits).

Supabase Pro starts at **$25/month**, including the recurring $10 allowance covering one Micro project's compute, 8GB database disk, 100GB Storage and 250GB egress; these cover the modelled storage sizes. Additional projects/compute and excess usage cost more. Select the specific London region, not a broad Europe placement. Database backups run daily with seven-day retention, but **do not back up Storage object contents**. [Pricing](https://supabase.com/pricing), [regions](https://supabase.com/docs/guides/platform/regions), [backup scope](https://supabase.com/docs/guides/platform/backups).

AWS native expansion would use S3 for immutable compressed raw chunks, Postgres only when needed, SQS for independent workers, and EventBridge for scheduling. SQS Standard can redeliver, so deduplicate by stable IDs and acknowledge after durable writes; a queue cannot itself ensure exactly-once application effects. [SQS delivery](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/standard-queues-at-least-once-delivery.html). Avoid adding SQS, Fargate and managed Postgres just for one small collector.

## Operations and boundaries

Use runtime secrets, never Git or logs: Fly encrypts its app secrets and injects them at runtime; Supabase Vault serves SQL-side secrets; AWS offers Secrets Manager. Fly includes Prometheus/Grafana metrics with roughly 15-day retention but no built-in metric alerting. CloudWatch covers AWS logs, metrics and alarms. Long-term redacted logs, alert delivery and their costs must be chosen separately. [Fly secrets](https://docs.fly.io/apps/secrets/), [Vault](https://supabase.com/docs/guides/database/vault), [Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), [Fly metrics](https://docs.fly.io/monitoring/metrics/), [CloudWatch](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/WhatIsCloudWatch.html).

Record last successful receive, publication/receive clock differences, disconnect gaps, queue backlog and disk usage. Preserve raw content hashes plus provenance outside Git; test restoring the DB and raw objects before relying on backups. Collection coverage is not measured true source latency, and a London region alone proves no speed advantage.

Bet Angel's desktop software requires Windows; keep that as a separate machine/licence decision from this Linux collection design. [System requirements](https://www.betangel.com/system-requirements/). No paid deployment, account access, subscription or betting execution was performed for this comparison.
