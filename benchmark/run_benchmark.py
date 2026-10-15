#!/usr/bin/env python3
"""Volume benchmark: which pipeline stage grows first (DESIGN.md §10.5).

For each target size, generates synthetic deliveries (proteomics dominates, as in reality), runs the
pipeline day by day like production, and reports time per stage, throughput, peak memory and storage.
Each size runs in its own process so peak memory is measured cleanly.

    python benchmark/run_benchmark.py                          # default ladder: 10k 100k 1M
    python benchmark/run_benchmark.py --sizes 1000000 10000000 # choose sizes (rows)
    python benchmark/run_benchmark.py --verify-up-to 1000000   # check ground truth up to this size

Results are printed and written to docs/benchmarks/<date>-<host>.md.
"""

import argparse
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "data" / "benchmark"
PROTEINS = 50                 # assays per proteomics sample
ROWS_PER_SUBJECT = PROTEINS + 4   # + 2 vitals, 1 Lab A, 1 Lab B
STAGES = ("read", "map_validate", "normalize_resolve", "write", "reresolve_held", "gold")


def _peak_memory_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024  # bytes on macOS, KB on Linux


def _folder_mb(path: Path) -> float:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / (1024 * 1024)


def run_single(target_rows: int, verify: bool) -> dict:
    """One size, in this process. Returns measurements as a dict."""
    import duckdb

    from omics_pipeline.ingest import run
    from omics_pipeline.layout import Layout, create_layout
    from omics_pipeline.synth import SynthSpec, deliver, delivery_days, generate, verify as verify_truth

    work = WORK / f"rows_{target_rows}"
    shutil.rmtree(work, ignore_errors=True)
    subjects = max(1, target_rows // ROWS_PER_SUBJECT)
    spec = SynthSpec(subjects=subjects, proteins=PROTEINS, days=2, error_rate=0.01,
                     correction_rate=0.01, retraction_rate=0.005, seed=11)

    started = time.perf_counter()
    manifest = generate(spec, work / "synthetic", ROOT / "config")
    generate_s = time.perf_counter() - started

    layout = Layout(work / "data")
    create_layout(layout)
    config = work / "synthetic" / "config"
    summaries = []
    started = time.perf_counter()
    for day in delivery_days(work / "synthetic"):
        deliver(day, layout.zone("landing"))
        summaries.append(run(layout, config))
    pipeline_s = time.perf_counter() - started

    con = duckdb.connect(str(layout.warehouse), read_only=True)
    stage_ms = dict(con.execute("SELECT stage, sum(duration_ms) FROM meta.run_stage_metrics GROUP BY stage").fetchall())
    silver_rows = con.execute("SELECT count(*) FROM silver.measurement").fetchone()[0]
    gold_rows = con.execute("SELECT count(*) FROM gold.measurement_current").fetchone()[0]
    con.close()

    result = {
        "target_rows": target_rows,
        "rows_delivered": manifest["rows_delivered"],
        "subjects": subjects,
        "runs": len(summaries),
        "generate_s": round(generate_s, 2),
        "pipeline_s": round(pipeline_s, 2),
        "rows_per_s": round(manifest["rows_delivered"] / pipeline_s) if pipeline_s else None,
        "stage_s": {stage: round(stage_ms.get(stage, 0.0) / 1000, 2) for stage in STAGES},
        "peak_memory_mb": round(_peak_memory_mb()),
        "warehouse_mb": round(layout.warehouse.stat().st_size / (1024 * 1024), 1),
        "bronze_mb": round(_folder_mb(layout.zone("bronze")), 1),
        "silver_rows": silver_rows,
        "gold_rows": gold_rows,
        "accounting_ok": all(s.balanced for s in summaries),
        "verified": None,
    }
    if verify:
        result["verified"] = verify_truth(layout.warehouse, work / "synthetic")["ok"]
    shutil.rmtree(work / "synthetic" / "deliveries", ignore_errors=True)  # keep the warehouse for inspection
    return result


def _machine() -> dict:
    memory_gb = None
    try:
        memory_gb = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3, 1)
    except (ValueError, OSError, AttributeError):
        pass
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "src", "benchmark"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        commit += "+uncommitted"  # a report must never claim a commit it did not run
    import duckdb

    return {
        "host": platform.node().split(".")[0],
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "cpus": os.cpu_count(),
        "memory_gb": memory_gb,
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "commit": commit or "unknown",
    }


def _report(results: list[dict], machine: dict) -> str:
    lines = [
        f"# Benchmark {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"Machine: {machine['platform']}, {machine['cpus']} CPUs, {machine['memory_gb']} GB RAM · "
        f"Python {machine['python']} · DuckDB {machine['duckdb']} · commit `{machine['commit']}`",
        "",
        f"Data: generated, {PROTEINS} proteomics assays per sample, 2 days (+ late Lab A, corrections, duplicate), "
        "1% row errors. Pipeline run day by day.",
        "",
        "| Rows delivered | Pipeline (s) | Rows/s | " + " | ".join(f"{s} (s)" for s in STAGES)
        + " | Peak mem (MB) | Warehouse (MB) | Bronze (MB) | Accounting | Ground truth |",
        "|" + "---|" * (8 + len(STAGES)),
    ]
    for r in results:
        stages = " | ".join(str(r["stage_s"][s]) for s in STAGES)
        truth = {True: "match", False: "MISMATCH", None: "not checked"}[r["verified"]]
        lines.append(
            f"| {r['rows_delivered']:,} | {r['pipeline_s']} | {r['rows_per_s']:,} | {stages} | {r['peak_memory_mb']:,} | "
            f"{r['warehouse_mb']} | {r['bronze_mb']} | {'OK' if r['accounting_ok'] else 'VIOLATED'} | {truth} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=int, nargs="+", default=[10_000, 100_000, 1_000_000], help="target rows")
    parser.add_argument("--verify-up-to", type=int, default=1_000_000, help="check ground truth up to this size")
    parser.add_argument("--single", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--verify", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-report", action="store_true", help="print only; do not write docs/benchmarks/")
    args = parser.parse_args()

    if args.single:
        print(json.dumps(run_single(args.single, args.verify)))
        return 0

    results = []
    for size in args.sizes:
        print(f"running {size:,} rows ...", flush=True)
        cmd = [sys.executable, __file__, "--single", str(size)] + (["--verify"] if size <= args.verify_up_to else [])
        done = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if done.returncode != 0:
            print(done.stderr)
            return 1
        result = json.loads(done.stdout.strip().splitlines()[-1])
        results.append(result)
        print(f"  {result['rows_delivered']:,} rows in {result['pipeline_s']} s "
              f"({result['rows_per_s']:,} rows/s), peak {result['peak_memory_mb']:,} MB, stages {result['stage_s']}")

    machine = _machine()
    report = _report(results, machine)
    print("\n" + report)
    if not args.no_report:
        out = ROOT / "docs" / "benchmarks" / f"{datetime.now():%Y-%m-%d-%H%M}-{machine['host']}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report)
        print(f"written to {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
