# Direct news collection and deployment

User authorized setting up the previously proposed small collection infra and a direct-source pipeline, with research into NewsAPI sources and publication behavior. Collection only; no betting-account or order integration.

Coordinator owns worker/scheduler, deployment files, shared docs/config, integration and publication. Strategy worker owns feed parsing/HTTP adapter and its tests. Provider worker owns NewsAPI/source research and candidate registry. One writer per artifact.

Completion: usable bounded RSS/Atom collection into existing metadata receipts, explicit source policy and clocks, restart-safe scheduling, deployment-ready outbound-only Fly worker, actual bounded public-source probe, proportionate tests, separate review, private GitHub publication. Cloud deployment depends on available Fly authentication; no token currently available. AWS session also expired.

Status: complete. Candidate implementation published in ff9e56f, Fly Dockerfile path corrected in fd8317e. One London worker collected 21 metadata versions from two configured feeds, retained both databases/cadence through a controlled restart, and uploaded a private Tigris backup verified by byte readback and fresh-directory restore checks. 142 tests passed; two separate scoped reviews READY. Deployment evidence and limits are in reports/direct-collection-2026-10-10.json. Automatic object backups, complete racing news, entity linking and semantic inference remain future scope.
