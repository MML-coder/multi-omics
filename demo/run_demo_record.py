#!/usr/bin/env python3
"""A short demo for a screen recording: the pipeline end to end, seen through the API.

Eight steps from an empty folder. Files are delivered, the pipeline runs, and every result is shown through
the read-only API the way a consumer would see it.

    python demo/run_demo_record.py --pause          # wait for Enter between steps (recording)
    python demo/run_demo_record.py                  # straight through (check it works)
    python demo/run_demo_record.py --pause --watch  # files trigger runs, like S3 events on AWS

With --watch, start the watcher first in another terminal; the demo only drops files and waits for it:

    PIPELINE_DATA_DIR=data/record PIPELINE_CONFIG_DIR=data/record-config pipeline watch

Uses data/record and a private config copy in data/record-config, and never deletes them: remove them
before running again. Starts the API on port 8000 (--port).
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_demo import FIXTURES, ROOT, CheckFailed, Demo  # noqa: E402

DATA = Path("data/record")
CONFIG = Path("data/record-config")
ROWS = "{source, analyte_code, value, unit, global_subject_id, observed_at, version}"
ONBOARDING = FIXTURES
ONBOARDING_CONTRACT = FIXTURES / "contracts" / "lab_c_v1.yaml"
LAB_C_FILE = "lab_c_first_delivery.csv"


WATCHER = f"PIPELINE_DATA_DIR={DATA} PIPELINE_CONFIG_DIR={CONFIG} pipeline watch"


class RecordDemo(Demo):
    def __init__(self, pause: bool, port: int, watch: bool):
        super().__init__(pause, DATA, CONFIG)
        self.port = port
        self.watch = watch

    def latest_run(self) -> dict | None:
        try:
            runs = self.get("/runs?limit=1")
        except urllib.error.HTTPError:
            return None                  # no warehouse yet, or a run is writing right now
        return runs[0] if runs else None

    def ingest(self, expect: tuple[int, ...] = (0,)) -> dict:
        """Process what was just delivered: run the pipeline, or (--watch) wait for `pipeline watch` to do it."""
        if not self.watch:
            self.pipeline("run", expect=expect)
            return self.latest_run()
        before = self.latest_run()
        print("\n… waiting for `pipeline watch` (other terminal) to pick the file(s) up")
        deadline = time.time() + 60
        while True:
            time.sleep(0.5)
            landing = [p for p in (ROOT / DATA / "landing").rglob("*") if p.is_file() and not p.name.startswith(".")]
            latest = self.latest_run()
            if latest and latest != before and not landing:
                break
            if time.time() > deadline:
                raise CheckFailed(f"no pipeline run within 60 s; is the watcher running? {WATCHER}")
        print(f"  {latest['run_id']}: files in={latest['files_in']} committed={latest['files_committed']} "
              f"quarantined={latest['files_quarantined']} duplicate={latest['files_duplicate']} | "
              f"rows ok={latest['rows_ok']} dlq={latest['rows_dlq']} held={latest['rows_held']}")
        return latest

    def get(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as response:
            return json.load(response)

    def api(self, path: str, rows: str | None = None):
        """Call the API and print the response; `rows` is a jq object filter applied to each list item."""
        if rows:
            print(f"\n$ curl -s 'localhost:{self.port}{path}' | jq -c '.[] | {rows}'")
            data = self.get(path)
            keys = [k.strip() for k in rows.strip("{}").split(",")]
            for item in data:
                print(json.dumps({k: item.get(k) for k in keys}, separators=(",", ":")))
        else:
            print(f"\n$ curl -s 'localhost:{self.port}{path}' | jq")
            data = self.get(path)
            print(json.dumps(data, indent=2))
        return data

    def record_id(self, path: str) -> str:
        print(f"\n$ RID=$(curl -s 'localhost:{self.port}{path}' | jq -r '.[0].record_id')")
        rid = self.get(path)[0]["record_id"]
        print(f"$ echo $RID\n{rid}")
        return rid

    def cat(self, *fixtures: str) -> None:
        print(f"\n$ cat {' '.join(str(FIXTURES / f) for f in fixtures)}")
        for f in fixtures:
            print((ROOT / FIXTURES / f).read_text(), end="")

    def start_api(self) -> subprocess.Popen:
        print(f"\n$ pipeline serve --port {self.port} &        # docs: http://localhost:{self.port}/docs")
        env = {**os.environ, "PIPELINE_DATA_DIR": str(self.data), "PIPELINE_CONFIG_DIR": str(self.config)}
        server = subprocess.Popen(
            [sys.executable, "-m", "omics_pipeline.cli", "serve", "--port", str(self.port)],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            try:
                self.get("/health")
                return server
            except urllib.error.HTTPError:
                return server            # up; 503 until the first run creates the warehouse
            except OSError:
                if server.poll() is not None:
                    raise CheckFailed(f"API did not start (is port {self.port} in use?)")
                time.sleep(0.2)
        server.terminate()
        raise CheckFailed("API did not start within 10 seconds")


def story(d: RecordDemo) -> None:
    existing = [str(path) for path in (DATA, CONFIG) if (ROOT / path).exists()]
    if existing:
        raise CheckFailed(f"data from an earlier run is still there; remove it first: rm -rf {' '.join(existing)}")
    d.step("Day 1: two labs deliver — who is who?",
           "Lab A and Lab B each send CRP results with their own patient IDs, sample IDs and units.")
    shutil.copytree(ROOT / "config", ROOT / CONFIG)
    d.pipeline("init")
    if d.watch:
        d.note(f"terminal 1 is running: {WATCHER}")
    d.server = d.start_api()
    d.cat("lab_a_sample.csv", "lab_b_sample.csv")
    d.shell(f"grep -E 'lab_a|lab_b' {CONFIG}/registry/enrollment.csv", lambda: print("".join(
        line for line in (ROOT / CONFIG / "registry/enrollment.csv").read_text().splitlines(keepends=True)
        if "lab_a" in line or "lab_b" in line), end=""))
    d.shell(f"cat {CONFIG}/sources/lab_b_v1.yaml", lambda: print((ROOT / CONFIG / "sources/lab_b_v1.yaml").read_text(), end=""))
    d.deliver("lab_a", "lab_a_sample.csv")
    d.deliver("lab_b", "lab_b_sample.csv")
    d.ingest()
    rows = d.api("/measurements?subject_id=G-0001", ROWS)
    d.check(sorted((r["source"], r["value"], r["unit"]) for r in rows) == [("lab_a", 3.2, "mg/L"), ("lab_b", 3.2, "mg/L")],
            "one person (G-0001) from two labs; Lab B's 3200 ug/L is now 3.2 mg/L")
    rid = d.record_id("/measurements?source=lab_b&subject_id=G-0001")
    lineage = d.api(f"/measurements/{rid}/lineage")
    d.check(lineage["steps"][0]["source_file"] == "lab_b_sample.csv" and lineage["steps"][0]["source_row"] == 1,
            "lineage: lab_b_sample.csv, row 1, raw copy in bronze")
    file_hash = hashlib.sha256((ROOT / FIXTURES / "lab_b_sample.csv").read_bytes()).hexdigest()
    d.shell(f"shasum -a 256 {FIXTURES}/lab_b_sample.csv", lambda: print(f"{file_hash}  {FIXTURES}/lab_b_sample.csv"))
    d.check(file_hash == lineage["steps"][0]["file_hash"], "file_hash = SHA-256 of the file's bytes (also its bronze name)")
    key_hash = hashlib.sha256(b"lab_b|S200|HSCRP").hexdigest()
    d.shell("printf 'lab_b|S200|HSCRP' | shasum -a 256", lambda: print(f"{key_hash}  -"))
    d.check(key_hash == rid, "record_id = SHA-256 of source | sample | test: the lab's own key, not the file")

    d.step("Proteomics arrives — it only knows sample IDs",
           "Olink sends protein levels per sample. Lab A already said whose samples S100 and S101 are.")
    d.cat("olink_sample.csv")
    d.deliver("olink", "olink_sample.csv")
    d.ingest()
    rows = d.api("/measurements?source=olink", ROWS)
    d.check({r["global_subject_id"] for r in rows} == {"G-0001", "G-0002"},
            "proteomics reached the right people through the samples Lab A linked")
    d.check(all(r["unit"] == "NPX" for r in rows), "kept on its own relative scale (NPX), never converted to mg/L")
    rid = d.record_id("/measurements?source=olink&subject_id=G-0001")
    lineage = d.api(f"/measurements/{rid}/lineage")
    d.check(lineage["steps"][-1]["method"] == "sample_link", "identity came from Lab A's sample link (file and row named)")

    d.step("Three more files: a new source, a correction and a withdrawal",
           "EHR vitals (operational data), Lab A corrects S100, and Lab A withdraws S101.")
    d.cat("lab_a_correction.csv", "lab_a_retraction.csv")
    d.deliver("ehr_vitals", "ehr_vitals_sample.csv")
    d.deliver("lab_a", "lab_a_correction.csv")
    d.deliver("lab_a", "lab_a_retraction.csv")
    d.ingest()
    rows = d.api("/measurements?subject_id=G-0001", ROWS)
    d.check(any(r["source"] == "ehr_vitals" for r in rows), "G-0001 now has vitals from the EHR as well")
    rid = d.record_id("/measurements?source=lab_a&subject_id=G-0001")
    lineage = d.api(f"/measurements/{rid}/lineage")
    d.check([h["change_reason"] for h in lineage["history"]] == ["NEW", "SOURCE_CORRECTION"],
            "history keeps 3.2 (v1) and shows the correction to 2.3 (v2)")
    d.check(d.get("/measurements?source=lab_a&subject_id=G-0002") == [], "S101 was withdrawn: gone from the API, kept in history")

    d.step("The same file arrives again", "Vendors resend files. Nothing may change.")
    before = d.get("/health")["measurements"]
    d.deliver("lab_b", "lab_b_sample.csv", as_name="lab_b_resent.csv")
    run = d.ingest()
    d.api("/runs?limit=1", "{run_id, files_in, files_committed, files_duplicate, rows_in}")
    d.check(run["files_duplicate"] == 1 and d.get("/health")["measurements"] == before,
            "recognized by its fingerprint and skipped; nothing changed")

    d.step("Units: a spelling we don't know, and a change nobody announced",
           "Lab B writes 'mg/dl'. We never guess units. Then Lab B sends mg/L values labelled ug/L.")
    d.cat("lab_b_unknown_unit.csv")
    d.deliver("lab_b", "lab_b_unknown_unit.csv")
    d.ingest()
    d.pipeline("dlq", "list")
    d.check(d.query("SELECT error_code, raw_value FROM meta.dlq WHERE status = 'OPEN'") == [("R006", "mg/dl")],
            "one row rejected (R006) and kept with its raw data; the other row loaded")
    d.shell(f"echo 'mg/dl,mg/dL' >> {CONFIG}/reference/unit_aliases.csv",
            lambda: (ROOT / CONFIG / "reference" / "unit_aliases.csv").open("a").write("mg/dl,mg/dL\n"))
    d.note("the fix is one reviewed line of reference data, not a code change")
    d.pipeline("dlq", "replay", "--error", "R006")
    rid = d.record_id("/measurements?source=lab_b&subject_id=G-0001&observed_from=2026-04-02&observed_to=2026-04-02")
    lineage = d.api(f"/measurements/{rid}/lineage")
    d.check(lineage["steps"][3]["from"] == "0.32 mg/dl" and lineage["steps"][3]["to"] == "3.2 mg/L",
            "0.32 mg/dl loaded as 3.2 mg/L; lineage shows exactly what the lab sent")
    d.cat("lab_b_silent_unit_change.csv")
    d.deliver("lab_b", "lab_b_silent_unit_change.csv")
    d.ingest(expect=(2,))
    d.pipeline("events", "--limit", "1")
    d.check(d.query("SELECT code FROM meta.events WHERE event_type = 'DistributionShift'") == [("B001",)],
            "the file looked perfect; only the values gave it away (B001 alert)")

    d.step("A lab adds a column", "Lab A starts sending specimen_type. Data keeps flowing; we are told; a person decides.")
    d.cat("lab_a_new_column.csv")
    d.deliver("lab_a", "lab_a_new_column.csv")
    d.ingest(expect=(2,))
    d.pipeline("drift")
    d.check(d.query("SELECT status FROM meta.schema_drift WHERE column_name = 'specimen_type'") == [("OPEN",)],
            "rows loaded; specimen_type tracked as OPEN; its values kept in bronze")
    d.shell(f"echo 'ignored_columns: [specimen_type]' >> {CONFIG}/sources/lab_a_v1.yaml",
            lambda: (ROOT / CONFIG / "sources" / "lab_a_v1.yaml").open("a").write("\nignored_columns: [specimen_type]\n"))
    d.note("decision recorded in the contract — reviewed like code")
    if d.watch:
        d.note("no file arrives, so nothing triggers the watcher: one manual run applies the decision")
    d.pipeline("run")
    d.pipeline("drift")
    d.check(d.query("SELECT status FROM meta.schema_drift WHERE column_name = 'specimen_type'") == [("IGNORED",)],
            "the decision is now part of the record")

    d.step("A breaking change: quarantine, review, release",
           "Lab B renames its ID columns. Guessing would load wrong data, so the file is stopped.")
    d.cat("lab_b_v2_renamed.csv")
    d.deliver("lab_b", "lab_b_v2_renamed.csv")
    d.ingest(expect=(2,))
    d.pipeline("quarantine", "list")
    file_hash, quarantine_path = d.query(
        "SELECT file_hash, quarantine_path FROM meta.ingested_files WHERE original_name = 'lab_b_v2_renamed.csv'")[0]
    d.check(d.query("SELECT quarantine_code FROM meta.ingested_files WHERE file_hash = ?", [file_hash]) == [("F004",)],
            "quarantined (F004): nothing loaded; expected vs received columns recorded")
    d.shell(f"diff {CONFIG}/sources/lab_b_v1.yaml {FIXTURES}/contracts/lab_b_v2.yaml", lambda: print(subprocess.run(
        ["diff", str(CONFIG / "sources/lab_b_v1.yaml"), str(FIXTURES / "contracts/lab_b_v2.yaml")],
        cwd=ROOT, capture_output=True, text=True).stdout.rstrip()))
    d.note("the reviewed fix: contract v2 maps the new names onto the same canonical fields")
    d.shell(f"cp {FIXTURES}/contracts/lab_b_v2.yaml {CONFIG}/sources/",
            lambda: shutil.copy(ROOT / FIXTURES / "contracts" / "lab_b_v2.yaml", ROOT / CONFIG / "sources"))
    d.pipeline("check-file", "--source", "lab_b", quarantine_path)
    d.pipeline("quarantine", "release", file_hash[:12])
    d.ingest()
    rid = d.record_id("/measurements?source=lab_b&subject_id=G-0001&observed_from=2026-04-01&observed_to=2026-04-01")
    lineage = d.api(f"/measurements/{rid}/lineage")
    d.check(lineage["steps"][1]["contract_version"] == "lab_b_v2" and "bronze" in lineage["steps"][0]["bronze_path"],
            "loaded under lab_b_v2; the raw file is in bronze; consumers see the same fields as before")

    d.step("A new lab joins: Lab C, configuration only",
           "Its own column names, its own patient IDs, CRP in mg/dL. No code changes: a contract, one alias, registry entries.")
    d.shell(f"cat {ONBOARDING_CONTRACT}", lambda: print((ROOT / ONBOARDING_CONTRACT).read_text(), end=""))
    d.shell(f"cp {ONBOARDING_CONTRACT} {CONFIG}/sources/",
            lambda: shutil.copy(ROOT / ONBOARDING_CONTRACT, ROOT / CONFIG / "sources"))
    for name, target in (("lab_c_aliases.csv", "reference/analyte_aliases.csv"), ("lab_c_registry.csv", "registry/enrollment.csv")):
        d.shell(f"cat {ONBOARDING}/{name} >> {CONFIG}/{target}",
                lambda name=name, target=target: (ROOT / CONFIG / target).open("a").write((ROOT / ONBOARDING / name).read_text()))
    d.note("in production these three changes are one reviewed pull request")
    d.shell(f"head -5 {ONBOARDING}/{LAB_C_FILE}   # 50 rows: 25 people, two blood draws each", lambda: print(
        "".join((ROOT / ONBOARDING / LAB_C_FILE).read_text().splitlines(keepends=True)[:5]), end=""))
    d.pipeline("check-file", "--source", "lab_c", str(ONBOARDING / LAB_C_FILE))
    d.shell(f"cp {ONBOARDING}/{LAB_C_FILE} {DATA}/landing/lab_c/", lambda: (
        (ROOT / DATA / "landing" / "lab_c").mkdir(parents=True, exist_ok=True),
        shutil.copy(ROOT / ONBOARDING / LAB_C_FILE, ROOT / DATA / "landing" / "lab_c")))
    run = d.ingest()
    d.check(run["rows_in"] == 50 and run["rows_ok"] == 50, "50 rows in, 50 loaded: every row accounted for")
    sources = d.api("/sources")
    d.check(next(s["measurements"] for s in sources if s["source"] == "lab_c") == 50, "Lab C shows up as a source with 50 measurements")
    rows = d.api("/measurements?subject_id=G-0001&analyte_code=CRP", ROWS)
    d.check(sum(r["source"] == "lab_c" for r in rows) == 2, "G-0001 now has CRP from Lab A, Lab B, Lab C and proteomics")
    rid = d.record_id("/measurements?source=lab_c&subject_id=G-0001")
    lineage = d.api(f"/measurements/{rid}/lineage")
    d.check(lineage["steps"][1]["contract_version"] == "lab_c_v1" and lineage["steps"][3]["factor"] == 10.0,
            "lineage: contract lab_c_v1, 0.17 mg/dL x 10 = 1.7 mg/L")
    print(f"\nRECORDING DEMO COMPLETE: {d.number} steps, all checks passed.")


def main() -> int:
    global DATA, CONFIG, WATCHER
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pause", action="store_true", help="wait for Enter between steps")
    parser.add_argument("--port", type=int, default=8000, help="API port (default 8000)")
    parser.add_argument("--watch", action="store_true", help="let `pipeline watch` run the pipeline when files land")
    parser.add_argument("--data", type=Path, default=DATA, help=f"data folder (default {DATA}; config goes next to it)")
    args = parser.parse_args()
    DATA, CONFIG = args.data, Path(f"{args.data}-config")
    WATCHER = f"PIPELINE_DATA_DIR={DATA} PIPELINE_CONFIG_DIR={CONFIG} pipeline watch"
    demo = RecordDemo(args.pause, args.port, args.watch)
    demo.server = None
    try:
        story(demo)
    except CheckFailed as exc:
        print(f"\nDEMO STOPPED at step {demo.number}: {exc}" if demo.number else f"Not started: {exc}")
        return 1
    finally:
        if demo.server is not None:
            demo.server.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
