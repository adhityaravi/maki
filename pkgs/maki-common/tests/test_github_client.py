"""Tests for ``maki_common.github_client.GitHubIssueClient``.

These tests stub GitHub App auth and swap the client's underlying
``httpx.AsyncClient`` for one backed by ``httpx.MockTransport`` so we can
drive each public method against fake responses without real network I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

import httpx
from maki_common.github_client import API, GitHubIssueClient
from maki_common.tools.github import GitHubAuth


def _run(coro):
    return asyncio.run(coro)


def _make_client(handler) -> GitHubIssueClient:
    """Build a client wired to a MockTransport and a stubbed auth.headers()."""
    client = GitHubIssueClient.__new__(GitHubIssueClient)
    client._owner = "acme"
    client._repo = "widgets"

    transport = httpx.MockTransport(handler)
    client._client = httpx.AsyncClient(transport=transport, timeout=5.0)

    class _StubAuth:
        async def headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer test-token"}

    client._auth = cast(GitHubAuth, _StubAuth())
    return client


# ---------------------------------------------------------------------------
# _request helper
# ---------------------------------------------------------------------------


def test_request_returns_response_and_logs_ok(caplog):
    """On 2xx, helper logs ok_log (if given) and returns the Response."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler)

    async def scenario():
        with caplog.at_level(logging.INFO, logger="maki_common.github_client"):
            return await client._request(
                "GET",
                f"{API}/ping",
                err_log="should-not-fire",
                ok_log="ping-ok",
                ok_extra={"foo": "bar"},
            )

    resp = _run(scenario())
    assert resp is not None
    assert resp.status_code == 200
    assert calls[0].headers.get("Authorization") == "Bearer test-token"
    assert any("ping-ok" in rec.message for rec in caplog.records)


def test_request_returns_none_on_http_error_and_logs(caplog):
    """On non-2xx, helper logs err_log with err_extra and returns None."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _make_client(handler)

    async def scenario():
        with caplog.at_level(logging.ERROR, logger="maki_common.github_client"):
            return await client._request(
                "GET",
                f"{API}/broken",
                err_log="request-failed",
                err_extra={"reason": "unit-test"},
            )

    result = _run(scenario())
    assert result is None
    err_records = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert any("request-failed" in rec.message for rec in err_records)


def test_request_skips_ok_log_when_not_provided(caplog):
    """Callers that log themselves (response-dependent fields) can omit ok_log."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _make_client(handler)

    async def scenario():
        with caplog.at_level(logging.INFO, logger="maki_common.github_client"):
            return await client._request(
                "GET",
                f"{API}/quiet",
                err_log="nope",
            )

    _run(scenario())
    info_records = [
        rec for rec in caplog.records if rec.name == "maki_common.github_client" and rec.levelno == logging.INFO
    ]
    assert info_records == []


# ---------------------------------------------------------------------------
# Public method smoke tests
# ---------------------------------------------------------------------------


def test_create_issue_returns_number():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/repos/acme/widgets/issues"
        payload = json.loads(request.content)
        assert payload == {"title": "hi", "body": "b", "labels": ["p1"]}
        return httpx.Response(201, json={"number": 42})

    client = _make_client(handler)
    number = _run(client.create_issue(title="hi", body="b", labels=["p1"]))
    assert number == 42


def test_create_issue_returns_none_on_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text="invalid")

    client = _make_client(handler)
    assert _run(client.create_issue(title="bad")) is None


def test_get_issue_returns_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/repos/acme/widgets/issues/7"
        return httpx.Response(200, json={"number": 7, "title": "t"})

    client = _make_client(handler)
    issue = _run(client.get_issue(7))
    assert issue == {"number": 7, "title": "t"}


def test_get_issue_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _make_client(handler)
    assert _run(client.get_issue(999)) is None


def test_get_issue_comments_shapes_output():
    raw = [
        {"user": {"login": "alice"}, "body": "hi", "created_at": "t1"},
        {"body": "no user"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("per_page") == "50"
        return httpx.Response(200, json=raw)

    client = _make_client(handler)
    comments = _run(client.get_issue_comments(5))
    assert comments == [
        {"author": "alice", "body": "hi", "created_at": "t1"},
        {"author": "unknown", "body": "no user", "created_at": ""},
    ]


def test_get_issue_comments_empty_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _make_client(handler)
    assert _run(client.get_issue_comments(5)) == []


def test_comment_issue_true_on_success_false_on_error():
    responses = iter([httpx.Response(201, json={}), httpx.Response(500)])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = _make_client(handler)
    assert _run(client.comment_issue(1, "hello")) is True
    assert _run(client.comment_issue(1, "hello")) is False


def test_close_issue_sends_patch_and_optional_comment():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(201, json={})
        if request.method == "PATCH":
            assert json.loads(request.content) == {"state": "closed"}
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {request.method}")

    client = _make_client(handler)
    assert _run(client.close_issue(3, comment="done")) is True
    assert ("POST", "/repos/acme/widgets/issues/3/comments") in seen
    assert ("PATCH", "/repos/acme/widgets/issues/3") in seen


def test_close_issue_false_on_patch_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return httpx.Response(500)
        return httpx.Response(201, json={})

    client = _make_client(handler)
    assert _run(client.close_issue(3)) is False


def test_add_label_and_remove_label():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert json.loads(request.content) == {"labels": ["bug"]}
            return httpx.Response(200, json=[])
        if request.method == "DELETE":
            assert request.url.path.endswith("/labels/bug")
            return httpx.Response(200, json=[])
        raise AssertionError

    client = _make_client(handler)
    assert _run(client.add_label(1, "bug")) is True
    assert _run(client.remove_label(1, "bug")) is True


def test_add_label_false_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _make_client(handler)
    assert _run(client.add_label(1, "bug")) is False
    assert _run(client.remove_label(1, "bug")) is False


def test_list_issues_paginates_and_sorts_by_priority():
    page1: list[dict[str, Any]] = []
    for i in range(99):
        page1.append(
            {
                "number": i,
                "labels": [{"name": "P3"}] if i % 2 == 0 else [{"name": "P1"}],
            }
        )
    page1.append({"number": 999, "pull_request": {}, "labels": []})
    page2 = [{"number": 1000, "labels": [{"name": "P5"}]}]

    pages = iter([page1, page2])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(pages))

    client = _make_client(handler)
    issues = _run(client.list_issues())
    assert all("pull_request" not in i for i in issues)
    priorities = [i["labels"][0]["name"] for i in issues]
    assert priorities == sorted(priorities, key=lambda p: {"P1": 1, "P3": 3, "P5": 5}[p])


def test_list_issues_empty_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _make_client(handler)
    assert _run(client.list_issues()) == []


def test_list_issues_sorts_by_priority_before_truncating():
    """Regression for #404: newest P1/P2 issues must survive the cap.

    Regardless of fetch direction, the priority sort must lift P1/P2 issues
    to the head of the returned list *before* any cap-driven truncation, so
    they always survive. The pages here are yielded in fixed order (P4s
    first, P1/P2 last) to simulate a fetch where high-priority items live
    on the trailing page — the head of the returned slice must still be
    the P1/P2, not P4 filler.
    """
    # Page 1: 100 low-priority issues (P4).
    page1 = [{"number": i, "labels": [{"name": "P4"}]} for i in range(100)]
    # Page 2: 100 more low-priority (P4).
    page2 = [{"number": 100 + i, "labels": [{"name": "P4"}]} for i in range(100)]
    # Page 3: the tail — a P1 and P2 that must not be lost.
    page3 = [
        {"number": 200, "labels": [{"name": "P1"}]},
        {"number": 201, "labels": [{"name": "P2"}]},
    ]
    pages = iter([page1, page2, page3])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(pages))

    client = _make_client(handler)
    # Cap of 50 — the returned list must still surface the P1 and P2 at the
    # head, not silently drop them along with the rest of the trailing tail.
    issues = _run(client.list_issues(max_results=50))
    assert len(issues) == 50
    numbers = [i["number"] for i in issues]
    assert 200 in numbers, "newest P1 dropped by pre-sort truncation"
    assert 201 in numbers, "newest P2 dropped by pre-sort truncation"
    # Priority ordering must hold: P1 before P2 before the P4 filler.
    priorities = [i["labels"][0]["name"] for i in issues]
    assert priorities[0] == "P1"
    assert priorities[1] == "P2"
    assert all(p == "P4" for p in priorities[2:])


def test_list_issues_fetches_newest_first_so_truncation_drops_oldest():
    """Regression for #552: cap-hit truncation must drop OLDEST, not newest.

    Silent oldest-first truncation caused reflection dedup blindness — every
    cycle re-filed the same bug because it could not see the previous cycle's
    output past the cap. Fetch must request ``direction=desc`` so that within
    the untriaged tier (which is stable-sorted) the newest survive the cap.
    """
    seen_directions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_directions.append(request.url.params.get("direction", ""))
        # Short page so pagination stops after one call.
        return httpx.Response(200, json=[{"number": 1, "labels": []}])

    client = _make_client(handler)
    _run(client.list_issues())
    assert seen_directions and all(d == "desc" for d in seen_directions), (
        f"list_issues must fetch newest-first; got directions={seen_directions}"
    )


def test_list_issues_truncation_keeps_newest_untriaged():
    """Regression for #552: after truncation, newest untriaged must survive.

    The API is asked in ``direction=desc`` order, so with the priority sort
    being stable within a tier, capping the returned slice should drop the
    oldest untriaged issues rather than the newest — the reverse of the
    pre-#552 default behavior.
    """
    # GitHub API returns issues in the direction we ask for; the handler
    # honors that so this test exercises the true end-to-end ordering.
    all_issues = [{"number": i, "labels": []} for i in range(150)]

    def handler(request: httpx.Request) -> httpx.Response:
        direction = request.url.params.get("direction", "asc")
        page = int(request.url.params.get("page", "1"))
        ordered = list(reversed(all_issues)) if direction == "desc" else all_issues
        start = (page - 1) * 100
        return httpx.Response(200, json=ordered[start : start + 100])

    client = _make_client(handler)
    issues = _run(client.list_issues(max_results=50))
    numbers = [i["number"] for i in issues]
    assert len(numbers) == 50
    # Newest (#149) must be present; oldest (#0) must be dropped.
    assert 149 in numbers, "newest untriaged dropped — direction=asc regression"
    assert 0 not in numbers, "oldest untriaged surfaced — cap ordering wrong"


def test_list_issues_no_cap_when_max_results_none():
    """max_results=None pages until GitHub returns a short page (last page)."""
    page1 = [{"number": i, "labels": []} for i in range(100)]
    page2 = [{"number": 100 + i, "labels": []} for i in range(100)]
    page3 = [{"number": 200 + i, "labels": []} for i in range(50)]  # short → last
    pages = iter([page1, page2, page3])
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(int(request.url.params.get("page", "1")))
        return httpx.Response(200, json=next(pages))

    client = _make_client(handler)
    issues = _run(client.list_issues(max_results=None))
    assert len(issues) == 250
    assert calls == [1, 2, 3]  # stopped on short page, no over-fetch


def test_search_issues_by_symbols_scores_and_sorts():
    """≥min_matches enforced; scoring is by matched-symbol count desc."""
    items = [
        # Two symbols matched — should pass threshold.
        {"number": 1, "title": "get_issue_comments truncates", "body": "affects github_client.py"},
        # One symbol matched — below default threshold, must be dropped.
        {"number": 2, "title": "Rework retries", "body": "touches github_client.py only"},
        # Three symbols matched — should sort to the head.
        {"number": 3, "title": "get_issue_comments per_page cap", "body": "in github_client.py"},
        # A PR — must be filtered even if it matches.
        {"number": 4, "title": "get_issue_comments PR", "body": "github_client.py per_page", "pull_request": {}},
        # Zero symbols matched — dropped.
        {"number": 5, "title": "Unrelated", "body": "nothing here"},
    ]
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/issues"
        captured.append(request.url.params.get("q", ""))
        return httpx.Response(200, json={"items": items})

    client = _make_client(handler)
    results = _run(
        client.search_issues_by_symbols(
            ["get_issue_comments", "github_client.py", "per_page"],
        )
    )
    # Quoted OR clause, correct repo scope.
    assert captured and "repo:acme/widgets" in captured[0]
    assert '"get_issue_comments"' in captured[0]
    assert " OR " in captured[0]
    # #3 (3 hits) beats #1 (2 hits); #2/#4/#5 dropped.
    assert [r["number"] for r in results] == [3, 1]
    assert results[0]["score"] == 3
    assert set(results[0]["matched"]) == {"get_issue_comments", "github_client.py", "per_page"}
    assert results[1]["score"] == 2


def test_search_issues_by_symbols_empty_input_returns_empty():
    """Whitespace-only or empty symbol list short-circuits — no request fired."""
    fired: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fired.append(request)
        return httpx.Response(200, json={"items": []})

    client = _make_client(handler)
    assert _run(client.search_issues_by_symbols([])) == []
    assert _run(client.search_issues_by_symbols(["", "  "])) == []
    assert fired == []


def test_search_issues_by_symbols_caps_or_clause_at_five():
    """GitHub rejects boolean queries with too many terms — cap at 5."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.params.get("q", ""))
        return httpx.Response(200, json={"items": []})

    client = _make_client(handler)
    _run(client.search_issues_by_symbols(["a", "b", "c", "d", "e", "f", "g"]))
    # 5 quoted terms → 4 " OR " joins in the OR clause.
    assert captured and captured[0].count(" OR ") == 4


def test_search_issues_by_symbols_returns_empty_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _make_client(handler)
    assert _run(client.search_issues_by_symbols(["x", "y"])) == []


def test_search_issues_by_symbols_min_matches_override():
    """min_matches=1 lets single-symbol hits through."""
    items = [
        {"number": 10, "title": "single hit", "body": "just alpha here"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": items})

    client = _make_client(handler)
    default_result = _run(client.search_issues_by_symbols(["alpha", "beta"]))
    assert default_result == []  # default min_matches=2 drops single-hit
    loose_result = _run(client.search_issues_by_symbols(["alpha", "beta"], min_matches=1))
    assert [r["number"] for r in loose_result] == [10]


def test_find_open_issue_matches_title():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/issues"
        return httpx.Response(
            200,
            json={
                "items": [
                    {"number": 11, "title": "Unrelated"},
                    {"number": 22, "title": "Refactor the client"},
                ],
            },
        )

    client = _make_client(handler)
    assert _run(client.find_open_issue("refactor")) == 22


def test_find_open_issue_none_on_no_match():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    client = _make_client(handler)
    assert _run(client.find_open_issue("nothing")) is None


def test_find_open_issue_none_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _make_client(handler)
    assert _run(client.find_open_issue("boom")) is None
