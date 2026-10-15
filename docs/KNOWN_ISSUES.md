# Known Issues

Gaps found while walking through the pipeline step by step, with what we decided to do about each.
Severity: **High** = can cause wrong data or a privacy breach · **Medium** = something important goes
unnoticed or unhandled · **Low / cosmetic** = noise or nice-to-have.

| # | Issue | Found when | Severity | Status |
|---|---|---|---|---|
| 1 | A file dropped directly into `landing/` (not a source folder) was silently ignored | Delivering the first file by hand | Medium | **Fixed**: quarantined as `F001` with an event |
| 2 | A back-fill after adopting a column would be labelled `SOURCE_CORRECTION` (vendor) instead of `DERIVATION_CHANGE` (us) | Designing the adopt workflow | Medium | Open: fix together with #5 before the first adoption |
| 3 | Pipeline events do not open a GitHub issue automatically | Designing CI/CD | Low | Production: a small Lambda on the event bus |
| 4 | Batch alerts `B002` volume anomaly, `B003` reject-rate spike, `B004` held too long are designed but not built; a run with many rejected rows still exits 0 | End-to-end run | Medium | Open: only `B001` (distribution shift) is built |
| 5 | Adding a canonical field would change every record's vendor fingerprint, so a replay would create false "corrections" | Designing the adopt workflow | High (on first adoption) | Open: must be fixed before any field is adopted |
| 6 | A file quarantined for a forbidden column still holds that data on disk; there is no audited purge | Forbid walkthrough | High (privacy) | Open: needs `quarantine purge` with reason, actor and audit record, approved by security |
| 7 | A missing or incomplete config folder produced a raw error or a misleading "no contract" message | Walkthrough setup | Medium | **Fixed**: clear message, exit code 3 |
| 8 | Drift tracking is not back-filled for files ingested before tracking existed | Ignore walkthrough | Low | Open: derive from `meta.ingested_files.columns` on first run |
| 9 | A quarantine is announced twice (log line and event) | Forbid walkthrough | Cosmetic | Open |
| 10 | A new contract version written without the old one's `ignored_columns` / `forbidden_columns` silently dropped those decisions | Rename walkthrough | High (privacy) | **Fixed**: decisions carry across all versions; mapping a forbidden column is a configuration error |
| 11 | `pipeline replay` has no dry-run plan ("N records in M files would change") | Designing production back-fills | Medium | Open: needed before back-fills can be approved by a person |
| 12 | Quarantined files are not re-checked and released automatically after a contract fix is deployed | Designing production back-fills | Low | Production: automate after deploy (never for `F007`) |
| 13 | A removed *optional* column (e.g. `result_status`) is not detected; corrections and retractions would become invisible | Optional-column walkthrough | Medium | Open: alert when a previously present column disappears |
| 14 | When a held subject (`H001`) is resolved later, its samples do not create sample links, so proteomics on those samples stays held | Milestone 1 review | Medium | Open |
| 15 | Files read back from bronze (replay, DLQ replay) gained a fake `source` column: DuckDB treated the `source=<name>` folder as a partition | Building the demo script | Medium | **Fixed**: partition detection disabled on reads; regression test added |

## How to read this

- **Fixed** items have a test that reproduces the original problem.
- **Open** items are tracked, not forgotten; each has a planned fix.
- **Production** items belong to the deployment around the pipeline (event bus, automation), not to the
  pipeline code itself.
