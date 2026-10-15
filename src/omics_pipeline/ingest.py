"""Run orchestration: landing → bronze → silver → gold (DESIGN.md §7.3, §7.5).

Each file is one unit of work processed in one database transaction: its silver rows, DLQ rows, sample
links and file status are committed together or not at all. A file that fails unexpectedly stays where
it is and is retried on the next run (P001).

Three entry points share the same per-file processing:
- `run`        new files from landing/
- `replay`     every file in bronze, in original arrival order (rebuild or apply a fix to history)
- `dlq_replay` bronze files that still have open DLQ rows (after a rule or reference-data fix)
"""

import hashlib
import json
import logging
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from omics_pipeline import __version__, adapters, drift, gold, rules, transform
from omics_pipeline.config import Contract, ConfigVersions, config_versions, load_contracts, select_contract
from omics_pipeline.layout import Layout, create_layout
from omics_pipeline.notify import HIGH, MEDIUM, Event, LocalNotifier, Notifier
from omics_pipeline.warehouse import connect, load_reference_data

log = logging.getLogger(__name__)

# F005: quarantine the whole file when more than this share of its rows is rejected.
MAX_REJECT_RATE = 0.5
SIDECAR_SUFFIX = ".meta.json"


@dataclass
class RunSummary:
    run_id: str
    run_type: str = "ingest"
    files_in: int = 0
    files_committed: int = 0
    files_quarantined: int = 0
    files_duplicate: int = 0
    files_failed: int = 0
    rows_in: int = 0
    rows_ok: int = 0
    rows_warn: int = 0
    rows_dlq: int = 0
    rows_held: int = 0
    rows_filtered: int = 0
    rows_resolved: int = 0
    gold_rows: int = 0
    quarantined: list[tuple[str, str]] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    notifier: Notifier | None = field(default=None, repr=False)

    def publish(self, event: Event) -> None:
        self.events.append(event)
        if self.notifier is not None:
            self.notifier.publish(event)

    @property
    def needs_attention(self) -> bool:
        return self.files_quarantined > 0 or bool(self.events)

    @property
    def exit_code(self) -> int:
        """0 = clean, 1 = processing failure, 2 = needs attention (quarantine or alert)."""
        if self.files_failed:
            return 1
        return 2 if self.needs_attention else 0

    @property
    def balanced(self) -> bool:
        return self.rows_in == self.rows_ok + self.rows_dlq + self.rows_held + self.rows_filtered

    def add(self, counts: transform.RowCounts) -> None:
        self.files_committed += 1
        self.rows_in += counts.rows_in
        self.rows_ok += counts.ok
        self.rows_warn += counts.warn
        self.rows_dlq += counts.dlq
        self.rows_held += counts.held
        self.rows_filtered += counts.filtered


@dataclass(frozen=True)
class FileInput:
    source: str
    path: Path
    file_hash: str
    original_name: str
    received_at: datetime
    in_bronze: bool


class Quarantine(Exception):
    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class StageTimer:
    """Records duration and row counts per stage into meta.run_stage_metrics (benchmark input)."""

    def __init__(self, con: duckdb.DuckDBPyConnection, run_id: str, file_hash: str | None):
        self.con, self.run_id, self.file_hash = con, run_id, file_hash

    @contextmanager
    def stage(self, name: str, rows_in: int | None = None) -> Iterator[dict]:
        start = time.perf_counter()
        result: dict = {}
        yield result
        self.con.execute(
            "INSERT INTO meta.run_stage_metrics VALUES (?, ?, ?, ?, ?, ?)",
            [self.run_id, self.file_hash, name, (time.perf_counter() - start) * 1000, rows_in, result.get("rows_out")],
        )


# ---------------------------------------------------------------------------------------------- bronze


def _land_in_bronze(layout: Layout, item: FileInput) -> Path:
    """Content-addressed copy plus a sidecar, so bronze alone can rebuild everything in arrival order."""
    bronze_dir = layout.zone("bronze") / f"source={item.source}"
    bronze_dir.mkdir(parents=True, exist_ok=True)
    bronze_path = bronze_dir / f"{item.file_hash}{item.path.suffix}"
    if not bronze_path.exists():
        shutil.copy2(item.path, bronze_path)
    sidecar = bronze_path.with_name(bronze_path.name + SIDECAR_SUFFIX)
    if not sidecar.exists():
        sidecar.write_text(json.dumps({
            "source": item.source, "original_name": item.original_name,
            "received_at": item.received_at.isoformat(), "file_hash": item.file_hash,
        }, indent=2))
    return bronze_path


def _bronze_files(layout: Layout, sources: set[str] | None, hashes: set[str] | None = None) -> list[FileInput]:
    items = []
    for sidecar in layout.zone("bronze").glob(f"source=*/*{SIDECAR_SUFFIX}"):
        meta = json.loads(sidecar.read_text())
        if sources is not None and meta["source"] not in sources:
            continue
        if hashes is not None and meta["file_hash"] not in hashes:
            continue
        items.append(FileInput(
            meta["source"], sidecar.with_name(sidecar.name.removesuffix(SIDECAR_SUFFIX)), meta["file_hash"],
            meta["original_name"], datetime.fromisoformat(meta["received_at"]), in_bronze=True,
        ))
    return sorted(items, key=lambda i: (i.received_at, i.original_name))


# Files dropped directly into landing/ (not into a source folder) are quarantined under this name.
UNASSIGNED = "_unassigned"


def _landing_files(layout: Layout, sources: set[str] | None) -> list[tuple[str, Path]]:
    """Files under landing/<source>/, oldest first (arrival order makes versioning deterministic).

    Files directly in landing/ are returned under UNASSIGNED so they are quarantined, not ignored.
    """
    found = [
        (UNASSIGNED, path) for path in layout.zone("landing").iterdir()
        if path.is_file() and not path.name.startswith(".") and sources is None
    ]
    for source_dir in sorted(p for p in layout.zone("landing").iterdir() if p.is_dir()):
        if sources is not None and source_dir.name not in sources:
            continue
        for path in source_dir.iterdir():
            if path.is_file() and not path.name.startswith("."):
                found.append((source_dir.name, path))
    return sorted(found, key=lambda item: (item[1].stat().st_mtime, item[1].name))


def ready_files(layout: Layout, settle_seconds: float, now: float | None = None) -> list[Path]:
    """Files waiting in landing, or [] while any of them changed in the last `settle_seconds`.

    Used by `pipeline watch`: a local copy is not atomic, so a file still being written must not be read.
    (On S3 an object only appears once its upload completes, so this check has no AWS equivalent.)
    """
    if not layout.zone("landing").exists():
        return []                      # not initialized yet; `pipeline init` or the first run creates it
    files = [path for _, path in _landing_files(layout, None)]
    now = time.time() if now is None else now
    return [] if any(now - path.stat().st_mtime < settle_seconds for path in files) else files


# ------------------------------------------------------------------------------------------- one file


def _pick_contract(con, item: FileInput, candidates: list[Contract]) -> tuple[Contract, list[str]]:
    if item.path.stat().st_size == 0:
        raise Quarantine("F003", "empty file")
    try:
        columns = adapters.read_columns(con, item.path, candidates[0].format)
    except duckdb.Error as exc:
        raise Quarantine("F002", f"unreadable: {exc}") from exc
    contract = select_contract(candidates, columns)
    if contract is None:
        expected = "; ".join(f"{c.name}: {sorted(c.required_columns)}" for c in candidates)
        raise Quarantine("F004", f"columns {columns} match no contract ({expected})")
    forbidden = drift.check_columns(contract, columns).forbidden
    if forbidden:
        drift.record(con, contract, columns, item.original_name, item.received_at)
        raise Quarantine("F007", f"forbidden columns {forbidden} (see {contract.name} forbidden_columns)")
    return contract, columns


def _process_rows(con, layout: Layout, contract: Contract, columns: list[str], item: FileInput,
                  ctx: transform.WriteContext, timer: StageTimer, summary: RunSummary) -> transform.RowCounts:
    pending: list[Event] = []
    undecided = drift.check_columns(contract, columns).open
    if undecided:
        pending.append(Event(
            "SchemaDriftDetected", MEDIUM, "F006", item.source, item.original_name, item.file_hash,
            f"columns not mapped by {contract.name}: {undecided}; ingested, values kept in bronze only "
            "(decide: adopt / ignored_columns / forbidden_columns)",
        ))
    con.begin()
    try:
        with timer.stage("read") as stage:
            rows_in = adapters.stage(con, item.path, contract)
            stage["rows_out"] = rows_in
        if rows_in == 0:
            raise Quarantine("F003", "header only, no data rows")
        bronze_path = item.path if item.in_bronze else _land_in_bronze(layout, item)
        with timer.stage("map_validate", rows_in) as stage:
            adapters.map_rows(con, contract, columns)
            rules.type_rows(con, contract)
            rules.filter_rows(con, contract)
            rules.validate(con, contract)
            transform.key_rows(con, contract)
            stage["rows_out"] = con.execute("SELECT count(*) FROM keyed").fetchone()[0]
        rate = rules.reject_rate(con)
        if rate > MAX_REJECT_RATE:
            raise Quarantine("F005", f"{rate:.0%} of rows rejected (limit {MAX_REJECT_RATE:.0%})")
        with timer.stage("normalize_resolve", rows_in) as stage:
            transform.normalize(con, contract)
            transform.reject_unknown_retractions(con)
            transform.version_rows(con, "result_status" in contract.fields, item.received_at)
            stage["rows_out"] = con.execute("SELECT count(*) FROM candidates").fetchone()[0]
        with timer.stage("write", rows_in) as stage:
            counts = transform.write(con, contract, ctx)
            stage["rows_out"] = counts.written
        if not counts.balanced:
            raise RuntimeError(f"row accounting violated for {item.original_name}: {counts}")
        for analyte, detail in transform.distribution_alerts(con, item.source):
            pending.append(Event("DistributionShift", HIGH, "B001", item.source, item.original_name,
                                 item.file_hash, f"{analyte}: {detail}"))
        drift.record(con, contract, columns, item.original_name, item.received_at)
        con.execute(
            "INSERT OR REPLACE INTO meta.ingested_files (file_hash, source, original_name, received_at, bronze_path, "
            "contract_version, status, last_run_id, columns) VALUES (?, ?, ?, ?, ?, ?, 'COMMITTED', ?, ?)",
            [item.file_hash, item.source, item.original_name, item.received_at, str(bronze_path), contract.name,
             ctx.run_id, columns],
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    # Announced only once the file is committed, so a rolled-back file raises no drift or shift events.
    for event in pending:
        summary.publish(event)
    return counts


def _quarantine(con, layout: Layout, item: FileInput, error: Quarantine, run_id: str) -> None:
    target_dir = layout.zone("quarantine") / f"source={item.source}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{item.file_hash}{item.path.suffix}"
    shutil.move(str(item.path), target)
    reason = {"code": error.code, "reason": error.reason, "original_name": item.original_name, "run_id": run_id}
    target.with_name(target.name + ".reason.json").write_text(json.dumps(reason, indent=2))
    con.execute(
        "INSERT OR REPLACE INTO meta.ingested_files (file_hash, source, original_name, received_at, quarantine_path, "
        "status, quarantine_code, quarantine_reason, last_run_id) VALUES (?, ?, ?, ?, ?, 'QUARANTINED', ?, ?, ?)",
        [item.file_hash, item.source, item.original_name, item.received_at, str(target), error.code, error.reason,
         run_id],
    )


def _handle(con, layout, contracts, item: FileInput, run_id: str, versions: ConfigVersions,
            summary: RunSummary) -> None:
    timer = StageTimer(con, run_id, item.file_hash)
    ctx = transform.WriteContext(
        run_id, item.file_hash, item.original_name, __version__, versions.reference, versions.registry
    )
    try:
        if item.source == UNASSIGNED:
            raise Quarantine("F001", "file is not inside a source folder; deliver to landing/<source>/")
        if item.source not in contracts:
            raise Quarantine("F001", f"no contract configured for source folder '{item.source}'")
        contract, columns = _pick_contract(con, item, contracts[item.source])
        summary.add(_process_rows(con, layout, contract, columns, item, ctx, timer, summary))
    except Quarantine as error:
        if item.in_bronze:
            # Bronze is immutable: a file that no longer fits any contract is reported, not moved.
            log.error("replay of %s failed: %s", item.original_name, error)
            summary.files_failed += 1
            summary.publish(Event("ProcessingFailed", HIGH, error.code, item.source, item.original_name,
                                  item.file_hash, f"replay: {error.reason}"))
            return
        _quarantine(con, layout, item, error, run_id)
        summary.files_quarantined += 1
        summary.quarantined.append((item.original_name, error.code))
        summary.publish(Event("FileQuarantined", HIGH, error.code, item.source, item.original_name,
                              item.file_hash, error.reason))
        log.warning("quarantined %s: %s", item.original_name, error)
        return
    except Exception as exc:
        log.exception("P001 processing failure for %s; left in place for retry", item.original_name)
        summary.files_failed += 1
        summary.publish(Event("ProcessingFailed", HIGH, "P001", item.source, item.original_name,
                              item.file_hash, f"{type(exc).__name__}: {exc}; file left in place for retry"))
        return
    if not item.in_bronze:
        item.path.unlink()


def _process_landing_file(con, layout, contracts, source: str, path: Path, run_id: str, versions,
                          summary: RunSummary) -> None:
    file_hash = file_sha256(path)
    summary.files_in += 1
    known = con.execute("SELECT status FROM meta.ingested_files WHERE file_hash = ?", [file_hash]).fetchone()
    if known is not None and known[0] in ("COMMITTED", "QUARANTINED"):
        log.info("I001 duplicate delivery %s (%s), skipping", path.name, known[0])
        summary.files_duplicate += 1
        path.unlink()
        return
    item = FileInput(source, path, file_hash, path.name, _now(), in_bronze=False)
    _handle(con, layout, contracts, item, run_id, versions, summary)


# ------------------------------------------------------------------------------------------- run types


def _sources_for(contracts: dict[str, list[Contract]], source: str | None, domain: str | None) -> set[str] | None:
    if source is None and domain is None:
        return None
    selected = {s for s, versions in contracts.items() if domain is None or versions[0].domain == domain}
    return selected & {source} if source is not None else selected


def _execute(layout: Layout, config_dir: Path, run_type: str,
             body: Callable[[duckdb.DuckDBPyConnection, dict, str, ConfigVersions, RunSummary], None]) -> RunSummary:
    create_layout(layout)
    contracts = load_contracts(config_dir)
    versions = config_versions(config_dir)
    con = connect(layout.warehouse)
    try:
        load_reference_data(con, config_dir)
        drift.reconcile(con, contracts)
        count = con.execute("SELECT count(*) FROM meta.pipeline_runs").fetchone()[0]
        run_id = f"run-{count + 1:04d}"
        summary = RunSummary(run_id, run_type, notifier=LocalNotifier(con, run_id))
        started = _now()
        body(con, contracts, summary.run_id, versions, summary)

        timer = StageTimer(con, summary.run_id, None)
        ctx = transform.WriteContext(summary.run_id, "", "", __version__, versions.reference, versions.registry)
        with timer.stage("reresolve_held") as stage:
            summary.rows_resolved = transform.reresolve_held(con, ctx)
            stage["rows_out"] = summary.rows_resolved
        with timer.stage("gold") as stage:
            summary.gold_rows = gold.rebuild(con)
            stage["rows_out"] = summary.gold_rows
        _record_run(con, summary, started, versions)
        return summary
    finally:
        con.close()


def run(layout: Layout, config_dir: Path, source: str | None = None, domain: str | None = None) -> RunSummary:
    """Process new files in landing/<source>/."""

    def body(con, contracts, run_id, versions, summary):
        for file_source, path in _landing_files(layout, _sources_for(contracts, source, domain)):
            _process_landing_file(con, layout, contracts, file_source, path, run_id, versions, summary)

    return _execute(layout, config_dir, "ingest", body)


def replay(layout: Layout, config_dir: Path, source: str | None = None) -> RunSummary:
    """Reprocess bronze files in arrival order with current code and configuration.

    Unchanged records write nothing; records whose derived values change get a DERIVATION_CHANGE
    version. With an empty warehouse this rebuilds everything from bronze.
    """

    def body(con, contracts, run_id, versions, summary):
        for item in _bronze_files(layout, None if source is None else {source}):
            summary.files_in += 1
            _handle(con, layout, contracts, item, run_id, versions, summary)

    return _execute(layout, config_dir, "replay", body)


def dlq_replay(layout: Layout, config_dir: Path, source: str | None = None, error_code: str | None = None) -> RunSummary:
    """Reprocess bronze files that still have open DLQ rows (optionally of one source or error code)."""

    def body(con, contracts, run_id, versions, summary):
        clauses = ["status = 'OPEN'"] + ["source = ?"] * (source is not None) + ["error_code = ?"] * (error_code is not None)
        params = [p for p in (source, error_code) if p is not None]
        hashes = {r[0] for r in con.execute(
            f"SELECT DISTINCT file_hash FROM meta.dlq WHERE {' AND '.join(clauses)}", params).fetchall()}
        for item in _bronze_files(layout, None, hashes):
            summary.files_in += 1
            _handle(con, layout, contracts, item, run_id, versions, summary)

    return _execute(layout, config_dir, "dlq_replay", body)


def release_quarantined(layout: Layout, hash_prefix: str) -> list[str]:
    """Move quarantined files back to landing so the next run retries them (e.g. after a new contract)."""
    con = connect(layout.warehouse)
    try:
        rows = con.execute(
            "SELECT file_hash, source, original_name, quarantine_path FROM meta.ingested_files "
            "WHERE status = 'QUARANTINED' AND starts_with(file_hash, ?)",
            [hash_prefix],
        ).fetchall()
        released = []
        for file_hash, source, original_name, quarantine_path in rows:
            target_dir = layout.zone("landing") / source
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(quarantine_path, target_dir / original_name)
            Path(quarantine_path + ".reason.json").unlink(missing_ok=True)
            con.execute("UPDATE meta.ingested_files SET status = 'RELEASED' WHERE file_hash = ?", [file_hash])
            released.append(original_name)
        return released
    finally:
        con.close()


def _record_run(con, summary: RunSummary, started: datetime, versions: ConfigVersions) -> None:
    status = "completed" if summary.files_failed == 0 else "completed_with_failures"
    con.execute(
        "INSERT INTO meta.pipeline_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            summary.run_id, summary.run_type, started, _now(), status, __version__, versions.reference,
            versions.registry, summary.files_in, summary.files_committed, summary.files_quarantined,
            summary.files_duplicate, summary.files_failed, summary.rows_in, summary.rows_ok, summary.rows_warn,
            summary.rows_dlq, summary.rows_held, summary.rows_filtered, summary.rows_resolved,
        ],
    )
