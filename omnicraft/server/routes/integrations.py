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


def _github_token() -> str | None:
    """Resolve a GitHub token: env first, then a cached ``gh`` CLI fallback."""
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env:
        return env.strip()
    global _gh_token_cache
    if _gh_token_cache is _UNSET:
        _gh_token_cache = _gh_cli_token()
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


async def _github_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET a GitHub API path with auth, mapping upstream failures to OmniCraftError."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.get(f"{_GITHUB_API}{path}", headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise OmniCraftError(
            f"could not reach GitHub: {exc}", code=ErrorCode.CONFLICT
        ) from exc
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
        raise OmniCraftError(
            f"GitHub returned {resp.status_code}", code=ErrorCode.CONFLICT
        )
    return resp.json()


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    """Shared card shape for an issue or a PR."""
    return {
        "number": item.get("number"),
        "title": item.get("title") or "",
        "url": item.get("html_url"),
        "state": item.get("state"),
        "author": (item.get("user") or {}).get("login"),
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


def create_integrations_router(*, auth_provider: AuthProvider | None = None) -> APIRouter:
    """Build the router for ``/v1/integrations/github/*``."""
    router = APIRouter()

    @router.get("/integrations/github/status")
    async def github_status(request: Request) -> dict[str, Any]:
        """Whether a GitHub token is available and, if so, whose login it is."""
        require_user(request, auth_provider)
        if not _github_token():
            return {"configured": False, "login": None}
        try:
            user = await _github_get("/user")
        except OmniCraftError:
            # A token is present but unusable (expired/scoped-out) — report
            # configured so the UI shows the panel with a clear error on use.
            return {"configured": True, "login": None}
        return {"configured": True, "login": user.get("login")}

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
            # The issues endpoint returns PRs too; drop them for the issue tab.
            raw = [it for it in raw if "pull_request" not in it]
        return {"data": [_normalize(it) for it in raw]}

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
        comments: list[dict[str, Any]] = []
        if item.get("comments"):
            raw = await _github_get(
                f"/repos/{repo}/issues/{number}/comments",
                {"per_page": _MAX_COMMENTS},
            )
            comments = [
                {"author": (c.get("user") or {}).get("login"), "body": c.get("body") or ""}
                for c in raw
            ]
        # ``comments`` stays the count (as on list rows); the bodies live under
        # ``comments_list`` so the shape is consistent between list and detail.
        return {**_normalize(item), "body": item.get("body") or "", "comments_list": comments}

    return router
