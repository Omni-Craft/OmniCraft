"""Unit tests for the GitHub integration's pure helpers."""

from __future__ import annotations

import pytest

from omnicraft.errors import OmniCraftError
from omnicraft.server.routes.integrations import _normalize, _validate_repo


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
