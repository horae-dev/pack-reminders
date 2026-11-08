# SPDX-License-Identifier: Apache-2.0
"""Loaders and the end-to-end harness shared by the tests.

What is real (pinned in uv.lock): the core's instance loader, `Runner` (lock, store, apply
and journal) and engine (planning, templating of `{{now}}` and `{{timezone}}`, routing of
`notify.push` to the notifier entry), and plugin-obsidian's `open_tasks` reading a fixture
vault of Markdown files with its own Tasks parser.

What stands in:

- `obsidian.reminders`, the transform this pack needs and no plugin provides yet (README
  "Gaps"): `reminders_reference.reminders`, registered under the planned plugin-obsidian
  manifest (which adds the transform and the `notify.push` intent it proposes);
- `ntfy.push`: `NtfyStandIn`, a sink that follows plugin-ntfy's README for what this pack
  relies on (the daily dedupe of a rule, quiet hours with `during: delay`) and records what
  would reach the phone. Its manifest is plugin-ntfy 0.2.0's;
- the model: `GuardLlm`, which fails the test if anything ever calls it.
"""

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from horae.apply import Applied, SinkCall, SinkError, StaticSinks
from horae.config import Instance, load_instance
from horae.contracts import Pipeline, RunRecord
from horae.contracts.intents import NotifyPush
from horae.contracts.plugin import PluginManifest
from horae.engine import LlmRequest, LlmResponse, ToolCallError, ToolResult
from horae.engine.testing import FixturePlugins
from horae.runner import Ports, Runner
from horae.store import Store
from horae_obsidian.source import open_tasks
from horae_sdk import Paths

from reminders_reference import REF, SOURCE, ReminderError, reminders

ROOT = Path(__file__).resolve().parent.parent
PIPELINES = ROOT / "pipelines"
PIPELINE = "remind"
TIMEZONE = "Europe/Paris"
ZONE = ZoneInfo(TIMEZONE)
NOTIFIER = "ntfy.push"


def read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def manifest() -> PluginManifest:
    return PluginManifest.model_validate(read_yaml(ROOT / "plugin.yaml"))


def pipeline(name: str = PIPELINE) -> Pipeline:
    return Pipeline.model_validate(read_yaml(PIPELINES / f"{name}.yaml"))


def notify_params(name: str = PIPELINE) -> dict[str, Any]:
    """The trusted parameters of the pipeline's one notifier entry."""
    for step in read_yaml(PIPELINES / f"{name}.yaml")["steps"]:
        for call in step.get("notify", []):
            if NOTIFIER in call:
                params: dict[str, Any] = call[NOTIFIER]
                return params
    raise AssertionError("no ntfy.push entry")


# plugin-obsidian 0.1.0's manifest plus the planned transform and the intent it proposes.
OBSIDIAN = {
    "name": "obsidian",
    "version": "0.2.0",
    "horae": ">=0,<0.2",
    "provides": {
        "sources": [SOURCE],
        "transforms": [REF],
        "sinks": ["obsidian.write_note", "obsidian.write_task"],
    },
    "permissions": {
        "intents": [
            "note.create",
            "note.append_section",
            "note.replace_owned_block",
            "task.create",
            "task.update_status",
            "notify.push",
        ]
    },
}
# plugin-obsidian 0.1.0 as released (main): no transform, no `notify.push`.
OBSIDIAN_RELEASED = {
    **OBSIDIAN,
    "version": "0.1.0",
    "provides": {"sources": [SOURCE], "sinks": ["obsidian.write_note", "obsidian.write_task"]},
    "permissions": {"intents": OBSIDIAN["permissions"]["intents"][:-1]},  # type: ignore[index]
}
# plugin-ntfy 0.2.0's manifest (main), without its runtime.
NTFY = {
    "name": "ntfy",
    "version": "0.2.0",
    "horae": ">=0,<0.2",
    "provides": {"notifiers": [NOTIFIER]},
    "permissions": {
        "network": ["ntfy.sh"],
        "secrets": ["NTFY_TOKEN"],
        "intents": ["notify.push"],
    },
}


class GuardLlm:
    """An `LlmBackend` that must never be called: the pipeline has no `llm` step."""

    def __init__(self) -> None:
        self.requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        raise AssertionError("the remind pipeline called an LLM")


@dataclass(frozen=True)
class Push:
    """A push as it would reach the phone."""

    title: str | None
    body: str
    proposed: datetime
    delivered: datetime
    """Local wall-clock time of delivery (naive), to the minute."""
    delivery: str
    """`sent`, `delayed` or `duplicate` (plugin-ntfy's `details.delivery`)."""


def _hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _quiet(wall: datetime, start: time, end: time) -> bool:
    at = wall.time()
    return start <= at < end if start < end else at >= start or at < end


@dataclass
class NtfyStandIn:
    """plugin-ntfy's `ntfy.push`, as far as this pack relies on it (plugin-ntfy README):

    - **daily dedupe** (`dedupe: daily`): the rule is the entry's server, topic and priority
      and the intent's title, body and link; a push is a `duplicate` when it would reach the
      phone less than one day (local wall clock, to the minute) after the rule's last one;
    - **quiet hours** with `during: delay`: a push that is not `critical` whose local time is
      in `[start, end)` is delivered at the next `end`, and counted from then.

    The daylight-saving details of both (the repeated and skipped hours) are plugin-ntfy's
    own tests; here a wall-clock time is the zone's reading of the instant."""

    clock: Callable[[], datetime]
    pushes: list[Push] = field(default_factory=list)
    params: list[dict[str, Any]] = field(default_factory=list)
    last: dict[tuple[Any, ...], datetime] = field(default_factory=dict)

    async def read(self, call: SinkCall) -> bytes | None:
        raise SinkError("not_supported", "a notifier has no target to read")

    async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
        intent = call.intent
        assert isinstance(intent, NotifyPush)
        params = call.params
        self.params.append(params)
        if params.get("dedupe", "daily") != "daily":
            raise SinkError("config_invalid", "this stand-in only models dedupe: daily")
        wall = self.clock().astimezone(ZONE).replace(tzinfo=None, second=0, microsecond=0)
        delivered, delivery = wall, "sent"
        quiet = params.get("quiet_hours")
        if quiet is not None and params.get("priority") != "critical":
            assert quiet.get("during", "delay") == "delay"
            start, end = _hhmm(quiet["start"]), _hhmm(quiet["end"])
            if _quiet(wall, start, end):
                delivered = datetime.combine(wall.date(), end)
                if delivered <= wall:
                    delivered += timedelta(days=1)
                delivery = "delayed"
        rule = (
            params.get("server"),
            params.get("topic"),
            params.get("priority"),
            intent.title,
            intent.body,
            intent.link,
        )
        previous = self.last.get(rule)
        if previous is not None and delivered < previous + timedelta(days=1):
            delivery = "duplicate"
        else:
            self.last[rule] = delivered
        self.pushes.append(Push(intent.title, intent.body, wall, delivered, delivery))
        return Applied(
            f"{params.get('server')}/{params.get('topic')}", details={"delivery": delivery}
        )

    def phone(self) -> list[Push]:
        """What reached (or will reach) the phone: every push but the duplicates."""
        return [push for push in self.pushes if push.delivery != "duplicate"]


def local(text: str) -> datetime:
    """`"2026-11-09 14:30"` in Paris, as a UTC instant (the first occurrence when repeated)."""
    return datetime.fromisoformat(text).replace(tzinfo=ZONE).astimezone(UTC)


class Remind:
    """The `remind` pipeline of a test instance, run at chosen times over a fixture vault."""

    def __init__(self, root: Path) -> None:
        (root / "pipelines").mkdir(parents=True)
        shutil.copy(PIPELINES / f"{PIPELINE}.yaml", root / "pipelines")
        (root / "horae.yaml").write_text(
            f"timezone: {TIMEZONE}\nstate_dir: state\n", encoding="utf-8"
        )
        self.instance: Instance = load_instance(root)
        self.vault = root / "vault"
        self.vault.mkdir()
        self.now = local("2026-11-09 06:00")
        self.llm = GuardLlm()
        self.ntfy = NtfyStandIn(clock=lambda: self.now)
        self.plugins = FixturePlugins()
        self.plugins.add(OBSIDIAN, {SOURCE: self._open_tasks, REF: self._reminders})
        self.plugins.add(NTFY)
        self.plugins.add(manifest())
        self.records: list[RunRecord] = []

    def write(self, text: str, note: str = "Tasks.md") -> None:
        (self.vault / note).write_text(text, encoding="utf-8")

    def _open_tasks(self, params: dict[str, Any], input: Any) -> ToolResult:
        # The core starts the plugin with the instance's TZ (the tests set it, conftest.py).
        data = open_tasks(
            Paths(read=(str(self.vault),)), **params, clock=lambda: self.now.astimezone(ZONE)
        )
        return ToolResult(data=data)

    def _reminders(self, params: dict[str, Any], input: Any) -> ToolResult:
        try:
            data, intents = reminders(params, input)
        except ReminderError as error:
            raise ToolCallError("tool_error", str(error)) from None
        return ToolResult(data=data, intents=intents)

    def run(self, at: datetime | str, *, check: bool = True) -> RunRecord:
        """One run started at `at` (a UTC instant, or a Paris wall-clock time); with `check`,
        it must succeed."""
        self.now = local(at) if isinstance(at, str) else at
        with Store.open(self.instance.store_path) as store:
            ports = Ports(
                self.plugins,
                self.plugins,
                self.llm,
                sinks=StaticSinks({NOTIFIER: self.ntfy}),
            )
            runner = Runner(self.instance, store, ports, clock=lambda: self.now)
            record = asyncio.run(runner.run(PIPELINE))
        if check:
            assert record.status in ("ok", "noop"), (record.status, [s.error for s in record.steps])
        self.records.append(record)
        return record

    def every_5_minutes(self, start: str, end: str) -> None:
        """Runs at every multiple of 5 minutes (UTC) from `start` to `end` (Paris), as cron
        `*/5` fires (offsets are whole hours here)."""
        moment, last = local(start), local(end)
        while moment <= last:
            self.run(moment)
            moment += timedelta(minutes=5)

    def proposed(self, record: RunRecord) -> list[tuple[str | None, str]]:
        """The pushes `record` proposed: (title, body)."""
        out = []
        for proposed in record.intents:
            intent = proposed.intent
            assert isinstance(intent, NotifyPush)
            assert proposed.applied_by == NOTIFIER
            out.append((intent.title, intent.body))
        return out
