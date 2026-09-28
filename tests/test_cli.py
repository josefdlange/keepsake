"""CLI smoke tests with isolated XDG directories (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from keepsake.cli import app
from keepsake.config import EXAMPLE, ConfigError, load

runner = CliRunner()


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def test_example_config_parses(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text(EXAMPLE)
    config = load(path)
    assert config.poll_interval == 60
    assert config.thresholds.max_deletes == 10
    assert config.webhook_url is None


def test_bad_config_is_explained(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text(EXAMPLE.replace('tie_breaker = "keep"', 'tie_breaker = "wife"'))
    with pytest.raises(ConfigError, match="tie_breaker"):
        load(path)


def test_missing_config_prints_example(xdg: Path) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 2
    assert "[keep]" in result.output


def test_status_before_setup(xdg: Path) -> None:
    (xdg / "config" / "keepsake").mkdir(parents=True)
    (xdg / "config" / "keepsake" / "config.toml").write_text(EXAMPLE)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "NOT SET UP (keepsake auth keep)" in result.output
    assert "Last sync:  never" in result.output
    assert oct((xdg / "data" / "keepsake").stat().st_mode & 0o777) == "0o700"


def test_sync_requires_once_and_setup(xdg: Path) -> None:
    (xdg / "config" / "keepsake").mkdir(parents=True)
    (xdg / "config" / "keepsake" / "config.toml").write_text(EXAMPLE)
    assert runner.invoke(app, ["sync"]).exit_code == 2
    result = runner.invoke(app, ["sync", "--once", "--dry-run"])
    assert result.exit_code == 1
    assert "keepsake auth keep" in result.output


def test_shipped_example_config_matches_the_builtin_one() -> None:
    shipped = Path(__file__).parent.parent / "deploy" / "config.example.toml"
    assert shipped.read_text() == EXAMPLE
