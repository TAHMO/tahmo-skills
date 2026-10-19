"""Correctness tests for tickets (fake ticketing API; no live network)."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from datetime import UTC, datetime

import pytest
from conftest import load_skill

TOKEN = {"TICKETING_API_TOKEN": "test-token"}


def issue(ticket, *, status="open", assignee=None, country="KE", station="TA00001",
          created="2026-09-01T08:00:00Z", updated="2026-09-02T08:00:00Z", due=None,
          labels=(), description="Station offline", opened_by="Ama Mensah"):  # fmt: skip
    return {
        "_id": f"{int(ticket):024x}",
        "ticket_id": str(ticket),
        "title": f"Issue {ticket}",
        "description": description,
        "status": status,
        "priority": "HIGH",
        "station": station,
        "deviceId": "06-00316",
        "labels": list(labels),
        "comments": ["checked on site"],
        "opened_by": {"_id": "p0", "full_name": opened_by},
        "assignee": assignee,
        "site": {"SiteCode": station, "SiteName": "Site", "Country": country},
        "created_at": created,
        "updated_at": updated,
        "due_date": due,
    }


ISSUES = [
    issue(1, assignee={"_id": "p1", "full_name": "Victor"}, due="2026-09-05T00:00:00Z",
          labels=["power"], created="2026-08-01T08:00:00Z"),
    issue(2, status="pending", assignee=[{"_id": "p1", "full_name": "Victor"},
                                         {"_id": "p2", "full_name": "George"}],
          labels=["Power", "comms"], created="2026-09-10T08:00:00Z"),
    issue(3, status="close", assignee="Victor", country="GH", station="TA00002",
          created="2026-07-01T00:00:00Z", updated="2026-07-11T00:00:00Z"),
    issue(4, assignee=None, station="TA00002", country="GH", created="2026-09-20T00:00:00Z"),
]  # fmt: skip


class FakeAPI:
    def __init__(self, issues=ISSUES, fail=()):
        self.issues = issues
        self.fail = list(fail)
        self.calls = []

    def __call__(self, request, timeout=None):
        url = urllib.parse.urlparse(request.full_url)
        params = dict(urllib.parse.parse_qsl(url.query))
        self.calls.append((url.path, params, request.get_header("X-api-key")))
        if self.fail:
            code, body = self.fail.pop(0)
            raise urllib.error.HTTPError(
                request.full_url, code, "error", {}, io.BytesIO(json.dumps(body).encode())
            )
        path = url.path.removeprefix("/api/")
        if path == "issues":
            data = [
                i
                for i in self.issues
                if params.get("status", i["status"]) == i["status"]
                and params.get("country", i["site"]["Country"]) == i["site"]["Country"]
            ]
            body = {"success": True, "message": "ok", "count": len(data), "data": data}
        elif path.startswith("issues/station/"):
            code = path.rsplit("/", 1)[1]
            data = [i for i in self.issues if i["station"] == code]
            body = {"success": True, "message": "ok", "count": len(data), "data": data}
        elif path.startswith("issues/number/"):
            n = path.rsplit("/", 1)[1]
            match = [i for i in self.issues if i["ticket_id"] == n]
            body = {"success": True, "message": "ok", **({"data": match[0]} if match else {})}
        else:
            ident = path.rsplit("/", 1)[1]
            match = [i for i in self.issues if i["_id"] == ident]
            body = {"success": True, "message": "ok", **({"data": match[0]} if match else {})}
        return io.BytesIO(json.dumps(body).encode())


@pytest.fixture(scope="module")
def mod():
    return load_skill("tickets", "tickets")


@pytest.fixture
def fixed_now(mod, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, tzinfo=UTC)

    monkeypatch.setattr(mod, "datetime", FixedDatetime)


def run(mod, monkeypatch, capsys, api, *argv):
    monkeypatch.setenv("TICKETING_API_TOKEN", TOKEN["TICKETING_API_TOKEN"])
    monkeypatch.setattr(mod.urllib.request, "urlopen", api)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    code = mod.main(list(argv))
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def test_list_sends_token_and_filters(mod, monkeypatch, capsys, fixed_now):
    api = FakeAPI()
    code, out, _ = run(mod, monkeypatch, capsys, api, "list", "--status", "open", "--country", "ke")
    assert code == 0
    path, params, token = api.calls[0]
    assert path == "/api/issues" and params == {"status": "open", "country": "KE"}
    assert token == "test-token"
    assert out["total_matches"] == 1 and out["issues"][0]["ticket_id"] == "1"
    first = out["issues"][0]
    assert first["overdue"] is True and first["open_age_days"] == 60
    assert first["assignees"] == ["Victor"] and first["country"] == "KE"


def test_list_newest_first_with_limit_and_client_side_filters(mod, monkeypatch, capsys, fixed_now):
    _, out, _ = run(mod, monkeypatch, capsys, FakeAPI(), "list", "--limit", "2")
    assert [i["ticket_id"] for i in out["issues"]] == ["4", "2"]
    assert out["total_matches"] == 4 and out["returned"] == 2

    _, out, _ = run(mod, monkeypatch, capsys, FakeAPI(), "list", "--label", "POWER")
    assert sorted(i["ticket_id"] for i in out["issues"]) == ["1", "2"]

    _, out, _ = run(mod, monkeypatch, capsys, FakeAPI(), "list", "--created-from", "2026-09-01")
    assert sorted(i["ticket_id"] for i in out["issues"]) == ["2", "4"]


def test_get_by_number_or_database_id(mod, monkeypatch, capsys, fixed_now):
    api = FakeAPI()
    _, out, _ = run(mod, monkeypatch, capsys, api, "get", "#003")
    assert api.calls[-1][0] == "/api/issues/number/3"  # leading zeros dropped
    assert out["ticket_id"] == "3" and out["comments"] == ["checked on site"]
    assert out["overdue"] is False and out["open_age_days"] is None  # closed

    db_id = ISSUES[0]["_id"]
    _, out, _ = run(mod, monkeypatch, capsys, api, "get", db_id)
    assert api.calls[-1][0] == f"/api/issues/{db_id}" and out["ticket_id"] == "1"


def test_get_missing_ticket_and_bad_reference(mod, monkeypatch, capsys):
    code, out, err = run(mod, monkeypatch, capsys, FakeAPI(), "get", "999")
    assert code == 1 and out is None and "no ticket found with ticket number 999" in err
    code, _, err = run(mod, monkeypatch, capsys, FakeAPI(), "get", "abc")
    assert code == 2 and "neither a ticket number" in err


def test_station_command_uses_station_endpoint(mod, monkeypatch, capsys, fixed_now):
    api = FakeAPI()
    _, out, _ = run(mod, monkeypatch, capsys, api, "station", "ta00002", "--status", "open")
    assert api.calls[0][0] == "/api/issues/station/TA00002"
    assert [i["ticket_id"] for i in out["issues"]] == ["4"]


def test_summary_counts_and_groups(mod, monkeypatch, capsys, fixed_now):
    _, out, _ = run(mod, monkeypatch, capsys, FakeAPI(), "summary", "--group-by", "assignee")
    assert out["total_issues"] == 4
    assert out["by_status"] == {"open": 2, "pending": 1, "close": 1}
    assert out["overdue"] == 1
    assert out["median_days_to_close"] == 10.0
    groups = {g["key"]: g for g in out["groups"]}
    assert groups["Victor"]["total"] == 3 and groups["Victor"]["closed"] == 1
    assert groups["George"]["total"] == 1
    assert groups["(unassigned)"]["open"] == 1
    assert any("counts once for each" in n for n in out["notes"])

    _, out, _ = run(mod, monkeypatch, capsys, FakeAPI(), "summary", "--group-by", "month")
    assert [g["key"] for g in out["groups"]] == ["2026-09", "2026-08", "2026-07"]

    _, out, _ = run(mod, monkeypatch, capsys, FakeAPI(), "summary", "--group-by", "country",
                    "--top", "1")  # fmt: skip
    assert out["groups_total"] == 2 and len(out["groups"]) == 1
    assert any("1 more group" in n for n in out["notes"])


def test_missing_and_rejected_token(mod, monkeypatch, capsys):
    monkeypatch.delenv("TICKETING_API_TOKEN", raising=False)
    assert mod.main(["list"]) == 2
    assert "TICKETING_API_TOKEN must be set" in capsys.readouterr().err

    api = FakeAPI(fail=[(401, {"success": False, "message": "Invalid or expired API token"})])
    code, _, err = run(mod, monkeypatch, capsys, api, "list")
    assert code == 2 and "Invalid or expired API token" in err


def test_retries_transient_errors(mod, monkeypatch, capsys, fixed_now):
    api = FakeAPI(fail=[(503, {}), (429, {})])
    code, out, _ = run(mod, monkeypatch, capsys, api, "list")
    assert code == 0 and len(api.calls) == 3 and out["total_matches"] == 4


def test_base_url_override(mod, monkeypatch, capsys, fixed_now):
    monkeypatch.setenv("TICKETING_API_BASE_URL", "https://staging.example.org/api/")
    api = FakeAPI()
    run(mod, monkeypatch, capsys, api, "list")
    assert api.calls[0][0] == "/api/issues"
    assert mod._base_url() == "https://staging.example.org/api"
