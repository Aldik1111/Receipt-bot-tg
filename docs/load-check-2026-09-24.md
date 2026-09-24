# Local concurrent SQLite/API check — 2026-09-24

Command: `python scripts/load_probe.py`

Environment: local Windows / Python 3.12, temporary SQLite database, loopback
aiohttp server, fake Telegram authentication. No production data or live APIs.

Workload: 10,000 seed transactions; concurrency 8; 80 HTTP requests (summary,
categories, transaction listing and creation); 20 direct storage writes to
simulate the bot; one real 100-row import; one concurrent SQLite backup.

| Measurement | Result |
| --- | --- |
| Workload elapsed, excluding setup | 1.780 s |
| HTTP p50 / p95 / maximum | 139.00 / 291.27 / 353.79 ms |
| Errors | 0 |
| Expected / actual final transactions | 10,140 / 10,140 |
| Expected / actual final amount | 1,042,000 / 1,042,000 tiyn |
| Live database integrity / foreign-key violations | ok / 0 |
| Backup integrity / foreign-key violations | ok / 0 |

The backup captured the initial 10,000 rows and 1,000,000 tiyn before the
concurrent writes committed; it is a valid earlier snapshot, not the final state.
Reported request latencies exclude time queued at the probe's semaphore.
This bounded local check does not establish production capacity or measure
Telegram polling, Gemini latency, multiple server processes or disk failure.

Separate regression tests verify that slow DB access leaves the API event loop
responsive, HTTP database connections run on worker threads, concurrent first
logins create exactly one complete user/catalog/book, and permission rejection
during transaction writes returns HTTP 403. Full suite: 182 tests passed.
