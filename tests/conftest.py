# SPDX-License-Identifier: Apache-2.0
"""Fixtures: a test instance of the `remind` pipeline in the Paris time zone."""

from pathlib import Path

import pytest

from pack import TIMEZONE, Remind


@pytest.fixture(autouse=True)
def instance_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    """The core starts every plugin with the instance's `TZ`; plugin-obsidian reads it to
    place a `⏰` date-time (its decision 3)."""
    monkeypatch.setenv("TZ", TIMEZONE)


@pytest.fixture
def remind(tmp_path: Path) -> Remind:
    return Remind(tmp_path / "instance")
