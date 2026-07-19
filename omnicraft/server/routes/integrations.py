"""Read-only GitHub integration.

Lets the web app browse a repository's issues and pull requests and pull one
into a new session as starting context — without leaving OmniCraft. A thin,
authenticated proxy over the GitHub REST API; no data is stored server-side.

The access token is resolved from the environment (``GITHUB_TOKEN`` /
``GH_TOKEN``) and, as a local convenience for a self-hosted single-user setup,
falls back to the machine's ``gh`` CLI login. A deployed multi-user server
should set ``GITHUB_TOKEN`` explicitly.

Linear (and other trackers) can slot in later as sibling providers under
``/v1/integrations/<provider>/…`` following the same normalized item shape.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request

from omnicraft.errors import ErrorCode, OmniCraftError
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user

_GITHUB_API = "https://api.github.com"
_TIMEOUT_S = 15.0
_PAGE_SIZE = 30
_MAX_COMMENTS = 10
# Cap on CI lookups per status poll. Each costs 1-2 requests, so an
# unbounded fan-out over a page of PRs would burn the rate limit.
_MAX_CI_LOOKUPS = 5
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Sentinel so the ``gh``-derived token is resolved at most once per process
# (env is re-read every call so an operator can set it without a restart).
_UNSET: Any = object()
_gh_token_cache: Any = _UNSET


def _gh_cli_token() -> str | None:
    """Best-effort local token from the ``gh`` CLI login. ``None`` if absent."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    return token or None


async def _github_token() -> str | None:
    """Resolve a GitHub token: env first, then a cached ``gh`` CLI fallback.

    The ``gh`` fallback shells out, so it runs in a worker thread — at up
    to 5s it would otherwise stall the whole event loop on the first call.
    """
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env:
        return env.strip()
    global _gh_token_cache
    if _gh_token_cache is _UNSET:
        _gh_token_cache = await asyncio.to_thread(_gh_cli_token)
    return _gh_token_cache


def _validate_repo(repo: str) -> None:
    """Reject anything that isn't a plain ``owner/name`` slug (no path injection).

    The character class allows dots (real repos use them), so ``..`` is banned
    separately to keep a value like ``../secret`` from traversing the API path.
    """
    if ".." in repo or not _REPO_RE.match(repo):
        raise OmniCraftError(
            "repo must be in 'owner/name' form, e.g. 'octocat/hello-world'",
            code=ErrorCode.INVALID_INPUT,
        )


async def _github_get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> Any:
    """GET a GitHub API path with auth, mapping upstream failures to OmniCraftError.

    :param path: API path below ``https://api.github.com``.
    :param params: Optional query parameters.
    :param client: Reuse this client instead of opening a new one — lets
        a caller making several calls share one connection pool.
    :returns: The decoded JSON body.
    :raises OmniCraftError: On transport failure, an error status, or a
        body that is not valid JSON.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = await _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{_GITHUB_API}{path}"
    try:
        if client is not None:
            resp = await client.get(url, headers=headers, params=params)
        else:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as owned:
                resp = await owned.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise OmniCraftError(f"could not reach GitHub: {exc}", code=ErrorCode.CONFLICT) from exc
    if resp.status_code == 404:
        raise OmniCraftError(
            "not found on GitHub — check the repository name and that your token can see it",
            code=ErrorCode.NOT_FOUND,
        )
    if resp.status_code == 401:
        raise OmniCraftError(
            "GitHub rejected the token — set GITHUB_TOKEN or run 'gh auth login'",
            code=ErrorCode.INVALID_INPUT,
        )
    if resp.status_code == 403:
        raise OmniCraftError(
            "GitHub denied the request (rate limit or insufficient access)",
            code=ErrorCode.CONFLICT,
        )
    if resp.status_code >= 400:
        raise OmniCraftError(f"GitHub returned {resp.status_code}", code=ErrorCode.CONFLICT)
    try:
        return resp.json()
    except ValueError as exc:
        # A proxy or captive portal answering 200 with HTML must not
        # surface as an unhandled decode error.
        raise OmniCraftError("GitHub returned a non-JSON body", code=ErrorCode.CONFLICT) from exc


def _pr_state(item: dict[str, Any]) -> str | None:
    """PR state with ``merged`` split out of GitHub's ``closed``.

    GitHub reports a merged PR as ``closed`` with a ``merged_at``
    timestamp; callers need to tell the two apart.
    """
    state = item.get("state")
    if state == "closed" and (item.get("merged_at") or item.get("merged")):
        return "merged"
    return state


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    """Shared card shape for an issue or a PR."""
    head = item.get("head")
    head = head if isinstance(head, dict) else {}
    return {
        "number": item.get("number"),
        "title": item.get("title") or "",
        "url": item.get("html_url"),
        "state": _pr_state(item),
        "head_branch": head.get("ref"),
        "head_sha": head.get("sha"),
        "author": (item.get("user") if isinstance(item.get("user"), dict) else {}).get("login"),
        "comments": item.get("comments", 0),
        "updated_at": item.get("updated_at"),
        # The issues endpoint tags PRs with a ``pull_request`` object; the pulls
        # endpoint carries ``head``. Either marks this row as a PR.
        "is_pr": "pull_request" in item or "head" in item,
        "labels": [
            label.get("name")
            for label in (item.get("labels") or [])
            if isinstance(label, dict) and label.get("name")
        ],
    }


def _head_repo(item: dict[str, Any]) -> str | None:
    """``owner/name`` of a PR's head repository, or ``None`` if absent.

    A PR from a deleted fork has ``head.repo == null``, so the caller
    treats ``None`` as "cannot tell" rather than "does not match".

    :param item: Raw pull-request payload from the pulls endpoint.
    :returns: The head repo slug, or ``None``.
    """
    head = item.get("head")
    repo = head.get("repo") if isinstance(head, dict) else None
    full_name = repo.get("full_name") if isinstance(repo, dict) else None
    return full_name if isinstance(full_name, str) else None


async def _commit_ci_status(
    repo: str,
    sha: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Aggregate CI state for a commit, or ``None`` when unavailable.

    Reads the legacy combined status first (that is what most status
    reporters write), then falls back to check-runs, which is where
    GitHub Actions reports. ``None`` means "nothing to show", never
    "failing" — including when GitHub answers with a shape we do not
    recognize.

    :param repo: ``owner/name`` slug.
    :param sha: Head commit SHA of the pull request.
    :param client: Optional shared HTTP client.
    :returns: ``"success"``, ``"failure"``, ``"pending"``, or ``None``.
    """
    try:
        combined = await _github_get(f"/repos/{repo}/commits/{sha}/status", client=client)
    except OmniCraftError:
        return None
    if isinstance(combined, dict) and combined.get("total_count"):
        state = combined.get("state")
        if state in {"error", "failure"}:
            return "failure"
        return state if state in {"success", "pending"} else None

    try:
        checks = await _github_get(f"/repos/{repo}/commits/{sha}/check-runs", client=client)
    except OmniCraftError:
        return None
    raw_runs = checks.get("check_runs") if isinstance(checks, dict) else None
    runs = [r for r in raw_runs if isinstance(r, dict)] if isinstance(raw_runs, list) else []
    if not runs:
        return None
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    conclusions = {r.get("conclusion") for r in runs}
    if conclusions & {"failure", "timed_out", "cancelled", "action_required"}:
        return "failure"
    return "success"


async def github_pull_requests_for_branch(repo: str, branch: str) -> list[dict[str, Any]]:
    """Pull requests opened from ``branch``, with their CI state.

    Queries both open and closed PRs so a merged branch still shows its
    PR. Returns ``[]`` — never raises — when GitHub is unconfigured,
    unreachable, rejects the request, or answers with an unexpected
    shape, so a status readout degrades to "no PRs" instead of failing.

    One HTTP client is shared across every call, and CI lookups for the
    matching PRs run concurrently, capped at :data:`_MAX_CI_LOOKUPS` —
    this runs on a UI poll, so an unbounded serial fan-out would cost a
    request per PR every time.

    :param repo: ``owner/name`` slug, e.g. ``"octocat/hello-world"``.
    :param branch: Head branch name, e.g. ``"feature/login"``.
    :returns: Card dicts with ``number``, ``title``, ``state``,
        ``ci_status``, and ``url``.
    """
    if not await _github_token():
        return []
    try:
        _validate_repo(repo)
    except OmniCraftError:
        return []
    owner = repo.split("/")[0]
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            raw = await _github_get(
                f"/repos/{repo}/pulls",
                {"state": "all", "head": f"{owner}:{branch}", "per_page": _PAGE_SIZE},
                client=client,
            )
            if not isinstance(raw, list):
                return []
            matches = [
                card
                for card, item in (
                    (_normalize(item), item) for item in raw if isinstance(item, dict)
                )
                # GitHub's ``head`` filter is owner-scoped, so a fork's
                # same-named branch comes back too; keep only PRs whose
                # head repo is this one (``None`` = head repo deleted,
                # which we cannot rule out and so keep).
                if card["head_branch"] == branch and _head_repo(item) in (None, repo)
            ]
            cards = [
                {
                    "number": card["number"],
                    "title": card["title"],
                    "state": card["state"],
                    "ci_status": None,
                    "url": card["url"],
                }
                for card in matches
            ]
            inspected = [
                (card, match["head_sha"])
                for card, match in zip(cards, matches, strict=True)
                if match["head_sha"]
            ][:_MAX_CI_LOOKUPS]
            statuses = await asyncio.gather(
                *(_commit_ci_status(repo, sha, client=client) for _card, sha in inspected),
                return_exceptions=True,
            )
            for (card, _sha), status in zip(inspected, statuses, strict=True):
                card["ci_status"] = status if isinstance(status, str) else None
            return cards
    except OmniCraftError:
        return []


def create_integrations_router(*, auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the router for ``/v1/integrations/github/*``."""
    router = APIRouter()

    @router.get("/integrations/github/status")
    async def github_status(request: Request) -> dict[str, Any]:
        """Whether a GitHub token is available and, if so, whose login it is."""
        require_user(request, auth_provider)
        if not await _github_token():
            return {"configured": False, "login": None}
        try:
            user = await _github_get("/user")
        except OmniCraftError:
            # A token is present but unusable (expired/scoped-out) — report
            # configured so the UI shows the panel with a clear error on use.
            return {"configured": True, "login": None}
        login = user.get("login") if isinstance(user, dict) else None
        return {"configured": True, "login": login}

    @router.get("/integrations/github/items")
    async def github_items(
        request: Request,
        repo: str = Query(...),
        type: str = Query("issue", pattern="^(issue|pr)$"),
        state: str = Query("open", pattern="^(open|closed|all)$"),
    ) -> dict[str, Any]:
        """List a repo's issues or pull requests (newest-updated first)."""
        require_user(request, auth_provider)
        _validate_repo(repo)
        params = {
            "state": state,
            "per_page": _PAGE_SIZE,
            "sort": "updated",
            "direction": "desc",
        }
        if type == "pr":
            raw = await _github_get(f"/repos/{repo}/pulls", params)
        else:
            raw = await _github_get(f"/repos/{repo}/issues", params)
        rows = [it for it in raw if isinstance(it, dict)] if isinstance(raw, list) else []
        if type != "pr":
            # The issues endpoint returns PRs too; drop them for the issue tab.
            rows = [it for it in rows if "pull_request" not in it]
        return {"data": [_normalize(it) for it in rows]}

    @router.get("/integrations/github/items/{number}")
    async def github_item(
        request: Request,
        number: int,
        repo: str = Query(...),
    ) -> dict[str, Any]:
        """One issue/PR with its body and first few comments (for the seed prompt)."""
        require_user(request, auth_provider)
        _validate_repo(repo)
        item = await _github_get(f"/repos/{repo}/issues/{number}")
        if not isinstance(item, dict):
            raise OmniCraftError("GitHub returned an unexpected shape", code=ErrorCode.CONFLICT)
        comments: list[dict[str, Any]] = []
        if item.get("comments"):
            raw = await _github_get(
                f"/repos/{repo}/issues/{number}/comments",
                {"per_page": _MAX_COMMENTS},
            )
            comments = [
                {
                    "author": (c.get("user") if isinstance(c.get("user"), dict) else {}).get(
                        "login"
                    ),
                    "body": c.get("body") or "",
                }
                for c in (raw if isinstance(raw, list) else [])
                if isinstance(c, dict)
            ]
        # ``comments`` stays the count (as on list rows); the bodies live under
        # ``comments_list`` so the shape is consistent between list and detail.
        return {**_normalize(item), "body": item.get("body") or "", "comments_list": comments}

    return router
