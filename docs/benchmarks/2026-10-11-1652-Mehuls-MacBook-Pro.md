# Benchmark 2026-10-11 16:52

Machine: Darwin 25.3.0 (arm64), 8 CPUs, 8.0 GB RAM · Python 3.12.15 · DuckDB 1.5.6 · commit `7fedc97`

Data: generated, 50 proteomics assays per sample, 2 days (+ late Lab A, corrections, duplicate), 1% row errors. Pipeline run day by day.

| Rows delivered | Pipeline (s) | Rows/s | read (s) | map_validate (s) | normalize_resolve (s) | write (s) | reresolve_held (s) | gold (s) | Peak mem (MB) | Warehouse (MB) | Bronze (MB) | Accounting | Ground truth |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 99,985 | 5.52 | 18,111 | 0.33 | 0.55 | 0.59 | 0.38 | 1.45 | 0.71 | 473 | 71.3 | 5.2 | OK | match |
