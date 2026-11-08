# SPDX-License-Identifier: Apache-2.0
"""The reference implementation of the `obsidian.reminders` transform (README "The transform").

The `remind` pipeline needs a deterministic transform that turns `obsidian.open_tasks` into
`notify.push` intents. No plugin provides one yet, and a pack has no runtime to ship one, so
this module is the **contract by example**: the tests run the pipeline with it standing in
for the planned transform, and plugin-obsidian can port it as is (it uses only the standard
library). It reads no clock, file or environment: everything comes from its parameters (the
pipeline passes `{{now}}` and `{{timezone}}`) and its input, so a run and its replay propose
the same pushes.

Behaviour (every time is a wall-clock time in `timezone`, read with the zone's rules):

- **Open tasks only.** A task is considered only when its status character is one of the
  source's `open_statuses` (default `" /"`) and it is neither `done` nor `cancelled` (`x`,
  `X`, `-`): a ticked or cancelled task never reminds, even if a source returned it.
- **The window** is `(end - window_minutes, end]`, `end` being `now` rounded down to the
  minute: with a run every 5 minutes, consecutive windows tile the day, so each minute
  belongs to exactly one run.
- **`⏰` reminders**: `⏰ HH:MM` is that time on the due date (no due date: no reminder),
  `⏰ YYYY-MM-DD HH:MM` that date and time. One push when the reminder's instant is in the
  window. A time in a skipped hour (spring forward) is the first instant after the gap; a
  time in a repeated hour (fall back) is its first occurrence: Debian cron's rules, as the
  core's scheduler (schemas decision 49 B). A date-only `⏰ YYYY-MM-DD` has no time: the task
  is listed in that day's digest.
- **Snoozed** tasks (the source's `snoozed`, or a `⏰` date or date-time after `now`) get no
  digest line and no overdue nudge; their reminder fires when the snooze ends.
- **The digest**: one push when `digest_at` falls in the window, listing the overdue tasks,
  those due today and those with a reminder today; none when the list is empty.
- **Overdue nudges**: one push per overdue task (due before today), proposed by every run
  from `overdue_at` for `overdue_for_minutes`, oldest first, at most `max_overdue`. The text
  is the same in every run, so the notifier's daily dedupe sends each nudge once a day.
- **Stable text**: titles and bodies hold the task's description, dates and times, never a
  countdown or the run's time, so the same reminder proposed twice is the same push.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REF = "obsidian.reminders"
SOURCE = "obsidian.open_tasks"

DEFAULT_OPEN_STATUSES = " /"
CLOSED_STATUSES = frozenset({"done", "cancelled"})
CLOSED_CHARS = frozenset("xX-")

MAX_WINDOW_MINUTES = 60
MAX_OVERDUE = 100
MAX_DIGEST_LINES = 25
MAX_DESCRIPTION = 200
"""Characters of a description in a push; longer ones are cut with `…`."""
MAX_TITLE = 500
MAX_BODY = 4096

_TIME = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]+")
_KNOWN = {
    "now",
    "timezone",
    "window_minutes",
    "digest_at",
    "overdue_at",
    "overdue_for_minutes",
    "max_overdue",
}


class ReminderError(ValueError):
    """The parameters or the input are not what the transform expects (the call fails)."""


@dataclass(frozen=True)
class Params:
    now: datetime
    zone: ZoneInfo
    window_minutes: int = 5
    digest_at: time | None = None
    overdue_at: time | None = None
    overdue_for_minutes: int = 60
    max_overdue: int = 20


def _int(params: Mapping[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ReminderError(f"{name} must be an integer from {low} to {high}")
    return value


def _clock_time(params: Mapping[str, Any], name: str) -> time | None:
    value = params.get(name)
    if value is None:
        return None
    match = _TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ReminderError(f'{name} must be a time "HH:MM"')
    return time(int(match.group(1)), int(match.group(2)))


def parse_params(params: Mapping[str, Any]) -> Params:
    """The transform's parameters; an unknown or invalid one is an error (never ignored)."""
    unknown = sorted(set(params) - _KNOWN)
    if unknown:
        raise ReminderError(f"unknown parameters: {', '.join(unknown)}")
    raw_now = params.get("now")
    try:
        now = datetime.fromisoformat(raw_now) if isinstance(raw_now, str) else None
    except ValueError:
        now = None
    if now is None or now.tzinfo is None:
        raise ReminderError("now must be an RFC 3339 time with an offset (`{{now}}`)")
    name = params.get("timezone")
    try:
        zone = ZoneInfo(name) if isinstance(name, str) and name else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    if zone is None:
        raise ReminderError("timezone must be an IANA time zone (`{{timezone}}`)")
    return Params(
        now=now.astimezone(UTC),
        zone=zone,
        window_minutes=_int(params, "window_minutes", 5, 1, MAX_WINDOW_MINUTES),
        digest_at=_clock_time(params, "digest_at"),
        overdue_at=_clock_time(params, "overdue_at"),
        overdue_for_minutes=_int(params, "overdue_for_minutes", 60, 1, 24 * 60),
        max_overdue=_int(params, "max_overdue", 20, 0, MAX_OVERDUE),
    )


def instant(day: date, at: time, zone: ZoneInfo) -> datetime:
    """The UTC instant of wall-clock `at` on `day` in `zone`: the first occurrence of a
    repeated time, the first instant after the gap for a skipped one."""
    naive = datetime.combine(day, at)
    moment = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if moment.astimezone(zone).replace(tzinfo=None) == naive:
        return moment
    # In a gap: `fold=0` read it with the offset before the transition, which lands after the
    # gap. Walk back to the transition (the first instant whose wall clock is past `naive`).
    step = timedelta(minutes=1)
    for _ in range(48 * 60):
        if (moment - step).astimezone(zone).replace(tzinfo=None) < naive:
            return moment
        moment -= step
    return moment  # pragma: no cover - no zone has a gap of two days


def _day(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReminderError("a date is a YYYY-MM-DD string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ReminderError("a date is a YYYY-MM-DD string") from None


def _hhmm(value: object) -> time | None:
    if value is None:
        return None
    match = _TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ReminderError('a reminder time is "HH:MM"')
    return time(int(match.group(1)), int(match.group(2)))


def clean(text: object, limit: int = MAX_DESCRIPTION) -> str:
    """Untrusted text as one line: control characters and line breaks become one space, cut
    to `limit` characters with `…`. Never empty."""
    line = _UNSAFE.sub(" ", text if isinstance(text, str) else "").strip()
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line or "(no description)"


@dataclass(frozen=True)
class Task:
    """What the transform reads of one `open_tasks` entry."""

    description: str
    due: date | None
    reminder_day: date | None
    reminder_time: time | None
    snoozed: bool

    def reminder_at(self, zone: ZoneInfo) -> datetime | None:
        """The instant of the `⏰` reminder, if it has a time and a day."""
        day = self.reminder_day or self.due
        if self.reminder_time is None or day is None:
            return None
        return instant(day, self.reminder_time, zone)


def _is_open(raw: Mapping[str, Any], open_statuses: str) -> bool:
    char = raw.get("status_char")
    return (
        isinstance(char, str)
        and len(char) == 1
        and char in open_statuses
        and char not in CLOSED_CHARS
        and raw.get("status") not in CLOSED_STATUSES
    )


def _task(raw: Mapping[str, Any], params: Params, today: date) -> Task:
    reminder = raw.get("reminder")
    if reminder is not None and not isinstance(reminder, Mapping):
        raise ReminderError("a reminder is an object")
    reminder = reminder or {}
    reminder_day = _day(reminder.get("date"))
    reminder_time = _hhmm(reminder.get("time"))
    snoozed = raw.get("snoozed") is True
    if reminder_day is not None:
        if reminder_time is None:
            snoozed = snoozed or reminder_day > today
        else:
            snoozed = snoozed or instant(reminder_day, reminder_time, params.zone) > params.now
    return Task(
        description=clean(raw.get("description")),
        due=_day(raw.get("due")),
        reminder_day=reminder_day,
        reminder_time=reminder_time,
        snoozed=snoozed,
    )


def _push(title: str, body: str) -> dict[str, Any]:
    return {"type": "notify.push", "title": clean(title, MAX_TITLE), "body": body[:MAX_BODY]}


def _digest_line(task: Task, today: date, zone: ZoneInfo) -> str:
    if task.due is not None and task.due < today:
        return f"- {task.description} (overdue, due {task.due.isoformat()})"
    at = task.reminder_at(zone)
    if at is not None and task.reminder_time is not None and at.astimezone(zone).date() == today:
        return f"- {task.reminder_time:%H:%M} {task.description}"
    return f"- {task.description}"


def _digest(tasks: list[Task], today: date, zone: ZoneInfo, more: int) -> dict[str, Any] | None:
    def listed(task: Task) -> bool:
        if task.snoozed:
            return False
        if task.due is not None and task.due <= today:
            return True
        at = task.reminder_at(zone)
        reminds_today = at is not None and at.astimezone(zone).date() == today
        return reminds_today or (task.reminder_time is None and task.reminder_day == today)

    chosen = [task for task in tasks if listed(task)]
    if not chosen:
        return None

    def order(task: Task) -> tuple[int, str, datetime]:
        if task.due is not None and task.due < today:
            return (0, task.due.isoformat(), datetime.min.replace(tzinfo=UTC))
        at = task.reminder_at(zone)
        return (1, "", datetime.max.replace(tzinfo=UTC) if at is None else at)

    chosen.sort(key=order)
    lines = [_digest_line(task, today, zone) for task in chosen[:MAX_DIGEST_LINES]]
    left = len(chosen) - len(lines)
    if left:
        lines.append(f"…and {left} more")
    if more:
        lines.append(f"({more} open tasks were not read)")
    return _push(f"Today, {today.isoformat()}", "\n".join(lines))


def _tasks(input: Any, params: Params, today: date) -> tuple[list[Task], int, int]:
    """The open tasks of every `open_tasks` source in `input`, the malformed ones skipped
    (counted), and how many the sources left out (`more`)."""
    if not isinstance(input, list):
        raise ReminderError("the input is the collect step's list of sources")
    sources = [e for e in input if isinstance(e, Mapping) and e.get("source") == SOURCE]
    if not sources:
        raise ReminderError(f"the input has no {SOURCE} data")
    tasks: list[Task] = []
    invalid = more = 0
    for source in sources:
        data = source.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("tasks"), list):
            raise ReminderError(f"{SOURCE} data has no task list")
        statuses = data.get("open_statuses", DEFAULT_OPEN_STATUSES)
        if not isinstance(statuses, str):
            raise ReminderError("open_statuses is a string")
        count = data.get("more", 0)
        more += count if isinstance(count, int) and not isinstance(count, bool) else 0
        for raw in data["tasks"]:
            if not isinstance(raw, Mapping) or not _is_open(raw, statuses):
                continue
            try:
                tasks.append(_task(raw, params, today))
            except ReminderError:
                invalid += 1
    return tasks, invalid, more


def reminders(params: Mapping[str, Any], input: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`(data, intents)`: a summary of what was decided, and the `notify.push` proposals in
    a fixed order (reminders by time, the digest, overdue nudges oldest first)."""
    p = parse_params(params)
    end = p.now.replace(second=0, microsecond=0)
    start = end - timedelta(minutes=p.window_minutes)
    today = end.astimezone(p.zone).date()
    tasks, invalid, more = _tasks(input, p, today)

    def in_window(moment: datetime | None) -> bool:
        return moment is not None and start < moment <= end

    due_now = sorted(
        (
            (at, task)
            for task in tasks
            if (at := task.reminder_at(p.zone)) is not None and in_window(at)
        ),
        key=lambda pair: pair[0],
    )
    # The time as written in the task (a skipped 02:30 still reads 02:30, pushed at 03:00).
    pushes = [_push(f"⏰ {task.reminder_time:%H:%M}", task.description) for _, task in due_now]
    digest = None
    if p.digest_at is not None and in_window(instant(today, p.digest_at, p.zone)):
        digest = _digest(tasks, today, p.zone, more)
        if digest is not None:
            pushes.append(digest)
    overdue = [t for t in tasks if not t.snoozed and t.due is not None and t.due < today]
    nudged: list[Task] = []
    if p.overdue_at is not None:
        opens = instant(today, p.overdue_at, p.zone)
        if opens <= end < opens + timedelta(minutes=p.overdue_for_minutes):
            overdue.sort(key=lambda task: task.due or date.max)
            nudged = overdue[: p.max_overdue]
    pushes += [
        _push(f"Overdue: due {task.due.isoformat()}", task.description)
        for task in nudged
        if task.due is not None
    ]
    data = {
        "window": {"start": _rfc3339(start), "end": _rfc3339(end)},
        "today": today.isoformat(),
        "open_tasks": len(tasks),
        "invalid_tasks": invalid,
        "reminders": len(due_now),
        "digest": digest is not None,
        "overdue": len(overdue),
        "overdue_nudges": len(nudged),
        "pushes": len(pushes),
    }
    return data, pushes


def _rfc3339(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


__all__ = ["REF", "SOURCE", "ReminderError", "clean", "instant", "parse_params", "reminders"]
