# SPDX-License-Identifier: Apache-2.0
"""`pipelines/remind.yaml` end to end through the core's `Runner` (tests/pack.py says what is
real and what stands in), over a fixture vault read by plugin-obsidian's real parser.

The day is Monday 2026-11-09 in Paris unless a test says otherwise. Time-travel across many
days, every daylight-saving rule and every quiet-hours edge are #3's; here are the basics.
"""

from datetime import datetime

import pytest
from horae.engine.testing import FixturePlugins

from pack import NOTIFIER, NTFY, OBSIDIAN_RELEASED, Remind, local, manifest, notify_params
from reminders_reference import SOURCE

TODAY = "2026-11-09"

OPEN = f"""# Tasks

- [ ] Call the plumber 📅 {TODAY} ⏰ 14:30
- [ ] Send the insurance form 📅 2026-11-05
- [ ] Renew the passport 📅 2026-11-08
- [ ] Book the train tickets 📅 {TODAY}
- [ ] Read the onboarding guide 📅 2026-11-20
"""

TICKED = f"""# Done and cancelled

- [x] Ticked reminder 📅 {TODAY} ⏰ 14:30 ✅ {TODAY}
- [X] Ticked upper case 📅 {TODAY} ⏰ 14:30 ✅ {TODAY}
- [-] Cancelled reminder 📅 {TODAY} ⏰ 14:30 ❌ {TODAY}
- [x] Ticked with a date-time ⏰ {TODAY} 14:30 ✅ {TODAY}
- [x] Ticked overdue 📅 2026-11-01 ✅ 2026-11-02
- [-] Cancelled overdue 📅 2026-11-01 ❌ 2026-11-02
- [x] Ticked due today 📅 {TODAY} ✅ {TODAY}
* [x] Ticked star bullet 📅 2026-11-01 ⏰ 14:30
1. [x] Ticked numbered 📅 {TODAY} ⏰ 14:30
- [>] Forwarded custom status 📅 2026-11-01 ⏰ 14:30
- [ ] Open parent 📅 2026-11-20
  - [x] Ticked child 📅 {TODAY} ⏰ 14:30
> - [x] Ticked in a quote 📅 {TODAY} ⏰ 14:30
"""
CLOSED_WORDS = ("Ticked", "Cancelled", "Forwarded")


def _all_text(remind: Remind) -> list[str]:
    return [
        f"{title}\n{body}" for record in remind.records for title, body in remind.proposed(record)
    ]


# --- acceptance: ticked tasks never remind ----------------------------------------------------


def test_ticked_and_cancelled_tasks_never_remind(remind: Remind) -> None:
    remind.write(OPEN)
    remind.write(TICKED, "Closed.md")
    for at in ("07:00", "09:00", "09:30", "14:30", "23:55"):
        remind.run(f"{TODAY} {at}")
    texts = _all_text(remind)
    # The open tasks did remind (the runs worked), the closed ones never did.
    assert any("Call the plumber" in text for text in texts)
    assert any("Send the insurance form" in text for text in texts)
    for text in texts:
        assert not any(word in text for word in CLOSED_WORDS), text
    assert not any(word in push.body for push in remind.ntfy.pushes for word in CLOSED_WORDS)


def test_a_task_ticked_before_its_time_does_not_remind(remind: Remind) -> None:
    remind.write(OPEN)
    remind.run(f"{TODAY} 14:25")
    remind.write(OPEN.replace("- [ ] Call the plumber", "- [x] Call the plumber"))
    record = remind.run(f"{TODAY} 14:30")
    assert remind.proposed(record) == []
    assert remind.ntfy.pushes == []


def test_a_ticked_overdue_task_stops_being_nudged(remind: Remind) -> None:
    remind.write(OPEN)
    first = remind.run(f"{TODAY} 09:00")
    assert ("Overdue: due 2026-11-05", "Send the insurance form") in remind.proposed(first)
    remind.write(OPEN.replace("- [ ] Send the insurance", "- [x] Send the insurance"))
    later = remind.run(f"{TODAY} 09:05")
    assert all("insurance" not in body for _, body in remind.proposed(later))
    # The next day it is still ticked: no nudge either.
    assert all(
        "insurance" not in body for _, body in remind.proposed(remind.run("2026-11-10 09:00"))
    )


# --- acceptance: no LLM call ever ------------------------------------------------------------


def test_no_run_ever_calls_an_llm(remind: Remind) -> None:
    """The instance declares no model at all, and the LLM port is a guard that fails the test
    if called: across a day of reminders, digest and nudges, nothing reaches it."""
    assert remind.instance.config.models == {}
    remind.write(OPEN)
    remind.every_5_minutes(f"{TODAY} 06:50", f"{TODAY} 09:10")
    remind.run(f"{TODAY} 14:30")
    assert remind.llm.requests == []
    assert remind.ntfy.phone()  # it did push
    for record in remind.records:
        assert record.llm == []
        assert record.input_hash is None
        assert record.cost.usd == 0
        assert record.cost.tokens_in == record.cost.tokens_out == 0
        assert [step.kind for step in record.steps] == ["collect", "transform", "notify"]


# --- reminders fire in the right window -----------------------------------------------------


@pytest.mark.parametrize(
    ("reminder", "fires_at"),
    [("14:30", "14:30"), ("14:26", "14:30"), ("14:29", "14:30"), ("14:31", "14:35")],
)
def test_a_reminder_fires_in_the_run_whose_window_holds_it(
    remind: Remind, reminder: str, fires_at: str
) -> None:
    """The window is (now - 5 min, now], now rounded down to the minute."""
    remind.write(f"- [ ] Water the plants 📅 {TODAY} ⏰ {reminder}\n")
    remind.every_5_minutes(f"{TODAY} 14:15", f"{TODAY} 14:45")
    fired = [
        record.started_at
        for record in remind.records
        if ("⏰ " + reminder, "Water the plants") in remind.proposed(record)
    ]
    assert fired == [local(f"{TODAY} {fires_at}")]


def test_a_run_a_few_seconds_late_keeps_its_window(remind: Remind) -> None:
    remind.write(f"- [ ] Water the plants 📅 {TODAY} ⏰ 14:30\n")
    remind.run(local(f"{TODAY} 14:25").replace(second=40))
    assert remind.ntfy.pushes == []
    record = remind.run(local(f"{TODAY} 14:30").replace(second=59))
    assert remind.proposed(record) == [("⏰ 14:30", "Water the plants")]


def test_a_reminder_date_time_fires_on_its_day_only(remind: Remind) -> None:
    remind.write("- [ ] Pick up the parcel ⏰ 2026-11-10 08:15\n")
    remind.run(f"{TODAY} 08:15")
    assert remind.ntfy.pushes == []
    remind.run("2026-11-10 08:15")
    assert [(p.title, p.body) for p in remind.ntfy.phone()] == [("⏰ 08:15", "Pick up the parcel")]


def test_without_a_due_date_a_bare_reminder_time_never_fires(remind: Remind) -> None:
    remind.write("- [ ] No day for this ⏰ 14:30\n")
    remind.run(f"{TODAY} 14:30")
    assert remind.ntfy.pushes == []


# --- the morning digest ---------------------------------------------------------------------


def test_the_digest_is_pushed_once_at_seven(remind: Remind) -> None:
    remind.write(OPEN)
    remind.write(TICKED, "Closed.md")
    remind.every_5_minutes(f"{TODAY} 06:45", f"{TODAY} 07:20")
    digests = [p for p in remind.ntfy.pushes if p.title == f"Today, {TODAY}"]
    assert len(digests) == 1
    assert digests[0].proposed == datetime.fromisoformat(f"{TODAY} 07:00")
    assert digests[0].delivery == "sent"  # 07:00 is the end of quiet hours, not in them
    assert digests[0].body == (
        "- Send the insurance form (overdue, due 2026-11-05)\n"
        "- Renew the passport (overdue, due 2026-11-08)\n"
        "- 14:30 Call the plumber\n"
        "- Book the train tickets"
    )


def test_no_digest_on_an_empty_day(remind: Remind) -> None:
    remind.write("- [ ] Read the onboarding guide 📅 2026-11-20\n")
    record = remind.run(f"{TODAY} 07:00")
    assert remind.proposed(record) == []


# --- overdue: one nudge a day ---------------------------------------------------------------


def test_one_overdue_nudge_per_task_per_day(remind: Remind) -> None:
    remind.write(OPEN)
    for day in (TODAY, "2026-11-10"):
        remind.every_5_minutes(f"{day} 08:30", f"{day} 10:30")
    nudges = [p for p in remind.ntfy.pushes if p.title is not None and "Overdue" in p.title]
    # Proposed by every run from 09:00 for an hour (12 runs a day), always the same text...
    proposed_at = sorted({p.proposed for p in nudges})
    assert len(proposed_at) == 24
    assert all(at.hour == 9 for at in proposed_at)
    # ...so plugin-ntfy's daily dedupe lets one per task through each day.
    phone = [(p.delivered.isoformat(), p.title, p.body) for p in remind.ntfy.phone() if p in nudges]
    assert phone == [
        (f"{TODAY}T09:00:00", "Overdue: due 2026-11-05", "Send the insurance form"),
        (f"{TODAY}T09:00:00", "Overdue: due 2026-11-08", "Renew the passport"),
        ("2026-11-10T09:00:00", "Overdue: due 2026-11-05", "Send the insurance form"),
        ("2026-11-10T09:00:00", "Overdue: due 2026-11-08", "Renew the passport"),
        # Due on the 9th: overdue from the 10th.
        ("2026-11-10T09:00:00", "Overdue: due 2026-11-09", "Call the plumber"),
        ("2026-11-10T09:00:00", "Overdue: due 2026-11-09", "Book the train tickets"),
    ]


def test_a_missed_nine_o_clock_run_is_caught_up(remind: Remind) -> None:
    remind.write(OPEN)
    remind.every_5_minutes(f"{TODAY} 09:20", f"{TODAY} 09:30")
    first = [p for p in remind.ntfy.phone() if p.title == "Overdue: due 2026-11-05"]
    assert [p.delivered.isoformat() for p in first] == [f"{TODAY}T09:20:00"]


def test_a_snoozed_task_is_not_nudged_until_its_snooze_ends(remind: Remind) -> None:
    remind.write(
        "- [ ] Snoozed to tomorrow 📅 2026-11-01 ⏰ 2026-11-10\n"
        f"- [ ] Snoozed to the afternoon 📅 2026-11-01 ⏰ {TODAY} 16:00\n"
    )
    remind.run(f"{TODAY} 07:00")
    remind.run(f"{TODAY} 09:00")
    assert remind.ntfy.pushes == []
    remind.run(f"{TODAY} 16:00")
    assert [(p.title, p.body) for p in remind.ntfy.pushes] == [
        ("⏰ 16:00", "Snoozed to the afternoon")
    ]
    remind.run("2026-11-10 07:00")
    (digest,) = remind.ntfy.pushes[1:]
    assert digest.title == "Today, 2026-11-10"
    assert "Snoozed to tomorrow" in digest.body


# --- quiet hours and daylight saving (basics; #3 has the full suite) ------------------------


def test_a_reminder_in_quiet_hours_is_delayed_to_their_end(remind: Remind) -> None:
    remind.write(f"- [ ] Take the bins out 📅 {TODAY} ⏰ 23:00\n")
    remind.every_5_minutes(f"{TODAY} 22:50", f"{TODAY} 23:10")
    (push,) = remind.ntfy.pushes
    assert push.delivery == "delayed"
    assert push.delivered == datetime.fromisoformat("2026-11-10 07:00")
    # The quiet hours are the pipeline's trusted parameters, handed to the notifier.
    assert remind.ntfy.params == [notify_params()]


def test_every_push_goes_to_the_one_notifier_entry(remind: Remind) -> None:
    remind.write(OPEN)
    record = remind.run(f"{TODAY} 09:00")
    assert record.intents
    for proposed in record.intents:
        assert proposed.proposed_by == "obsidian.reminders"
        assert proposed.applied_by == NOTIFIER
        assert proposed.intent.type == "notify.push"
        assert getattr(proposed.intent, "link", None) is None


def test_spring_forward_a_skipped_reminder_fires_once_after_the_gap(remind: Remind) -> None:
    """2026-03-29 in Paris: 02:00 CET jumps to 03:00 CEST. A ⏰ 02:30 fires once, at the
    first instant after the gap (Debian cron, as the core's scheduler); the digest once."""
    remind.write("- [ ] Check the boiler 📅 2026-03-29 ⏰ 02:30\n")
    remind.every_5_minutes("2026-03-29 00:00", "2026-03-29 07:30")
    texts = [(r.started_at, remind.proposed(r)) for r in remind.records if remind.proposed(r)]
    assert texts == [
        (local("2026-03-29 03:00"), [("⏰ 02:30", "Check the boiler")]),
        (local("2026-03-29 07:00"), [("Today, 2026-03-29", "- 02:30 Check the boiler")]),
    ]


def test_fall_back_a_repeated_reminder_fires_once(remind: Remind) -> None:
    """2026-10-25 in Paris: 03:00 CEST goes back to 02:00 CET, so 02:30 happens twice. The
    reminder fires at the first one only; the digest once."""
    remind.write("- [ ] Check the boiler 📅 2026-10-25 ⏰ 02:30\n")
    remind.every_5_minutes("2026-10-25 00:00", "2026-10-25 07:30")
    fired = [r.started_at for r in remind.records if remind.proposed(r)]
    first_0230 = local("2026-10-25 02:30")  # fold 0: 02:30 CEST, 00:30 UTC
    assert fired == [first_0230, local("2026-10-25 07:00")]
    assert first_0230.hour == 0


def test_the_real_source_does_not_even_collect_closed_tasks(remind: Remind) -> None:
    """plugin-obsidian's parser, on the fixture: of the closed note, only the open parent is
    collected (the transform's own status check is the second line of defence)."""
    remind.write(TICKED, "Closed.md")
    record = remind.run(f"{TODAY} 14:30")
    (snapshot,) = record.inputs
    assert [t["description"] for t in snapshot.data["tasks"]] == ["Open parent"]


def test_with_the_released_plugin_obsidian_the_run_fails_at_planning(remind: Remind) -> None:
    """The gap (README "Gaps"): plugin-obsidian 0.1.0 provides no `obsidian.reminders`, so
    the run stops before any plugin is called and pushes nothing."""
    remind.plugins = FixturePlugins()
    remind.plugins.add(OBSIDIAN_RELEASED, {SOURCE: lambda params, input: {"tasks": []}})
    remind.plugins.add(NTFY)
    remind.plugins.add(manifest())
    remind.write(OPEN)
    record = remind.run(f"{TODAY} 09:00", check=False)
    assert record.status == "failed"
    assert [step.status for step in record.steps] == ["skipped", "failed", "skipped"]
    error = record.steps[1].error
    assert error is not None
    assert error.kind == "tool_not_provided"
    assert remind.plugins.calls == []
    assert remind.ntfy.pushes == []
