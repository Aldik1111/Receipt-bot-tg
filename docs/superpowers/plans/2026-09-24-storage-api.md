# Storage and API implementation plan

Goal: complete the five follow-up changes authorized in the conversation.

Constraints: preserve API response shapes, book isolation, integer tiyn, existing
data and the `import db` interface. Keep the preceding uncommitted bug fixes.
Work in the current checkout; no deployment or dependency changes.

Architecture: explicit storage dependencies and facade exports; SQL aggregation
for summaries; run synchronous database calls in worker threads, while HTTP body
reading and responses remain in aiohttp's event loop.

- [x] Replace injected storage namespaces with explicit imports. Test modules in
  fresh interpreters before importing the facade and run existing storage tests.
- [x] Implement `storage.reporting.get_summary_totals` and `get_category_totals`.
  Test empty periods, dates, transfers, missing categories and book isolation.
- [x] Make API database access asynchronous using `asyncio.to_thread`; make
  helpers that access the database awaitable. Test loop responsiveness and DB
  thread placement, and rerun the existing HTTP scenarios.
- [x] Update README categorization and quota descriptions from implementation.
- [x] Exercise concurrent authenticated HTTP reads/writes alongside imports and
  SQLite backups on a temporary database. Record timings and integrity results;
  do not extrapolate workstation measurements to production capacity.
- [x] Run the full unittest suite, tracked-artifact guard, diff checks and review.

Review focus: authentication before mutation, unauthorized book access, transfer
exclusion, concurrent write failures, cancellation/shutdown of worker operations.

Validation: 182 unittest tests passed. Concurrent first-user initialization
was additionally fixed with a write transaction and recheck; transaction
POST/PATCH now return 403 when storage rejects revoked write permission.
See docs/load-check-2026-09-24.md for the final local probe.
