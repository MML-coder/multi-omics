"""Command-line entry point: `pipeline <command>` (DESIGN.md §7.5, ADR-001)."""

import argparse
import logging
import os
import sys
from pathlib import Path

from omics_pipeline import __version__
from omics_pipeline.config import resolve_config_dir
from omics_pipeline.layout import create_layout, resolve_layout


def _query(args: argparse.Namespace, sql: str, params: list | None = None) -> list[tuple] | None:
    import duckdb

    layout = resolve_layout(args.data_dir)
    if not layout.warehouse.exists():
        print("no runs yet")
        return None
    con = duckdb.connect(str(layout.warehouse), read_only=True)
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def _print_summary(summary) -> int:
    print(
        f"{summary.run_id} ({summary.run_type}): files in={summary.files_in} committed={summary.files_committed} "
        f"quarantined={summary.files_quarantined} duplicate={summary.files_duplicate} failed={summary.files_failed}"
    )
    print(
        f"  rows in={summary.rows_in} ok={summary.rows_ok} (warn={summary.rows_warn}) dlq={summary.rows_dlq} "
        f"held={summary.rows_held} filtered={summary.rows_filtered} | resolved held={summary.rows_resolved} "
        f"| gold={summary.gold_rows} | accounting {'OK' if summary.balanced else 'VIOLATED'}"
    )
    for event in summary.events:
        print(f"  [{event.severity.upper()}] {event.event_type} {event.code} {event.source}/{event.file_name}: {event.detail}")
    if summary.exit_code == 2:
        print("  needs attention: see `pipeline events` and `pipeline quarantine list`")
    return summary.exit_code


def _cmd_init(args: argparse.Namespace) -> int:
    layout = resolve_layout(args.data_dir)
    created = create_layout(layout)
    if created:
        for path in created:
            print(f"created {path}")
    else:
        print(f"layout already present at {layout.root}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from omics_pipeline.ingest import run

    return _print_summary(run(resolve_layout(args.data_dir), resolve_config_dir(args.config_dir), args.source, args.domain))


def _cmd_watch(args: argparse.Namespace) -> int:
    """Run the pipeline whenever files land: the local stand-in for S3 event → queue → worker."""
    import time
    from datetime import datetime

    from omics_pipeline.config import ConfigError
    from omics_pipeline.ingest import ready_files, run

    layout = resolve_layout(args.data_dir)
    config_dir = resolve_config_dir(args.config_dir)
    print(f"watching {layout.zone('landing')} every {args.interval:g}s (Ctrl-C to stop)", flush=True)
    attempted: set | None = None     # files a run left behind; not retried until landing or config changes

    def snapshot(paths: list[Path]) -> set:
        config_changed = max((p.stat().st_mtime for p in config_dir.rglob("*") if p.is_file()), default=0)
        return {(path, path.stat().st_mtime) for path in paths} | {("config", config_changed)}

    try:
        while True:
            files = ready_files(layout, args.settle)
            state = snapshot(files)
            if files and state != attempted:
                names = ", ".join(sorted(path.name for path in files))
                print(f"\n{datetime.now():%H:%M:%S} {names} arrived → pipeline run", flush=True)
                try:
                    _print_summary(run(layout, config_dir))
                except ConfigError as exc:   # keep watching: fixing the config is the next thing a person does
                    print(f"configuration error: {exc}", flush=True)
                left = ready_files(layout, 0)
                attempted = snapshot(left) if left else None
                if left:
                    print(f"  {len(left)} file(s) still in landing; retried when landing or config changes", flush=True)
                sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from omics_pipeline.ingest import replay

    return _print_summary(replay(resolve_layout(args.data_dir), resolve_config_dir(args.config_dir), args.source))


def _cmd_status(args: argparse.Namespace) -> int:
    rows = _query(
        args,
        "SELECT run_id, run_type, status, files_committed, files_quarantined, files_duplicate, rows_in, rows_ok, "
        "rows_dlq, rows_held, rows_resolved FROM meta.pipeline_runs ORDER BY run_id DESC LIMIT ?",
        [args.limit],
    )
    if rows is None:
        return 0
    print(f"{'run_id':<9} {'type':<10} {'status':<11} files(ok/quar/dup)  rows(in/ok/dlq/held)  resolved")
    for r in rows:
        print(f"{r[0]:<9} {r[1]:<10} {r[2]:<11} {f'{r[3]}/{r[4]}/{r[5]}':<19} {f'{r[6]}/{r[7]}/{r[8]}/{r[9]}':<21} {r[10]}")
    return 0


def _cmd_events(args: argparse.Namespace) -> int:
    rows = _query(
        args,
        "SELECT run_id, severity, event_type, code, source, file_name, detail FROM meta.events "
        "ORDER BY raised_at DESC LIMIT ?",
        [args.limit],
    )
    if rows is None:
        return 0
    if not rows:
        print("no events")
    for run_id, severity, event_type, code, source, file_name, detail in rows:
        print(f"{run_id}  [{severity.upper()}] {event_type} {code} {source}/{file_name}: {detail}")
    return 0


def _cmd_drift(args: argparse.Namespace) -> int:
    rows = _query(
        args,
        "SELECT source, column_name, status, files_seen, first_seen_file, last_seen_at, decided_in "
        "FROM meta.schema_drift ORDER BY status = 'OPEN' DESC, source, column_name",
    )
    if rows is None:
        return 0
    if not rows:
        print("no schema drift recorded")
    for source, column, status, files, first_file, last_seen, decided_in in rows:
        decision = f"decided in {decided_in}" if decided_in else "UNDECIDED"
        print(f"{status:<10} {source}.{column:<20} seen in {files} file(s) since {first_file}; last {last_seen:%Y-%m-%d %H:%M}; {decision}")
    return 0


def _cmd_check_file(args: argparse.Namespace) -> int:
    import duckdb

    from omics_pipeline.adapters import read_columns
    from omics_pipeline.config import load_contracts, select_contract
    from omics_pipeline.drift import check_columns

    contracts = load_contracts(resolve_config_dir(args.config_dir))
    if args.source not in contracts:
        print(f"no contract for source '{args.source}' → quarantine F001")
        return 2
    candidates = contracts[args.source]
    columns = read_columns(duckdb.connect(), args.file, candidates[0].format)
    check = check_columns(select_contract(candidates, columns), columns)
    print(f"file columns: {columns}")
    print(f"contract:     {check.contract.name if check.contract else 'none matches'}")
    for label, values in (("undecided", check.open), ("ignored", check.ignored), ("forbidden", check.forbidden)):
        if values:
            print(f"{label + ':':<13} {values}")
    print(f"verdict:      {check.verdict}")
    return 0 if check.verdict == "ingest cleanly" else 2


def _cmd_lineage(args: argparse.Namespace) -> int:
    import json

    import duckdb

    from omics_pipeline.lineage import measurement_lineage

    layout = resolve_layout(args.data_dir)
    if not layout.warehouse.exists():
        print("no runs yet")
        return 1
    con = duckdb.connect(str(layout.warehouse), read_only=True)
    try:
        result = measurement_lineage(con, args.record_id)
    finally:
        con.close()
    if result is None:
        print(f"no record {args.record_id}")
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


def _cmd_dlq_list(args: argparse.Namespace) -> int:
    clauses = ["status = 'OPEN'"] + ["source = ?"] * (args.source is not None)
    rows = _query(
        args,
        "SELECT source, error_code, count(*), list(DISTINCT raw_value) FILTER (WHERE raw_value IS NOT NULL)[1:3] "
        f"FROM meta.dlq WHERE {' AND '.join(clauses)} GROUP BY ALL ORDER BY 1, 2",
        [args.source] if args.source else [],
    )
    if rows is None:
        return 0
    if not rows:
        print("no open DLQ rows")
    for source, code, count, examples in rows:
        print(f"{source:<12} {code}  {count:>4}  e.g. {examples}")
    return 0


def _cmd_dlq_replay(args: argparse.Namespace) -> int:
    from omics_pipeline.ingest import dlq_replay

    return _print_summary(
        dlq_replay(resolve_layout(args.data_dir), resolve_config_dir(args.config_dir), args.source, args.error)
    )


def _cmd_quarantine_list(args: argparse.Namespace) -> int:
    rows = _query(
        args,
        "SELECT left(file_hash, 12), source, original_name, quarantine_code, quarantine_reason "
        "FROM meta.ingested_files WHERE status = 'QUARANTINED' ORDER BY received_at",
    )
    if rows is None:
        return 0
    if not rows:
        print("no quarantined files")
    for file_hash, source, name, code, reason in rows:
        print(f"{file_hash}  {source:<12} {name}  {code}: {reason}")
    return 0


def _cmd_quarantine_release(args: argparse.Namespace) -> int:
    from omics_pipeline.ingest import release_quarantined

    released = release_quarantined(resolve_layout(args.data_dir), args.file_hash)
    for name in released:
        print(f"released {name} to landing; it will be retried on the next `pipeline run`")
    if not released:
        print(f"no quarantined file matches '{args.file_hash}'")
    return 0 if released else 1


def _cmd_generate(args: argparse.Namespace) -> int:
    from omics_pipeline.synth import SynthSpec, generate

    spec = SynthSpec(args.subjects, args.proteins, args.days, args.error_rate, args.correction_rate,
                     args.retraction_rate, args.seed)
    manifest = generate(spec, args.out, resolve_config_dir(args.config_dir))
    print(f"generated {manifest['rows_delivered']} rows over {len(manifest['delivery_days'])} delivery days in {args.out}")
    print(f"  truth: {manifest['expected_gold_rows']} gold rows, {manifest['expected_rejects']} rejected rows")
    print(f"  use --config-dir {args.out / 'config'} (it holds the generated registry)")
    return 0


def _cmd_deliver(args: argparse.Namespace) -> int:
    from omics_pipeline.synth import deliver

    layout = resolve_layout(args.data_dir)
    create_layout(layout)
    for path in deliver(args.source_dir / "deliveries" / f"day={args.day:03d}", layout.zone("landing")):
        print(f"delivered {path.parent.name}/{path.name}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from omics_pipeline.synth import verify

    report = verify(resolve_layout(args.data_dir).warehouse, args.source_dir)
    print(f"gold:    expected {report['gold_expected']}, actual {report['gold_actual']}")
    print(f"rejects: expected {report['rejects_expected']}, actual {report['rejects_actual']}")
    for name in ("missing", "unexpected", "mismatched", "rejects_missing", "rejects_unexpected"):
        if report[name]:
            print(f"  {name}: {report[name]}")
    print("MATCHES GROUND TRUTH" if report["ok"] else "DOES NOT MATCH GROUND TRUTH")
    return 0 if report["ok"] else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from omics_pipeline.api import create_app

    uvicorn.run(create_app(resolve_layout(args.data_dir)), host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="Multi-source measurement pipeline",
        epilog="Exit codes: 0 clean, 1 processing failure, 2 needs attention (quarantine or alert), 3 configuration error.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--data-dir", type=Path, default=None, help="Data directory (default: $PIPELINE_DATA_DIR or ./data)")
    parser.add_argument(
        "--config-dir", type=Path, default=None, help="Config directory (default: $PIPELINE_CONFIG_DIR or ./config)"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Create the data directory layout").set_defaults(handler=_cmd_init)

    run = commands.add_parser("run", help="Process new files in landing/<source>/")
    run.add_argument("--source", help="Only this source")
    run.add_argument("--domain", choices=["operational", "scientific"], help="Only sources in this domain")
    run.set_defaults(handler=_cmd_run)

    watch = commands.add_parser("watch", help="Run the pipeline whenever files land in landing/ (Ctrl-C to stop)")
    watch.add_argument("--interval", type=float, default=2.0, help="Seconds between checks (default 2)")
    watch.add_argument("--settle", type=float, default=1.0,
                       help="Wait until no landing file changed for this many seconds (default 1)")
    watch.set_defaults(handler=_cmd_watch)

    replay = commands.add_parser("replay", help="Reprocess bronze files with current code and configuration")
    replay.add_argument("--source", help="Only this source")
    replay.set_defaults(handler=_cmd_replay)

    status = commands.add_parser("status", help="Show recent runs")
    status.add_argument("--limit", type=int, default=10)
    status.set_defaults(handler=_cmd_status)

    events = commands.add_parser("events", help="Operational events: quarantines, schema drift, alerts, failures")
    events.add_argument("--limit", type=int, default=20)
    events.set_defaults(handler=_cmd_events)

    lineage_cmd = commands.add_parser("lineage", help="Source-to-canonical lineage of one record")
    lineage_cmd.add_argument("record_id")
    lineage_cmd.set_defaults(handler=_cmd_lineage)

    drift_cmd = commands.add_parser("drift", help="Schema drift per source column and its decision status")
    drift_cmd.set_defaults(handler=_cmd_drift)

    check = commands.add_parser("check-file", help="Check a vendor file against the contracts without ingesting it")
    check.add_argument("--source", required=True)
    check.add_argument("file", type=Path)
    check.set_defaults(handler=_cmd_check_file)

    dlq = commands.add_parser("dlq", help="Inspect or replay rejected rows").add_subparsers(dest="dlq_command", required=True)
    dlq_list = dlq.add_parser("list", help="Open DLQ rows grouped by source and error code")
    dlq_list.add_argument("--source")
    dlq_list.set_defaults(handler=_cmd_dlq_list)
    dlq_replay = dlq.add_parser("replay", help="Reprocess files with open DLQ rows after a fix")
    dlq_replay.add_argument("--source")
    dlq_replay.add_argument("--error", help="Only files with open rows of this error code")
    dlq_replay.set_defaults(handler=_cmd_dlq_replay)

    quarantine = commands.add_parser("quarantine", help="Inspect or release quarantined files").add_subparsers(
        dest="quarantine_command", required=True
    )
    quarantine.add_parser("list", help="Quarantined files and reasons").set_defaults(handler=_cmd_quarantine_list)
    release = quarantine.add_parser("release", help="Move a quarantined file back to landing for retry")
    release.add_argument("file_hash", help="File hash or prefix (from `quarantine list`)")
    release.set_defaults(handler=_cmd_quarantine_release)

    gen = commands.add_parser("generate", help="Write seeded synthetic deliveries plus ground truth")
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--subjects", type=int, default=100)
    gen.add_argument("--proteins", type=int, default=20, help="Assays per proteomics sample")
    gen.add_argument("--days", type=int, default=3)
    gen.add_argument("--error-rate", type=float, default=0.05)
    gen.add_argument("--correction-rate", type=float, default=0.05)
    gen.add_argument("--retraction-rate", type=float, default=0.02)
    gen.add_argument("--seed", type=int, default=42)
    gen.set_defaults(handler=_cmd_generate)

    dlv = commands.add_parser("deliver", help="Copy one generated delivery day into landing/")
    dlv.add_argument("--from", dest="source_dir", type=Path, required=True)
    dlv.add_argument("--day", type=int, required=True)
    dlv.set_defaults(handler=_cmd_deliver)

    ver = commands.add_parser("verify", help="Compare gold and the DLQ with generated ground truth")
    ver.add_argument("--from", dest="source_dir", type=Path, required=True)
    ver.set_defaults(handler=_cmd_verify)

    serve = commands.add_parser("serve", help="Start the read-only access API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(handler=_cmd_serve)

    return parser


EXIT_CONFIG_ERROR = 3


def _describe_dirs(args: argparse.Namespace) -> str:
    """Which data and config folders this command uses, and why (flag, environment variable or default)."""
    from omics_pipeline.config import CONFIG_DIR_ENV
    from omics_pipeline.layout import DATA_DIR_ENV

    def origin(flag_value, env_name: str) -> str:
        if flag_value is not None:
            return "flag"
        return f"${env_name}" if os.environ.get(env_name) else "default"

    data = resolve_layout(args.data_dir).root
    config = resolve_config_dir(args.config_dir)
    return (f"[pipeline] data: {data} ({origin(args.data_dir, DATA_DIR_ENV)}) · "
            f"config: {config} ({origin(args.config_dir, CONFIG_DIR_ENV)})")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    # On stderr, so it is always visible but never mixes with output that is piped or parsed.
    print(_describe_dirs(args), file=sys.stderr)
    from omics_pipeline.config import ConfigError

    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(main())
