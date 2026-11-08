# SPDX-License-Identifier: Apache-2.0
"""Time travel (#3): the real `remind` pipeline, through the core's `Runner` (tests/pack.py),
run every 5 minutes over simulated days with a fake clock, the ntfy dedupe state carried from
run to run. Each test asserts the full log of what reaches the phone.

A log line is `delivered-at delivery title | body` (the body's lines joined by ` / `), in
the zone's wall-clock time. The digest of a day is pushed at 07:00 (the end of the quiet
hours); a reminder in the quiet hours is its own push at 07:00 too, and the digest leaves it out.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import EllipsisType
from typing import Any

import pytest

from pack import Remind, local

Edit = Callable[[dict[str, Any]], None]
Make = Callable[..., Remind]


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Make:
    def build(timezone: str = "Europe/Paris", edit: Edit | None = None) -> Remind:
        monkeypatch.setenv("TZ", timezone)  # what the core gives the plugin
        return Remind(tmp_path / "instance", timezone, edit)

    return build


def configure(
    *,
    quiet: dict[str, str] | EllipsisType | None = ...,
    priority: str | None = None,
    **transform: Any,
) -> Edit:
    """Other trusted parameters for the pipeline: `quiet` replaces both quiet-hours blocks
    (`None` removes them; by default they are kept), `priority` the push's, `transform` the
    transform's parameters."""

    def edit(document: dict[str, Any]) -> None:
        for step in document["steps"]:
            if "transform" in step:
                params = step["transform"][0]["obsidian.reminders"]
                params.update(transform)
                if quiet is not ...:
                    params.pop("quiet_hours", None)
                    if quiet is not None:
                        params["quiet_hours"] = dict(quiet)
            if "notify" in step:
                params = step["notify"][0]["ntfy.push"]
                if quiet is not ...:
                    params.pop("quiet_hours", None)
                    if quiet is not None:
                        params["quiet_hours"] = dict(quiet)
                if priority is not None:
                    params["priority"] = priority

    return edit


def log(remind: Remind) -> list[str]:
    """What reaches the phone, in delivery order (stable for equal times)."""
    pushes = sorted(remind.ntfy.phone(), key=lambda push: push.delivered)
    return [
        f"{push.delivered:%Y-%m-%d %H:%M} {push.delivery} {push.title} | "
        + (push.body or "").replace("\n", " / ")
        for push in pushes
    ]


def proposing(remind: Remind, title: str) -> list[datetime]:
    """The local times of the runs that proposed a push titled `title`. The core delivers a
    keyed push once a day, so only the first of them reaches the notifier (`remind.ntfy`)."""
    return [
        record.started_at.astimezone(remind.zone).replace(tzinfo=None)
        for record in remind.records
        if any(proposed == title for proposed, _ in remind.proposed(record))
    ]


def asleep(remind: Remind, start: str, end: str) -> Callable[[datetime], bool]:
    """The host is asleep from `start` (included) to `end` (excluded): no run in between."""
    first, last = local(start, remind.zone), local(end, remind.zone)
    return lambda moment: first <= moment < last


# --- daylight saving: Europe/Paris -----------------------------------------------------------


def test_paris_spring_forward_day(make: Make) -> None:
    """2026-03-29: 02:00 CET jumps to 03:00 CEST (a 23-hour day). A reminder in the skipped hour
    is proposed from the first instant after the gap; both it and one at 03:15 are in the
    quiet hours, so they arrive at 07:00 (CEST, 05:00 UTC), once, and the digest leaves them
    out. The digest and the overdue nudge come at their wall-clock times, one day each."""
    remind = make()
    remind.write(
        "- [ ] Check the boiler 📅 2026-03-29 ⏰ 02:30\n"
        "- [ ] Call the bank 📅 2026-03-29 ⏰ 03:15\n"
        "- [ ] Water the plants 📅 2026-03-29 ⏰ 08:30\n"
        "- [ ] Send the form 📅 2026-03-28\n"
        "- [ ] File the taxes 📅 2026-03-27\n"
    )
    remind.travel("2026-03-29 00:00", "2026-03-30 09:30")
    assert log(remind) == [
        "2026-03-29 07:00 delayed ⏰ 2026-03-29 02:30 | Check the boiler",
        "2026-03-29 07:00 delayed ⏰ 2026-03-29 03:15 | Call the bank",
        "2026-03-29 07:00 sent Today, 2026-03-29 | Overdue: 2 / - File the taxes (due 2026-03-27)"
        " / - Send the form (due 2026-03-28) / Today: 1 / - 08:30 Water the plants",
        "2026-03-29 08:30 sent ⏰ 2026-03-29 08:30 | Water the plants",
        "2026-03-29 09:00 sent Overdue: due 2026-03-28 | Send the form",
        "2026-03-30 07:00 sent Today, 2026-03-30 | Overdue: 5 / - File the taxes (due 2026-03-27)"
        " / - Send the form (due 2026-03-28) / - Check the boiler (due 2026-03-29)"
        " / - Call the bank (due 2026-03-29) / - Water the plants (due 2026-03-29)",
        "2026-03-30 09:00 sent Overdue: due 2026-03-29 | Check the boiler",
        "2026-03-30 09:00 sent Overdue: due 2026-03-29 | Call the bank",
        "2026-03-30 09:00 sent Overdue: due 2026-03-29 | Water the plants",
    ]
    # The skipped 02:30 is first proposed by the run at 03:00 CEST (01:00 UTC), then by every
    # run for an hour: 12 runs. The core sends its key (`rem:<task>:2026-03-29T02:30`) once that
    # day: the 11 repeats never reach the notifier.
    proposals = proposing(remind, "⏰ 2026-03-29 02:30")
    assert len(proposals) == 12
    assert proposals[0] == datetime.fromisoformat("2026-03-29 03:00")
    (first,) = [p for p in remind.ntfy.pushes if p.title == "⏰ 2026-03-29 02:30"]
    assert first.delivery == "delayed"


def test_paris_fall_back_day(make: Make) -> None:
    """2026-10-25: 03:00 CEST goes back to 02:00 CET (a 25-hour day), so 02:30 happens twice.
    The reminder at 02:30 fires at the first one only (00:30 UTC), never again at the second
    (01:30 UTC), and arrives at 07:00 CET (06:00 UTC), after the extra hour. Reminders
    after the night, the digest and the overdue nudge fire once, at their wall-clock times."""
    remind = make()
    remind.write(
        "- [ ] Check the boiler 📅 2026-10-25 ⏰ 02:30\n"
        "- [ ] Swap the winter tyres 📅 2026-10-25 ⏰ 07:30\n"
        "- [ ] Send the form 📅 2026-10-24\n"
    )
    remind.travel("2026-10-25 00:00", "2026-10-26 09:30")
    assert log(remind) == [
        "2026-10-25 07:00 delayed ⏰ 2026-10-25 02:30 | Check the boiler",
        "2026-10-25 07:00 sent Today, 2026-10-25 | Overdue: 1 / - Send the form (due 2026-10-24)"
        " / Today: 1 / - 07:30 Swap the winter tyres",
        "2026-10-25 07:30 sent ⏰ 2026-10-25 07:30 | Swap the winter tyres",
        "2026-10-25 09:00 sent Overdue: due 2026-10-24 | Send the form",
        "2026-10-26 07:00 sent Today, 2026-10-26 | Overdue: 3 / - Send the form (due 2026-10-24)"
        " / - Check the boiler (due 2026-10-25) / - Swap the winter tyres (due 2026-10-25)",
        "2026-10-26 09:00 sent Overdue: due 2026-10-25 | Check the boiler",
        "2026-10-26 09:00 sent Overdue: due 2026-10-25 | Swap the winter tyres",
    ]
    first_0230 = local("2026-10-25 02:30")
    assert first_0230.hour == 0  # 00:30 UTC, the CEST one
    proposals = proposing(remind, "⏰ 2026-10-25 02:30")
    assert len(proposals) == 12  # an hour of runs, which spans the repeated hour
    assert proposals[0] == datetime.fromisoformat("2026-10-25 02:30")
    # The core's day-scoped key is one for the whole 25-hour day: one push, 11 repeats dropped.
    (first,) = [p for p in remind.ntfy.pushes if p.title == "⏰ 2026-10-25 02:30"]
    assert first.delivery == "delayed"
    # Of the 25 hours between the two 07:00 digests, none is lost or doubled.
    digests = [p for p in remind.ntfy.phone() if (p.title or "").startswith("Today,")]
    assert [d.delivered for d in digests] == [
        datetime.fromisoformat("2026-10-25 07:00"),
        datetime.fromisoformat("2026-10-26 07:00"),
    ]


# --- daylight saving: America/New_York and a half-hour zone ---------------------------------


def test_new_york_spring_forward_day(make: Make) -> None:
    """2026-03-08: 02:00 EST jumps to 03:00 EDT. The 02:30 reminder is proposed at 03:00 EDT
    (07:00 UTC); quiet hours there are 22:00-07:00 New York time, so it waits for 07:00 EDT."""
    remind = make("America/New_York")
    remind.write(
        "- [ ] Check the boiler 📅 2026-03-08 ⏰ 02:30\n- [ ] Walk the dog 📅 2026-03-08 ⏰ 07:30\n"
    )
    remind.travel("2026-03-08 00:00", "2026-03-08 09:30")
    assert log(remind) == [
        "2026-03-08 07:00 delayed ⏰ 2026-03-08 02:30 | Check the boiler",
        "2026-03-08 07:00 sent Today, 2026-03-08 | Today: 1 / - 07:30 Walk the dog",
        "2026-03-08 07:30 sent ⏰ 2026-03-08 07:30 | Walk the dog",
    ]
    first = next(p for p in remind.ntfy.pushes if p.title == "⏰ 2026-03-08 02:30")
    assert first.proposed == datetime.fromisoformat("2026-03-08 03:00")


def test_new_york_fall_back_day(make: Make) -> None:
    """2026-11-01: 02:00 EDT goes back to 01:00 EST, so 01:30 happens twice. The reminder
    fires at the first 01:30 (05:30 UTC) only; the digest at 07:00 EST (12:00 UTC)."""
    remind = make("America/New_York")
    remind.write(
        "- [ ] Check the boiler 📅 2026-11-01 ⏰ 01:30\n- [ ] Walk the dog 📅 2026-11-01 ⏰ 07:30\n"
    )
    remind.travel("2026-11-01 00:00", "2026-11-01 09:30")
    assert log(remind) == [
        "2026-11-01 07:00 delayed ⏰ 2026-11-01 01:30 | Check the boiler",
        "2026-11-01 07:00 sent Today, 2026-11-01 | Today: 1 / - 07:30 Walk the dog",
        "2026-11-01 07:30 sent ⏰ 2026-11-01 07:30 | Walk the dog",
    ]
    assert len(proposing(remind, "⏰ 2026-11-01 01:30")) == 12  # an hour of runs, across the repeat
    (first,) = [p for p in remind.ntfy.pushes if p.title == "⏰ 2026-11-01 01:30"]
    assert first.delivery == "delayed"
    assert local("2026-11-01 01:30", remind.zone).hour == 5  # the EDT one, 05:30 UTC


def test_half_hour_zone_india(make: Make) -> None:
    """Asia/Kolkata, UTC+5:30 with no daylight saving: the quiet hours, the digest and the
    overnight reminders are on the local clock, whose runs fall on :00/:05 UTC+5:30."""
    remind = make("Asia/Kolkata")
    remind.write(
        "- [ ] Call Mumbai 📅 2026-11-09 ⏰ 22:15\n"
        "- [ ] Prepare the demo 📅 2026-11-10 ⏰ 06:45\n"
        "- [ ] Send the invoice 📅 2026-11-10\n"
    )
    remind.travel("2026-11-09 21:30", "2026-11-10 09:30")
    assert log(remind) == [
        "2026-11-10 07:00 delayed ⏰ 2026-11-09 22:15 | Call Mumbai",
        "2026-11-10 07:00 delayed ⏰ 2026-11-10 06:45 | Prepare the demo",
        # Call Mumbai (overdue) has its own push, so the digest leaves it out.
        "2026-11-10 07:00 sent Today, 2026-11-10 | Today: 1 / - Send the invoice",
        "2026-11-10 09:00 sent Overdue: due 2026-11-09 | Call Mumbai",
    ]
    assert local("2026-11-10 07:00", remind.zone).strftime("%H:%M") == "01:30"  # UTC


def test_half_hour_daylight_saving_lord_howe(make: Make) -> None:
    """Australia/Lord_Howe shifts by 30 minutes: on 2026-10-04, 02:00 jumps to 02:30. A 02:15
    reminder is proposed at 02:30, a 02:45 one at 02:45, both held to 07:00."""
    remind = make("Australia/Lord_Howe")
    remind.write(
        "- [ ] Check the pump 📅 2026-10-04 ⏰ 02:15\n- [ ] Feed the cat 📅 2026-10-04 ⏰ 02:45\n"
    )
    remind.travel("2026-10-04 00:00", "2026-10-04 09:30")
    assert log(remind) == [
        "2026-10-04 07:00 delayed ⏰ 2026-10-04 02:15 | Check the pump",
        "2026-10-04 07:00 delayed ⏰ 2026-10-04 02:45 | Feed the cat",
    ]
    pump = proposing(remind, "⏰ 2026-10-04 02:15")
    assert pump[0] == datetime.fromisoformat("2026-10-04 02:30")
    assert len(pump) == 12
    assert len([p for p in remind.ntfy.pushes if p.title == "⏰ 2026-10-04 02:15"]) == 1


# --- quiet hours 22:00-07:00 -----------------------------------------------------------------

NIGHT = (
    "- [ ] Evening call 📅 2026-11-09 ⏰ 21:55\n"
    "- [ ] Bedtime reading 📅 2026-11-09 ⏰ 22:00\n"
    "- [ ] Night check 📅 2026-11-10 ⏰ 03:00\n"
    "- [ ] Early bird 📅 2026-11-10 ⏰ 06:50\n"
    "- [ ] Last minute 📅 2026-11-10 ⏰ 06:59\n"
    "- [ ] Wake up 📅 2026-11-10 ⏰ 07:00\n"
    "- [ ] Plain chore 📅 2026-11-10\n"
)
DIGEST_0710 = (
    "2026-11-10 07:00 sent Today, 2026-11-10 | Overdue: 1 / - Evening call (due 2026-11-09)"
    " / Today: 2 / - 07:00 Wake up / - Plain chore"
)


def test_quiet_hours_delay_to_seven_and_the_digest_leaves_them_out(make: Make) -> None:
    remind = make()
    remind.write(NIGHT)
    remind.travel("2026-11-09 21:30", "2026-11-10 09:30")
    assert log(remind) == [
        "2026-11-09 21:55 sent ⏰ 2026-11-09 21:55 | Evening call",
        # 22:00 is in the quiet hours, 07:00 is not: [22:00, 07:00).
        "2026-11-10 07:00 delayed ⏰ 2026-11-09 22:00 | Bedtime reading",
        "2026-11-10 07:00 delayed ⏰ 2026-11-10 03:00 | Night check",
        "2026-11-10 07:00 delayed ⏰ 2026-11-10 06:50 | Early bird",
        # 06:59 is quiet, but the next run is at 07:00, which is not: sent, not delayed.
        "2026-11-10 07:00 sent ⏰ 2026-11-10 06:59 | Last minute",
        "2026-11-10 07:00 sent ⏰ 2026-11-10 07:00 | Wake up",
        # The reminders of the night are not repeated in the digest (nor Bedtime reading, which
        # is overdue); the others are listed once.
        DIGEST_0710,
        "2026-11-10 09:00 sent Overdue: due 2026-11-09 | Evening call",
        "2026-11-10 09:00 sent Overdue: due 2026-11-09 | Bedtime reading",
    ]
    text = "\n".join(push.body or "" for push in remind.ntfy.phone())
    for title in ("Night check", "Early bird", "Last minute"):
        assert text.count(title) == 1, title  # one push, not also a digest line
    assert text.count("Bedtime reading") == 2  # its push, then the next morning's nudge


def test_quiet_hours_drop_send_the_night_reminders_in_the_digest(make: Make) -> None:
    """`during: drop`: the notifier would drop a push proposed in the quiet hours, so the
    transform proposes none (nothing is dropped) and the 07:00 digest lists the night's
    reminders, since the previous digest, under "During quiet hours"."""
    remind = make(edit=configure(quiet={"start": "22:00", "end": "07:00", "during": "drop"}))
    remind.write(NIGHT)
    remind.travel("2026-11-09 21:30", "2026-11-10 09:30")
    assert log(remind) == [
        "2026-11-09 21:55 sent ⏰ 2026-11-09 21:55 | Evening call",
        "2026-11-10 07:00 sent ⏰ 2026-11-10 07:00 | Wake up",
        "2026-11-10 07:00 sent Today, 2026-11-10 | Overdue: 1 / - Evening call (due 2026-11-09)"
        " / During quiet hours: 4 / - ⏰ 22:00 Bedtime reading / - ⏰ 03:00 Night check"
        " / - ⏰ 06:50 Early bird / - ⏰ 06:59 Last minute / Today: 2 / - 07:00 Wake up"
        " / - Plain chore",
        "2026-11-10 09:00 sent Overdue: due 2026-11-09 | Evening call",
        "2026-11-10 09:00 sent Overdue: due 2026-11-09 | Bedtime reading",
    ]
    assert not [p for p in remind.ntfy.pushes if p.delivery == "dropped"]


def test_a_dropped_night_reminder_is_still_in_the_digest(make: Make) -> None:
    remind = make(edit=configure(quiet={"start": "22:00", "end": "07:00", "during": "drop"}))
    remind.write("- [ ] Night check 📅 2026-11-10 ⏰ 03:00\n")
    remind.travel("2026-11-10 02:30", "2026-11-10 07:30")
    assert [p for p in remind.ntfy.phone() if p.title == "⏰ 2026-11-10 03:00"] == []
    digest = [p for p in remind.ntfy.phone() if p.title == "Today, 2026-11-10"]
    assert [p.body for p in digest] == ["During quiet hours: 1\n- ⏰ 03:00 Night check"]


def test_critical_pushes_bypass_the_quiet_hours(make: Make) -> None:
    """With `priority: critical` on the notifier, nothing waits for 07:00."""
    remind = make(edit=configure(priority="critical"))
    remind.write(NIGHT)
    remind.travel("2026-11-09 21:30", "2026-11-10 09:30")
    assert log(remind) == [
        "2026-11-09 21:55 sent ⏰ 2026-11-09 21:55 | Evening call",
        "2026-11-09 22:00 sent ⏰ 2026-11-09 22:00 | Bedtime reading",
        "2026-11-10 03:00 sent ⏰ 2026-11-10 03:00 | Night check",
        "2026-11-10 06:50 sent ⏰ 2026-11-10 06:50 | Early bird",
        "2026-11-10 07:00 sent ⏰ 2026-11-10 06:59 | Last minute",
        "2026-11-10 07:00 sent ⏰ 2026-11-10 07:00 | Wake up",
        DIGEST_0710,
        "2026-11-10 09:00 sent Overdue: due 2026-11-09 | Evening call",
        "2026-11-10 09:00 sent Overdue: due 2026-11-09 | Bedtime reading",
    ]
    assert {p.delivery for p in remind.ntfy.pushes} == {"sent"}  # nothing delayed or dropped


# --- late and skipped runs -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resume", "delivered"),
    [
        ("14:35", "14:35"),  # a 25-minute host sleep: the first run after it delivers
        ("15:25", "15:25"),  # the lookback (now - 60 min, now] still holds 14:30
        ("15:30", None),  # one run later it does not: the reminder is lost
    ],
)
def test_a_host_sleep_over_a_reminder(make: Make, resume: str, delivered: str | None) -> None:
    remind = make()
    remind.write("- [ ] Water the plants 📅 2026-11-09 ⏰ 14:30\n")
    remind.travel(
        "2026-11-09 14:00",
        "2026-11-09 15:45",
        skip=asleep(remind, "2026-11-09 14:10", f"2026-11-09 {resume}"),
    )
    expected = (
        []
        if delivered is None
        else [f"2026-11-09 {delivered} sent ⏰ 2026-11-09 14:30 | Water the plants"]
    )
    assert log(remind) == expected


def test_every_other_run_skipped_still_delivers_once(make: Make) -> None:
    remind = make()
    remind.write("- [ ] Water the plants 📅 2026-11-09 ⏰ 14:30\n")
    remind.travel(
        "2026-11-09 14:00", "2026-11-09 15:45", skip=lambda moment: moment.minute % 10 == 0
    )
    assert log(remind) == ["2026-11-09 14:35 sent ⏰ 2026-11-09 14:30 | Water the plants"]
    assert len(proposing(remind, "⏰ 2026-11-09 14:30")) > 1  # proposed by several runs...
    assert len(remind.ntfy.pushes) == 1  # ...and the core lets one through


@pytest.mark.parametrize(("wake", "delivered"), [("07:55", "07:55"), ("08:05", None)])
def test_the_digest_after_a_morning_sleep(make: Make, wake: str, delivered: str | None) -> None:
    """A host asleep through 07:00 sends the digest at wake-up if the lookback still holds
    07:00 (until 07:55); later, that day's digest is lost."""
    remind = make()
    remind.write("- [ ] Plain chore 📅 2026-11-09\n")
    remind.travel(
        "2026-11-09 06:30",
        "2026-11-09 08:30",
        skip=asleep(remind, "2026-11-09 06:40", f"2026-11-09 {wake}"),
    )
    expected = (
        []
        if delivered is None
        else [f"2026-11-09 {delivered} sent Today, 2026-11-09 | Today: 1 / - Plain chore"]
    )
    assert log(remind) == expected


def test_a_night_reminder_found_after_a_late_wake_up_is_sent_at_once(make: Make) -> None:
    """The host wakes at 07:20, after the quiet hours: the 06:30 reminder is not held back
    any more, and the digest does not list it."""
    remind = make()
    remind.write("- [ ] Take the bins out 📅 2026-11-09 ⏰ 06:30\n- [ ] Pay 📅 2026-11-09\n")
    remind.travel(
        "2026-11-09 06:00",
        "2026-11-09 08:00",
        skip=asleep(remind, "2026-11-09 06:20", "2026-11-09 07:20"),
    )
    assert log(remind) == [
        "2026-11-09 07:20 sent ⏰ 2026-11-09 06:30 | Take the bins out",
        "2026-11-09 07:20 sent Today, 2026-11-09 | Today: 1 / - Pay",
    ]


# --- configurations that cross midnight ------------------------------------------------------


def test_a_digest_and_nudges_that_cross_midnight(make: Make) -> None:
    """`digest_at: 23:50` and `overdue_at: 23:30` (for two hours), no quiet hours: the runs
    after midnight still belong to the previous day's digest and nudges, which must not be
    delivered again."""
    remind = make(
        edit=configure(quiet=None, digest_at="23:50", overdue_at="23:30", overdue_for_minutes=120)
    )
    remind.write(
        "- [ ] Send the form 📅 2026-11-08\n"
        "- [ ] Pay the rent 📅 2026-11-09\n"
        "- [ ] Book the doctor 📅 2026-11-10\n"
    )
    remind.travel("2026-11-09 23:00", "2026-11-10 01:45")
    digests = proposing(remind, "Today, 2026-11-09")
    assert digests[0] == datetime.fromisoformat("2026-11-09 23:50")
    assert digests[-1] == datetime.fromisoformat("2026-11-10 00:45")  # lookback ends
    assert log(remind) == [
        "2026-11-09 23:30 sent Overdue: due 2026-11-08 | Send the form",
        "2026-11-09 23:50 sent Today, 2026-11-09 | Overdue: 1 / - Send the form (due 2026-11-08)"
        " / Today: 1 / - Pay the rent",
    ]


def test_quiet_hours_that_end_after_midnight(make: Make) -> None:
    """Quiet hours 23:00-01:00: the digest at 23:50, a 23:30 and a 00:30 reminder all wait
    for 01:00 and arrive once; the digest leaves out the reminder that has its own push."""
    quiet = {"start": "23:00", "end": "01:00", "during": "delay"}
    remind = make(edit=configure(quiet=quiet, digest_at="23:50"))
    remind.write(
        "- [ ] Lock up 📅 2026-11-09 ⏰ 23:30\n"
        "- [ ] Check the oven 📅 2026-11-10 ⏰ 00:30\n"
        "- [ ] Pay the rent 📅 2026-11-09\n"
    )
    remind.travel("2026-11-09 22:30", "2026-11-10 02:00")
    assert log(remind) == [
        "2026-11-10 01:00 delayed ⏰ 2026-11-09 23:30 | Lock up",
        "2026-11-10 01:00 delayed Today, 2026-11-09 | Today: 1 / - Pay the rent",
        "2026-11-10 01:00 delayed ⏰ 2026-11-10 00:30 | Check the oven",
    ]


def test_a_late_evening_reminder_is_delivered_once_with_the_default_quiet_hours(
    make: Make,
) -> None:
    """The default configuration: a ⏰ 23:45 reminder is held to 07:00, once; its lookback
    runs past midnight, whose runs must not queue it a second time."""
    remind = make()
    remind.write("- [ ] Late call 📅 2026-11-09 ⏰ 23:45\n")
    remind.travel("2026-11-09 23:30", "2026-11-10 08:00")
    assert log(remind) == ["2026-11-10 07:00 delayed ⏰ 2026-11-09 23:45 | Late call"]


# --- a daily recurring reminder over three days ----------------------------------------------


def _pills(day: str, next_day: str | None) -> str:
    done = f"- [x] Take the pills 📅 {day} ⏰ 08:00 🔁 every day ✅ {day}\n"
    nxt = (
        f"- [ ] Take the pills 📅 {next_day} ⏰ 08:00 🔁 every day\n"
        if next_day is not None
        else ""
    )
    return nxt + done


def test_a_daily_recurring_reminder_over_three_days(make: Make) -> None:
    """Each morning at 08:00 the reminder fires, 24 hours after the last one: the daily
    dedupe does not swallow it. The user ticks it at 08:10, which creates tomorrow's task."""
    remind = make()
    remind.write("- [ ] Take the pills 📅 2026-11-09 ⏰ 08:00 🔁 every day\n")

    def tick(day: str, next_day: str) -> Callable[[], None]:
        return lambda: remind.write(_pills(day, next_day))

    remind.travel(
        "2026-11-09 06:30",
        "2026-11-12 09:30",
        edits={
            "2026-11-09 08:10": tick("2026-11-09", "2026-11-10"),
            "2026-11-10 08:10": tick("2026-11-10", "2026-11-11"),
            "2026-11-11 08:10": tick("2026-11-11", "2026-11-12"),
        },
    )
    assert log(remind) == [
        "2026-11-09 07:00 sent Today, 2026-11-09 | Today: 1 / - 08:00 Take the pills",
        "2026-11-09 08:00 sent ⏰ 2026-11-09 08:00 | Take the pills",
        "2026-11-10 07:00 sent Today, 2026-11-10 | Today: 1 / - 08:00 Take the pills",
        "2026-11-10 08:00 sent ⏰ 2026-11-10 08:00 | Take the pills",
        "2026-11-11 07:00 sent Today, 2026-11-11 | Today: 1 / - 08:00 Take the pills",
        "2026-11-11 08:00 sent ⏰ 2026-11-11 08:00 | Take the pills",
        "2026-11-12 07:00 sent Today, 2026-11-12 | Today: 1 / - 08:00 Take the pills",
        "2026-11-12 08:00 sent ⏰ 2026-11-12 08:00 | Take the pills",
    ]
    reminders = [p for p in remind.ntfy.phone() if (p.title or "").startswith("⏰")]
    gaps = {b.delivered - a.delivered for a, b in pairwise(reminders)}
    assert gaps == {timedelta(days=1)}


# --- ticked mid-day: never again -------------------------------------------------------------


def test_tasks_ticked_during_the_day_never_remind_afterwards(make: Make) -> None:
    remind = make()
    remind.write(
        "- [ ] Before its time 📅 2026-11-09 ⏰ 15:00\n"
        "- [ ] After its time 📅 2026-11-09 ⏰ 10:00\n"
        "- [ ] Overdue one 📅 2026-11-08\n"
        "- [ ] Overdue two 📅 2026-11-08\n"
        "- [ ] Left open 📅 2026-11-09 ⏰ 16:00\n"
    )

    def tick(*names: str) -> Callable[[], None]:
        def edit() -> None:
            text = remind.vault.joinpath("Tasks.md").read_text(encoding="utf-8")
            for name in names:
                text = text.replace(f"- [ ] {name}", f"- [x] {name}")
            remind.write(text)

        return edit

    remind.travel(
        "2026-11-09 06:00",
        "2026-11-10 09:30",
        edits={
            "2026-11-09 08:40": tick("Overdue two"),  # before its nudge: none at all
            "2026-11-09 09:20": tick("Overdue one"),  # after its nudge: no other
            "2026-11-09 10:20": tick("After its time"),
            "2026-11-09 12:00": tick("Before its time"),
        },
    )
    assert log(remind) == [
        "2026-11-09 07:00 sent Today, 2026-11-09 | Overdue: 2 / - Overdue one (due 2026-11-08)"
        " / - Overdue two (due 2026-11-08) / Today: 3 / - 10:00 After its time"
        " / - 15:00 Before its time / - 16:00 Left open",
        "2026-11-09 09:00 sent Overdue: due 2026-11-08 | Overdue one",
        "2026-11-09 10:00 sent ⏰ 2026-11-09 10:00 | After its time",
        "2026-11-09 16:00 sent ⏰ 2026-11-09 16:00 | Left open",
        # The next morning: only the task left open is overdue.
        "2026-11-10 07:00 sent Today, 2026-11-10 | Overdue: 1 / - Left open (due 2026-11-09)",
        "2026-11-10 09:00 sent Overdue: due 2026-11-09 | Left open",
    ]
