# Benchmark 2026-10-11 16:48

Machine: Darwin 25.3.0 (arm64), 8 CPUs, 8.0 GB RAM · Python 3.12.15 · DuckDB 1.5.6 · commit `90f2131` + uncommitted reresolve_held fix (committed next)

Data: generated, 50 proteomics assays per sample, 2 days (+ late Lab A, corrections, duplicate), 1% row errors. Pipeline run day by day.

| Rows delivered | Pipeline (s) | Rows/s | read (s) | map_validate (s) | normalize_resolve (s) | write (s) | reresolve_held (s) | gold (s) | Peak mem (MB) | Warehouse (MB) | Bronze (MB) | Accounting | Ground truth |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1,000,242 | 32.32 | 30,950 | 0.48 | 3.44 | 4.48 | 5.0 | 7.97 | 4.97 | 1,172 | 630.0 | 52.3 | OK | match |
