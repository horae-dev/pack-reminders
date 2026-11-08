<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# pack-reminders

Horae pipeline pack: reminders, daily digest and overdue nudges from the vault, without an LLM.

> 🚧 **Early (0.1.0, unreleased), and it cannot run yet.** The pipeline needs a deterministic transform, `obsidian.reminders`, that no plugin provides yet and that a pack cannot ship (it has no runtime): see [Gaps](#gaps). Until it exists, `horae run remind` fails at planning (`tool_not_provided`, tested). The pipeline passes its checks against the core's `main`, through the core's `Runner`, with plugin-obsidian's real task parser and a reference implementation of the transform ([`tests/reminders_reference.py`](tests/reminders_reference.py)) standing in.

## What it does

Every 5 minutes the `remind` pipeline reads your open Obsidian tasks and pushes, through ntfy:

- **`⏰` reminders**, at their time: `- [ ] Call the plumber 📅 2026-11-09 ⏰ 14:30` sends `⏰ 14:30` / `Call the plumber` at 14:30;
- **the morning digest** at 07:00: overdue tasks, then those due today or with a reminder today;
- **one overdue nudge a day** per overdue task, at 09:00;
- nothing during **quiet hours** (22:00 to 07:00): a push is delayed to 07:00.

A ticked `[x]` or cancelled `[-]` task never reminds, and a snoozed one waits for its snooze to end. **No run ever calls a model**: the pipeline has no `llm` step.

## What is in the pack

A pack is a plugin with no runtime: it ships files, not a process (`plugin.yaml` has no `runtime`, so no network, secret, path or intent either).

| File | What it is |
|---|---|
| [`plugin.yaml`](plugin.yaml) | the manifest: provides the pipeline `remind`, and asks for no permission |
| [`pipelines/remind.yaml`](pipelines/remind.yaml) | the pipeline: `collect` → `transform` → `notify`, cron `*/5 * * * *` |
| [`tests/reminders_reference.py`](tests/reminders_reference.py) | the reference implementation of the `obsidian.reminders` transform (test code, not shipped: the contract by example for plugin-obsidian) |

### The pipeline

```yaml
name: remind
trigger: { cron: "*/5 * * * *" }
steps:
  - collect:
      - obsidian.open_tasks: {}
  - transform:
      - obsidian.reminders:
          now: "{{now}}"
          timezone: "{{timezone}}"
          window_minutes: 5
          digest_at: "07:00"
          overdue_at: "09:00"
          overdue_for_minutes: 60
          max_overdue: 20
  - notify:
      - ntfy.push:
          server: https://ntfy.example.org
          topic: horae-reminders
          priority: high
          tags: [alarm_clock]
          quiet_hours: { start: "22:00", end: "07:00", during: delay }
          dedupe: daily
```

## Install

Not yet (see the banner). Once `obsidian.reminders` is released:

1. Copy [`pipelines/remind.yaml`](pipelines/remind.yaml) into your instance's `pipelines/` folder (the core does not install pack pipelines yet), and set your ntfy `server` and an unguessable `topic`.
2. Install plugin-obsidian (the vault approved as its `paths.write`) and plugin-ntfy, with **a state directory approved for plugin-ntfy** (`paths.write` in `horae.lock`): `dedupe: daily` needs it, and this pack relies on it (plugin-ntfy README, "Reminder state").
3. Set `timezone` in `horae.yaml`: the windows, the digest and quiet hours are all local times.
4. Run it with the built-in scheduler (`horae serve`) or an external one every 5 minutes (`horae run remind`), with a push monitor (`scheduler.monitors.remind`): a missed run is a missed reminder.

## Design

**Deterministic, every 5 minutes.** The trigger is `*/5 * * * *` in the instance time zone. There is no `llm` step, so nothing can call a model (tested with an instance that declares no model and an LLM port that fails the test if called). The core's scheduler serialises scheduled runs (`max_concurrent: 1`) and skips a fire time that would start more than `misfire_grace_s` late (schemas decision 49 D, D2): a skipped run is a skipped window.

**Where the logic lives.** The core's `transform` step only calls plugin tools; it has no built-in filter or formatter, and templating is plain substitution of `{{now}}`, `{{timezone}}` and `{{date}}` into parameters (never over step data). So "tasks whose `⏰` falls in the last 5 minutes", "once a day" and "the digest at 07:00" cannot be expressed in the pipeline: they need a transform plugin. The pipeline names `obsidian.reminders` (see [Gaps](#gaps) for why plugin-obsidian) and passes it `{{now}}` and `{{timezone}}`, so the transform reads no clock and a replay proposes the same pushes.

**The window.** `(now − window_minutes, now]`, with `now` (the run's start) rounded down to the minute. With runs on the 5-minute marks the windows tile the day, so each reminder time belongs to exactly one run; a run that starts a few seconds late keeps its window. A reminder is never early, and at most 4 minutes late when its time is not on a 5-minute mark.

**Reminders.** `⏰ HH:MM` is that time on the due date (without a due date there is no reminder), `⏰ YYYY-MM-DD HH:MM` that date and time. A time in the skipped hour of a spring-forward night fires once, at the first instant after the gap; a time in the repeated hour of a fall-back night fires once, at its first occurrence (Debian cron, the core scheduler's rule). A date-only `⏰ YYYY-MM-DD` (a snooze to a day) lists the task in that day's digest.

**The digest** is proposed once, by the run whose window holds `digest_at` (07:00): the overdue tasks (oldest first), then today's by reminder time, then the rest due today; at most 25 lines, and none when the list is empty. 07:00 is the end of quiet hours, so it is sent at once.

**Overdue nudges.** One push per overdue task (due before today), proposed by every run from `overdue_at` (09:00) for `overdue_for_minutes` (60), oldest first, at most `max_overdue` (20) per run. The text is the same in every run, so plugin-ntfy's `dedupe: daily` lets each through **once a day**; proposing for an hour rather than once catches up up to 11 missed runs.

**Ticked, cancelled, snoozed.** plugin-obsidian's `open_tasks` only returns open tasks (`[ ]`, `[/]`, or the `open_statuses` it is given), so a ticked or cancelled task is not even collected. The transform checks again (a status of `done` or `cancelled`, or a status character `x`, `X`, `-`, or one outside the source's `open_statuses`, is closed), whatever a source returns. A snoozed task (the source's `snoozed`, or a `⏰` date or date-time after `now`) gets no digest line and no nudge; its reminder fires when the snooze ends (the snooze *is* its `⏰`).

**Stable text, no countdown.** plugin-ntfy's reminder state deduplicates on the push's text (title, body, link, with server, topic and priority), so every title and body holds only the task's description, dates and times: `⏰ 14:30` / `Call the plumber`, `Overdue: due 2026-11-05` / `Send the insurance form`, `Today, 2026-11-09` / the list. A late run that proposes a reminder again is the same push, and a duplicate. Descriptions are untrusted text: line breaks and control characters become spaces, and each is cut to 200 characters.

**Quiet hours** are plugin-ntfy's (`quiet_hours`, `during: delay`): a reminder at 23:00 is published with a delay and reaches the phone at 07:00. The pack only chooses the window: 22:00 to 07:00, ending when the digest is due, and keeping the nudges out of the night. `priority: high` is the entry's (trusted) choice for every push; `critical`, which bypasses quiet hours, is not used.

**One notifier entry.** Every push is a `notify.push` with no link, routed to the one `ntfy.push` entry. A second entry taking `notify.push` (say, a separate priority for the digest) would make every push ambiguous for the core, and a link would need a `link_prefix` (below).

### The transform: `obsidian.reminders`

The contract the pack needs; [`tests/reminders_reference.py`](tests/reminders_reference.py) implements it with the standard library only, and the tests in [`tests/test_reminders_reference.py`](tests/test_reminders_reference.py) are its specification.

| Parameter | Meaning |
|---|---|
| `now` | Required. The run's start, RFC 3339 with an offset: `"{{now}}"` |
| `timezone` | Required. IANA zone of the wall-clock times: `"{{timezone}}"` |
| `window_minutes` | 1 to 60, default 5: the cron interval |
| `digest_at` | `"HH:MM"`; omitted: no digest |
| `overdue_at` | `"HH:MM"`; omitted: no overdue nudge |
| `overdue_for_minutes` | 1 to 1440, default 60 |
| `max_overdue` | 0 to 100, default 20: nudges per run |

An unknown or invalid parameter fails the call (a typo never switches a feature off silently). **Input**: the `collect` step's list; it reads the `data` of every `obsidian.open_tasks` entry (`tasks`, `open_statuses`, `more`), and fails when there is none. A task it cannot read (a malformed date or time) is skipped and counted. **Output**: a summary (`window`, `today`, counts) as data, and the `notify.push` intents (`title`, `body`, no `link`): the reminders by time, the digest, then the nudges. It reads no clock, file or environment.

## Gaps

What the pack could not do itself, and what it does instead. Nothing was opened in other repositories.

1. **No transform provides the reminder logic (blocking).** The core has no built-in transforms (a `transform` step calls plugin tools only, `horae.engine.executor`), the template language has no filters or conditions, and a pack has no runtime. No released plugin provides a transform (plugin-obsidian 0.1.0: `open_tasks`, `write_note`, `write_task`; plugin-ntfy 0.2.0: the `ntfy.push` notifier, which proposes nothing). PLAN.md names `tasks.*` transforms only as examples (`tasks.normalize`, `tasks.dedupe_fingerprint`), and no released plugin implements them. **Proposed:** `obsidian.reminders` in plugin-obsidian, with `notify.push` added to its `permissions.intents` (a proposing permission). Why plugin-obsidian: it owns the task shape and its time zone rules (its decision 3), it already holds all the vault data, and it has no network access; putting it in plugin-ntfy (which already holds `notify.push`) would hand every open task to a process with network access and a token. A dedicated `plugin-reminders` (no network, no path, `notify.push` only) is the least-privilege alternative. The reference implementation can be ported as is.
2. **One overdue nudge a day relies on plugin-ntfy's `dedupe: daily`.** The core gives a pipeline without an LLM step a new `dedupe_key` every run, so the once-a-day guarantee is plugin-ntfy's reminder state, which needs a `paths.write` directory approved in `horae.lock`. With `dedupe: off` an overdue task would be pushed by each of the 12 runs of the nudge hour (the reminders and the digest are still once, by their window). A stable rule key from the core is proposed in horae#123.
3. **A missed run is a missed reminder.** The core's scheduler never catches up a missed fire time (decision 49 D). The nudges catch up for an hour; a `⏰` reminder only in its window. A larger `window_minutes` would catch up too (plugin-ntfy drops the repeats) but makes reminders up to that late; the push monitor is what reports missed runs.
4. **No deep links, no Done / Snooze buttons.** A push carries no `link`: an `obsidian://` link needs a `link_prefix` on the entry and plugin-ntfy does not send it yet (plugin-ntfy#4), and action buttons need the core's signed `/act` links (horae#38, PR horae#115, open). The text is ready for them (one push per task).
5. **Withheld tasks never remind.** plugin-obsidian withholds a task whose key is not unique (`duplicate_key`, `uniqueness_unknown`; its decision 17), so a copy-pasted task line gets no reminder until the duplicate is removed. The digest says when the source left tasks out (`more`), not when it withheld them.
6. **Wording is not localised**: English titles and ISO dates (`Overdue: due 2026-11-05`).

The full time-travel suite (many days, every daylight-saving and quiet-hours edge) is #3.

## Development

`uv` checks the pack with the core's own code: `horae` (the core), `horae-sdk` and `horae-plugin-obsidian` come from their repositories' `main`, pinned by `uv.lock` (dev dependencies only: the pack ships no Python).

```
uv sync
uv lock --check && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
uv run pre-commit run --all-files
```

The tests run the pipeline through the core's `Runner` (store, routing, apply) with plugin-obsidian's real `open_tasks` over a fixture vault, the reference transform, and a stand-in of `ntfy.push` that follows plugin-ntfy's README for daily dedupe and quiet hours ([`tests/pack.py`](tests/pack.py)).

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
