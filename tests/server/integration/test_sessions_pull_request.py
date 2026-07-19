"""Tests for ``POST /v1/sessions/{id}/pull-request``.

The branch and repository come from the runner's git status; the pull
request is opened against the GitHub API. Both are stubbed here — the
GitHub layer at ``_github_get``/``_github_post``, so the idempotency and
title-derivation logic under test is the real one.

The contract these pin: the call never duplicates an open pull request
(including against a race that GitHub reports as ``422``), never pushes
the branch, and turns every refusal into an actionable ``4xx`` rather
than a ``500``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnicraft.entities import Conversation, ResolvedAccess, SessionPermission
from omnicraft.errors import ErrorCode, OmniCraftError
from omnicraft.runtime import _globals, set_runner_client, set_runner_router
from omnicraft.server.auth import (
    LEVEL_EDIT,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    UnifiedAuthProvider,
)
from omnicraft.server.routes import integrations
from omnicraft.server.routes.sessions import create_sessions_router

_SESSION_ID = "conv_pr"
_REPO = "octocat/hello-world"
_BRANCH = "feature/add-login"
# Zero-width space: what defuses an ``@mention`` without hiding it.
_ZWSP = "\u200b"


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
    """Agent store stub — this route never resolves an agent."""

    def get(self, agent_id: str) -> None:
        """
        :param agent_id: Agent id.
        :returns: None (no agents in the stub).
        """
        return


class _FakeRunnerClient:
    """Runner client returning one canned git-status payload.

    :param payload: JSON body the runner answers with.
    :param exc: Exception raised instead of answering.
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

    async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        """
        :param url: Runner-relative path being fetched.
        :param _kwargs: Ignored transport options.
        :returns: The canned response.
        """
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
    """Build a runner git-status body for a branch ready to be PR'd.

    :param overrides: Fields to override on the default payload.
    :returns: The runner response body.
    """
    return {
        "object": "session.git_status",
        "session_id": _SESSION_ID,
        "workspace": "/work/repo",
        "branch": _BRANCH,
        "base_branch": "origin/main",
        "ahead": 2,
        "behind": 0,
        "diff": {"added": 10, "removed": 1, "files": 2},
        "repo_slug": _REPO,
        "error": None,
    } | overrides


def _pr_item(number: int = 7, *, state: str = "open", title: str = "Add login") -> dict[str, Any]:
    """A raw GitHub pull-request payload for this branch.

    :param number: Pull request number.
    :param state: GitHub state, ``"open"`` or ``"closed"``.
    :param title: Pull request title.
    :returns: The raw item as the pulls endpoint returns it.
    """
    return {
        "number": number,
        "title": title,
        "state": state,
        "html_url": f"https://github.com/{_REPO}/pull/{number}",
        "head": {"ref": _BRANCH, "sha": "abc123", "repo": {"full_name": _REPO}},
        "user": {"login": "octocat"},
        "updated_at": "2026-07-19T00:00:00Z",
    }


def _fork_pr_item(number: int) -> dict[str, Any]:
    """A pull request from a fork's same-named branch.

    GitHub's owner-scoped ``head`` filter really does return these, so
    they are what fills the pages ahead of this repository's own pull
    request — and the lookup must skip them, not stop at them.

    :param number: Pull request number.
    :returns: The raw item as the pulls endpoint returns it.
    """
    item = _pr_item(number=number, title=f"Fork work {number}")
    item["head"] = {
        "ref": _BRANCH,
        "sha": "def456",
        "repo": {"full_name": f"fork{number}/hello-world"},
    }
    return item


def _commits(*subjects: str) -> dict[str, Any]:
    """A compare response carrying *subjects* as commit messages.

    :param subjects: Commit subject lines, oldest first.
    :returns: The compare payload.
    """
    return {"commits": [{"commit": {"message": s}} for s in subjects]}


class _FakeGitHub:
    """In-memory GitHub API double for ``_github_get``/``_github_post``.

    The pulls endpoint honours ``state``, ``head``, ``page`` and
    ``per_page`` the way GitHub does — including the head filter's owner
    scope, which lets a fork's same-named branch through — so a lookup
    that only reads the first page cannot pass by accident.

    :param pulls: Raw pull-request items the pulls endpoint lists.
    :param commits: Compare response for the base..head range.
    :param branch_exists: Whether the head branch is on the remote.
    :param post_status: Status the create-PR call answers with.
    :param post_body: Body the create-PR call answers with.
    :param pulls_after_post: Items the pulls endpoint lists once the
        create call has been made — models another client winning the
        race.
    :param pulls_error_after_post: Raised by the pulls endpoint after the
        create call — models GitHub failing exactly during the recovery.
    """

    def __init__(
        self,
        *,
        pulls: list[dict[str, Any]] | None = None,
        commits: dict[str, Any] | None = None,
        branch_exists: bool = True,
        post_status: int = 201,
        post_body: Any = None,
        pulls_after_post: list[dict[str, Any]] | None = None,
        pulls_error_after_post: Exception | None = None,
    ) -> None:
        """
        :returns: None.
        """
        self.pulls = pulls or []
        self.commits = commits if commits is not None else _commits("Add login")
        self.branch_exists = branch_exists
        self.post_status = post_status
        self.post_body = post_body
        self.pulls_after_post = pulls_after_post
        self.pulls_error_after_post = pulls_error_after_post
        self.posts: list[dict[str, Any]] = []
        self.pull_pages: list[int] = []

    def _list_pulls(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Page and filter the pull-request list like GitHub does.

        :param params: Query parameters of the request.
        :returns: The requested page.
        :raises Exception: ``pulls_error_after_post``, once a create call
            has been made.
        """
        if self.posts and self.pulls_error_after_post is not None:
            raise self.pulls_error_after_post
        items = self.pulls
        if self.posts and self.pulls_after_post is not None:
            items = self.pulls_after_post
        state = params.get("state")
        if state in {"open", "closed"}:
            items = [item for item in items if item.get("state") == state]
        head = params.get("head")
        if head:
            # GitHub's ``head`` filter is ``owner:branch`` and owner-scoped:
            # it keeps every pull request with that branch name, including
            # one opened from a fork of the same owner.
            branch = str(head).split(":", 1)[-1]
            items = [item for item in items if (item.get("head") or {}).get("ref") == branch]
        per_page = int(params.get("per_page") or 30)
        page = int(params.get("page") or 1)
        self.pull_pages.append(page)
        start = (page - 1) * per_page
        return items[start : start + per_page]

    async def get(self, path: str, params: Any = None, **_kwargs: Any) -> Any:
        """Answer a GitHub GET.

        :param path: API path.
        :param params: Query parameters.
        :returns: The canned body.
        :raises OmniCraftError: For a missing branch.
        """
        if path == f"/repos/{_REPO}/pulls":
            return self._list_pulls(params or {})
        if path == f"/repos/{_REPO}":
            return {"default_branch": "main"}
        if path.startswith(f"/repos/{_REPO}/branches/"):
            if not self.branch_exists:
                raise OmniCraftError("not found on GitHub", code=ErrorCode.NOT_FOUND)
            return {"name": _BRANCH}
        if path.startswith(f"/repos/{_REPO}/compare/"):
            return self.commits
        if "/status" in path or "/check-runs" in path:
            return {}
        raise AssertionError(f"unexpected GitHub GET {path}")

    async def post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        """Answer a GitHub POST.

        :param path: API path.
        :param payload: Request body.
        :returns: ``(status, body)``.
        """
        assert path == f"/repos/{_REPO}/pulls"
        self.posts.append(payload)
        body = self.post_body if self.post_body is not None else _pr_item(number=42)
        return self.post_status, body


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> _FakeGitHub:
    """Install a default GitHub double and a configured token.

    :param monkeypatch: Pytest patcher.
    :returns: The double, mutable by each test before it calls the route.
    """
    fake = _FakeGitHub()

    async def _token() -> str:
        return "gh_token"

    monkeypatch.setattr(integrations, "_github_token", _token)
    monkeypatch.setattr(integrations, "_github_get", fake.get)
    monkeypatch.setattr(integrations, "_github_post", fake.post)
    return fake


def _use_runner(**overrides: Any) -> None:
    """Point the route at a runner answering with this git status.

    :param overrides: Fields to override on the default payload.
    :returns: None.
    """
    set_runner_client(  # type: ignore[arg-type]
        _FakeRunnerClient(payload=_runner_payload(**overrides))
    )


@pytest.mark.asyncio
async def test_opens_a_pull_request_for_the_branch(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """The happy path opens one PR and reports ``created=True``."""
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 200
    assert resp.json() == {
        "object": "session.pull_request",
        "session_id": _SESSION_ID,
        "number": 42,
        "url": f"https://github.com/{_REPO}/pull/42",
        "created": True,
        "title": "Add login",
    }
    assert github.posts == [{"title": "Add login", "body": "", "head": _BRANCH, "base": "main"}]


@pytest.mark.asyncio
async def test_single_commit_lends_its_subject_as_the_title(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """One commit: title is its subject verbatim, body stays empty."""
    github.commits = _commits("fix(auth): reject expired tokens\n\nlong body")
    _use_runner()

    await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert github.posts[0]["title"] == "fix(auth): reject expired tokens"
    assert github.posts[0]["body"] == ""


@pytest.mark.asyncio
async def test_several_commits_take_the_title_from_a_real_commit(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """Many commits: the title is the first commit's subject, not the branch."""
    github.commits = _commits("add the login form", "wire the endpoint")
    _use_runner()

    await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert github.posts[0]["title"] == "add the login form"
    assert github.posts[0]["body"] == "- `add the login form`\n- `wire the endpoint`"


@pytest.mark.asyncio
async def test_mentions_and_markdown_in_subjects_are_defused(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """A commit subject cannot ping a team or render as a link."""
    github.commits = _commits(
        "notify @org/team about [the outage](http://evil.test)",
        "thank @octocat",
    )
    _use_runner()

    await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    title = github.posts[0]["title"]
    body = github.posts[0]["body"]
    # The handles stay readable, but no '@' is left touching a name, so
    # GitHub resolves none of them into a notification.
    assert f"@{_ZWSP}org/team" in title
    assert "@org/team" not in title
    assert "@octocat" not in body
    # Active markdown is inert inside the code spans the body is built from.
    assert body == (
        f"- `notify @{_ZWSP}org/team about [the outage](http://evil.test)`\n"
        f"- `thank @{_ZWSP}octocat`"
    )


@pytest.mark.asyncio
async def test_backticks_in_a_subject_cannot_break_out_of_the_code_span(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """A subject quoting code keeps the rest of the body inert."""
    github.commits = _commits("drop `legacy` flag", "keep the rest")
    _use_runner()

    await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert github.posts[0]["body"].splitlines()[0] == "- ``drop `legacy` flag``"


@pytest.mark.asyncio
async def test_blank_subjects_are_nothing_to_open_a_pull_request_for(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """No usable subject means no derived content, so nothing is opened."""
    github.commits = {"commits": [{"commit": {"message": "   "}}, {"commit": {}}]}
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 409
    assert github.posts == []


@pytest.mark.asyncio
async def test_existing_open_pull_request_is_reused(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """An open PR for the branch comes back untouched, never duplicated."""
    github.pulls = [_pr_item(number=7)]
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    body = resp.json()
    assert resp.status_code == 200
    assert (body["number"], body["created"]) == (7, False)
    assert github.posts == []


@pytest.mark.asyncio
async def test_closed_pull_request_does_not_block_a_new_one(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """Only an *open* PR is reused; a merged one is history."""
    github.pulls = [{**_pr_item(number=7, state="closed"), "merged_at": "2026-01-01T00:00:00Z"}]
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.json()["created"] is True
    assert len(github.posts) == 1


@pytest.mark.asyncio
async def test_github_422_recovers_the_pull_request_that_won_the_race(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """A concurrent creation is a 200 with ``created=False``, not an error."""
    github.post_status = 422
    github.post_body = {
        "message": "Validation Failed",
        "errors": [{"message": "A pull request already exists for octocat:feature/add-login."}],
    }
    github.pulls_after_post = [_pr_item(number=9)]
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    body = resp.json()
    assert resp.status_code == 200
    assert (body["number"], body["created"]) == (9, False)


@pytest.mark.asyncio
async def test_open_pull_request_past_the_first_page_is_still_found(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """A repository busy enough to paginate must not get a duplicate PR."""
    github.pulls = [_fork_pr_item(n) for n in range(100, 130)] + [_pr_item(number=9)]
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    body = resp.json()
    assert resp.status_code == 200
    assert (body["number"], body["created"]) == (9, False)
    assert github.posts == []
    assert github.pull_pages == [1, 2]


@pytest.mark.asyncio
async def test_422_recovery_reads_past_the_first_page(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """The race recovery paginates too, instead of reporting bad input."""
    github.post_status = 422
    github.post_body = {"message": "Validation Failed"}
    github.pulls_after_post = [_fork_pr_item(n) for n in range(100, 130)] + [_pr_item(number=11)]
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    body = resp.json()
    assert resp.status_code == 200
    assert (body["number"], body["created"]) == (11, False)


@pytest.mark.asyncio
async def test_lookup_hitting_the_page_cap_refuses_instead_of_duplicating(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """Stopping the search is never reported as "there is no pull request".

    More matching pull requests than the walk is willing to read leaves
    the answer unknown, and an unknown answer must not become a POST.
    """
    pages = integrations._MAX_PR_LOOKUP_PAGES
    github.pulls = [_fork_pr_item(n) for n in range(100, 100 + pages * 30 + 5)]
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 409
    assert "stopped looking" in resp.json()["error"]["message"]
    # Decisive: no second pull request was opened on a partial answer.
    assert github.posts == []
    assert github.pull_pages == list(range(1, pages + 1))


@pytest.mark.asyncio
async def test_422_recovery_failing_is_a_distinct_readable_409(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """A failed read-back is never reported as invalid input.

    The pull request may well exist; blaming the caller would send them
    to fix a request that was fine.
    """
    github.post_status = 422
    github.post_body = {"message": "Validation Failed"}
    github.pulls_error_after_post = OmniCraftError(
        "could not reach GitHub: timed out",
        code=ErrorCode.CONFLICT,
    )
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    message = resp.json()["error"]["message"]
    assert resp.status_code == 409
    assert "could not be read back" in message
    assert "timed out" in message


@pytest.mark.asyncio
async def test_unrecoverable_422_is_a_readable_400(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """A 422 with no PR behind it surfaces GitHub's own reason."""
    github.post_status = 422
    github.post_body = {"message": "Validation Failed", "errors": [{"message": "No commits"}]}
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 400
    assert "No commits" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_unpushed_branch_says_to_push_it(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """The branch is never pushed for the caller — it is told to push."""
    github.branch_exists = False
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 409
    assert "git push -u origin feature/add-login" in resp.json()["error"]["message"]
    assert github.posts == []


@pytest.mark.asyncio
async def test_token_without_write_access_is_a_403(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """GitHub's 403 becomes an actionable permission error."""
    github.post_status = 403
    github.post_body = {"message": "Resource not accessible by integration"}
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 403
    assert "write access" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_github_not_configured_is_a_readable_400(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a token there is nothing to open the PR with."""

    async def _no_token() -> None:
        return None

    monkeypatch.setattr(integrations, "_github_token", _no_token)
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 400
    assert "GITHUB_TOKEN" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_workspace_without_a_github_remote_is_a_409(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """No ``repo_slug`` means no repository to open a PR against."""
    del github
    _use_runner(repo_slug=None)

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 409
    assert "github.com remote" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_malformed_repo_slug_is_rejected_before_any_api_call(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """A slug that is not ``owner/name`` never reaches an API path."""
    _use_runner(repo_slug="../../etc/passwd")

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 400
    assert github.posts == []


@pytest.mark.asyncio
async def test_non_git_workspace_is_a_409(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """A workspace with no branch cannot have a pull request."""
    del github
    _use_runner(branch=None, base_branch=None, repo_slug=None)

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 409
    assert "not a git repository" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_branch_equal_to_base_is_a_409(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """A branch that tracks itself falls back to the default branch."""
    _use_runner(branch="main", base_branch="origin/main")

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 409
    assert "base branch" in resp.json()["error"]["message"]
    assert github.posts == []


@pytest.mark.asyncio
async def test_nothing_to_compare_is_a_409(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """A branch level with its base has nothing to propose."""
    github.commits = _commits()
    _use_runner()

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 409
    assert "nothing to open a pull request for" in resp.json()["error"]["message"]
    assert github.posts == []


@pytest.mark.asyncio
async def test_git_failure_on_the_runner_is_a_409(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """Unlike the status bar, this route refuses instead of degrading."""
    del github
    _use_runner(branch=None, repo_slug=None, error="git timed out")

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 409
    assert "git timed out" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_unreachable_runner_is_a_409_not_a_500(
    client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """A runner that cannot be reached is a readable refusal."""
    del github
    set_runner_client(  # type: ignore[arg-type]
        _FakeRunnerClient(exc=httpx.ConnectError("boom"))
    )

    resp = await client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 409
    assert resp.json()["error"]["message"]


class _StubPermissionStore:
    """In-memory permission store with the methods access checks use.

    :param grants: ``{(user_id, session_id): level}`` seed grants.
    """

    def __init__(self, grants: dict[tuple[str, str], int]) -> None:
        """
        :param grants: Seed grants keyed by user and session.
        :returns: None.
        """
        self._grants = grants

    def get(self, user_id: str, conversation_id: str) -> SessionPermission | None:
        """
        :param user_id: Grantee.
        :param conversation_id: Session the grant is on.
        :returns: The grant, or ``None``.
        """
        level = self._grants.get((user_id, conversation_id))
        if level is None:
            return None
        return SessionPermission(
            user_id=user_id,
            conversation_id=conversation_id,
            level=level,
        )

    def is_admin(self, user_id: str) -> bool:
        """
        :param user_id: User to check.
        :returns: ``False`` — this suite has no admins.
        """
        del user_id
        return False

    def resolve_access(self, user_id: str | None, conversation_id: str) -> ResolvedAccess:
        """
        :param user_id: Authenticated caller, or ``None``.
        :param conversation_id: Session being accessed.
        :returns: The caller's admin flag and grants.
        """
        if user_id is None:
            return ResolvedAccess(is_admin=False, user_grant_level=None, public_grant_level=None)
        user_grant = self.get(user_id, conversation_id)
        public_grant = self.get(RESERVED_USER_PUBLIC, conversation_id)
        return ResolvedAccess(
            is_admin=False,
            user_grant_level=user_grant.level if user_grant is not None else None,
            public_grant_level=public_grant.level if public_grant is not None else None,
        )


@pytest.fixture
async def guarded_client(runner_globals_reset: None) -> AsyncIterator[httpx.AsyncClient]:
    """Client for an app enforcing per-user permissions on the session.

    ``alice`` may edit the session, ``viewer`` may only read it.

    :param runner_globals_reset: Ensures clean runner globals.
    :returns: Iterator yielding the client.
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
            # Strict header mode: opt out of the suite-wide single-user
            # default so a headerless request is rejected, as in a
            # deployed multi-user server.
            auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
            permission_store=_StubPermissionStore(  # type: ignore[arg-type]
                {
                    ("alice@example.com", _SESSION_ID): LEVEL_EDIT,
                    ("viewer@example.com", _SESSION_ID): LEVEL_READ,
                }
            ),
        ),
        prefix="/v1",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as c:
        yield c


@pytest.mark.asyncio
async def test_read_only_collaborator_cannot_open_a_pull_request(
    guarded_client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """Opening a PR is a write: LEVEL_READ is rejected before GitHub."""
    _use_runner()

    resp = await guarded_client.post(
        f"/v1/sessions/{_SESSION_ID}/pull-request",
        headers={"X-Forwarded-Email": "viewer@example.com"},
    )

    assert resp.status_code == 403
    # Decisive: nothing was opened on the user's behalf.
    assert github.posts == []


@pytest.mark.asyncio
async def test_unauthenticated_caller_cannot_open_a_pull_request(
    guarded_client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """Without an identity the request never reaches GitHub."""
    _use_runner()

    resp = await guarded_client.post(f"/v1/sessions/{_SESSION_ID}/pull-request")

    assert resp.status_code == 401
    assert github.posts == []


@pytest.mark.asyncio
async def test_edit_collaborator_can_open_a_pull_request(
    guarded_client: httpx.AsyncClient,
    github: _FakeGitHub,
) -> None:
    """The gate lets an edit collaborator through — it is not a blanket deny."""
    _use_runner()

    resp = await guarded_client.post(
        f"/v1/sessions/{_SESSION_ID}/pull-request",
        headers={"X-Forwarded-Email": "alice@example.com"},
    )

    assert resp.status_code == 200
    assert len(github.posts) == 1
