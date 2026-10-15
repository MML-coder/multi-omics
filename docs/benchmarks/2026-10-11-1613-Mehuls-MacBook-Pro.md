# Benchmark 2026-10-11 16:13

Machine: Darwin 25.3.0 (arm64), 8 CPUs, 8.0 GB RAM · Python 3.12.15 · DuckDB 1.5.6 · commit `90f2131` (before the reresolve_held fix)

Data: generated, 50 proteomics assays per sample, 2 days (+ late Lab A, corrections, duplicate), 1% row errors. Pipeline run day by day.

| Rows delivered | Pipeline (s) | Rows/s | read (s) | map_validate (s) | normalize_resolve (s) | write (s) | reresolve_held (s) | gold (s) | Peak mem (MB) | Warehouse (MB) | Bronze (MB) | Accounting | Ground truth |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 9,993 | 1.27 | 7,855 | 0.06 | 0.15 | 0.12 | 0.11 | 0.14 | 0.08 | 299 | 20.5 | 0.5 | OK | match |
| 99,985 | 4.96 | 20,163 | 0.3 | 0.45 | 0.54 | 0.33 | 1.44 | 0.67 | 559 | 71.0 | 5.2 | OK | match |
