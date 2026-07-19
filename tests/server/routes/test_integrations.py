"""Unit tests for the GitHub integration's pure helpers."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnicraft.errors import OmniCraftError
from omnicraft.server.routes import integrations
from omnicraft.server.routes.integrations import (
    _commit_ci_status,
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
async def test_branch_pull_requests_is_unavailable_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured integration is not an error, but it is not an answer either."""

    async def _no_token() -> None:
        return None

    monkeypatch.setattr(integrations, "_github_token", _no_token)
    found = await github_pull_requests_for_branch("o/r", "feature")
    assert (found.cards, found.status) == ([], "unavailable")


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

    found = await github_pull_requests_for_branch("o/r", "feature")
    assert found.cards == [
        {
            "number": 7,
            "title": "Add login",
            "state": "merged",
            "ci_status": "failure",
            "url": "https://github.com/o/r/pull/7",
        }
    ]
    # One short page back from GitHub: the list is everything there is.
    assert found.status == "ok"


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

    cards = (await github_pull_requests_for_branch("o/r", "feature")).cards
    assert cards[0]["ci_status"] == "pending"


def _pr_row(**overrides: Any) -> dict[str, Any]:
    """A well-formed pulls row for the ``feature`` branch.

    :param overrides: Fields to replace on the row.
    :returns: The raw row as GitHub would send it.
    """
    return {
        "number": 7,
        "title": "Add login",
        "html_url": "https://github.com/o/r/pull/7",
        "state": "open",
        "head": {"ref": "feature", "sha": "abc", "repo": {"full_name": "o/r"}},
        **overrides,
    }


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(_pr_row(), id="complete_row"),
        # Everything below is genuinely optional: a real pull request
        # must never be dropped — and the list marked short — over one.
        pytest.param(_pr_row(head={"ref": "feature"}), id="no_head_sha_only_skips_ci"),
        pytest.param(_pr_row(head={"ref": "feature", "repo": None}), id="head_repo_deleted"),
        pytest.param(_pr_row(title=None), id="no_title"),
        pytest.param(_pr_row(user=None, labels=None, updated_at=None), id="no_author_or_labels"),
    ],
)
def test_readable_pr_rows_are_kept(row: dict[str, Any]) -> None:
    """Only what a card is built from is required; optional fields stay optional."""
    assert integrations._is_readable_pr_row(row) is True


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(_pr_row(number=True), id="number_is_a_bool"),
        pytest.param(_pr_row(number="7"), id="number_is_a_string"),
        pytest.param(_pr_row(head={"ref": ""}), id="head_ref_is_empty"),
        pytest.param(_pr_row(head={"sha": "abc"}), id="head_ref_is_missing"),
        pytest.param(_pr_row(state=None), id="state_is_missing"),
        pytest.param(_pr_row(state=""), id="state_is_empty"),
        pytest.param(_pr_row(html_url=None), id="url_is_missing"),
        pytest.param(_pr_row(html_url=""), id="url_is_empty"),
    ],
)
def test_unreadable_pr_rows_are_rejected(row: dict[str, Any]) -> None:
    """A row that cannot produce a valid card is not quietly turned into one."""
    assert integrations._is_readable_pr_row(row) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        pytest.param({"message": "Not Found"}, id="object_instead_of_list"),
        pytest.param(None, id="null_body"),
        pytest.param(["nonsense"], id="list_of_scalars"),
        pytest.param([{"number": 1, "head": "not-an-object"}], id="head_is_a_string"),
        pytest.param([{"head": {"ref": "feature"}}], id="number_is_missing"),
        pytest.param([_pr_row(number=True)], id="number_is_a_bool"),
        pytest.param([_pr_row(head={"ref": ""})], id="head_ref_is_empty"),
        pytest.param([_pr_row(state=None)], id="state_is_missing"),
        pytest.param([_pr_row(html_url=None)], id="url_is_missing"),
    ],
)
async def test_branch_pull_requests_degrades_on_unexpected_shapes(
    monkeypatch: pytest.MonkeyPatch,
    raw: object,
) -> None:
    """An unreadable GitHub body is "cannot tell", not "this branch has no PR".

    Every row here is unreadable, so there is nothing to build a list
    from — reporting an empty ``"ok"`` list would let a client conclude
    the branch has no pull request and drop one it really has.
    """

    async def _token() -> str:
        return "t"

    async def _get(path: str, params: dict[str, object] | None = None, **_kw: object) -> object:
        del path, params
        return raw

    monkeypatch.setattr(integrations, "_github_token", _token)
    monkeypatch.setattr(integrations, "_github_get", _get)

    found = await github_pull_requests_for_branch("o/r", "feature")
    assert (found.cards, found.status) == ([], "unavailable")


@pytest.mark.asyncio
async def test_an_unreadable_row_among_good_ones_degrades_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped row is confessed: the readable PRs ship, marked incomplete."""

    async def _token() -> str:
        return "t"

    async def _get(path: str, params: dict[str, object] | None = None, **_kw: object) -> object:
        del params
        if path.endswith("/pulls"):
            return [
                {
                    "number": 7,
                    "title": "Readable",
                    "html_url": "https://github.com/o/r/pull/7",
                    "state": "open",
                    "head": {"ref": "feature", "sha": "abc", "repo": {"full_name": "o/r"}},
                },
                # Could be a PR for this very branch — no way to know.
                {"number": 8, "head": "not-an-object"},
            ]
        return {"total_count": 1, "state": "success"}

    monkeypatch.setattr(integrations, "_github_token", _token)
    monkeypatch.setattr(integrations, "_github_get", _get)

    found = await github_pull_requests_for_branch("o/r", "feature")
    assert [c["number"] for c in found.cards] == [7]
    # Never "ok": one row was lost, so the list is known to be short.
    assert found.status == "partial"


@pytest.mark.asyncio
async def test_ci_lookups_are_capped_per_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the first few PRs get a CI lookup; the rest report ``"unknown"``."""

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

    found = await github_pull_requests_for_branch("o/r", "feature")
    cards = found.cards

    # Every matching PR is still reported — only the CI enrichment is bounded.
    assert len(cards) == 12
    assert found.status == "ok"
    assert len(status_calls) == integrations._MAX_CI_LOOKUPS
    assert [c["ci_status"] for c in cards[: integrations._MAX_CI_LOOKUPS]] == ["success"] * 5
    # Past the cap CI was never asked about, so nothing is claimed — an
    # unasked PR must not read as "no CI configured".
    assert all(c["ci_status"] == "unknown" for c in cards[integrations._MAX_CI_LOOKUPS :])


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

    cards = (await github_pull_requests_for_branch("o/r", "feature")).cards
    assert len(cards) == 1
    # The lookup blew up, so the state is unknown — not "no checks".
    assert cards[0]["ci_status"] == "unknown"


@pytest.mark.asyncio
async def test_ci_status_is_none_when_the_commit_has_no_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit GitHub confirms has no CI reports ``"none"``, not ``"unknown"``."""

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
        if path == "/repos/o/r/commits/abc/status":
            return {"total_count": 0, "state": "pending"}
        if path == "/repos/o/r/commits/abc/check-runs":
            return {"check_runs": []}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(integrations, "_github_token", _token)
    monkeypatch.setattr(integrations, "_github_get", _get)

    cards = (await github_pull_requests_for_branch("o/r", "feature")).cards
    # Asked and answered: this repo simply runs no CI on the branch.
    assert cards[0]["ci_status"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("combined", "checks", "expected"),
    [
        # A combined status with no readable count says nothing at all —
        # not even the zero that would license asking check-runs.
        pytest.param({}, None, "unknown", id="combined_without_a_count"),
        pytest.param({"total_count": "two"}, None, "unknown", id="combined_count_is_a_string"),
        pytest.param({"total_count": -1}, None, "unknown", id="combined_count_is_negative"),
        pytest.param(
            {"total_count": 3, "state": "reticulating"},
            None,
            "unknown",
            id="combined_state_is_unknown",
        ),
        pytest.param({"total_count": 0}, {}, "unknown", id="checks_without_the_list"),
        pytest.param(
            {"total_count": 0},
            {"check_runs": ["nonsense"]},
            "unknown",
            id="check_run_is_a_scalar",
        ),
        pytest.param(
            {"total_count": 0},
            {"check_runs": [{"status": "reticulating"}]},
            "unknown",
            id="check_run_status_is_unknown",
        ),
        # Completed with a conclusion we cannot read: it is not a pass.
        pytest.param(
            {"total_count": 0},
            {"check_runs": [{"status": "completed", "conclusion": None}]},
            "unknown",
            id="conclusion_is_missing",
        ),
        pytest.param(
            {"total_count": 0},
            {"check_runs": [{"status": "completed", "conclusion": "mystery"}]},
            "unknown",
            id="conclusion_is_unknown",
        ),
        # A run still going next to one whose result cannot be read: the
        # unreadable sibling decides, because "pending" would claim CI is
        # merely unfinished when we cannot say that.
        pytest.param(
            {"total_count": 0},
            {
                "check_runs": [
                    {"status": "in_progress", "conclusion": None},
                    {"status": "completed", "conclusion": "mystery"},
                ]
            },
            "unknown",
            id="in_progress_beside_an_unreadable_conclusion",
        ),
        pytest.param(
            {"total_count": 0},
            {
                "check_runs": [
                    {"status": "in_progress", "conclusion": None},
                    {"status": "completed"},
                ]
            },
            "unknown",
            id="in_progress_beside_a_missing_conclusion",
        ),
        # A run genuinely still going, all finished ones readable.
        pytest.param(
            {"total_count": 0},
            {
                "check_runs": [
                    {"status": "in_progress", "conclusion": None},
                    {"status": "completed", "conclusion": "success"},
                ]
            },
            "pending",
            id="in_progress_beside_a_readable_pass",
        ),
        # A known failure still outranks a run that has not finished.
        pytest.param(
            {"total_count": 0},
            {
                "check_runs": [
                    {"status": "in_progress", "conclusion": None},
                    {"status": "completed", "conclusion": "failure"},
                ]
            },
            "failure",
            id="failure_outranks_an_unfinished_sibling",
        ),
        # Recognized non-failing conclusions still add up to a pass.
        pytest.param(
            {"total_count": 0},
            {
                "check_runs": [
                    {"status": "completed", "conclusion": "neutral"},
                    {"status": "completed", "conclusion": "skipped"},
                ]
            },
            "success",
            id="neutral_and_skipped_are_a_pass",
        ),
        # A run that definitely failed is a fact, even next to a sibling
        # we cannot read — on either axis it can be unreadable on.
        pytest.param(
            {"total_count": 0},
            {
                "check_runs": [
                    {"status": "completed", "conclusion": "failure"},
                    {"status": "completed", "conclusion": "mystery"},
                ]
            },
            "failure",
            id="a_real_failure_outranks_an_unreadable_conclusion",
        ),
        pytest.param(
            {"total_count": 0},
            {
                "check_runs": [
                    {"status": "completed", "conclusion": "failure"},
                    {"status": "reticulating", "conclusion": None},
                ]
            },
            "failure",
            id="a_real_failure_outranks_an_unreadable_status",
        ),
    ],
)
async def test_malformed_ci_bodies_are_unknown_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
    combined: object,
    checks: object,
    expected: str,
) -> None:
    """A CI body this code cannot read never becomes "no CI" or a pass."""

    async def _get(path: str, params: dict[str, object] | None = None, **_kw: object) -> object:
        del params
        if path.endswith("/status"):
            return combined
        if checks is None:
            raise AssertionError("check-runs must not be asked without a readable count")
        return checks

    monkeypatch.setattr(integrations, "_github_get", _get)

    assert await _commit_ci_status("o/r", "abc") == expected


@pytest.mark.asyncio
async def test_ci_status_is_unknown_when_the_checks_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused check-runs call is unknown, never mistaken for "no CI"."""

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
        if path == "/repos/o/r/commits/abc/status":
            return {"total_count": 0, "state": "pending"}
        raise OmniCraftError("check-runs refused")

    monkeypatch.setattr(integrations, "_github_token", _token)
    monkeypatch.setattr(integrations, "_github_get", _get)

    cards = (await github_pull_requests_for_branch("o/r", "feature")).cards
    assert cards[0]["ci_status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("available", "expected_cards", "expected_status"),
    [
        # Exactly a page's worth and nothing beyond it: complete, and the
        # status bar has no reason to distrust the list.
        pytest.param(30, 30, "ok", id="exactly_one_page"),
        # One PR past the page: the list really is short, and says so.
        pytest.param(31, 30, "partial", id="more_than_one_page"),
    ],
)
async def test_a_full_page_is_told_apart_from_a_truncated_one(
    monkeypatch: pytest.MonkeyPatch,
    available: int,
    expected_cards: int,
    expected_status: str,
) -> None:
    """The over-fetched row decides truncation, so 30 PRs are not called partial."""

    async def _token() -> str:
        return "t"

    asked: list[object] = []

    async def _get(path: str, params: dict[str, object] | None = None, **_kw: object) -> object:
        if path.endswith("/pulls"):
            asked.append((params or {}).get("per_page"))
            rows = [
                {
                    "number": n,
                    "title": f"PR {n}",
                    "html_url": f"https://github.com/o/r/pull/{n}",
                    "state": "open",
                    "head": {"ref": "feature", "sha": f"sha{n}", "repo": {"full_name": "o/r"}},
                }
                for n in range(available)
            ]
            return rows[: int((params or {}).get("per_page") or available)]
        return {"total_count": 1, "state": "success"}

    monkeypatch.setattr(integrations, "_github_token", _token)
    monkeypatch.setattr(integrations, "_github_get", _get)

    found = await github_pull_requests_for_branch("o/r", "feature")
    # One row over the page is fetched purely as a truncation signal and
    # is never handed to the caller.
    assert asked == [integrations._PAGE_SIZE + 1]
    assert len(found.cards) == expected_cards
    assert found.status == expected_status


@pytest.mark.asyncio
async def test_branch_pull_requests_is_unavailable_when_github_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected list call is "cannot tell", not "this branch has no PR"."""

    async def _token() -> str:
        return "t"

    async def _get(path: str, params: dict[str, object] | None = None, **_kw: object) -> object:
        del path, params
        raise OmniCraftError("bad credentials")

    monkeypatch.setattr(integrations, "_github_token", _token)
    monkeypatch.setattr(integrations, "_github_get", _get)

    found = await github_pull_requests_for_branch("o/r", "feature")
    assert (found.cards, found.status) == ([], "unavailable")


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
