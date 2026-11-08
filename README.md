<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# pack-reminders

Horae pipeline pack: reminders, daily digest and overdue nudges from the vault, without an LLM.

> 🚧 **Early (0.1.0, unreleased).** The pipeline needs plugin-obsidian's `obsidian.reminders` transform, which is in review ([plugin-obsidian#10](https://github.com/horae-dev/plugin-obsidian/pull/10)). The tests run the pipeline through the core's `Runner` with plugin-obsidian's real task parser and transform, pinned to that pull request's commit in `uv.lock`. Once it is merged, the pin goes back to `main`.

## What it does

Every 5 minutes the `remind` pipeline reads your open Obsidian tasks and pushes them through ntfy:

- **`⏰` reminders** at their time: `- [ ] Call the plumber 📅 2026-11-09 ⏰ 14:30` sends `⏰ 2026-11-09 14:30` / `Call the plumber` at 14:30.
- **The morning digest** at 07:00: how many tasks are overdue and the oldest of them, then today's tasks by reminder time, then the rest due today.
- **One overdue nudge** at 09:00 for each task that became overdue yesterday. Older overdue tasks appear only in the digest.
- **Nothing during quiet hours** (22:00 to 07:00): a push is delayed to 07:00.

A ticked `[x]` or cancelled `[-]` task never reminds. A snoozed task waits for its snooze to end. **No run ever calls a model**: the pipeline has no `llm` step.

## What is in the pack

A pack is a plugin with no runtime: it ships files, not a process (`plugin.yaml` has no `runtime`, so no network, secret, path or intent either).

| File | What it is |
|---|---|
| [`plugin.yaml`](plugin.yaml) | the manifest: provides the pipeline `remind`, and asks for no permission |
| [`pipelines/remind.yaml`](pipelines/remind.yaml) | the pipeline: `collect` → `transform` → `notify`, cron `*/5 * * * *` |

### The pipeline

```yaml
name: remind
trigger: { cron: "*/5 * * * *" }
steps:
  - collect:
      - obsidian.open_tasks: { limit: 2000 }
  - transform:
      - obsidian.reminders:
          now: "{{now}}"
          timezone: "{{timezone}}"
          window_minutes: 60
          digest_at: "07:00"
          overdue_at: "09:00"
          overdue_for_minutes: 60
          max_overdue: 10
          quiet_hours: { start: "22:00", end: "07:00" }
  - notify:
      - ntfy.push:
          server: https://ntfy.invalid     # placeholder: set your server
          topic: CHANGE-ME                 # placeholder: set an unguessable topic
          priority: high
          tags: [alarm_clock]
          quiet_hours: { start: "22:00", end: "07:00", during: delay }
          dedupe: daily
```

## Install

Not yet (see the banner). Once `obsidian.reminders` is released:

1. Copy [`pipelines/remind.yaml`](pipelines/remind.yaml) into your instance's `pipelines/` folder (the core does not install pack pipelines yet). Replace the placeholders `https://ntfy.invalid` and `CHANGE-ME` with your ntfy server and an unguessable topic. As shipped they fail, so a copy that was not edited never pushes anywhere.
2. Install plugin-obsidian (the vault approved as its `paths.write`) and plugin-ntfy, with **a state directory approved for plugin-ntfy** (`paths.write` in `horae.lock`): `dedupe: daily` needs it, and this pack relies on it (plugin-ntfy README, "Reminder state").
3. Set `timezone` in `horae.yaml`: the windows, the digest and quiet hours are all local times.
4. Run it with the built-in scheduler (`horae serve`) or an external one every 5 minutes (`horae run remind`), with a push monitor (`scheduler.monitors.remind`). The 60-minute lookback catches up a few missed runs. If you change the cron interval, keep `window_minutes` at least as long as the interval: checking that is up to you.

## Design

**Deterministic, every 5 minutes.** The trigger is `*/5 * * * *` in the instance time zone. There is no `llm` step, so nothing can call a model. A test checks this with an instance that declares no model and an LLM port that fails the test if called. The core's scheduler serialises scheduled runs (`max_concurrent: 1`) and skips a fire time that would start more than `misfire_grace_s` late (schemas decision 49 D, D2).

**Where the logic lives.** The core's `transform` step only calls plugin tools; templating is plain substitution of `{{now}}`, `{{timezone}}` and `{{date}}` into parameters. A pack has no runtime. So the reminder logic is a transform in plugin-obsidian, `obsidian.reminders`. That plugin owns the task shape and its time zone rules, and it has no network access. The pipeline passes it `{{now}}` and `{{timezone}}`, so the transform reads no clock and a replay proposes the same pushes.

**The lookback.** Every run proposes what falls in `(now − 60 min, now]`, with `now` (the run's start) rounded down to the minute. A reminder therefore reaches the phone at the first run at or after its time, at most 4 minutes late when its time is not on a 5-minute mark. The runs that follow propose it again with the same text, and plugin-ntfy's `dedupe: daily` drops those repeats. A run that misfired is caught up by the next one. `window_minutes` must be at least the cron interval; checking that is up to the operator.

**Reminders.** `⏰ HH:MM` is that time on the due date; without a due date there is no reminder. `⏰ YYYY-MM-DD HH:MM` is that date and time. The title carries the date and time (`⏰ 2026-11-09 14:30`), so a daily recurring task is a new push each day rather than a duplicate. A time in the skipped hour of a spring-forward night fires at the first instant after the gap. A time in the repeated hour of a fall-back night fires at its first occurrence (Debian cron, the core scheduler's rule). A date-only `⏰ YYYY-MM-DD` (a snooze to a day) lists the task in that day's digest.

**The digest** (`Today, 2026-11-09`) is proposed by every run whose lookback holds `digest_at` (07:00), with the same text each time, so only the first one is sent. It has two sections. `Overdue: N` gives the count and the oldest `max_overdue` (10). `Today: N` lists the tasks by reminder time, then the rest due today. The body stays under 3,800 UTF-8 bytes and is never cut mid-line; `…and N more` says what did not fit. When the source left tasks out (past its `limit` of 2000, withheld duplicates, unreadable notes, Horae's own output), the digest ends with `Some tasks could not be read`. No digest is sent when the list is empty. The digest time and nudge times are computed for today and yesterday, so a `digest_at` near midnight still works.

**Overdue nudges.** At `overdue_at` (09:00), one push for each task that became overdue, meaning it was due yesterday: `Overdue: due 2026-11-08` / `Renew the passport`. Every run proposes it for `overdue_for_minutes` (60), and plugin-ntfy sends it once. Older overdue tasks are only counted and listed in the digest, so a long backlog does not mean a long list of pushes every morning.

**Ticked, cancelled, snoozed.** plugin-obsidian's `open_tasks` only returns open tasks, and the transform checks the status again whatever a source returns. A task counts as snoozed when its `⏰` date-time is after `now`, or its date-only `⏰` is after today. The transform works this out itself; the source's `snoozed` flag is only a hint. A snoozed task gets no nudge. It stays in the digest when it is due that day or its `⏰` is that day (listed by its time). Its reminder fires when the snooze ends.

**Stable text, no countdown.** plugin-ntfy's reminder state deduplicates on the push's text (title, body and link, with server, topic and priority). Every title and body therefore holds only the task's description, dates and times. Descriptions are untrusted text: Markdown links become their text, `#tags` are dropped, line breaks and control characters become spaces, and the result is cut to 200 characters on a word boundary. If two pushes of one run would have the same text (two tasks with the same description and time), each body gets a short suffix from its task key (` · 3fa2`), so neither is lost as a duplicate.

**Quiet hours** belong to plugin-ntfy (`quiet_hours`, `during: delay`). A reminder at 23:00 is published with a delay and reaches the phone at 07:00. The transform receives the same window (`quiet_hours`, kept equal to the notifier's) and leaves out of the 07:00 digest any task whose reminder fell inside it. That reminder arrives as its own push at 07:00, so it is not listed twice. The window ends when the digest is due and keeps the nudges out of the night.

**One notifier entry, `priority: high`.** Every push is a `notify.push` with no link, routed to the one `ntfy.push` entry, so every push, the digest included, has `high` priority. A second entry taking `notify.push` (for a different digest priority, say) would make every push ambiguous for the core. `critical`, which bypasses quiet hours, is not used.

### The transform: `obsidian.reminders`

The transform lives in plugin-obsidian (`horae_obsidian.reminders`); its README section "Reminders" documents the parameters. This pack's tests run its golden vectors (`horae_obsidian.vectors`, [`tests/test_reminders_vectors.py`](tests/test_reminders_vectors.py)) and the pipeline end to end against the version pinned in `uv.lock`.

## Gaps

1. **plugin-obsidian is pinned to a pull request's commit** ([plugin-obsidian#10](https://github.com/horae-dev/plugin-obsidian/pull/10)) until that PR is merged. After the merge, set `branch = "main"` in `pyproject.toml` and run `uv lock --upgrade-package horae-plugin-obsidian`.
2. **Once a day relies on plugin-ntfy's `dedupe: daily`.** The core gives a pipeline without an LLM step a new `dedupe_key` on every run, so the lookback's repeats are dropped only by plugin-ntfy's reminder state. That state needs a `paths.write` directory approved in `horae.lock`. A stable rule key from the core is proposed in horae#123. If the task list changes within the digest's lookback (a task ticked at 07:30), the new text is a new digest.
3. **A missed run longer than the lookback is a missed reminder.** The core's scheduler never catches up a missed fire time (decision 49 D). The push monitor is what reports missed runs.
4. **No deep links, no Done / Snooze buttons.** A push carries no `link`. An `obsidian://` link needs a `link_prefix` on the entry, and plugin-ntfy does not send it yet (plugin-ntfy#4). Action buttons need the core's signed `/act` links (horae#38, PR horae#115, open).
5. **Withheld tasks never remind.** plugin-obsidian withholds a task whose key is not unique (its decision 17), so a copy-pasted task line gets no reminder until the duplicate is removed. The digest then says "Some tasks could not be read".
6. **Wording is not localised**: English titles and ISO dates (`Overdue: due 2026-11-05`).
7. **Not used yet**: the `⏳` scheduled and `🛫` start dates, and a bare `⏰ HH:MM` without a due date (no reminder).

The full time-travel suite (many days, every daylight-saving and quiet-hours edge) is #3.

## Development

`uv` checks the pack with the core's own code. `horae` (the core) and `horae-sdk` come from the core repository's `main`, and `horae-plugin-obsidian` from the commit in `pyproject.toml`; `uv.lock` pins them all. They are dev dependencies only: the pack ships no Python.

```
uv sync
uv lock --check && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
uv run pre-commit run --all-files
```

The tests run the pipeline through the core's `Runner` (store, routing, apply). They use plugin-obsidian's real `open_tasks` over a fixture vault and its real `obsidian.reminders`, plus a stand-in of `ntfy.push` that follows plugin-ntfy's README for daily dedupe and quiet hours ([`tests/pack.py`](tests/pack.py)).

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
