---
name: tickets
description: Query the TAHMO ticketing system (tickets.tahmo.org) for station issues. List and filter tickets, look one up by ticket number, get a station's ticket history, or count and group tickets (open, pending, closed, overdue, and age, per assignee, country, month, label, station, priority, or opener). Use for questions about TAHMO station problems, maintenance workload, or who is handling what.
license: MIT
compatibility: Requires Python 3.12 and uv; no third-party packages. Calls https://tickets.tahmo.org/api over HTTPS and requires TICKETING_API_TOKEN in the environment.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/tickets.py *)
metadata:
  catalog-group: operations
  openclaw:
    requires:
      env:
        - TICKETING_API_TOKEN
    primaryEnv: TICKETING_API_TOKEN
---

# tickets

Read-only access to the TAHMO ticketing system. Every command prints JSON on
stdout. Ticket numbers (`ticket_id`, e.g. `1234`) are what people quote; the
24-character `id` is the database ID.

## When to use

- "What's ticket 1234 about?", "Which tickets are open for TA00172?"
- "How many open tickets does each person have?", "Which country has the most
  overdue issues?", "How many tickets were opened per month this year?"
- Explaining station data problems: pair with `observations` or
  `observations-satellite-qc` to check whether a gap or a removed day matches
  a ticket.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/tickets.py list    [FILTERS] [--limit 20]
uv run ${CLAUDE_SKILL_DIR}/scripts/tickets.py get     TICKET
uv run ${CLAUDE_SKILL_DIR}/scripts/tickets.py station CODE [FILTERS] [--limit 50]
uv run ${CLAUDE_SKILL_DIR}/scripts/tickets.py summary [FILTERS] [--group-by assignee] [--top 20]
```

Commands:

- `list` — matching issues, newest first. `total_matches` counts every match;
  `returned` is how many are shown (`--limit`, at most 200).
- `get TICKET` — one issue in full, with comments. TICKET is a ticket number
  (`1234`, `#1234`) or a database ID.
- `station CODE` — issues for one station, e.g. `TA00172`.
- `summary` — counts without listing: totals by status, overdue, median and
  oldest age of unresolved issues, and the same per group. `--group-by` is one
  of `assignee` (default), `country`, `status`, `priority`, `month`, `label`,
  `station`, `opened_by`. **Use this to count; never page through `list`.**

Filters (`list`, `station`, `summary`; all optional, combined with AND):

- `--status open|pending|close`
- `--country KE` — two-letter country code
- `--station TA00172` (`list`, `summary`)
- `--assignee ID` — assignee database ID
- `--label power` — case-insensitive
- `--created-from YYYY-MM-DD`, `--created-to YYYY-MM-DD`

Credentials: `TICKETING_API_TOKEN` is sent as the `X-API-Key` header. Set
`TICKETING_API_BASE_URL` to use another server (default
`https://tickets.tahmo.org/api`). Listing reads every matching issue, which can
take about half a minute.

## Output

Each issue has `ticket_id`, `id`, `title`, `status`, `priority`, `station`,
`country`, `site_name`, `device_id`, `labels`, `assignees`, `opened_by`,
`created_at`, `updated_at`, `due_date`, `overdue` (unresolved and past its due
date), `open_age_days` (unresolved issues only), and `description` (cut to 300
characters in lists; full in `get`, which adds `comments` and `muted`).

`summary` returns `total_issues`, `by_status`, `overdue`,
`median_open_age_days`, `oldest_open_days`, `median_days_to_close`,
`first_created`, `last_created`, `groups_total`, and `groups` (each with
`total`, `open`, `pending`, `closed`, `overdue`, `median_open_age_days`,
`oldest_open_days`). An issue with several assignees or labels counts once for
each, so group totals can exceed the number of issues. `median_days_to_close`
uses the last update as the close date, since the system records no close
date.

## Failures

- `TICKETING_API_TOKEN` missing, or rejected by the API → exit 2 with the API's
  message (e.g. "Invalid or expired API token").
- No ticket with that number or ID → exit 1, "no ticket found".
- Rate limits and server errors are retried; persistent failures exit 1.

## Examples

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/tickets.py get 1234
uv run ${CLAUDE_SKILL_DIR}/scripts/tickets.py station TA00172 --status open
uv run ${CLAUDE_SKILL_DIR}/scripts/tickets.py summary --status open --group-by assignee
uv run ${CLAUDE_SKILL_DIR}/scripts/tickets.py summary --country KE --group-by month \
    --created-from 2026-01-01
```
