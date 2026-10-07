"""The scheduled PR synchronization command refuses anything but the real HOSxP (D-29)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ppr import cli
from ppr.config.settings import Settings


def _settings(**kw: object) -> Settings:
    return Settings(None, None, None, 30, **kw)  # type: ignore[arg-type]


@pytest.mark.spec("D-29")
def test_sync_prs_refuses_mock_mode(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli._sync_prs(_settings(hosxp_mode="mock")) == 2
    assert "PPR_HOSXP_MODE=sql" in capsys.readouterr().out


@pytest.mark.spec("D-29")
def test_sync_prs_needs_the_site_query_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _settings(hosxp_mode="sql", hosxp_queries_file=tmp_path / "missing.toml")
    assert cli._sync_prs(s) == 2
    assert capsys.readouterr().out.startswith("error:")
