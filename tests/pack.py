# SPDX-License-Identifier: Apache-2.0
"""Loaders and the end-to-end harness shared by the tests.

What is real (pinned in uv.lock): the core's instance loader, `Runner` (lock, store, apply
and journal) and engine (planning, templating of `{{now}}` and `{{timezone}}`, routing of
`notify.push` to the notifier entry), and plugin-obsidian, under its own manifest: its
`open_tasks` reading a fixture vault of Markdown files with its Tasks parser, and its
`obsidian.reminders` transform.

What stands in:

- `ntfy.push`: `NtfyStandIn`, a sink that follows plugin-ntfy's README for what this pack
  relies on (the daily dedupe of a rule, quiet hours with `during: delay`) and records what
  would reach the phone. Its manifest is plugin-ntfy 0.2.0's;
- the model: `GuardLlm`, which fails the test if anything ever calls it.
"""

import asyncio
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
from horae_obsidian import manifest as obsidian_manifest
from horae_obsidian.reminders import REF, SOURCE, reminders
from horae_obsidian.source import open_tasks
from horae_sdk import Paths, PluginError

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


# plugin-obsidian's manifest, as its code declares it (with `obsidian.reminders` and the
# `notify.push` intent it proposes).
OBSIDIAN: dict[str, Any] = obsidian_manifest()
# plugin-obsidian before the transform (0.1.0 as first released): no `obsidian.reminders`, no
# `notify.push`.
OBSIDIAN_RELEASED: dict[str, Any] = {
    **OBSIDIAN,
    "provides": {k: v for k, v in OBSIDIAN["provides"].items() if k != "transforms"},
    "permissions": {
        **OBSIDIAN["permissions"],
        "intents": [i for i in OBSIDIAN["permissions"]["intents"] if i != "notify.push"],
    },
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
    """`sent`, `delayed`, `dropped` or `duplicate` (plugin-ntfy's `details.delivery`)."""


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
    - **quiet hours**: a push that is not `critical` whose local time is in `[start, end)` is,
      with `during: delay` (the default), delivered at the next `end` and counted from then;
      with `during: drop`, never delivered (`dropped`) but recorded like a sent push and counted
      from the drop: the same push proposed again right after the quiet hours is a `duplicate`
      (a dropped push is stale by design; it can be pushed a day after the drop);

    The daylight-saving details of both (the repeated and skipped hours) are plugin-ntfy's
    own tests; here a wall-clock time is the zone's reading of the instant."""

    clock: Callable[[], datetime]
    zone: ZoneInfo = ZONE
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
        wall = self.clock().astimezone(self.zone).replace(tzinfo=None, second=0, microsecond=0)
        delivered, delivery = wall, "sent"
        quiet = params.get("quiet_hours")
        if quiet is not None and params.get("priority") != "critical":
            start, end = _hhmm(quiet["start"]), _hhmm(quiet["end"])
            if _quiet(wall, start, end):
                delivered = datetime.combine(wall.date(), end)
                if delivered <= wall:
                    delivered += timedelta(days=1)
                delivery = "delayed"
                if quiet.get("during", "delay") == "drop":
                    delivery = "dropped"
        rule = (
            params.get("server"),
            params.get("topic"),
            params.get("priority"),
            intent.title,
            intent.body,
            intent.link,
        )
        previous = self.last.get(rule)
        if delivery == "dropped":
            delivered = wall  # counted from the drop
        if previous is not None and delivered < previous + timedelta(days=1):
            delivery = "duplicate"  # already on its way to the phone (or dropped) today
        else:
            self.last[rule] = delivered
        self.pushes.append(Push(intent.title, intent.body, wall, delivered, delivery))
        return Applied(
            f"{params.get('server')}/{params.get('topic')}", details={"delivery": delivery}
        )

    def phone(self) -> list[Push]:
        """What reached (or will reach) the phone: every push but the duplicates and the
        dropped ones."""
        return [push for push in self.pushes if push.delivery not in ("duplicate", "dropped")]


def local(text: str, zone: ZoneInfo = ZONE) -> datetime:
    """`"2026-11-09 14:30"` in Paris (or `zone`), as a UTC instant (the first occurrence when
    repeated)."""
    return datetime.fromisoformat(text).replace(tzinfo=zone).astimezone(UTC)


class Remind:
    """The `remind` pipeline of a test instance, run at chosen times over a fixture vault."""

    def __init__(
        self,
        root: Path,
        timezone: str = TIMEZONE,
        edit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """`timezone` is the instance's; `edit` changes the pipeline's parsed YAML (a copy) before
        it is loaded, to try other trusted parameters."""
        (root / "pipelines").mkdir(parents=True)
        shutil.copy(PIPELINES / f"{PIPELINE}.yaml", root / "pipelines")
        if edit is not None:
            path = root / "pipelines" / f"{PIPELINE}.yaml"
            document = read_yaml(path)
            edit(document)
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        (root / "horae.yaml").write_text(
            f"timezone: {timezone}\nstate_dir: state\n", encoding="utf-8"
        )
        self.zone = ZoneInfo(timezone)
        self.instance: Instance = load_instance(root)
        self.vault = root / "vault"
        self.vault.mkdir()
        self.now = local("2026-11-09 06:00", self.zone)
        self.llm = GuardLlm()
        self.ntfy = NtfyStandIn(clock=lambda: self.now, zone=self.zone)
        self.plugins = FixturePlugins()
        self.plugins.add(OBSIDIAN, {SOURCE: self._open_tasks, REF: self._reminders})
        self.plugins.add(NTFY)
        self.plugins.add(manifest())
        self.records: list[RunRecord] = []
        self._session: tuple[Store, asyncio.Runner] | None = None

    def write(self, text: str, note: str = "Tasks.md") -> None:
        (self.vault / note).write_text(text, encoding="utf-8")

    def _open_tasks(self, params: dict[str, Any], input: Any) -> ToolResult:
        # The core starts the plugin with the instance's TZ (the tests set it, conftest.py).
        data = open_tasks(
            Paths(read=(str(self.vault),)), **params, clock=lambda: self.now.astimezone(self.zone)
        )
        return ToolResult(data=data)

    def _reminders(self, params: dict[str, Any], input: Any) -> ToolResult:
        try:
            data, intents = reminders(params, input)
        except PluginError as error:  # the plugin's tool error
            raise ToolCallError("tool_error", str(error)) from None
        return ToolResult(data=data, intents=intents)

    @contextmanager
    def session(self) -> Iterator[None]:
        """Keep the store and the event loop open across the runs inside (a long time travel);
        each run still gets its own `Runner`, as each cron run is a new process."""
        with Store.open(self.instance.store_path) as store, asyncio.Runner() as loop:
            self._session = (store, loop)
            try:
                yield
            finally:
                self._session = None

    def run(self, at: datetime | str, *, check: bool = True) -> RunRecord:
        """One run started at `at` (a UTC instant, or a Paris wall-clock time); with `check`,
        it must succeed."""
        self.now = local(at, self.zone) if isinstance(at, str) else at
        if self._session is None:
            with self.session():
                return self.run(at, check=check)
        store, loop = self._session
        ports = Ports(
            self.plugins, self.plugins, self.llm, sinks=StaticSinks({NOTIFIER: self.ntfy})
        )
        runner = Runner(self.instance, store, ports, clock=lambda: self.now)
        record = loop.run(runner.run(PIPELINE))
        if check:
            assert record.status in ("ok", "noop"), (record.status, [s.error for s in record.steps])
        self.records.append(record)
        return record

    def every_5_minutes(self, start: str, end: str) -> None:
        """Runs at every multiple of 5 minutes (UTC) from `start` to `end` (Paris), as cron
        `*/5` fires (offsets are whole hours here)."""
        self.travel(start, end)

    def travel(
        self,
        start: str,
        end: str,
        *,
        every: int = 5,
        skip: Callable[[datetime], bool] | None = None,
        edits: dict[str, Callable[[], None]] | None = None,
    ) -> None:
        """Time travel: a run every `every` minutes from `start` to `end` (local times), unless
        `skip(instant)` (the host was asleep), the clock being the run's instant and the ntfy
        dedupe state carried from run to run. `edits` maps a local time to a change of the
        vault, made just before the first run at or after it (the user ticks a task)."""
        moment, last = local(start, self.zone), local(end, self.zone)
        pending = sorted((local(at, self.zone), edit) for at, edit in (edits or {}).items())
        with self.session():
            self._travel(moment, last, every, skip, pending)

    def _travel(
        self,
        moment: datetime,
        last: datetime,
        every: int,
        skip: Callable[[datetime], bool] | None,
        pending: list[tuple[datetime, Callable[[], None]]],
    ) -> None:
        while moment <= last:
            while pending and pending[0][0] <= moment:
                pending.pop(0)[1]()
            if skip is None or not skip(moment):
                self.run(moment)
            moment += timedelta(minutes=every)

    def proposed(self, record: RunRecord) -> list[tuple[str | None, str]]:
        """The pushes `record` proposed: (title, body)."""
        out = []
        for proposed in record.intents:
            intent = proposed.intent
            assert isinstance(intent, NotifyPush)
            assert proposed.applied_by == NOTIFIER
            out.append((intent.title, intent.body))
        return out
