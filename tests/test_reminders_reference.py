# SPDX-License-Identifier: Apache-2.0
"""The `obsidian.reminders` contract, on the reference implementation directly: the shapes a
source could hand it, its parameters, and the properties plugin-ntfy relies on (stable text,
bounded output)."""

from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from reminders_reference import (
    MAX_DESCRIPTION,
    SOURCE,
    ReminderError,
    clean,
    instant,
    parse_params,
    reminders,
)

PARIS = "Europe/Paris"
ZONE = ZoneInfo(PARIS)
TODAY = "2026-11-09"


def task(description: str, **fields: Any) -> dict[str, Any]:
    """An `open_tasks` entry in plugin-obsidian 0.1.0's shape (the fields the transform
    reads, and a few it does not)."""
    entry: dict[str, Any] = {
        "key": f"obsidian:line:Tasks.md:{abs(hash(description)) % 1000}",
        "source_fingerprint": None,
        "description": description,
        "status": "todo",
        "status_char": " ",
        "path": "Tasks.md",
        "line": 1,
        "due": None,
        "reminder": None,
        "snoozed_until": None,
        "snoozed": False,
        "recurring": False,
    }
    entry.update(fields)
    return entry


def collected(*tasks: dict[str, Any], **data: Any) -> list[dict[str, Any]]:
    return [
        {
            "source": SOURCE,
            "params": {},
            "data": {"tasks": list(tasks), "more": 0, "open_statuses": " /", **data},
        }
    ]


def params(at: str, **extra: Any) -> dict[str, Any]:
    """The pipeline's parameters, rendered for a run at Paris time `at` on TODAY."""
    now = datetime.fromisoformat(f"{TODAY} {at}").replace(tzinfo=ZONE).astimezone(UTC)
    base: dict[str, Any] = {
        "now": now.isoformat().replace("+00:00", "Z"),
        "timezone": PARIS,
        "window_minutes": 5,
        "digest_at": "07:00",
        "overdue_at": "09:00",
        "overdue_for_minutes": 60,
        "max_overdue": 20,
    }
    return {**base, **extra}


def pushes(at: str, *tasks: dict[str, Any], **extra: Any) -> list[tuple[str, str]]:
    _, intents = reminders(params(at, **extra), collected(*tasks))
    return [(i["title"], i["body"]) for i in intents]


REMINDING = {"due": TODAY, "reminder": {"date": None, "time": "14:30"}}
OVERDUE = {"due": "2026-11-01"}

CLOSED_SHAPES = [
    {"status": "done", "status_char": "x"},
    {"status": "done", "status_char": "X"},
    {"status": "cancelled", "status_char": "-"},
    # A status word and character that disagree: either one closes it.
    {"status": "done", "status_char": " "},
    {"status": "todo", "status_char": "x"},
    # A theme's status the source was not told is open.
    {"status": "custom", "status_char": ">"},
    {"status": "todo", "status_char": ""},
    {"status": "todo", "status_char": None},
]


@pytest.mark.parametrize("shape", CLOSED_SHAPES)
@pytest.mark.parametrize("at", ["07:00", "09:00", "14:30"])
def test_a_closed_task_never_reminds_whatever_the_source_returns(
    shape: dict[str, Any], at: str
) -> None:
    closed = [
        task("Closed reminder", **REMINDING, **shape),
        task("Closed overdue", **OVERDUE, **shape),
        task("Closed today", due=TODAY, **shape),
    ]
    assert pushes(at, *closed) == []


def test_a_custom_status_the_source_counts_as_open_reminds() -> None:
    forwarded = task("Forwarded", status="custom", status_char=">", **REMINDING)
    _, intents = reminders(params("14:30"), collected(forwarded, open_statuses=" />"))
    assert [i["body"] for i in intents] == ["Forwarded"]
    assert pushes("14:30", forwarded) == []  # default open statuses: not open


def test_in_progress_tasks_remind() -> None:
    assert pushes("14:30", task("Half done", status="in_progress", status_char="/", **REMINDING))


def test_the_text_is_stable_across_runs() -> None:
    """No countdown and nothing of the run's time: plugin-ntfy's rule is the text, so the same
    reminder or nudge proposed by two runs must be the same push."""
    tasks = [task("Pay the bill", **OVERDUE), task("Call", **REMINDING)]
    nine, nine_thirty = pushes("09:00", *tasks), pushes("09:30", *tasks)
    assert nine == nine_thirty == [("Overdue: due 2026-11-01", "Pay the bill")]
    at_reminder = pushes("14:30", *tasks, overdue_at="14:00")
    assert at_reminder[0] == ("⏰ 14:30", "Call")
    assert at_reminder[1:] == nine
    for title, body in [*nine, *at_reminder]:
        assert "min" not in title
        assert "09:" not in title + body


def test_the_digest_order_and_bounds() -> None:
    tasks = [task(f"Task {n:02}", due=TODAY) for n in range(30)]
    tasks.append(task("Late", due="2026-11-02"))
    tasks.append(task("Morning call", due=TODAY, reminder={"date": None, "time": "08:00"}))
    tasks.append(task("Woken today", reminder={"date": TODAY, "time": None}))
    tasks.append(task("Not today", due="2026-11-10"))
    _, intents = reminders(params("07:00"), collected(*tasks, more=4))
    (digest,) = intents
    lines = digest["body"].split("\n")
    assert digest["title"] == f"Today, {TODAY}"
    assert lines[:3] == [
        "- Late (overdue, due 2026-11-02)",
        "- 08:00 Morning call",
        "- Task 00",
    ]
    assert len(lines) == 25 + 2
    assert lines[-2:] == ["…and 8 more", "(4 open tasks were not read)"]
    assert "Not today" not in digest["body"]


def test_overdue_nudges_are_capped_oldest_first() -> None:
    tasks = [task(f"Old {n}", due=f"2026-10-{n + 1:02}") for n in range(30)]
    data, intents = reminders(params("09:00", max_overdue=3), collected(*reversed(tasks)))
    assert [i["body"] for i in intents] == ["Old 0", "Old 1", "Old 2"]
    assert data["overdue"] == 30
    assert data["overdue_nudges"] == 3


def test_overdue_nudges_stop_after_their_hour() -> None:
    late = task("Pay the bill", **OVERDUE)
    assert pushes("08:55", late) == []
    assert pushes("09:55", late)
    assert pushes("10:00", late) == []


def test_a_malformed_task_is_skipped_and_counted() -> None:
    good = task("Call", **REMINDING)
    bad = [
        task("Bad due", due="2026-13-01", reminder={"date": None, "time": "14:30"}),
        task("Bad time", due=TODAY, reminder={"date": None, "time": "25:00"}),
        task("Bad reminder", due=TODAY, reminder="14:30"),
    ]
    data, intents = reminders(params("14:30"), collected(good, *bad, {"not": "a task"}))
    assert [i["body"] for i in intents] == ["Call"]
    assert data["invalid_tasks"] == 3


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        [{"source": "calendar.events", "params": {}, "data": {}}],
        [{"source": SOURCE, "params": {}, "data": {"tasks": "none"}}],
        [{"source": SOURCE, "params": {}, "data": {"tasks": [], "open_statuses": 3}}],
    ],
)
def test_an_input_without_open_tasks_fails_the_call(value: Any) -> None:
    with pytest.raises(ReminderError):
        reminders(params("09:00"), value)


@pytest.mark.parametrize(
    "bad",
    [
        {"now": "2026-11-09T09:00:00"},  # no offset
        {"now": "yesterday"},
        {"now": None},
        {"timezone": "Mars/Olympus"},
        {"timezone": ""},
        {"window_minutes": 0},
        {"window_minutes": True},
        {"window_minutes": "5"},
        {"digest_at": "7:00"},
        {"overdue_at": "24:00"},
        {"max_overdue": 101},
        {"window_minute": 5},  # a typo is an error, never a silently ignored setting
    ],
)
def test_invalid_parameters_fail_the_call(bad: dict[str, Any]) -> None:
    with pytest.raises(ReminderError):
        parse_params({**params("09:00"), **bad})


def test_optional_parameters() -> None:
    parsed = parse_params({"now": "2026-11-09T08:00:00Z", "timezone": PARIS})
    assert (parsed.window_minutes, parsed.digest_at, parsed.overdue_at) == (5, None, None)
    # Without digest_at and overdue_at, only reminders are pushed: none at 07:00 or 09:00.
    tasks = [task("Pay", **OVERDUE), task("Today", due=TODAY)]
    for now in ("2026-11-09T06:00:00Z", "2026-11-09T08:00:00Z"):
        _, intents = reminders({"now": now, "timezone": PARIS}, collected(*tasks))
        assert intents == []


def test_clean_makes_one_bounded_line() -> None:
    assert clean("Pay\nthe\x07bill\u2028now") == "Pay the bill now"
    assert clean("") == clean(None) == "(no description)"
    long = clean("x" * 1000)
    assert len(long) == MAX_DESCRIPTION
    assert long.endswith("…")


def test_instant_follows_cron_rules_on_daylight_saving_days() -> None:
    zone = ZONE
    # Skipped (spring forward, 02:00 → 03:00 CEST): the first instant after the gap.
    assert instant(date(2026, 3, 29), time(2, 30), zone) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    # Repeated (fall back, 03:00 CEST → 02:00 CET): the first occurrence.
    assert instant(date(2026, 10, 25), time(2, 30), zone) == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )
    assert instant(date(2026, 11, 9), time(7, 0), zone) == datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
