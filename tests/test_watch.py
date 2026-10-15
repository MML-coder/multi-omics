"""`pipeline watch`: runs the pipeline when files land, never on a file still being written."""

import os
import subprocess
import sys
import time

import duckdb

from omics_pipeline.ingest import ready_files


def _age(path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_settled_file_is_ready(env) -> None:
    path = env.drop("lab_a", "lab_a_sample.csv")
    _age(path, 5)

    assert ready_files(env.layout, settle_seconds=1) == [path]


def test_nothing_is_ready_while_any_file_is_still_changing(env) -> None:
    settled = env.drop("lab_a", "lab_a_sample.csv")
    _age(settled, 5)
    env.drop("lab_b", "lab_b_sample.csv")          # just written

    assert ready_files(env.layout, settle_seconds=1) == []


def test_file_directly_in_landing_is_ready_so_it_gets_quarantined(env) -> None:
    stray = env.layout.zone("landing") / "stray.csv"
    stray.write_text("a,b\n1,2\n")
    _age(stray, 5)

    assert ready_files(env.layout, settle_seconds=1) == [stray]


def test_watch_runs_the_pipeline_when_a_file_lands(env) -> None:
    watcher = subprocess.Popen(
        [sys.executable, "-m", "omics_pipeline.cli", "--data-dir", str(env.layout.root),
         "--config-dir", str(env.config_dir), "watch", "--interval", "0.2", "--settle", "0.2"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        env.drop("lab_a", "lab_a_sample.csv")
        deadline = time.time() + 30
        runs = 0
        while time.time() < deadline and not runs:
            time.sleep(0.3)
            if env.layout.warehouse.exists():
                try:
                    runs = env.scalar("SELECT count(*) FROM meta.pipeline_runs WHERE files_committed = 1")
                except duckdb.Error:
                    pass                               # the watcher is writing; try again
        assert runs == 1
        assert not any((env.layout.zone("landing") / "lab_a").iterdir())
    finally:
        watcher.terminate()
        watcher.wait(timeout=10)
