# Talk track

A rough flow for walking through the project: what it does, a live demo, then how it would scale. Order:
assumptions → workflow → bottleneck → reasoning.

## Setup

Three terminals, all in the repository root with the virtual environment active:

```bash
cd ~/workspace/multi-omics
source .venv/bin/activate
```

| Terminal | Command | Role |
|---|---|---|
| 1 — demo | `python demo/run_demo.py --pause` | Runs each step, checks it, waits for Enter |
| 2 — database | `python demo/inspect_demo.py` | Shows the tables behind each step (prints the SQL first) |
| 3 — dashboard | `streamlit run app/dashboard.py -- --data-dir data/demo` | http://localhost:8501 |

Rhythm for every step:

1. Terminal 1: press **Enter**. Wait for the step's ✔ checks and the `[Enter for the next step]` prompt.
2. Terminal 2: press **Enter** (shows the same step number). Type a number to jump to a step.
3. Browser: **reload** if the step changed data.

Only query or reload while terminal 1 is paused. DuckDB allows one writer or many readers, not both; a
query during a running step gets a lock message (terminal 2) or an error (dashboard). Retry once paused.

Step 1 deletes `data/demo`, so the dashboard shows "No warehouse yet" until step 2 finishes.

## Flow

| Part | What | Show |
|---|---|---|
| 1 | The problem and assumptions | README |
| 2 | Architecture | [DESIGN.md §7](DESIGN.md#7-architecture-and-design) local diagram |
| 3 | Live demo | The three terminals |
| 4 | What's still open | [KNOWN_ISSUES.md](KNOWN_ISSUES.md) |
| 5 | Scaling | [DESIGN.md §10](DESIGN.md#10-scaling-how-the-design-evolves) AWS diagram and benchmark |

**The problem.** Moving rows is the easy part. The hard part is knowing whose measurement it is, which
version is true, and where it came from. The prototype proves those; the AWS design builds around them.
Assumptions: files arrive in batches; vendors change formats without notice; identity evidence can arrive
late; nothing is edited in place.

**Architecture.** Landing → bronze (immutable, named by SHA-256) → silver (every version plus lineage) →
gold (current view). Contracts are YAML per vendor and version. Rules compile to SQL. One transaction per
file. Bad files go to quarantine (F-codes), bad rows to the DLQ (R-codes).

### Demo steps

Spend a bit longer on the **bold** steps; the others need a sentence each.

| Step | What happens | Talking point | Terminal 2 / dashboard |
|---|---|---|---|
| 1 | Empty folders, private config copy | Everything from nothing. | — |
| **2** | EHR, Lab B, Olink delivered; Olink arrives before Lab A | Proteomics has only sample IDs, so it's held (H002) rather than guessed. | Olink rows `held`; gold = 4 |
| **3** | Lab A links S100 → P001 | Held rows are re-checked every run: v1 held → v2 RESOLVED, dated by the blood draw. | Olink v1/v2; reload: person G-0001 has three sources |
| **4** | `pipeline lineage` on Lab B P001 | 3200 ug/L × 0.001 = 3.2 mg/L, from lab_b_sample.csv row 1, raw file in bronze. | Lineage columns; dashboard "Where did a value come from?" |
| **5** | Same Lab A file resent | The fingerprint marks it a duplicate; gold unchanged. | Run shows dup = 1 |
| **6** | Correction (C) and retraction (W) | Same natural key, so same record. A new version; nothing overwritten; old files can't undo it. | Lab A S100 2.3; S101 gone from gold |
| **7** | Unknown unit `mg/dl` → DLQ → alias fix → replay | Units are never guessed. The fix is config, then replay. | DLQ OPEN → RESOLVED |
| 8 | Lab A adds a column | Data keeps flowing, someone is told, a person decides. | drift OPEN → IGNORED |
| 9 | Patient names arrive | Forbidden column: whole file quarantined, high severity. | F007 |
| **10** | Lab B renames columns | Breaking change: quarantine, contract v2 maps new names to the same canonical fields, check-file, release. | F004 → `lab_b_v2` |
| 11 | Values in mg/L labelled ug/L | The file is valid; only the distribution gives it away. | B001 |
| 12 | Crash mid-write (pytest) | Covered by a test: nothing half-written, rerun recovers. | No change to the demo database |
| **13** | Delete the database; replay bronze | Data rebuilds identically from raw files. Run history doesn't, which is why it lives in Postgres in production. Open Operations before Enter, reload after. | One replay run; person view unchanged |
| **14** | 200 synthetic people vs an answer key | Real data has no answer key, so the generator writes one. Every value and every rejected row matches. Both sides come from the same author, so this checks intent, not truth. | Optional: `streamlit run app/dashboard.py --server.port 8502 -- --data-dir data/demo-scale` |
| 15 | Runs and events | Shows one run because of step 13 — same point as step 13. | — |

Every topic in the brief is a step with a check that fails the run if it's wrong, and the demo doubles as
the acceptance test in CI.

### What's still open

Fourteen issues tracked, four fixed. Two worth mentioning:
- **#5:** adopting a new field would change every record's fingerprint and create false corrections.
- **#11:** replay has no dry-run plan, which a person needs before approving a production back-fill.

## Scaling

Open [DESIGN.md §10](DESIGN.md#10-scaling-how-the-design-evolves): the AWS diagram and the
bottleneck → AWS answer table.

**Measured.** 1M rows in 32 s on a laptop, ground truth matched. Slowest: re-resolving held rows (~25%),
then write and gold. Warehouse ~12× bronze.

**Volume — what breaks first:**
1. One writer → parallel workers per source (SQS FIFO group = source), Iceberg commits.
2. Full gold rebuild → incremental `MERGE` of touched records; full rebuild stays as the repair tool.
3. Re-checking every held row → resolve when the sample link arrives; daily sweep as a safety net.
4. Write cost grows faster than data → bucket by `record_id`. The cause is a hypothesis (primary-key
   index) that needs testing first.
5. Large Olink files → split into chunks with a manifest.
6. Back-fills → open choice: the diagram shows EMR/Spark; the alternative is the same container fanned
   out by partition (one engine, no risk of two rule implementations drifting). Spark only if one
   partition doesn't fit in a container.

**Number of sources — mostly a people problem:** onboarding is config only (contract + Terraform module
per source); every source has an owner who gets its alerts; failures stay inside one file and one source;
freshness checks per source; a new kind of data gets its own model, sharing identity, lineage and contracts.

**Rollout:** (1) S3 landing + one Fargate worker running today's code + RDS metadata, one vendor;
(2) Iceberg, per-source parallelism, incremental gold; (3) fan-out back-fills, read model and API,
automatic release after contract fixes.

**Open questions:** the cause of the write-cost growth; how much Parquet reduces storage amplification;
real AWS throughput (the laptop numbers don't extrapolate linearly).
