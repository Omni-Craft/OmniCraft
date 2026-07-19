"""GitHub integration.

Lets the web app browse a repository's issues and pull requests and pull one
into a new session as starting context — without leaving OmniCraft. A thin,
authenticated proxy over the GitHub REST API; no data is stored server-side.

The routed endpoints are read-only. The one write path is opening a pull
request for a session's branch, exposed by the sessions router through the
helpers here so the token and the HTTP plumbing stay in one place.

The access token is resolved from the environment (``GITHUB_TOKEN`` /
``GH_TOKEN``) and, as a local convenience for a self-hosted single-user setup,
falls back to the machine's ``gh`` CLI login. A deployed multi-user server
should set ``GITHUB_TOKEN`` explicitly.

Linear (and other trackers) can slot in later as sibling providers under
``/v1/integrations/<provider>/…`` following the same normalized item shape.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import subprocess
import urllib.parse
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request

from omnicraft.errors import ErrorCode, OmniCraftError
from omnicraft.host.git_worktree import WorktreeError, validate_branch_name
from omnicraft.server.auth import AuthProvider
from omnicraft.server.routes._auth_helpers import require_user

_GITHUB_API = "https://api.github.com"
_TIMEOUT_S = 15.0
_PAGE_SIZE = 30
_MAX_COMMENTS = 10
# Cap on CI lookups per status poll. Each costs 1-2 requests, so an
# unbounded fan-out over a page of PRs would burn the rate limit.
_MAX_CI_LOOKUPS = 5
# Cap on commit subjects quoted in a generated pull-request body.
_MAX_PR_COMMITS = 20
# Pages walked when looking for a branch's open pull request. The query
# is already filtered to one head branch, so a second page is already
# unusual; this is a runaway guard, and hitting it refuses rather than
# answering "no pull request".
_MAX_PR_LOOKUP_PAGES = 5
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# GitHub's documented check-run vocabulary. A value outside these sets is
# a shape this code cannot read, so it reports "unknown" rather than
# guessing which side of the fence it falls on.
_CHECK_RUN_STATUSES = frozenset(
    {"queued", "in_progress", "completed", "waiting", "requested", "pending"}
)
_FAILING_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
)
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

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
) -> str:
    """Aggregate CI state for a commit.

    Reads the legacy combined status first (that is what most status
    reporters write), then falls back to check-runs, which is where
    GitHub Actions reports.

    "No CI at all" and "we could not ask" are different answers and are
    kept apart: ``"none"`` means GitHub was asked and gave a well-formed
    answer saying there are no checks, ``"unknown"`` means the question
    went unanswered — the request failed, or the body was not one this
    code can read. Anything unrecognized — a missing count, a scalar
    where a check-run belongs, a status or conclusion outside GitHub's
    documented vocabulary — is ``"unknown"``; it is never rounded to
    ``"none"``, ``"pending"`` or ``"success"``.

    :param repo: ``owner/name`` slug.
    :param sha: Head commit SHA of the pull request.
    :param client: Optional shared HTTP client.
    :returns: ``"success"``, ``"failure"``, ``"pending"``, ``"none"``, or
        ``"unknown"``.
    """
    try:
        combined = await _github_get(f"/repos/{repo}/commits/{sha}/status", client=client)
    except OmniCraftError:
        return "unknown"
    if not isinstance(combined, dict):
        return "unknown"
    total = combined.get("total_count")
    # A body with no readable count says nothing — not even "zero", which
    # is what licenses the fall-through to check-runs below.
    if not isinstance(total, int) or isinstance(total, bool):
        return "unknown"
    if total:
        state = combined.get("state")
        if state in {"error", "failure"}:
            return "failure"
        return state if state in {"success", "pending"} else "unknown"

    try:
        checks = await _github_get(f"/repos/{repo}/commits/{sha}/check-runs", client=client)
    except OmniCraftError:
        return "unknown"
    runs = checks.get("check_runs") if isinstance(checks, dict) else None
    if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
        return "unknown"
    if not runs:
        return "none"

    statuses = {r.get("status") for r in runs}
    if not statuses <= _CHECK_RUN_STATUSES:
        return "unknown"
    if statuses != {"completed"}:
        return "pending"
    conclusions = {r.get("conclusion") for r in runs}
    # A failed run is a fact even if a sibling is unreadable; claiming
    # success is not, so that needs every conclusion recognized.
    if conclusions & _FAILING_CONCLUSIONS:
        return "failure"
    if not conclusions <= _PASSING_CONCLUSIONS:
        return "unknown"
    return "success"


def _is_readable_pr_row(item: Any) -> bool:
    """Whether a raw pulls row carries the fields a card is built from.

    A row missing them cannot be matched to a branch, so dropping it
    silently would shrink the list without the caller ever knowing.

    :param item: One entry from the pulls endpoint.
    :returns: ``True`` when the row can be read.
    """
    if not isinstance(item, dict) or not isinstance(item.get("number"), int):
        return False
    head = item.get("head")
    return isinstance(head, dict) and isinstance(head.get("ref"), str)


@dataclasses.dataclass(frozen=True)
class BranchPullRequests:
    """A branch's pull-request cards plus how far the list can be trusted.

    :param cards: Card dicts with ``number``, ``title``, ``state``,
        ``ci_status``, and ``url``.
    :param status: ``"ok"`` when the list is everything GitHub has,
        ``"partial"`` when PRs are known to be missing from it — more
        than one page exists, or a row came back unreadable — and
        ``"unavailable"`` when the lookup never produced an answer at
        all: an unconfigured, unreachable or refusing GitHub, or a body
        nothing could be read from. An empty ``"unavailable"`` list means
        "cannot tell", never "no PRs".
    """

    cards: list[dict[str, Any]]
    status: str


_PRS_UNAVAILABLE = BranchPullRequests(cards=[], status="unavailable")


async def github_pull_requests_for_branch(repo: str, branch: str) -> BranchPullRequests:
    """Pull requests opened from ``branch``, with their CI state.

    Queries both open and closed PRs so a merged branch still shows its
    PR. Never raises: when GitHub is unconfigured, unreachable, rejects
    the request, or answers with an unexpected shape, the result is an
    empty ``"unavailable"`` list, so a status readout degrades instead of
    failing — but the caller can still tell that apart from a branch that
    genuinely has no pull request.

    One HTTP client is shared across every call, and CI lookups for the
    matching PRs run concurrently, capped at :data:`_MAX_CI_LOOKUPS` —
    this runs on a UI poll, so an unbounded serial fan-out would cost a
    request per PR every time. PRs past that cap keep ``ci_status
    == "unknown"``: not asked, so nothing is claimed.

    :param repo: ``owner/name`` slug, e.g. ``"octocat/hello-world"``.
    :param branch: Head branch name, e.g. ``"feature/login"``.
    :returns: The cards and the list's trustworthiness.
    """
    if not await _github_token():
        return _PRS_UNAVAILABLE
    try:
        _validate_repo(repo)
    except OmniCraftError:
        return _PRS_UNAVAILABLE
    owner = repo.split("/")[0]
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            # Ask for one row past the page so a full page can be told
            # apart from a truncated one: the extra row is only a "there
            # is more" signal and never reaches the caller. Exactly a
            # page's worth of PRs would otherwise always read as
            # truncated, and the status bar would distrust a list that
            # was in fact complete.
            raw = await _github_get(
                f"/repos/{repo}/pulls",
                {"state": "all", "head": f"{owner}:{branch}", "per_page": _PAGE_SIZE + 1},
                client=client,
            )
            if not isinstance(raw, list):
                return _PRS_UNAVAILABLE
            truncated = len(raw) > _PAGE_SIZE
            page = raw[:_PAGE_SIZE]
            readable = [item for item in page if _is_readable_pr_row(item)]
            # Nothing legible in a non-empty body: the answer is no
            # answer, not "this branch has no pull request".
            if page and not readable:
                return _PRS_UNAVAILABLE
            # A row we could not read may have been a PR for this branch,
            # so the list is knowingly short.
            status = "partial" if truncated or len(readable) < len(page) else "ok"
            matches = [
                card
                for card, item in ((_normalize(item), item) for item in readable)
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
                    "ci_status": "unknown",
                    "url": card["url"],
                }
                for card in matches
            ]
            inspected = [
                (card, match["head_sha"])
                for card, match in zip(cards, matches, strict=True)
                if match["head_sha"]
            ][:_MAX_CI_LOOKUPS]
            ci_states = await asyncio.gather(
                *(_commit_ci_status(repo, sha, client=client) for _card, sha in inspected),
                return_exceptions=True,
            )
            for (card, _sha), ci in zip(inspected, ci_states, strict=True):
                card["ci_status"] = ci if isinstance(ci, str) else "unknown"
            return BranchPullRequests(cards=cards, status=status)
    except OmniCraftError:
        return _PRS_UNAVAILABLE


def _validate_ref(ref: str) -> None:
    """Reject a branch name git itself would refuse, as invalid input.

    Doubles as the guard against path traversal in the API paths below —
    ``validate_branch_name`` bans ``..``, control characters and a
    leading ``-``.

    :param ref: Branch name, e.g. ``"feature/login"``.
    :raises OmniCraftError: If the name is not a valid git branch name.
    """
    try:
        validate_branch_name(ref)
    except WorktreeError as exc:
        raise OmniCraftError(exc.message, code=ErrorCode.INVALID_INPUT) from exc


def _ref_path(ref: str) -> str:
    """Encode a branch name for a GitHub API path, keeping ``/`` intact.

    GitHub matches ``feature/login`` as a literal path, so escaping the
    separator would look up a branch that does not exist.

    :param ref: Branch name already checked by :func:`_validate_ref`.
    :returns: The path-safe branch name.
    """
    return urllib.parse.quote(ref, safe="/")


async def _github_post(path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    """POST a GitHub API path with auth, returning the raw outcome.

    Unlike :func:`_github_get` this does not raise on an error status:
    opening a pull request has to read GitHub's ``422`` body to tell
    "a pull request already exists" apart from a real rejection.

    :param path: API path below ``https://api.github.com``.
    :param payload: JSON request body.
    :returns: ``(status_code, decoded body)``; the body is ``None`` when
        it is absent or not JSON.
    :raises OmniCraftError: On transport failure only.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = await _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(f"{_GITHUB_API}{path}", headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise OmniCraftError(f"could not reach GitHub: {exc}", code=ErrorCode.CONFLICT) from exc
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def _github_message(payload: Any) -> str | None:
    """Best-effort human-readable reason out of a GitHub error body.

    :param payload: Decoded GitHub error body.
    :returns: The message, with any per-field errors appended, or ``None``.
    """
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    details = [
        detail.get("message")
        for detail in (payload.get("errors") or [])
        if isinstance(detail, dict) and detail.get("message")
    ]
    parts = [str(part) for part in [message, *details] if part]
    return " — ".join(parts) or None


async def github_default_branch(repo: str) -> str:
    """The repository's default branch, e.g. ``"main"``.

    :param repo: ``owner/name`` slug.
    :returns: The default branch name.
    :raises OmniCraftError: If GitHub is unreachable, denies the request,
        or reports no default branch.
    """
    _validate_repo(repo)
    raw = await _github_get(f"/repos/{repo}")
    branch = raw.get("default_branch") if isinstance(raw, dict) else None
    if not isinstance(branch, str) or not branch:
        raise OmniCraftError(
            f"GitHub reported no default branch for {repo}",
            code=ErrorCode.CONFLICT,
        )
    return branch


async def github_branch_exists(repo: str, branch: str) -> bool:
    """Whether ``branch`` exists on the remote repository.

    :param repo: ``owner/name`` slug.
    :param branch: Branch name.
    :returns: ``True`` when GitHub knows the branch.
    :raises OmniCraftError: On any failure other than "not found".
    """
    _validate_repo(repo)
    _validate_ref(branch)
    try:
        await _github_get(f"/repos/{repo}/branches/{_ref_path(branch)}")
    except OmniCraftError as exc:
        if exc.code == ErrorCode.NOT_FOUND:
            return False
        raise
    return True


async def github_commit_subjects(
    repo: str,
    *,
    base: str,
    head: str,
    limit: int = _MAX_PR_COMMITS,
) -> list[str]:
    """Subject lines of the commits on ``head`` that are not on ``base``.

    :param repo: ``owner/name`` slug.
    :param base: Branch the pull request would merge into, e.g. ``"main"``.
    :param head: Branch carrying the work.
    :param limit: Cap on returned subjects.
    :returns: Subjects, oldest first. Empty when there is nothing to merge.
    :raises OmniCraftError: If GitHub is unreachable or denies the request.
    """
    _validate_repo(repo)
    _validate_ref(base)
    _validate_ref(head)
    raw = await _github_get(f"/repos/{repo}/compare/{_ref_path(base)}...{_ref_path(head)}")
    commits = raw.get("commits") if isinstance(raw, dict) else None
    subjects: list[str] = []
    for entry in commits if isinstance(commits, list) else []:
        commit = entry.get("commit") if isinstance(entry, dict) else None
        message = commit.get("message") if isinstance(commit, dict) else None
        subject = message.strip().splitlines()[0].strip() if isinstance(message, str) else ""
        if subject:
            subjects.append(subject)
    return subjects[:limit]


async def _open_pull_request_for_branch(repo: str, branch: str) -> dict[str, Any] | None:
    """The open pull request whose head is ``branch``, if there is one.

    Deliberately strict, unlike :func:`github_pull_requests_for_branch`,
    which degrades every failure to ``[]`` so the status bar keeps
    rendering: deciding whether to open a pull request must never read a
    failed lookup as "there is none", or it opens a duplicate.

    Asks GitHub for open pull requests with this exact head and walks the
    pages until a short one ends the list, so a repository with more open
    pull requests than fit on one page still finds it. The page cap is a
    runaway guard, not an answer: reaching it refuses the lookup, because
    "we stopped looking" must never be reported as "there is none".

    :param repo: ``owner/name`` slug.
    :param branch: Head branch name.
    :returns: The card, or ``None`` when no open pull request exists.
    :raises OmniCraftError: If GitHub is unreachable, denies the request,
        answers with an unexpected shape, or has more pages of matching
        pull requests than the cap allows — never silently ``None``.
    """
    owner = repo.split("/")[0]
    for page in range(1, _MAX_PR_LOOKUP_PAGES + 1):
        raw = await _github_get(
            f"/repos/{repo}/pulls",
            {
                "state": "open",
                "head": f"{owner}:{branch}",
                "per_page": _PAGE_SIZE,
                "page": page,
            },
        )
        if not isinstance(raw, list):
            raise OmniCraftError(
                "GitHub returned an unexpected pull-request list",
                code=ErrorCode.CONFLICT,
            )
        for item in raw:
            if not isinstance(item, dict):
                continue
            card = _normalize(item)
            # The ``head`` filter is owner-scoped, so a fork's same-named
            # branch comes back too (``None`` = head repo deleted, which
            # we cannot rule out and so keep).
            if card["head_branch"] == branch and _head_repo(item) in (None, repo):
                if isinstance(card.get("number"), int):
                    return card
        if len(raw) < _PAGE_SIZE:
            return None
    raise OmniCraftError(
        f"{repo} has more than {_MAX_PR_LOOKUP_PAGES * _PAGE_SIZE} open pull "
        f"requests for {branch!r}; OmniCraft stopped looking and will not open "
        f"another one — close or merge the existing ones first",
        code=ErrorCode.CONFLICT,
    )


async def github_open_pull_request(
    repo: str,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
) -> tuple[dict[str, Any], bool]:
    """Open a pull request from ``head`` into ``base``, at most once.

    An already-open pull request for ``head`` is returned as-is rather
    than duplicated — including when a concurrent caller opened it after
    the lookup, which GitHub reports as a ``422``.

    :param repo: ``owner/name`` slug.
    :param head: Branch carrying the work; must already be pushed.
    :param base: Branch to merge into, e.g. ``"main"``.
    :param title: Pull request title.
    :param body: Pull request body (may be empty).
    :returns: ``(card, created)`` — ``created`` is ``False`` when an
        existing pull request was returned.
    :raises OmniCraftError: If GitHub rejects the request.
    """
    _validate_repo(repo)
    _validate_ref(head)
    _validate_ref(base)
    existing = await _open_pull_request_for_branch(repo, head)
    if existing is not None:
        return existing, False

    status, payload = await _github_post(
        f"/repos/{repo}/pulls",
        {"title": title, "body": body, "head": head, "base": base},
    )
    if status in (200, 201) and isinstance(payload, dict):
        return _normalize(payload), True
    if status == 422:
        try:
            raced = await _open_pull_request_for_branch(repo, head)
        except OmniCraftError as exc:
            # GitHub refused, most likely as a duplicate, and we cannot
            # read back what already exists. Saying "invalid input" here
            # would blame the caller for a transient failure.
            raise OmniCraftError(
                f"GitHub rejected the pull request ("
                f"{_github_message(payload) or 'validation failed'}) and the "
                f"existing pull request for {head!r} could not be read back: "
                f"{exc.message}",
                code=ErrorCode.CONFLICT,
            ) from exc
        if raced is not None:
            return raced, False
        raise OmniCraftError(
            _github_message(payload) or "GitHub rejected the pull request",
            code=ErrorCode.INVALID_INPUT,
        )
    if status == 401:
        raise OmniCraftError(
            "GitHub rejected the token — set GITHUB_TOKEN or run 'gh auth login'",
            code=ErrorCode.INVALID_INPUT,
        )
    if status in (403, 404):
        # GitHub answers 404, not 403, when a token cannot see (or write
        # to) a repository, so the two share one actionable message.
        raise OmniCraftError(
            f"GitHub denied opening a pull request on {repo} — the token needs "
            f"write access to pull requests on that repository",
            code=ErrorCode.FORBIDDEN,
        )
    raise OmniCraftError(
        _github_message(payload) or f"GitHub returned {status} when opening the pull request",
        code=ErrorCode.CONFLICT,
    )


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
