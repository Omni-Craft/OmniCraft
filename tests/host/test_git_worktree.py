"""Tests for host-side git worktree operations.

Exercises ``omnicraft.host.git_worktree`` against real ``git`` in a
temp repository — the operations run actual ``git worktree add`` /
``remove`` / ``branch -D`` so a regression in argv construction, repo-
root resolution, or removal ordering fails loud here.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnicraft.host.git_worktree import (
    CreatedWorktree,
    NotAGitRepositoryError,
    WorktreeError,
    create_worktree,
    delete_snapshot,
    git_ahead_behind,
    git_diff,
    git_diff_stat,
    git_remote_slug,
    git_upstream_ref,
    list_snapshots,
    list_worktrees,
    merge_worktree,
    remove_worktree,
    restore_snapshot,
    snapshot_worktree,
    validate_branch_name,
)

# Deterministic identity + config so the tests don't depend on the
# developer's global git config (user.name / init.defaultBranch).
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> None:
    """Run a git command in ``repo``, raising on failure.

    :param repo: Repository directory to run in.
    :param args: Git arguments after ``git``, e.g. ``("add", ".")``.
    """
    import os

    subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
    )


def _current_branch(path: Path) -> str:
    """Return the checked-out branch name at ``path``.

    :param path: A work tree (main or linked worktree) directory.
    :returns: Branch name, e.g. ``"feature/login"``.
    """
    import os

    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout.strip()


def _rev_parse(path: Path, ref: str = "HEAD") -> str:
    """Return the commit sha that ``ref`` resolves to at ``path``.

    :param path: A work tree directory.
    :param ref: Ref to resolve, e.g. ``"HEAD"`` or ``"develop"``.
    :returns: The 40-char commit sha.
    """
    import os

    return subprocess.run(
        ["git", "rev-parse", ref],
        cwd=path,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout.strip()


def _branch_exists(repo: Path, branch: str) -> bool:
    """Return whether ``branch`` exists in ``repo``.

    :param repo: Repository directory.
    :param branch: Branch name to check, e.g. ``"feature/login"``.
    :returns: ``True`` if the local branch exists.
    """
    import os

    out = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout.strip()
    return out != ""


def _worktree_count(repo: Path) -> int:
    """Return how many worktrees are registered for ``repo``.

    :param repo: Repository directory.
    :returns: Worktree count, where ``1`` means only the main work
        tree exists (no linked worktree was added).
    """
    import os

    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout
    # --porcelain emits one "worktree <path>" line per worktree.
    return out.count("worktree ")


@pytest.fixture()
def git_repo(tmp_path: Path) -> Iterator[Path]:
    """Create a one-commit git repo and yield its resolved root.

    :returns: Iterator yielding the repo root path (realpath, so it
        matches what ``git rev-parse --show-toplevel`` returns).
    """
    # Resolve so comparisons match git's realpath output (macOS
    # /tmp -> /private/tmp).
    repo = (tmp_path / "myrepo").resolve()
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    yield repo


def test_create_worktree_places_sibling_of_repo_root(git_repo: Path) -> None:
    """A new worktree lands at ``<repo>-worktrees/<branch>`` with the branch checked out."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/login")
    expected = git_repo.parent / "myrepo-worktrees" / "feature-login"
    # Path proves the sibling layout + slash->dash dir sanitization;
    # a regression in _resolve_worktree_path would change this.
    assert created.worktree_path == str(expected)
    assert Path(created.worktree_path).is_dir()
    # The branch is actually checked out in the worktree (not just the dir made).
    assert _current_branch(Path(created.worktree_path)) == "feature/login"
    assert isinstance(created, CreatedWorktree)


def test_create_worktree_resolves_repo_root_from_subdir(git_repo: Path) -> None:
    """Picking a subdir still anchors the worktree at the repo root's sibling."""
    sub = git_repo / "src"
    sub.mkdir()
    created = create_worktree(repo_path=str(sub), branch_name="wip")
    # Sibling of the repo ROOT, not of the picked subdir — proves
    # rev-parse --show-toplevel is used rather than the raw repo_path.
    assert created.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "wip")


def test_create_worktree_from_linked_worktree_anchors_at_main_repo(git_repo: Path) -> None:
    """Creating a worktree while inside a LINKED worktree anchors at the MAIN repo.

    Resolving the repo root naively (``rev-parse --show-toplevel``) from a
    linked worktree would nest the new worktree under it
    (``…/feature-a-worktrees/feature-b``). ``_main_work_tree`` resolves to
    the main checkout so worktrees stay siblings
    (``…/myrepo-worktrees/feature-b``) — the fork-resume picker prefills a
    worktree as the source session's workspace, so this is the common path.
    """
    # First worktree, created off the main repo.
    first = create_worktree(repo_path=str(git_repo), branch_name="feature/a")
    first_path = Path(first.worktree_path)
    assert first_path == git_repo.parent / "myrepo-worktrees" / "feature-a"

    # Second worktree, requested from INSIDE the first (linked) worktree.
    second = create_worktree(repo_path=str(first_path), branch_name="feature/b")

    # Sibling of the MAIN repo, NOT nested under the first worktree. A
    # regression to --show-toplevel would put it under
    # ``feature-a-worktrees/`` and this fails.
    assert second.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "feature-b")
    assert "feature-a-worktrees" not in second.worktree_path
    assert Path(second.worktree_path).is_dir()
    assert _current_branch(Path(second.worktree_path)) == "feature/b"


def test_create_worktree_from_base_branch(git_repo: Path) -> None:
    """A worktree branches from the explicit base ref's tip, not HEAD."""
    # Advance develop with its own commit so it differs from main —
    # otherwise the test would pass even if base_branch were ignored
    # (both would resolve to the same single commit).
    _git(git_repo, "checkout", "-q", "-b", "develop")
    (git_repo / "dev.txt").write_text("dev-only")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "dev commit")
    _git(git_repo, "checkout", "-q", "main")

    created = create_worktree(
        repo_path=str(git_repo), branch_name="from-develop", base_branch="develop"
    )
    assert _current_branch(Path(created.worktree_path)) == "from-develop"
    # Points at develop's tip, not main's — proves base_branch routed
    # the new branch to develop rather than falling back to HEAD.
    assert _rev_parse(Path(created.worktree_path)) == _rev_parse(git_repo, "develop")
    assert _rev_parse(Path(created.worktree_path)) != _rev_parse(git_repo, "main")


def test_create_worktree_unknown_base_branch_fails(git_repo: Path) -> None:
    """An unresolvable base ref fails loud (after the best-effort fetch)."""
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="x", base_branch="nope-not-a-branch")
    # Proves _ensure_base_resolvable rejects rather than silently
    # branching from HEAD when the requested base is missing.
    assert "base branch does not exist" in exc.value.message


@pytest.mark.parametrize("option_like", ["-f", "--exec-path"])
def test_create_worktree_option_like_base_branch_not_executed(
    git_repo: Path, option_like: str
) -> None:
    """A base_branch that looks like a git flag is rejected, never executed.

    ``base_branch`` is user-supplied and reaches ``git rev-parse`` and
    ``git worktree add`` argv. An option-like value (e.g. ``"-f"``, which
    is ``git worktree add``'s ``--force``) must be treated as an
    unresolvable rev, not parsed as a flag. This guards the end-to-end
    security property at the public API: the ref-resolution pre-check and
    the ``--end-of-options`` argv terminators together keep such a value
    from creating a worktree. A regression that let ``"-f"`` through as a
    flag would build a worktree from the wrong base (and force-create it)
    instead of failing — so the assertion below would see a linked
    worktree appear.
    """
    with pytest.raises(WorktreeError):
        create_worktree(repo_path=str(git_repo), branch_name="from-flag", base_branch=option_like)
    # Still only the main work tree — no linked worktree was added, proving
    # git treated the value as a (rejected) rev rather than a flag that
    # would have run `worktree add`. If `-f` were parsed as --force, the
    # count would be 2.
    assert _worktree_count(git_repo) == 1


def test_create_worktree_duplicate_branch_fails(git_repo: Path) -> None:
    """Creating two worktrees for the same branch name fails loud with the friendly error."""
    create_worktree(repo_path=str(git_repo), branch_name="dup")
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="dup")
    # The pre-check catches the existing branch before git's raw error;
    # we must NOT silently reuse the existing worktree.
    assert "already exists" in exc.value.message


def test_create_worktree_existing_branch_no_worktree_fails(git_repo: Path) -> None:
    """A branch that exists WITHOUT a worktree is still rejected by the pre-check.

    Proves the pre-check keys off branch existence, not directory
    occupancy — creating a worktree for a plain pre-existing branch
    would otherwise hit git's raw error.
    """
    _git(git_repo, "branch", "preexisting")
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="preexisting")
    assert "already exists" in exc.value.message
    assert "preexisting" in exc.value.message


def test_create_worktree_non_repo_fails(tmp_path: Path) -> None:
    """A directory that isn't a git repo is rejected."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(plain), branch_name="x")
    assert "not a git repository" in exc.value.message


def test_remove_worktree_deletes_dir_and_branch(git_repo: Path) -> None:
    """``delete_branch=True`` removes the directory AND the branch."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/login")
    remove_worktree(
        worktree_path=created.worktree_path, branch="feature/login", delete_branch=True
    )
    # Directory gone (git worktree remove --force ran)...
    assert not Path(created.worktree_path).exists()
    # ...and the branch deleted (git branch -D ran, after the worktree
    # was removed — git would refuse otherwise).
    assert not _branch_exists(git_repo, "feature/login")


def test_remove_worktree_keeps_branch_when_flag_false(git_repo: Path) -> None:
    """``delete_branch=False`` removes the directory but keeps the branch."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/keep")
    remove_worktree(
        worktree_path=created.worktree_path, branch="feature/keep", delete_branch=False
    )
    assert not Path(created.worktree_path).exists()
    # Branch survives — only the checkout directory was removed.
    assert _branch_exists(git_repo, "feature/keep")


def test_remove_worktree_missing_path_fails(git_repo: Path) -> None:
    """Removing a non-existent worktree path fails loud."""
    with pytest.raises(WorktreeError) as exc:
        remove_worktree(
            worktree_path=str(git_repo.parent / "myrepo-worktrees" / "ghost"),
            branch=None,
            delete_branch=False,
        )
    assert "does not exist" in exc.value.message


def test_list_worktrees_returns_main_first(git_repo: Path) -> None:
    """With no linked worktrees, only the main tree is listed."""
    result = list_worktrees(repo_path=str(git_repo))
    assert len(result) == 1
    main = result[0]
    assert main.path == str(git_repo)
    assert main.branch == "main"
    assert main.is_main is True
    assert main.detached is False


def test_list_worktrees_includes_linked(git_repo: Path) -> None:
    """A created worktree shows up with its branch and is not flagged main."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/login")
    result = list_worktrees(repo_path=str(git_repo))
    # Main first, then the linked worktree.
    assert result[0].is_main is True
    linked = next(w for w in result if not w.is_main)
    assert linked.path == created.worktree_path
    assert linked.branch == "feature/login"
    assert linked.detached is False


def test_list_worktrees_from_linked_resolves_same_list(git_repo: Path) -> None:
    """Listing from inside a linked worktree resolves the main repo's full list."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/a")
    # Query from the linked worktree — should still see BOTH worktrees.
    result = list_worktrees(repo_path=created.worktree_path)
    paths = {w.path for w in result}
    assert str(git_repo) in paths
    assert created.worktree_path in paths


def test_list_worktrees_reports_detached_head(git_repo: Path) -> None:
    """A detached-HEAD worktree lists with ``branch=None`` and ``detached=True``."""
    head = _rev_parse(git_repo)
    wt = git_repo.parent / "myrepo-worktrees" / "detached"
    wt.parent.mkdir(parents=True, exist_ok=True)
    # Add a worktree checked out at a bare commit → detached HEAD.
    _git(git_repo, "worktree", "add", "--detach", str(wt), head)
    result = list_worktrees(repo_path=str(git_repo))
    detached = next(w for w in result if w.path == str(wt))
    assert detached.branch is None
    assert detached.detached is True


def test_list_worktrees_non_git_path_fails(tmp_path: Path) -> None:
    """A non-git directory fails loud (the route maps this to 'no worktrees')."""
    plain = (tmp_path / "plain").resolve()
    plain.mkdir()
    with pytest.raises(WorktreeError):
        list_worktrees(repo_path=str(plain))


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "-leading",
        "a..b",
        "a/.hidden",
        "x.lock",
        "x.lock/y",
        "a b",
        "a~b",
        "a:b",
        "/lead",
        "trail/",
    ],
)
def test_validate_branch_name_rejects_bad(bad: str) -> None:
    """Branch names violating git ref-format are rejected before reaching argv."""
    with pytest.raises(WorktreeError):
        validate_branch_name(bad)


@pytest.mark.parametrize("good", ["feature/login", "fix-123", "a/b/c", "release_2", "v1.2"])
def test_validate_branch_name_accepts_good(good: str) -> None:
    """Well-formed branch names pass validation."""
    validate_branch_name(good)  # must not raise


# ── Arena: git_diff ──────────────────────────────────────────────────


def test_git_diff_includes_tracked_and_untracked(git_repo: Path) -> None:
    """A racer's diff shows both edited tracked files and new untracked ones."""
    wt = create_worktree(repo_path=str(git_repo), branch_name="arena-x", base_branch="main")
    (Path(wt.worktree_path) / "README.md").write_text("hi\nAGENT EDIT\n")
    (Path(wt.worktree_path) / "novo.py").write_text("print('from agent')\n")

    result = git_diff(worktree_path=wt.worktree_path, base_ref="main")

    assert result.truncated is False
    assert "AGENT EDIT" in result.diff  # tracked change vs base
    assert "novo.py" in result.diff and "from agent" in result.diff  # untracked new file


def test_git_diff_empty_when_no_changes(git_repo: Path) -> None:
    """A pristine worktree diffs to the empty string."""
    wt = create_worktree(repo_path=str(git_repo), branch_name="arena-clean", base_branch="main")
    assert git_diff(worktree_path=wt.worktree_path, base_ref="main").diff == ""


def test_git_diff_rejects_non_worktree(tmp_path: Path) -> None:
    """A non-git path is a WorktreeError, not a silent empty diff."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError):
        git_diff(worktree_path=str(plain), base_ref="HEAD")


# ── Arena: merge_worktree (promote winner) ───────────────────────────


def test_merge_worktree_commits_and_merges_into_base(git_repo: Path) -> None:
    """The winner's uncommitted work is committed and merged into a clean base."""
    wt = create_worktree(repo_path=str(git_repo), branch_name="arena-win", base_branch="main")
    (Path(wt.worktree_path) / "README.md").write_text("hi\nwinner\n")
    (Path(wt.worktree_path) / "feature.py").write_text("x = 1\n")

    result = merge_worktree(
        worktree_path=wt.worktree_path,
        branch="arena-win",
        base_branch="main",
        commit_message="arena: promote",
    )

    assert result.outcome == "merged"
    assert (git_repo / "README.md").read_text() == "hi\nwinner\n"
    assert (git_repo / "feature.py").read_text() == "x = 1\n"


def test_merge_worktree_refuses_dirty_base(git_repo: Path) -> None:
    """A base with uncommitted changes is left untouched (no merge)."""
    wt = create_worktree(repo_path=str(git_repo), branch_name="arena-d", base_branch="main")
    (Path(wt.worktree_path) / "README.md").write_text("hi\nracer\n")
    (git_repo / "README.md").write_text("hi\nDIRTY\n")  # uncommitted change in main

    result = merge_worktree(
        worktree_path=wt.worktree_path,
        branch="arena-d",
        base_branch="main",
        commit_message="x",
    )

    assert result.outcome == "base_dirty"
    assert (git_repo / "README.md").read_text() == "hi\nDIRTY\n"  # untouched


def test_merge_worktree_refuses_when_base_not_checked_out(git_repo: Path) -> None:
    """If the main work tree is on another branch, the merge refuses."""
    _git(git_repo, "checkout", "-q", "-b", "elsewhere")
    wt = create_worktree(repo_path=str(git_repo), branch_name="arena-n", base_branch="main")
    (Path(wt.worktree_path) / "README.md").write_text("hi\nracer\n")

    result = merge_worktree(
        worktree_path=wt.worktree_path,
        branch="arena-n",
        base_branch="main",
        commit_message="x",
    )

    assert result.outcome == "base_not_checked_out"


def test_merge_worktree_aborts_on_conflict_leaving_base_intact(git_repo: Path) -> None:
    """A conflicting merge is aborted; the base keeps its content, no merge state."""
    wt = create_worktree(repo_path=str(git_repo), branch_name="arena-c", base_branch="main")
    (Path(wt.worktree_path) / "README.md").write_text("RACER VERSION\n")
    # Advance main on the same line so the merge must conflict.
    (git_repo / "README.md").write_text("MAIN VERSION\n")
    _git(git_repo, "commit", "-qam", "main advance")

    result = merge_worktree(
        worktree_path=wt.worktree_path,
        branch="arena-c",
        base_branch="main",
        commit_message="x",
    )

    assert result.outcome == "conflict"
    assert (git_repo / "README.md").read_text() == "MAIN VERSION\n"  # unchanged
    # No half-finished merge left behind.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == ""


# ── Worktree snapshots (checkpoint / restore) ────────────────────────


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_snapshot_restore_is_an_exact_match(git_repo: Path) -> None:
    """Restore rewrites snapshot files, drops later files, recreates deletions."""
    _write(git_repo, "keep.txt", "keep\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "seed")

    # State A: edit README, add an untracked file, delete keep.txt.
    _write(git_repo, "README.md", "STATE A\n")
    _write(git_repo, "new.txt", "novo A\n")
    (git_repo / "keep.txt").unlink()
    snap = snapshot_worktree(worktree_path=str(git_repo), label="estado A")
    assert snap.label == "estado A"

    # Diverge: change README again, add another file, recreate keep, drop new.
    _write(git_repo, "README.md", "STATE B\n")
    _write(git_repo, "another.txt", "B\n")
    _write(git_repo, "keep.txt", "recreated\n")
    (git_repo / "new.txt").unlink()

    result = restore_snapshot(worktree_path=str(git_repo), snapshot_id=snap.id)

    assert (git_repo / "README.md").read_text() == "STATE A\n"  # restored
    assert (git_repo / "new.txt").read_text() == "novo A\n"  # snapshot untracked back
    assert not (git_repo / "keep.txt").exists()  # deleted-in-A stays deleted
    assert not (git_repo / "another.txt").exists()  # created-after removed
    assert result.backup_id is not None  # pre-restore state was captured


def test_restore_is_reversible_via_auto_backup(git_repo: Path) -> None:
    """The auto-backup lets a restore be undone."""
    _write(git_repo, "f.txt", "V1\n")
    snap_v1 = snapshot_worktree(worktree_path=str(git_repo), label="v1")
    _write(git_repo, "f.txt", "V2\n")

    undo = restore_snapshot(worktree_path=str(git_repo), snapshot_id=snap_v1.id)
    assert (git_repo / "f.txt").read_text() == "V1\n"

    restore_snapshot(worktree_path=str(git_repo), snapshot_id=undo.backup_id, auto_backup=False)
    assert (git_repo / "f.txt").read_text() == "V2\n"  # back to the pre-restore state


def test_snapshot_does_not_move_head_or_create_branches(git_repo: Path) -> None:
    """Snapshots live under a private ref — never a branch, never moving HEAD."""
    head_before = _rev_parse(git_repo, "HEAD")
    _write(git_repo, "wip.txt", "wip\n")
    snapshot_worktree(worktree_path=str(git_repo), label="wip")

    assert _rev_parse(git_repo, "HEAD") == head_before
    branches = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert branches == ["main"]


def test_list_and_delete_snapshots(git_repo: Path) -> None:
    """Snapshots list newest-first and delete cleanly."""
    _write(git_repo, "a.txt", "1\n")
    first = snapshot_worktree(worktree_path=str(git_repo), label="one")
    _write(git_repo, "a.txt", "2\n")
    second = snapshot_worktree(worktree_path=str(git_repo), label="two")

    ids = [s.id for s in list_snapshots(worktree_path=str(git_repo))]
    assert set(ids) >= {first.id, second.id}

    delete_snapshot(worktree_path=str(git_repo), snapshot_id=first.id)
    remaining = {s.id for s in list_snapshots(worktree_path=str(git_repo))}
    assert first.id not in remaining and second.id in remaining


def test_restore_rejects_unknown_or_malformed_id(git_repo: Path) -> None:
    """A bad id is a WorktreeError, never a path-injecting ref lookup."""
    with pytest.raises(WorktreeError):
        restore_snapshot(worktree_path=str(git_repo), snapshot_id="../../evil")
    with pytest.raises(WorktreeError):
        restore_snapshot(worktree_path=str(git_repo), snapshot_id="1783-nope")


def test_ahead_behind_counts_both_directions(git_repo: Path) -> None:
    """``git_ahead_behind`` separates the two sides of a diverged history."""
    _git(git_repo, "branch", "base")
    _write(git_repo, "a.txt", "a\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "on main")
    _write(git_repo, "b.txt", "b\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "on main 2")

    ahead, behind = git_ahead_behind(worktree_path=str(git_repo), base_ref="base")
    # main moved twice, base did not — the argument order must not swap
    # these (rev-list --left-right prints behind first).
    assert (ahead, behind) == (2, 0)


def test_ahead_behind_sees_commits_only_on_the_base(git_repo: Path) -> None:
    """Commits added to the base alone count as ``behind``, not ``ahead``."""
    _git(git_repo, "checkout", "-q", "-b", "topic")
    _git(git_repo, "checkout", "-q", "main")
    _write(git_repo, "a.txt", "a\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "on main")
    _git(git_repo, "checkout", "-q", "topic")

    ahead, behind = git_ahead_behind(worktree_path=str(git_repo), base_ref="main")
    assert (ahead, behind) == (0, 1)


def test_ahead_behind_rejects_an_unresolvable_base(git_repo: Path) -> None:
    """A base ref that does not exist is an error, not a silent zero."""
    with pytest.raises(WorktreeError):
        git_ahead_behind(worktree_path=str(git_repo), base_ref="origin/nope")


def test_diff_stat_sums_committed_and_uncommitted_changes(git_repo: Path) -> None:
    """``git_diff_stat`` aggregates numstat over the whole change set."""
    _git(git_repo, "branch", "base")
    _write(git_repo, "a.txt", "1\n2\n3\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "committed")
    # Uncommitted on top of the commit — both must land in the totals.
    _write(git_repo, "README.md", "hi\nthere\n")

    stat = git_diff_stat(worktree_path=str(git_repo), base_ref="base")
    assert (stat.files, stat.added, stat.removed) == (2, 5, 1)


def test_diff_stat_counts_binary_files_without_lines(git_repo: Path) -> None:
    """A binary file counts as a changed file but adds no line counts."""
    _git(git_repo, "branch", "base")
    (git_repo / "blob.bin").write_bytes(b"\x00\x01\x02\x00")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "binary")

    stat = git_diff_stat(worktree_path=str(git_repo), base_ref="base")
    # numstat reports "-\t-" here; parsing it as an int would crash.
    assert (stat.files, stat.added, stat.removed) == (1, 0, 0)


def test_diff_stat_is_empty_on_a_clean_tree(git_repo: Path) -> None:
    """No changes means zeroes, not an error."""
    stat = git_diff_stat(worktree_path=str(git_repo), base_ref="HEAD")
    assert (stat.files, stat.added, stat.removed) == (0, 0, 0)


def test_upstream_ref_is_none_without_tracking(git_repo: Path) -> None:
    """A branch that tracks nothing yields ``None`` rather than raising."""
    assert git_upstream_ref(worktree_path=str(git_repo)) is None


def test_upstream_ref_reports_the_tracking_branch(git_repo: Path) -> None:
    """A configured upstream comes back in ``remote/branch`` form."""
    _git(git_repo, "remote", "add", "origin", "https://github.com/octocat/hello-world.git")
    _git(git_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(git_repo, "branch", "--set-upstream-to=origin/main", "main")
    assert git_upstream_ref(worktree_path=str(git_repo)) == "origin/main"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/octocat/hello-world.git", "octocat/hello-world"),
        ("https://github.com/octocat/hello-world", "octocat/hello-world"),
        ("git@github.com:octocat/hello-world.git", "octocat/hello-world"),
        ("https://gitlab.com/octocat/hello-world.git", None),
    ],
)
def test_remote_slug_parses_both_url_forms(
    git_repo: Path,
    url: str,
    expected: str | None,
) -> None:
    """Only github.com remotes yield an ``owner/name`` slug."""
    _git(git_repo, "remote", "add", "origin", url)
    assert git_remote_slug(worktree_path=str(git_repo)) == expected


def test_remote_slug_is_none_without_a_remote(git_repo: Path) -> None:
    """A repo with no ``origin`` is not an error."""
    assert git_remote_slug(worktree_path=str(git_repo)) is None


def test_list_worktrees_outside_a_repo_is_typed_as_not_a_repository(tmp_path: Path) -> None:
    """A plain directory raises the *specific* error, not a generic failure.

    Callers key off this to answer "no repo here" with empty fields
    instead of an error, so the subclass must survive refactors.
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NotAGitRepositoryError):
        list_worktrees(repo_path=str(plain))


def test_list_worktrees_missing_path_is_not_a_repository(tmp_path: Path) -> None:
    """A path that does not exist is 'nothing here', not a git failure."""
    with pytest.raises(NotAGitRepositoryError):
        list_worktrees(repo_path=str(tmp_path / "gone"))


def test_list_worktrees_timeout_is_a_plain_worktree_error(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout must NOT be mistaken for 'not a git repository'.

    The two produce opposite UI: an empty status bar versus a visible
    error. Collapsing them hides a hung repo behind a clean-looking
    readout.
    """
    import subprocess as sp

    def _timeout(*_args: object, **_kwargs: object) -> None:
        raise sp.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(sp, "run", _timeout)
    with pytest.raises(WorktreeError) as excinfo:
        list_worktrees(repo_path=str(git_repo))
    assert not isinstance(excinfo.value, NotAGitRepositoryError)
    assert "timed out" in excinfo.value.message


def test_list_worktrees_uses_the_timeout_it_is_given(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The short read budget reaches git, rather than the 120s default."""
    import subprocess as sp

    seen: list[float | None] = []
    real_run = sp.run

    def _record(*args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("timeout"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(sp, "run", _record)
    list_worktrees(repo_path=str(git_repo), timeout=5.0)
    # Both the main-work-tree resolution and the listing itself, so a
    # regression that threads the timeout into only one is caught.
    assert seen == [5.0, 5.0]
