# Benchmark 2026-10-11 16:51

Machine: Darwin 25.3.0 (arm64), 8 CPUs, 8.0 GB RAM · Python 3.12.15 · DuckDB 1.5.6 · commit `90f2131` + uncommitted reresolve_held fix (committed next)

Data: generated, 50 proteomics assays per sample, 2 days (+ late Lab A, corrections, duplicate), 1% row errors. Pipeline run day by day.

| Rows delivered | Pipeline (s) | Rows/s | read (s) | map_validate (s) | normalize_resolve (s) | write (s) | reresolve_held (s) | gold (s) | Peak mem (MB) | Warehouse (MB) | Bronze (MB) | Accounting | Ground truth |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 99,985 | 6.02 | 16,599 | 0.38 | 0.69 | 0.64 | 0.41 | 1.63 | 0.81 | 457 | 71.0 | 5.2 | OK | match |
