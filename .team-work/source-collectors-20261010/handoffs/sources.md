# Direct-source and NewsAPI research handoff

Author `/root/vps_research_costs`; checked 2026-10-10. Owned research outputs:

- `docs/newsapi-source-research.md` — 1,043 whitespace words, public facts versus proposed architecture, exact feeds and source holds.
- `research/news-source-candidates.json` — 11 candidates, two enabled recommendations, official source/policy registry and four bounded feed probe records.

Immediate recommendations: GOV.UK equine Atom and Met Office news RSS, each hourly, metadata only. Both exact URLs returned HTTP200 and parsed as expected. GOV.UK's Atom items contain `updated` but not `published`: publication must remain unknown. Met Office has RSS `pubDate`; preserve literal title/link and attribution, skip description/content:encoded/images. These are equine-policy/health and weather context sources, not comprehensive horse-racing news.

Primary policy evidence: [GOV.UK feed/reuse terms](https://www.gov.uk/help/terms-conditions), [OGL v3](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/), [Met Office feed directory](https://weather.metoffice.gov.uk/guides/rss), [Met Office RSS terms](https://www.metoffice.gov.uk/policies/tandc). The latter explicitly permits RSS purposes while requiring unchanged supplied content/links and attribution. Recommended cadence and retention-review interval are engineering choices, not provider guarantees.

NewsAPI findings: Sources endpoint describes only the Top Headlines publisher subset. Everything advertises a much larger corpus with domain filtering; BHA/Racing Post/Sporting Life/ATR inclusion remains unverified. Public terms confirm public-page aggregation, not private crawler/feed/indexer implementation. Their current country parameter documentation lists only `us`, while the UK landing page still gives a `gb` example; runtime unresolved. No key or article payload obtained. The report does not reverse engineer software or build from copied NewsAPI data.

BBC horse RSS returned200/11 items in a single bounded feasibility read, but current feed-use policy was inaccessible; keep held. Guardian, Sporting Life, BHA and Racing Post have source-specific policy issues; ATR terms returned429 and were not retried. Jockey Club press releases offer original fixture statements, but supported feed/reuse permission is unverified. The GOV.UK `horse` alternative returned an irrelevant first match and duplicates the preferred equine channel, so it is not enabled. GDELT was not queried.

Parent config `research/collector-sources.json` inspected after research completion: it correctly enables only the two recommended exact URLs at 3,600 seconds, records policy/licence URLs and attribution, separates Atom updated from publication, preserves Met Office titles/links and describes context-only coverage. This is policy/config inspection, not deployed-runtime verification.

Checks completed: JSON parsed; unique candidate IDs; policy/probe references resolved; enabled recommendation consistency and hourly intervals checked. Feed probe status/count/clock details are recorded in JSON; raw XML/article bodies were not retained by this research author. No credentials, cloud changes, scheduling or subscriptions were performed by this author; parent deployment work is separate.

SHA-256:

```text
053f7b28912c52063199d81d8fe7c7b18229f3dfc0e4a9ba319f9504239a9463  docs/newsapi-source-research.md
90c2d63a9c604f4f8975e10fb859052e6452b52d72c08990cc122608dcecd612  research/news-source-candidates.json
```

Collector/feed implementation review is a separate subsequent handoff and is not implied by these research checks.
