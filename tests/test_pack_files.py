# SPDX-License-Identifier: Apache-2.0
"""The pack's files, read with the core's own loaders and contracts."""

from horae.config.cron import cron_problem
from horae.contracts.pipeline import (
    CollectStep,
    LlmStep,
    NotifyStep,
    TransformStep,
    call_parts,
)
from horae_obsidian.reminders import REF, SOURCE, parse_params

from pack import NOTIFIER, PIPELINES, manifest, notify_params, pipeline


def test_the_manifest_is_a_pack_with_no_runtime_and_no_permission() -> None:
    loaded = manifest()
    assert loaded.name == "pack-reminders"
    assert loaded.runtime is None
    assert loaded.provides.pipelines == ["remind"]
    for kind in ("sources", "transforms", "sinks", "notifiers", "skills"):
        assert getattr(loaded.provides, kind) == []
    permissions = loaded.permissions
    assert permissions.network == []
    assert permissions.secrets == []
    assert permissions.paths.read == permissions.paths.write == []
    assert permissions.intents == []


def test_every_pipeline_file_is_provided() -> None:
    files = sorted(path.stem for path in PIPELINES.glob("*.yaml"))
    assert files == sorted(manifest().provides.pipelines)


def test_the_pipeline_has_no_llm_step() -> None:
    """Acceptance: no LLM call ever. The definition is the program (ADR 0001): without an
    `llm` step the engine has nothing that could call a model."""
    loaded = pipeline()
    assert not any(isinstance(step, LlmStep) for step in loaded.steps)
    kinds = [type(step).__name__ for step in loaded.steps]
    assert kinds == ["CollectStep", "TransformStep", "NotifyStep"]


def test_it_runs_every_five_minutes_in_the_instance_time_zone() -> None:
    trigger = pipeline().trigger
    assert trigger is not None
    assert trigger.cron == "*/5 * * * *"
    assert cron_problem(trigger.cron) is None
    # No trigger time zone: the instance's, which plugin-ntfy's quiet hours also read.
    assert trigger.timezone is None


def test_the_steps_call_the_tools_with_valid_parameters() -> None:
    collect, transform, notify = pipeline().steps
    assert isinstance(collect, CollectStep)
    assert isinstance(transform, TransformStep)
    assert isinstance(notify, NotifyStep)
    ((source, source_params, _),) = [call_parts(call) for call in collect.collect]
    assert source == SOURCE
    assert source_params == {"limit": 2000}  # the source's maximum: read every task it can
    ((ref, params, _),) = [call_parts(call) for call in transform.transform]
    assert ref == REF
    assert params["now"] == "{{now}}"
    assert params["timezone"] == "{{timezone}}"
    # The rendered parameters are valid for the transform.
    rendered = {**params, "now": "2026-11-09T13:30:00Z", "timezone": "Europe/Paris"}
    parsed = parse_params(rendered)
    # The lookback covers at least the cron interval (5 minutes), with room for missed runs.
    assert parsed.window_minutes == 60
    assert parsed.max_overdue == 10
    # The transform's quiet hours are the notifier's.
    quiet = notify_params()["quiet_hours"]
    assert params["quiet_hours"] == {"start": quiet["start"], "end": quiet["end"]}
    assert [call_parts(call)[0] for call in notify.notify] == [NOTIFIER]


def test_the_notifier_dedupes_daily_and_has_quiet_hours() -> None:
    params = notify_params()
    # One overdue nudge a day and no repeat from a late run rely on plugin-ntfy's dedupe.
    assert params["dedupe"] == "daily"
    assert params["quiet_hours"] == {"start": "22:00", "end": "07:00", "during": "delay"}
    # The digest (07:00) is not in quiet hours ([start, end)), nor are the overdue nudges.
    assert params["quiet_hours"]["end"] <= "07:00"
    # No proposed push carries a link, so no link_prefix is needed (README "Gaps").
    assert "link_prefix" not in params
    assert params["server"].startswith("https://")
    # Placeholders only, that fail when copied as they are: a reserved TLD, a loud topic.
    assert params["server"] == "https://ntfy.invalid"
    assert params["topic"] == "CHANGE-ME"
    # One entry for every push: `high`, not `critical` (which would bypass quiet hours).
    assert params["priority"] == "high"
