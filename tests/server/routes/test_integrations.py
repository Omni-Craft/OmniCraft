"""Unit tests for the GitHub integration's pure helpers."""

from __future__ import annotations

import asyncio

import pytest

from omnicraft.errors import OmniCraftError
from omnicraft.server.routes import integrations
from omnicraft.server.routes.integrations import (
    _normalize,
    _validate_repo,
    github_pull_requests_for_branch,
)


@pytest.mark.parametrize("repo", ["octocat/hello-world", "cli/cli", "a.b/c.d", "org_1/repo-2"])
def test_validate_repo_accepts_plain_slugs(repo: str) -> None:
    """Well-formed ``owner/name`` slugs pass."""
    _validate_repo(repo)  # must not raise


@pytest.mark.parametrize(
    "repo",
    [
        "../etc",  # traversal via dot-dot
        "a/../b",
        "owner",  # no slash
        "owner/name/extra",  # too many segments
        "owner /name",  # space
        "owner/na me",
        "",
    ],
)
def test_validate_repo_rejects_bad_input(repo: str) -> None:
    """Traversal and malformed values are rejected before hitting the API path."""
    with pytest.raises(OmniCraftError):
        _validate_repo(repo)


def test_normalize_issue_shape() -> None:
    """An issue maps to the card shape and is not flagged as a PR."""
    out = _normalize(
        {
            "number": 42,
            "title": "Fix the thing",
            "html_url": "https://github.com/o/r/issues/42",
            "state": "open",
            "user": {"login": "alice"},
            "comments": 3,
            "updated_at": "2026-07-01T00:00:00Z",
            "labels": [{"name": "bug"}, {"name": "p1"}, {"no_name": True}],
        }
    )
    assert out == {
        "number": 42,
        "title": "Fix the thing",
        "url": "https://github.com/o/r/issues/42",
        "state": "open",
        "author": "alice",
        "comments": 3,
        "updated_at": "2026-07-01T00:00:00Z",
        "is_pr": False,
        # Issues carry no ``head``; the PR-only fields stay None.
        "head_branch": None,
        "head_sha": None,
        "labels": ["bug", "p1"],
    }


def test_normalize_detects_pull_request() -> None:
    """A ``pull_request`` marker (issues endpoint) or ``head`` (pulls endpoint) flags a PR."""
    assert _normalize({"number": 1, "pull_request": {"url": "..."}})["is_pr"] is True
    assert _normalize({"number": 2, "head": {"ref": "feature"}})["is_pr"] is True
    assert _normalize({"number": 3})["is_pr"] is False


def test_normalize_tolerates_missing_fields() -> None:
    """A sparse payload degrades to safe defaults rather than raising."""
    out = _normalize({})
    assert out["title"] == "" and out["author"] is None and out["labels"] == []


@pytest.mark.asyncio
async def test_branch_pull_requests_returns_empty_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured integration is not an error — it has no PRs."""

    async def _no_token() -> None:
        return None

    monkeypatch.setattr(integrations, "_github_token", _no_token)
    assert await github_pull_requests_for_branch("o/r", "feature") == []


@pytest.mark.asyncio
async def test_branch_pull_requests_maps_cards_and_ci_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching PRs come back as cards with their aggregate CI state."""

    async def _token() -> str:
        return "t"

    monkeypatch.setattr(integrations, "_github_token", _token)

    async def _get(
        path: str,
        params: dict[str, object] | None = None,
        **_kw: object,
    ) -> object:
        del params
        if path == "/repos/o/r/pulls":
            return [
                {
                    "number": 7,
                    "title": "Add login",
                    "html_url": "https://github.com/o/r/pull/7",
                    "state": "closed",
                    "merged_at": "2026-07-01T00:00:00Z",
                    "head": {"ref": "feature", "sha": "abc", "repo": {"full_name": "o/r"}},
                },
                # A different branch entirely — filtered on the ref.
                {
                    "number": 8,
                    "title": "Other branch",
                    "html_url": "https://github.com/o/r/pull/8",
                    "state": "open",
                    "head": {"ref": "other", "sha": "def", "repo": {"full_name": "o/r"}},
                },
                # A FORK's PR from an identically named branch: same
                # ``head.ref``, different head repo. Only the repo check
                # can drop this one.
                {
                    "number": 9,
                    "title": "Fork lookalike",
                    "html_url": "https://github.com/o/r/pull/9",
                    "state": "open",
                    "head": {
                        "ref": "feature",
                        "sha": "fff",
                        "repo": {"full_name": "someone-else/r"},
                    },
                },
            ]
        if path == "/repos/o/r/commits/abc/status":
            return {"total_count": 2, "state": "failure"}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(integrations, "_github_get", _get)

    assert await github_pull_requests_for_branch("o/r", "feature") == [
        {
            "number": 7,
            "title": "Add login",
            "state": "merged",
            "ci_status": "failure",
            "url": "https://github.com/o/r/pull/7",
        }
    ]


@pytest.mark.asyncio
async def test_ci_state_falls_back_to_check_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no legacy statuses, GitHub Actions check-runs decide the state."""

    async def _token() -> str:
        return "t"

    monkeypatch.setattr(integrations, "_github_token", _token)

    async def _get(
        path: str,
        params: dict[str, object] | None = None,
        **_kw: object,
    ) -> object:
        del params
        if path == "/repos/o/r/pulls":
            return [
                {
                    "number": 7,
                    "title": "T",
                    "html_url": "u",
                    "state": "open",
                    "head": {"ref": "feature", "sha": "abc"},
                }
            ]
        if path == "/repos/o/r/commits/abc/status":
            return {"total_count": 0, "state": "pending"}
        if path == "/repos/o/r/commits/abc/check-runs":
            return {
                "check_runs": [
                    {"status": "completed", "conclusion": "success"},
                    {"status": "in_progress", "conclusion": None},
                ]
            }
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(integrations, "_github_get", _get)

    cards = await github_pull_requests_for_branch("o/r", "feature")
    assert cards[0]["ci_status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param({"message": "Not Found"}, id="object_instead_of_list"),
        pytest.param(None, id="null_body"),
        pytest.param(["nonsense"], id="list_of_scalars"),
        pytest.param([{"number": 1, "head": "not-an-object"}], id="head_is_a_string"),
    ],
)
async def test_branch_pull_requests_degrades_on_unexpected_shapes(
    monkeypatch: pytest.MonkeyPatch,
    raw: object,
) -> None:
    """An unexpected GitHub body yields no PRs rather than an exception."""

    async def _token() -> str:
        return "t"

    async def _get(path: str, params: dict[str, object] | None = None, **_kw: object) -> object:
        del path, params
        return raw

    monkeypatch.setattr(integrations, "_github_token", _token)
    monkeypatch.setattr(integrations, "_github_get", _get)

    assert await github_pull_requests_for_branch("o/r", "feature") == []


@pytest.mark.asyncio
async def test_ci_lookups_are_capped_per_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the first few PRs get a CI lookup; the rest report ``None``."""

    async def _token() -> str:
        return "t"

    status_calls: list[str] = []

    async def _get(path: str, params: dict[str, object] | None = None, **_kw: object) -> object:
        del params
        if path.endswith("/pulls"):
            return [
                {
                    "number": n,
                    "title": f"PR {n}",
                    "html_url": f"https://github.com/o/r/pull/{n}",
                    "state": "open",
                    "head": {"ref": "feature", "sha": f"sha{n}", "repo": {"full_name": "o/r"}},
                }
                for n in range(12)
            ]
        status_calls.append(path)
        return {"total_count": 1, "state": "success"}

    monkeypatch.setattr(integrations, "_github_token", _token)
    monkeypatch.setattr(integrations, "_github_get", _get)

    cards = await github_pull_requests_for_branch("o/r", "feature")

    # Every matching PR is still reported — only the CI enrichment is bounded.
    assert len(cards) == 12
    assert len(status_calls) == integrations._MAX_CI_LOOKUPS
    assert [c["ci_status"] for c in cards[: integrations._MAX_CI_LOOKUPS]] == ["success"] * 5
    assert all(c["ci_status"] is None for c in cards[integrations._MAX_CI_LOOKUPS :])


@pytest.mark.asyncio
async def test_ci_status_survives_a_failing_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """One PR's CI lookup blowing up does not take the whole list down."""

    async def _token() -> str:
        return "t"

    async def _get(path: str, params: dict[str, object] | None = None, **_kw: object) -> object:
        del params
        if path.endswith("/pulls"):
            return [
                {
                    "number": 1,
                    "title": "PR",
                    "html_url": "u",
                    "state": "open",
                    "head": {"ref": "feature", "sha": "abc", "repo": {"full_name": "o/r"}},
                }
            ]
        raise RuntimeError("boom")

    monkeypatch.setattr(integrations, "_github_token", _token)
    monkeypatch.setattr(integrations, "_github_get", _get)

    cards = await github_pull_requests_for_branch("o/r", "feature")
    assert len(cards) == 1
    assert cards[0]["ci_status"] is None


@pytest.mark.asyncio
async def test_token_lookup_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``gh`` CLI fallback is awaited off-thread, so the loop keeps running."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(integrations, "_gh_token_cache", integrations._UNSET)

    ticks = 0

    def _slow_cli() -> str:
        import time

        time.sleep(0.2)
        return "from-gh"

    monkeypatch.setattr(integrations, "_gh_cli_token", _slow_cli)

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(_ticker())
    try:
        assert await integrations._github_token() == "from-gh"
    finally:
        task.cancel()

    # A synchronous subprocess.run would have frozen the loop for the
    # whole 0.2s and left this at zero.
    assert ticks > 1
