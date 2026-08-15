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

    The API returns issues sorted asc-by-created, so newly-filed issues land
    on the last page. If the cap is applied *before* the priority sort, those
    fresh issues get silently dropped even when they carry P1/P2 labels — the
    work loop then never sees them. Sort-then-truncate must land priority
    issues at the head of the returned list regardless of file order.
    """
    # Page 1: 100 low-priority issues (P4) filed first.
    page1 = [{"number": i, "labels": [{"name": "P4"}]} for i in range(100)]
    # Page 2: 100 low-priority (P4) filed next.
    page2 = [{"number": 100 + i, "labels": [{"name": "P4"}]} for i in range(100)]
    # Page 3: the tail — a fresh P1 and P2 that must not be lost.
    page3 = [
        {"number": 200, "labels": [{"name": "P1"}]},
        {"number": 201, "labels": [{"name": "P2"}]},
    ]
    pages = iter([page1, page2, page3])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(pages))

    client = _make_client(handler)
    # Cap of 50 — the returned list must still surface the P1 and P2 at the
    # head, not silently drop them along with the rest of the asc tail.
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
