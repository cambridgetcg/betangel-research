# Direct news collection and deployment

User authorized setting up the previously proposed small collection infra and a direct-source pipeline, with research into NewsAPI sources and publication behavior. Collection only; no betting-account or order integration.

Coordinator owns worker/scheduler, deployment files, shared docs/config, integration and publication. Strategy worker owns feed parsing/HTTP adapter and its tests. Provider worker owns NewsAPI/source research and candidate registry. One writer per artifact.

Completion: usable bounded RSS/Atom collection into existing metadata receipts, explicit source policy and clocks, restart-safe scheduling, deployment-ready outbound-only Fly worker, actual bounded public-source probe, proportionate tests, separate review, private GitHub publication. Cloud deployment depends on available Fly authentication; no token currently available. AWS session also expired.

Status: implementation/research in progress. No cloud resource created as of kickoff.
