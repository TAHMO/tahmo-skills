# Contributing

## Publish model

`main` is the consumer-facing branch. Anything merged to `main` is considered
published. Each skill script carries a module-level `_SKILL_VERSION` constant —
the sole source of skill version identity. SKILL.md does **not** carry a version
field. The version-bump workflow rewrites `_SKILL_VERSION` on merge to `main`.

## PR workflow

1. Branch off `main`.
2. Open a PR back into `main`.
3. CI must pass (ruff, inline-deps, pytest, per-script `--help`).
4. Rebase onto `main` before merging — linear history is preferred.
5. Use the GitHub merge button.

Authors **must not** edit any `_SKILL_VERSION` constant by hand.

## Skill correctness tests

Per-skill tests live in `skills/<name>/tests/`. Shared helpers are in
`tests/conftest.py`. Run them with `uv sync --group dev && uv run pytest`.

## Version bumps

On push to `main`, `.github/workflows/version-bump.yml` bumps changed skills and
publishes a lean plugin payload to `plugin-dist`. Bump kind comes from PR labels:

| Label            | Bump kind |
| ---------------- | --------- |
| `release: major` | major     |
| `release: minor` | minor     |
| (none)           | patch     |

## Local development against weather-skills-core

```bash
tools/run_with_local_core.sh skills/tahmo-fetch/scripts/fetch.py --help
```

Core is pinned to `main` in `pyproject.toml` and every
skill script's PEP 723 header.

## Adding a skill

1. Create `skills/<name>/` with `SKILL.md`, `scripts/<script>.py` (PEP 723
   header plus `_SKILL_VERSION = "0.1.0"`), and `tests/test_<name>.py`.
2. Keep each script self-contained. CI runs `--help` on every
   `scripts/*.py`, and every script in a skill must share one `_SKILL_VERSION`.
3. Tests must not hit the network. Mock at the HTTP layer (see
   `skills/tahmo-fetch/tests/`).
4. Add the skill to the table in `README.md` and to `agents/tahmo.md`.

## Live checks

Tests are fully mocked. For a manual end-to-end check against the API, use
TAHMO credentials (TAHMO's public demo account from
[API-V2-Python-examples](https://github.com/TAHMO/API-V2-Python-examples)
exposes a few stations):

```bash
TAHMO_API_USERNAME=... TAHMO_API_PASSWORD=... \
  uv run skills/tahmo-fetch/scripts/fetch.py --list-stations
```
