# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = []
# ///
"""Query the TAHMO ticketing system (https://tickets.tahmo.org).

Commands: list, get, station, summary. Prints JSON on stdout.
Credentials: TICKETING_API_TOKEN (sent as X-API-Key); optional TICKETING_API_BASE_URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from statistics import median

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.1"

DEFAULT_BASE_URL = "https://tickets.tahmo.org/api"
HTTP_TIMEOUT = 120  # listing every issue takes about half a minute
MAX_ATTEMPTS = 3
STATUSES = ("open", "pending", "close")
GROUPS = ("assignee", "country", "status", "priority", "month", "label", "station", "opened_by")
DESCRIPTION_CHARS = 300
UNASSIGNED = "(unassigned)"
UNKNOWN = "(unknown)"
_DATABASE_ID = re.compile(r"^[0-9a-fA-F]{24}$")


class SkillError(Exception):
    exit_code = 1


class UsageError(SkillError):
    exit_code = 2


# --------------------------------------------------------------------------- API


def _base_url() -> str:
    return (os.environ.get("TICKETING_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _token() -> str:
    token = (os.environ.get("TICKETING_API_TOKEN") or "").strip()
    if not token:
        raise UsageError(
            "TICKETING_API_TOKEN must be set to a TAHMO ticketing API key "
            "(sent as the X-API-Key header)."
        )
    return token


def api_get(path: str, params: dict | None = None) -> dict:
    """GET a ticketing endpoint and return its JSON body (``success`` checked)."""
    query = {k: v for k, v in (params or {}).items() if v is not None}
    url = f"{_base_url()}/{path.lstrip('/')}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "X-API-Key": _token()}
    )
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
                body = resp.read()
            break
        except urllib.error.HTTPError as exc:
            message = _error_message(exc)
            if exc.code in (401, 403):
                raise UsageError(
                    f"the ticketing API rejected the token (HTTP {exc.code}: {message}). "
                    "Check TICKETING_API_TOKEN."
                ) from None
            if exc.code == 429 or exc.code >= 500:
                last_error = f"HTTP {exc.code} {message}".strip()
            else:
                raise SkillError(
                    f"ticketing API error: HTTP {exc.code} {message}".strip()
                ) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = str(getattr(exc, "reason", exc))
        if attempt == MAX_ATTEMPTS:
            raise SkillError(
                f"ticketing API request failed after {MAX_ATTEMPTS} attempts: {last_error}"
            )
        time.sleep(2.0 * attempt)
    try:
        payload = json.loads(body)
    except ValueError:
        raise SkillError("ticketing API returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise SkillError("ticketing API returned an unexpected response")
    if not payload.get("success"):
        raise SkillError(payload.get("message") or "ticketing API request was unsuccessful")
    return payload


def _error_message(exc: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(exc.read()).get("message", ""))
    except Exception:  # noqa: BLE001
        return ""


def fetch_issues(status=None, assignee=None, country=None, station=None) -> list[dict]:
    if station:
        payload = api_get(f"issues/station/{urllib.parse.quote(station)}")
        issues = payload.get("data") or []
        return [
            i
            for i in issues
            if (not status or i.get("status") == status)
            and (not assignee or assignee in _assignee_ids(i))
            and (not country or (_site(i).get("Country") or "").upper() == country)
        ]
    payload = api_get("issues", {"status": status, "assignee": assignee, "country": country})
    return payload.get("data") or []


def fetch_issue(ref: str) -> dict:
    """One issue by ticket number (1234, #1234) or 24-character database ID."""
    text = ref.strip()
    if _DATABASE_ID.match(text):
        payload, what = api_get(f"issues/{text}"), f"database ID {text}"
    else:
        number = text.lstrip("#").strip()
        if not (number.isascii() and number.isdigit() and int(number) >= 1):
            raise UsageError(
                f"{ref!r} is neither a ticket number (e.g. 1234) nor a 24-character database ID"
            )
        number = str(int(number))  # the API matches "4", not "004"
        payload, what = api_get(f"issues/number/{number}"), f"ticket number {number}"
    # A lookup that matches nothing answers HTTP 200, success: true, no data.
    if not payload.get("data"):
        raise SkillError(f"no ticket found with {what}")
    return payload["data"]


# ------------------------------------------------------------------ issue fields


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _site(issue: dict) -> dict:
    site = issue.get("site")
    return site if isinstance(site, dict) else {}


def _people(value) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"full_name": value}]
    if isinstance(value, dict):
        return [value]
    return [p if isinstance(p, dict) else {"full_name": str(p)} for p in value]


def _assignee_names(issue: dict) -> list[str]:
    return [p["full_name"] for p in _people(issue.get("assignee")) if p.get("full_name")]


def _assignee_ids(issue: dict) -> list[str]:
    return [p["_id"] for p in _people(issue.get("assignee")) if p.get("_id")]


def _opened_by(issue: dict) -> str | None:
    people = _people(issue.get("opened_by"))
    return people[0].get("full_name") if people else None


def _unresolved(issue: dict) -> bool:
    return issue.get("status") != "close"


def _overdue(issue: dict, now: datetime) -> bool:
    due = _parse_time(issue.get("due_date"))
    return _unresolved(issue) and due is not None and due < now


def _age_days(issue: dict, now: datetime) -> int | None:
    created = _parse_time(issue.get("created_at"))
    return max((now - created).days, 0) if created else None


def serialize(issue: dict, now: datetime, *, full: bool = False) -> dict:
    site = _site(issue)
    description = issue.get("description") or ""
    if not full and len(description) > DESCRIPTION_CHARS:
        description = description[:DESCRIPTION_CHARS].rstrip() + "…"
    out = {
        "ticket_id": issue.get("ticket_id"),
        "id": issue.get("_id"),
        "title": issue.get("title"),
        "status": issue.get("status"),
        "priority": issue.get("priority"),
        "station": issue.get("station"),
        "country": site.get("Country"),
        "site_name": site.get("SiteName"),
        "device_id": issue.get("deviceId"),
        "labels": issue.get("labels") or [],
        "assignees": _assignee_names(issue),
        "opened_by": _opened_by(issue),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "due_date": issue.get("due_date"),
        "overdue": _overdue(issue, now),
        "open_age_days": _age_days(issue, now) if _unresolved(issue) else None,
        "description": description,
    }
    if full:
        out["comments"] = issue.get("comments") or []
        out["muted"] = bool(issue.get("isMuted"))
    return out


# ------------------------------------------------------------------ filtering


def _matches(issue: dict, label, created_from, created_to) -> bool:
    if label and label.lower() not in (str(x).lower() for x in issue.get("labels") or []):
        return False
    created = _parse_time(issue.get("created_at"))
    day = created.date().isoformat() if created else None
    if created_from and (day is None or day < created_from):
        return False
    return not (created_to and (day is None or day > created_to))


def _group_keys(issue: dict, group_by: str) -> list[str]:
    if group_by == "assignee":
        return _assignee_names(issue) or [UNASSIGNED]
    if group_by == "label":
        return [str(x) for x in issue.get("labels") or []] or ["(no label)"]
    if group_by == "country":
        return [_site(issue).get("Country") or UNKNOWN]
    if group_by == "month":
        created = _parse_time(issue.get("created_at"))
        return [created.strftime("%Y-%m") if created else UNKNOWN]
    if group_by == "opened_by":
        return [_opened_by(issue) or UNKNOWN]
    return [issue.get(group_by) or UNKNOWN]  # status, priority, station


def summarise(issues: list[dict], group_by: str, now: datetime) -> dict:
    """Counts, ages and overdue issues, overall and per group."""

    def stats(members: list[dict]) -> dict:
        ages = [a for i in members if _unresolved(i) for a in [_age_days(i, now)] if a is not None]
        return {
            "total": len(members),
            "open": sum(i.get("status") == "open" for i in members),
            "pending": sum(i.get("status") == "pending" for i in members),
            "closed": sum(i.get("status") == "close" for i in members),
            "overdue": sum(_overdue(i, now) for i in members),
            "median_open_age_days": float(median(ages)) if ages else None,
            "oldest_open_days": max(ages) if ages else None,
        }

    groups: dict[str, list[dict]] = defaultdict(list)
    for issue in issues:
        for key in _group_keys(issue, group_by):
            groups[key].append(issue)
    rows = [{"key": key, **stats(members)} for key, members in groups.items()]
    if group_by == "month":
        rows.sort(key=lambda r: r["key"], reverse=True)
    else:
        rows.sort(key=lambda r: (-r["total"], r["key"]))

    # The service has no close date: creation to last update approximates it.
    closed_days = [
        max((u - c).days, 0)
        for i in issues
        if i.get("status") == "close"
        for c, u in [(_parse_time(i.get("created_at")), _parse_time(i.get("updated_at")))]
        if c and u
    ]
    created = sorted(c for i in issues if (c := _parse_time(i.get("created_at"))))
    overall = stats(issues)
    return {
        "total_issues": overall["total"],
        "by_status": dict(Counter(i.get("status") for i in issues).most_common()),
        "overdue": overall["overdue"],
        "median_open_age_days": overall["median_open_age_days"],
        "oldest_open_days": overall["oldest_open_days"],
        "median_days_to_close": float(median(closed_days)) if closed_days else None,
        "first_created": created[0].date().isoformat() if created else None,
        "last_created": created[-1].date().isoformat() if created else None,
        "group_by": group_by,
        "groups_total": len(rows),
        "groups": rows,
    }


# ------------------------------------------------------------------ commands


def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from None


def _station_code(value: str) -> str:
    code = value.strip().upper()
    if not code:
        raise argparse.ArgumentTypeError("station code cannot be empty")
    return code


def _filtered(args) -> list[dict]:
    issues = fetch_issues(
        status=args.status,
        assignee=args.assignee,
        country=args.country.upper() if args.country else None,
        station=getattr(args, "station", None),
    )
    return [i for i in issues if _matches(i, args.label, args.created_from, args.created_to)]


def _filters(args) -> dict:
    keys = ("status", "assignee", "country", "station", "label", "created_from", "created_to")
    return {k: getattr(args, k, None) for k in keys if getattr(args, k, None) is not None}


def cmd_list(args, now) -> dict:
    issues = _filtered(args)
    issues.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    shown = issues[: args.limit]
    return {
        "filters": _filters(args),
        "total_matches": len(issues),
        "returned": len(shown),
        "issues": [serialize(i, now) for i in shown],
    }


def cmd_get(args, now) -> dict:
    return serialize(fetch_issue(args.ticket), now, full=True)


def cmd_summary(args, now) -> dict:
    result = summarise(_filtered(args), args.group_by, now)
    notes = []
    if args.group_by in ("assignee", "label"):
        notes.append(
            "An issue with several assignees or labels counts once for each, so group "
            "totals can add up to more than the number of issues."
        )
    if result["groups_total"] > args.top:
        notes.append(
            f"{result['groups_total'] - args.top} more group(s) not listed; raise --top "
            "or narrow the filters."
        )
    result["groups"] = result["groups"][: args.top]
    return {"filters": _filters(args), **result, "notes": notes}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query the TAHMO ticketing system. Prints JSON on stdout.",
        epilog=f"skill version: {_SKILL_VERSION}",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add_filters(p, *, station=True):
        p.add_argument("--status", choices=STATUSES, help="Issue status.")
        p.add_argument("--assignee", help="Assignee database ID.")
        p.add_argument("--country", help="Two-letter country code, e.g. KE.")
        if station:
            p.add_argument("--station", type=_station_code, help="Station code, e.g. TA00172.")
        p.add_argument("--label", help="Only issues with this label (case-insensitive).")
        p.add_argument("--created-from", type=_iso_date, help="Created on or after YYYY-MM-DD.")
        p.add_argument("--created-to", type=_iso_date, help="Created on or before YYYY-MM-DD.")

    p_list = sub.add_parser("list", help="List issues, newest first.")
    add_filters(p_list)
    p_list.add_argument("--limit", type=int, default=20, help="Issues to show (1-200, default 20).")

    p_get = sub.add_parser("get", help="One issue in full, including comments.")
    p_get.add_argument("ticket", help="Ticket number (1234 or #1234) or 24-character database ID.")

    p_station = sub.add_parser("station", help="Issues for one station, newest first.")
    p_station.add_argument("station", type=_station_code, help="Station code, e.g. TA00172.")
    add_filters(p_station, station=False)
    p_station.add_argument("--limit", type=int, default=50, help="Issues to show (default 50).")

    p_sum = sub.add_parser("summary", help="Count and group issues instead of listing them.")
    add_filters(p_sum)
    p_sum.add_argument(
        "--group-by", choices=GROUPS, default="assignee", help="Grouping (default assignee)."
    )
    p_sum.add_argument("--top", type=int, default=20, help="Groups to show (1-200, default 20).")
    return parser


COMMANDS = {"list": cmd_list, "get": cmd_get, "station": cmd_list, "summary": cmd_summary}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        limit = getattr(args, "limit", None) or getattr(args, "top", None)
        if limit is not None and not 1 <= limit <= 200:
            raise UsageError("--limit/--top must be between 1 and 200")
        result = COMMANDS[args.command](args, datetime.now(UTC))
    except SkillError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return exc.exit_code
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
