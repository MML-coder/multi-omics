from pathlib import Path

import pytest

from omics_pipeline.cli import main
from omics_pipeline.layout import DATA_DIR_ENV, ZONES, Layout, create_layout, resolve_layout


def test_create_layout_creates_every_zone(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "data")

    created = create_layout(layout)

    assert sorted(p.name for p in created) == sorted(ZONES)
    assert all(layout.zone(name).is_dir() for name in ZONES)


def test_create_layout_is_idempotent(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "data")
    create_layout(layout)
    marker = layout.zone("bronze") / "existing.csv"
    marker.write_text("keep me")

    created_again = create_layout(layout)

    assert created_again == []
    assert marker.read_text() == "keep me"


def test_unknown_zone_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown zone"):
        Layout(tmp_path).zone("platinum")


def test_resolve_layout_prefers_argument_then_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "from_env"))

    assert resolve_layout(tmp_path / "explicit").root == tmp_path / "explicit"
    assert resolve_layout().root == tmp_path / "from_env"


def test_cli_init_runs_twice_safely(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = tmp_path / "data"

    assert main(["--data-dir", str(data_dir), "init"]) == 0
    assert main(["--data-dir", str(data_dir), "init"]) == 0

    assert "layout already present" in capsys.readouterr().out


def test_cli_announces_which_folders_it_uses(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("PIPELINE_CONFIG_DIR", "some/config")

    main(["--data-dir", str(tmp_path / "data"), "init"])

    err = capsys.readouterr().err
    assert f"data: {tmp_path / 'data'} (flag)" in err
    assert "config: some/config ($PIPELINE_CONFIG_DIR)" in err
