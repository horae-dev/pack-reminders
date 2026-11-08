# SPDX-License-Identifier: Apache-2.0
"""The `obsidian.reminders` contract this pack relies on: plugin-obsidian's golden vectors
(`horae_obsidian.vectors`), run against the transform pinned in uv.lock. The transform's
own tests are plugin-obsidian's; a change of behaviour there shows here as a failing vector
until the pack's expectations are reviewed."""

from typing import Any

import pytest
from horae_obsidian.reminders import reminders
from horae_obsidian.vectors import reminder_vectors
from horae_sdk import PluginError

VECTORS = reminder_vectors()


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_the_transform_answers_its_vectors(vector: dict[str, Any]) -> None:
    if "error" in vector:
        with pytest.raises(PluginError):
            reminders(vector["params"], vector["input"])
        return
    data, intents = reminders(vector["params"], vector["input"])
    assert intents == vector["intents"]
    for name, value in vector.get("data", {}).items():
        assert data[name] == value, name


def test_every_push_has_stable_text() -> None:
    """plugin-ntfy dedupes on the push's text: no countdown, no run time, ever."""
    for vector in VECTORS:
        for intent in vector.get("intents", []):
            assert intent["type"] == "notify.push"
            assert "link" not in intent
            assert " min" not in intent["title"] + intent["body"]


def test_the_vectors_cover_what_the_pack_relies_on() -> None:
    names = " ".join(v["name"] for v in VECTORS)
    for topic in ("lookback", "digest", "nudge", "spring forward", "fall back", "suffix"):
        assert topic in names, topic
