Closes #<issue>

## Summary

<!-- What changed and why, in a few lines. -->

## Acceptance criteria

<!-- Copy them from the issue; each maps to at least one test. -->
- [ ]

## Definition of done (see AGENTS.md)

- [ ] CI green: the **CI OK** check (lockfile, lint, types, tests, gitleaks)
- [ ] `uv run pre-commit run --all-files` passes locally
- [ ] Fixtures synthetic (no personal data, no secret, no real host)
- [ ] Commits carry `Graft-Agent`, `Graft-Model` and `Graft-Task` trailers

## For the maintainer to judge

<!-- Wording changes: show the before and after text of each push. -->

## Declared additions

<!-- Write "none" or list each with a justification (they appear in plugin.yaml permissions). -->
- Network hosts:
- Secrets:
- Paths:
- Intents:
- LLM calls:
