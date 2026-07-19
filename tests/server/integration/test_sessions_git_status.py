"""Tests for ``GET /v1/sessions/{id}/git-status``.

The workspace lives on the runner, so the route proxies the git half
there and joins it with pull requests resolved server-side. These tests
stub the runner and the GitHub integration to pin the contract the
composer status bar consumes — in particular that a missing workspace,
a non-git workspace, and an unconfigured GitHub integration are all
*successful* responses, never errors.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnicraft.entities import Conversation
from omnicraft.errors import OmniCraftError
from omnicraft.runtime import _globals, set_runner_client, set_runner_router
from omnicraft.server.routes import integrations
from omnicraft.server.routes.sessions import create_sessions_router

_SESSION_ID = "conv_git"


class _ConversationStore:
    """Single-conversation store — enough for route auth/validation."""

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """
        :param conversation_id: Conversation id to look up.
        :returns: The canned conversation, or ``None``.
        """
        if conversation_id != _SESSION_ID:
            return None
        return Conversation(
            id=_SESSION_ID,
            created_at=1,
            updated_at=1,
            root_conversation_id=_SESSION_ID,
            agent_id="ag_test",
        )


class _StubAgentStore:
    """Agent store stub — these routes never resolve an agent."""

    def get(self, agent_id: str) -> None:
        """
        :param agent_id: Agent id.
        :returns: None (no agents in the stub).
        """
        return


class _FakeRunnerClient:
    """Runner client returning one canned git-status payload.

    :param payload: JSON body the runner answers with.
    :param exc: Exception raised instead of answering, to simulate an
        unreachable runner.
    """

    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        exc: Exception | None = None,
    ) -> None:
        """
        :param payload: Canned runner response body.
        :param exc: Exception to raise on every request.
        :returns: None.
        """
        self._payload = payload or {}
        self._exc = exc
        self.calls: list[str] = []

    async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        """
        :param url: Runner-relative path being fetched.
        :param _kwargs: Ignored transport options.
        :returns: The canned response.
        """
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        return httpx.Response(
            200,
            json=self._payload,
            request=httpx.Request("GET", f"http://runner{url}"),
        )


@pytest.fixture
def runner_globals_reset() -> Iterator[None]:
    """Restore the process-global runner client/router after each test.

    :returns: Iterator yielding once, inside the reset window.
    """
    prior_client = _globals._runner_client
    prior_router = _globals._runner_router
    set_runner_client(None)
    set_runner_router(None)
    yield
    set_runner_client(prior_client)
    set_runner_router(prior_router)


@pytest.fixture
def app(runner_globals_reset: None) -> FastAPI:
    """Build a FastAPI app with just the sessions router mounted.

    :param runner_globals_reset: Ensures clean runner globals.
    :returns: The configured app.
    """
    del runner_globals_reset
    app = FastAPI()

    @app.exception_handler(OmniCraftError)
    async def _handle_omnicraft_error(request: Request, exc: OmniCraftError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            _ConversationStore(),  # type: ignore[arg-type]
            _StubAgentStore(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client bound to the test app.

    :param app: The FastAPI app under test.
    :returns: Iterator yielding the client.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as c:
        yield c


def _runner_payload(**overrides: Any) -> dict[str, Any]:
    """Build a runner git-status body, defaulting to the empty shape.

    :param overrides: Fields to override on the empty payload.
    :returns: The runner response body.
    """
    return {
        "object": "session.git_status",
        "session_id": _SESSION_ID,
        "workspace": None,
        "branch": None,
        "base_branch": None,
        "ahead": None,
        "behind": None,
        "diff": None,
        "repo_slug": None,
        "error": None,
        **overrides,
    }


@pytest.fixture(autouse=True)
def no_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the network unless it opts into GitHub.

    :param monkeypatch: Pytest patcher.
    :returns: None.
    """

    async def _no_token() -> None:
        return None

    monkeypatch.setattr(integrations, "_github_token", _no_token)


@pytest.mark.asyncio
async def test_session_without_workspace_is_an_empty_success(
    client: httpx.AsyncClient,
) -> None:
    """No workspace is a normal all-``None`` answer, not a 404 or an error."""
    set_runner_client(_FakeRunnerClient(payload=_runner_payload()))  # type: ignore[arg-type]

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    assert resp.status_code == 200
    assert resp.json() == {
        "object": "session.git_status",
        "session_id": _SESSION_ID,
        "workspace": None,
        "branch": None,
        "base_branch": None,
        "ahead": None,
        "behind": None,
        "diff": None,
        "repo_slug": None,
        "prs": [],
        # Nothing to ask GitHub about, so the empty list is the truth.
        "prs_status": "ok",
        "error": None,
    }


@pytest.mark.asyncio
async def test_non_git_workspace_reports_the_path_and_nothing_else(
    client: httpx.AsyncClient,
) -> None:
    """A workspace outside a git repo keeps ``error`` clear."""
    set_runner_client(  # type: ignore[arg-type]
        _FakeRunnerClient(payload=_runner_payload(workspace="/tmp/plain"))
    )

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    body = resp.json()
    assert resp.status_code == 200
    assert body["workspace"] == "/tmp/plain"
    assert body["branch"] is None
    assert body["diff"] is None
    assert body["error"] is None


@pytest.mark.asyncio
async def test_branch_with_diff_and_ahead_behind_passes_through(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git fields and matching PRs are joined into one response."""
    runner = _FakeRunnerClient(
        payload=_runner_payload(
            workspace="/repo",
            branch="feature/login",
            base_branch="origin/main",
            ahead=3,
            behind=1,
            diff={"added": 42, "removed": 7, "files": 4},
            repo_slug="octocat/hello-world",
        )
    )
    set_runner_client(runner)  # type: ignore[arg-type]

    async def _prs(repo: str, branch: str) -> integrations.BranchPullRequests:
        assert (repo, branch) == ("octocat/hello-world", "feature/login")
        return integrations.BranchPullRequests(
            cards=[
                {
                    "number": 12,
                    "title": "Add login",
                    "state": "open",
                    "ci_status": "pending",
                    "url": "https://github.com/octocat/hello-world/pull/12",
                }
            ],
            status="ok",
        )

    monkeypatch.setattr(integrations, "github_pull_requests_for_branch", _prs)

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    body = resp.json()
    assert resp.status_code == 200
    assert (body["branch"], body["base_branch"]) == ("feature/login", "origin/main")
    assert (body["ahead"], body["behind"]) == (3, 1)
    assert body["diff"] == {"added": 42, "removed": 7, "files": 4}
    assert body["prs"] == [
        {
            "number": 12,
            "title": "Add login",
            "state": "open",
            "ci_status": "pending",
            "url": "https://github.com/octocat/hello-world/pull/12",
        }
    ]
    assert body["prs_status"] == "ok"
    # The slug drives the "create PR" / compare URL, so it must be
    # present even though a PR already exists here.
    assert body["repo_slug"] == "octocat/hello-world"
    assert runner.calls == [f"/v1/sessions/{_SESSION_ID}/git-status"]


@pytest.mark.asyncio
async def test_git_failure_surfaces_as_the_error_field(
    client: httpx.AsyncClient,
) -> None:
    """A failed git command is reported in-band, still with a 200."""
    set_runner_client(  # type: ignore[arg-type]
        _FakeRunnerClient(
            payload=_runner_payload(
                workspace="/repo",
                error="git command timed out after 5s",
            )
        )
    )

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    body = resp.json()
    assert resp.status_code == 200
    assert body["error"] == "git command timed out after 5s"
    assert body["branch"] is None
    assert body["prs"] == []


@pytest.mark.asyncio
async def test_unreachable_runner_becomes_an_error_not_a_502(
    client: httpx.AsyncClient,
) -> None:
    """The status bar must keep rendering when the runner is down."""
    set_runner_client(  # type: ignore[arg-type]
        _FakeRunnerClient(exc=httpx.ConnectError("no runner"))
    )

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    assert resp.status_code == 200
    assert resp.json()["error"]


@pytest.mark.asyncio
async def test_unconfigured_github_yields_no_prs_and_no_error(
    client: httpx.AsyncClient,
) -> None:
    """Without a token the PR lookup is skipped, not failed."""
    set_runner_client(  # type: ignore[arg-type]
        _FakeRunnerClient(
            payload=_runner_payload(
                workspace="/repo",
                branch="feature/login",
                repo_slug="octocat/hello-world",
                diff={"added": 1, "removed": 0, "files": 1},
            )
        )
    )

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    body = resp.json()
    assert resp.status_code == 200
    assert body["prs"] == []
    # No token means the list is unknowable, not empty — and it is still
    # not a git ``error``.
    assert body["prs_status"] == "unavailable"
    assert body["error"] is None
    assert body["branch"] == "feature/login"


@pytest.mark.asyncio
async def test_pull_request_lookup_never_raises(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GitHub outage degrades to an unavailable PR list, keeping the git half."""

    async def _token() -> str:
        return "t"

    monkeypatch.setattr(integrations, "_github_token", _token)

    async def _boom(_path: str, _params: dict[str, Any] | None = None) -> Any:
        raise OmniCraftError("could not reach GitHub")

    monkeypatch.setattr(integrations, "_github_get", _boom)
    set_runner_client(  # type: ignore[arg-type]
        _FakeRunnerClient(
            payload=_runner_payload(
                workspace="/repo",
                branch="feature/login",
                repo_slug="octocat/hello-world",
            )
        )
    )

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    body = resp.json()
    assert resp.status_code == 200
    assert body["prs"] == []
    # The outage is confessed here, never through ``error`` — that field
    # belongs to git and the runner alone.
    assert body["prs_status"] == "unavailable"
    assert body["error"] is None
    assert body["branch"] == "feature/login"


@pytest.mark.asyncio
async def test_truncated_pull_request_list_is_reported_as_partial(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list GitHub may have cut short says so, so a client can stop trusting it."""

    async def _prs(repo: str, branch: str) -> integrations.BranchPullRequests:
        del repo, branch
        return integrations.BranchPullRequests(
            cards=[
                {
                    "number": n,
                    "title": f"PR {n}",
                    "state": "open",
                    "ci_status": "unknown",
                    "url": f"https://github.com/octocat/hello-world/pull/{n}",
                }
                for n in range(30)
            ],
            status="partial",
        )

    monkeypatch.setattr(integrations, "github_pull_requests_for_branch", _prs)
    set_runner_client(  # type: ignore[arg-type]
        _FakeRunnerClient(
            payload=_runner_payload(
                workspace="/repo",
                branch="feature/login",
                repo_slug="octocat/hello-world",
            )
        )
    )

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    body = resp.json()
    assert len(body["prs"]) == 30
    assert body["prs_status"] == "partial"
    assert body["error"] is None


@pytest.mark.asyncio
async def test_pull_request_lookup_crash_is_reported_as_unavailable(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lookup that blows up outright still answers, and admits it knows nothing."""

    async def _prs(repo: str, branch: str) -> integrations.BranchPullRequests:
        del repo, branch
        raise RuntimeError("boom")

    monkeypatch.setattr(integrations, "github_pull_requests_for_branch", _prs)
    set_runner_client(  # type: ignore[arg-type]
        _FakeRunnerClient(
            payload=_runner_payload(
                workspace="/repo",
                branch="feature/login",
                repo_slug="octocat/hello-world",
            )
        )
    )

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    body = resp.json()
    assert resp.status_code == 200
    assert (body["prs"], body["prs_status"]) == ([], "unavailable")
    assert body["error"] is None
    assert body["branch"] == "feature/login"


@pytest.mark.asyncio
async def test_ci_state_distinguishes_no_checks_from_not_asked(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``none`` and ``unknown`` reach the client as distinct answers."""

    async def _prs(repo: str, branch: str) -> integrations.BranchPullRequests:
        del repo, branch
        return integrations.BranchPullRequests(
            cards=[
                {
                    "number": 1,
                    "title": "No CI here",
                    "state": "open",
                    "ci_status": "none",
                    "url": "https://github.com/octocat/hello-world/pull/1",
                },
                {
                    "number": 2,
                    "title": "Never asked",
                    "state": "open",
                    "ci_status": "unknown",
                    "url": "https://github.com/octocat/hello-world/pull/2",
                },
            ],
            status="ok",
        )

    monkeypatch.setattr(integrations, "github_pull_requests_for_branch", _prs)
    set_runner_client(  # type: ignore[arg-type]
        _FakeRunnerClient(
            payload=_runner_payload(
                workspace="/repo",
                branch="feature/login",
                repo_slug="octocat/hello-world",
            )
        )
    )

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    body = resp.json()
    assert [p["ci_status"] for p in body["prs"]] == ["none", "unknown"]


def test_merged_pull_request_state_is_distinguished_from_closed() -> None:
    """``_pr_state`` splits merged out of GitHub's ``closed``."""
    assert integrations._pr_state({"state": "open"}) == "open"
    assert integrations._pr_state({"state": "closed"}) == "closed"
    assert integrations._pr_state({"state": "closed", "merged_at": "2026-01-01"}) == "merged"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(["not", "an", "object"], id="list_instead_of_object"),
        pytest.param({"branch": {"unexpected": "shape"}}, id="branch_is_an_object"),
        pytest.param({"diff": {"added": "lots"}}, id="diff_counts_are_not_ints"),
        pytest.param({"ahead": "three"}, id="ahead_is_not_an_int"),
        pytest.param({}, id="empty_object"),
    ],
)
@pytest.mark.asyncio
async def test_malformed_runner_payload_never_becomes_a_500(
    client: httpx.AsyncClient,
    payload: Any,
) -> None:
    """A runner speaking a different dialect degrades, it does not crash."""
    set_runner_client(_FakeRunnerClient(payload=payload))  # type: ignore[arg-type]

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "session.git_status"
    assert body["prs"] == []


@pytest.mark.asyncio
async def test_non_json_runner_body_never_becomes_a_500(
    client: httpx.AsyncClient,
) -> None:
    """An HTML error page from the runner is contained, not re-raised."""

    class _HtmlRunner(_FakeRunnerClient):
        async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
            """
            :param url: Requested path.
            :param _kwargs: Ignored.
            :returns: A 200 carrying HTML instead of JSON.
            """
            self.calls.append(url)
            return httpx.Response(
                200,
                text="<html>gateway</html>",
                request=httpx.Request("GET", f"http://runner{url}"),
            )

    set_runner_client(_HtmlRunner())  # type: ignore[arg-type]

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    assert resp.status_code == 200
    assert resp.json()["error"]


@pytest.mark.asyncio
async def test_repo_slug_is_present_without_any_pull_request(
    client: httpx.AsyncClient,
) -> None:
    """A dirty branch with no PR still gets the slug, so "create PR" can render."""
    set_runner_client(  # type: ignore[arg-type]
        _FakeRunnerClient(
            payload=_runner_payload(
                workspace="/repo",
                branch="feature/login",
                repo_slug="octocat/hello-world",
                diff={"added": 3, "removed": 1, "files": 2},
            )
        )
    )

    resp = await client.get(f"/v1/sessions/{_SESSION_ID}/git-status")

    body = resp.json()
    assert body["repo_slug"] == "octocat/hello-world"
    assert body["prs"] == []
    assert body["prs_status"] == "unavailable"
