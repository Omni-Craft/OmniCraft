"""Host-side git worktree operations for session-start worktrees.

Runs ``git`` (via argv lists, never a shell) on the host in response to
``host.create_worktree`` / ``host.remove_worktree`` frames. Branch names
are validated against git ref-format rules before reaching argv. See
designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# fetch/add can be slow on large repos; bound it so git can't hang the
# host's tunnel loop.
_GIT_TIMEOUT_S: float = 120.0

# Read-only status queries sit in front of a UI request, so they get a much
# tighter budget than the mutating worktree operations above.
_GIT_READ_TIMEOUT_S: float = 5.0

# Max directory-collision suffixes (``-2`` .. ``-N``) before giving up.
_MAX_DIR_COLLISION_SUFFIX: int = 50

# Cap on the diff text carried back over the tunnel, so a runaway change set
# (generated files, vendored deps) can't produce a multi-megabyte frame.
_MAX_DIFF_BYTES: int = 200_000

# Cap on untracked files whose full contents are inlined into an arena diff.
_MAX_UNTRACKED_FILES: int = 100

# Chars git refuses in a ref: space, control chars, ``~^:?*[\``, DEL.
# (``..``, leading ``-``/``.``, ``/`` edges, ``.lock``, ``@{`` are
# checked separately.)
_INVALID_BRANCH_CHARS = re.compile(r"[\x00-\x20~^:?*\[\\\x7f]")


class WorktreeError(Exception):
    """Raised when a git worktree operation fails.

    The message is user-facing and surfaced verbatim in the
    ``host.*_worktree_result`` frame's ``error`` field.

    :param message: Human-readable failure reason, e.g.
        ``"not a git repository: /tmp/x"``.
    """

    def __init__(self, message: str) -> None:
        """Initialize with the user-facing error message.

        :param message: Error string surfaced to the API caller.
        """
        super().__init__(message)
        self.message = message


class NotAGitRepositoryError(WorktreeError):
    """Raised when a path simply is not inside a git repository.

    Split out of :class:`WorktreeError` so callers can tell "there is no
    repo here" (an ordinary, reportable state) apart from "git failed"
    (a timeout, a broken install, a corrupt repo) — the two must not
    collapse into the same empty answer.
    """


def validate_branch_name(name: str) -> None:
    """Validate a git branch name against ``git check-ref-format`` rules.

    :param name: Proposed branch name, e.g. ``"feature/login"``.
    :raises WorktreeError: If the name is empty or violates any
        ref-format rule. The message names the specific violation.
    """
    if not name:
        raise WorktreeError("branch name must not be empty")
    if name.startswith("-"):
        raise WorktreeError(f"branch name must not start with '-': {name!r}")
    if name.startswith("/") or name.endswith("/"):
        raise WorktreeError(f"branch name must not start or end with '/': {name!r}")
    if name.endswith("."):
        raise WorktreeError(f"branch name must not end with '.': {name!r}")
    if any(part.endswith(".lock") for part in name.split("/")):
        raise WorktreeError(f"branch name path components must not end with '.lock': {name!r}")
    if ".." in name:
        raise WorktreeError(f"branch name must not contain '..': {name!r}")
    if "//" in name:
        raise WorktreeError(f"branch name must not contain '//': {name!r}")
    if "@{" in name:
        raise WorktreeError(f"branch name must not contain '@{{': {name!r}")
    if name == "@":
        raise WorktreeError("branch name must not be '@'")
    if _INVALID_BRANCH_CHARS.search(name):
        raise WorktreeError(
            f"branch name {name!r} contains an invalid character; spaces, "
            f"control characters, and any of ~ ^ : ? * [ \\ are not allowed"
        )
    # No path component may start with '.' (e.g. ".hidden" or "a/.b").
    if any(part.startswith(".") for part in name.split("/")):
        raise WorktreeError(f"branch name path components must not start with '.': {name!r}")


def _sanitize_dirname(branch_name: str) -> str:
    """Derive a single-segment directory name from a branch name.

    Slashes collapse to ``-`` so the worktree lives in one directory.

    :param branch_name: Validated branch name, e.g. ``"feature/login"``.
    :returns: Filesystem-safe single segment, e.g. ``"feature-login"``.
    """
    return branch_name.strip("/").replace("/", "-")


def _run_git(
    args: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    timeout: float = _GIT_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, returning the completed process.

    :param args: Git argv *after* ``git``, e.g.
        ``["rev-parse", "--show-toplevel"]``. Passed as a list so no
        shell parsing occurs.
    :param cwd: Working directory to run git in, e.g.
        ``"/Users/alice/myrepo"``.
    :param env: Full environment for the subprocess (e.g. a scratch
        ``GIT_INDEX_FILE`` for snapshot plumbing). ``None`` inherits the
        host process environment.
    :param timeout: Seconds before the command is killed. Defaults to
        :data:`_GIT_TIMEOUT_S`; read-only status queries pass the much
        shorter :data:`_GIT_READ_TIMEOUT_S`.
    :returns: The completed process with captured text stdout/stderr.
    :raises WorktreeError: If git is not installed, or the command
        exceeds *timeout*.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise WorktreeError("git is not installed on the host") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git command timed out after {timeout:.0f}s") from exc


def _git_error(label: str, result: subprocess.CompletedProcess[str]) -> WorktreeError:
    """Build a WorktreeError from a failed git command.

    Includes the exit code (always present) and stderr when non-empty,
    so no invented "unknown error" fallback is needed.

    :param label: What failed, e.g. ``"git worktree add failed"``.
    :param result: The completed process with a non-zero return code.
    :returns: A :class:`WorktreeError` with code + stderr detail.
    """
    detail = result.stderr.strip()
    suffix = f": {detail}" if detail else ""
    return WorktreeError(f"{label} (exit {result.returncode}){suffix}")


def _main_work_tree(repo_path: str, *, timeout: float = _GIT_TIMEOUT_S) -> str:
    """Resolve the MAIN work tree for any path inside a git repo.

    ``git worktree list --porcelain`` enumerates every work tree of the
    repository; its first entry is always the main one (the checkout all
    linked worktrees share). Run from ``repo_path``, this resolves the
    same main work tree whether the user picked the main checkout, a
    subdirectory, or a *linked worktree* — so a new worktree is always
    created as a sibling of the MAIN repo (e.g.
    ``…/myrepo-worktrees/<branch>``) rather than nested inside a worktree
    the session happened to start in (which ``rev-parse --show-toplevel``
    would produce: ``…/myrepo-worktrees/feature-worktrees/<branch>``).

    :param repo_path: Absolute path inside a git repository — the
        directory the user picked, e.g.
        ``"/Users/alice/myrepo-worktrees/feature"``.
    :param timeout: Seconds before git is killed. Defaults to the
        mutation budget; status reads pass the short read budget.
    :returns: Absolute path of the main work tree, e.g.
        ``"/Users/alice/myrepo"``.
    :raises NotAGitRepositoryError: If ``repo_path`` is not a directory
        or not inside a git work tree.
    :raises WorktreeError: If git itself failed (timeout, not installed,
        unreadable repository).
    """
    if not Path(repo_path).is_dir():
        raise NotAGitRepositoryError(f"path is not a directory: {repo_path}")
    result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_path, timeout=timeout)
    if result.returncode != 0:
        # Only git's own "not a repository" wording means there is
        # nothing here; any other non-zero exit is a real failure and
        # must not masquerade as an empty workspace.
        if "not a git repository" in result.stderr.lower():
            raise NotAGitRepositoryError(f"not a git repository: {repo_path}")
        raise _git_error("git worktree list failed", result)
    for line in result.stdout.splitlines():
        # Porcelain format: the first record's ``worktree <path>`` line is
        # the main work tree; linked worktrees follow.
        if line.startswith("worktree "):
            return line[len("worktree ") :].strip()
    raise WorktreeError(f"could not resolve main work tree for {repo_path}")


@dataclass
class WorktreeInfo:
    """One entry from ``git worktree list``.

    :param path: Absolute worktree directory, e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Checked-out branch without the ``refs/heads/``
        prefix, e.g. ``"feature/login"``. ``None`` when the worktree
        is in detached-HEAD state.
    :param is_main: ``True`` for the repository's main work tree (the
        first ``git worktree list`` record), ``False`` for linked
        worktrees.
    :param detached: ``True`` when the worktree has a detached HEAD
        (no branch checked out).
    """

    path: str
    branch: str | None
    is_main: bool
    detached: bool


def list_worktrees(*, repo_path: str, timeout: float = _GIT_TIMEOUT_S) -> list[WorktreeInfo]:
    """List the git worktrees of the repository containing ``repo_path``.

    Resolves the main work tree first (so a linked worktree resolves the
    same list as the main checkout), then parses
    ``git worktree list --porcelain``. The first record is always the
    main work tree; the rest are linked worktrees.

    :param repo_path: Absolute path inside a git repository — the
        directory the user picked, e.g. ``"/Users/alice/myrepo"``.
    :param timeout: Seconds before each git call is killed. Defaults to
        the mutation budget; status reads pass the short read budget.
    :returns: One :class:`WorktreeInfo` per worktree, main first.
    :raises NotAGitRepositoryError: If ``repo_path`` is not a directory
        or not inside a git work tree.
    :raises WorktreeError: If ``git worktree list`` fails.
    """
    repo_root = _main_work_tree(repo_path, timeout=timeout)
    result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root, timeout=timeout)
    if result.returncode != 0:
        raise _git_error("git worktree list failed", result)

    worktrees: list[WorktreeInfo] = []
    path: str | None = None
    branch: str | None = None
    detached = False
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
            branch = None
            detached = False
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif line == "detached":
            detached = True
        elif line == "" and path is not None:
            # Blank line terminates a record.
            worktrees.append(
                WorktreeInfo(
                    path=path,
                    branch=branch,
                    is_main=not worktrees,
                    detached=detached,
                )
            )
            path = None
    # The porcelain output may omit a trailing blank line for the last record.
    if path is not None:
        worktrees.append(
            WorktreeInfo(path=path, branch=branch, is_main=not worktrees, detached=detached)
        )
    return worktrees


def _local_branch_exists(repo_root: str, branch_name: str) -> bool:
    """Return whether a local branch already exists in the repo.

    :param repo_root: Absolute repo work-tree root, e.g.
        ``"/Users/alice/myrepo"``.
    :param branch_name: Branch name to check, e.g. ``"feature/login"``.
    :returns: ``True`` if ``refs/heads/<branch_name>`` resolves.
    """
    return (
        _run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=repo_root,
        ).returncode
        == 0
    )


def _resolve_worktree_path(repo_root: str, branch_name: str) -> Path:
    """Compute a collision-free sibling worktree directory path.

    Places the worktree at
    ``<parent-of-repo-root>/<repo-name>-worktrees/<sanitized-branch>``,
    appending a numeric suffix if that path already exists on disk.

    :param repo_root: Absolute repo work-tree root, e.g.
        ``"/Users/alice/myrepo"``.
    :param branch_name: Validated branch name, e.g.
        ``"feature/login"``.
    :returns: A path that does not yet exist, e.g.
        ``Path("/Users/alice/myrepo-worktrees/feature-login")``.
    :raises WorktreeError: If no free path is found within
        :data:`_MAX_DIR_COLLISION_SUFFIX` attempts.
    """
    root = Path(repo_root)
    base_dir = root.parent / f"{root.name}-worktrees"
    dirname = _sanitize_dirname(branch_name)
    candidate = base_dir / dirname
    if not candidate.exists():
        return candidate
    for suffix in range(2, _MAX_DIR_COLLISION_SUFFIX + 1):
        candidate = base_dir / f"{dirname}-{suffix}"
        if not candidate.exists():
            return candidate
    raise WorktreeError(
        f"could not find a free worktree directory under {base_dir} "
        f"after {_MAX_DIR_COLLISION_SUFFIX} attempts"
    )


def _ensure_base_resolvable(repo_root: str, base_branch: str) -> None:
    """Make ``base_branch`` resolvable, fetching once if needed.

    If the base ref doesn't resolve locally (e.g. a remote-tracking
    branch not yet fetched), attempt a single ``git fetch`` and
    re-check. A fetch failure (offline) is not fatal on its own — the
    subsequent re-check produces the user-facing error.

    :param repo_root: Absolute repo work-tree root, e.g.
        ``"/Users/alice/myrepo"``.
    :param base_branch: Base ref the user requested, e.g. ``"main"``
        or ``"origin/main"``.
    :raises WorktreeError: If the base ref cannot be resolved even
        after a fetch attempt.
    """
    # --end-of-options forces git to treat the user-supplied base_branch as a
    # rev, never an option, so a value like "--exec-path" can't inject a git
    # flag (argv-only, no shell). Note: a bare "--" would not work here — git
    # rev-parse treats args after "--" as pathspecs, not revs.
    if (
        _run_git(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", base_branch], cwd=repo_root
        ).returncode
        == 0
    ):
        return
    # Best-effort fetch from the default remote, then re-verify.
    _run_git(["fetch"], cwd=repo_root)
    if (
        _run_git(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", base_branch], cwd=repo_root
        ).returncode
        != 0
    ):
        raise WorktreeError(f"base branch does not exist: {base_branch}")


@dataclass
class CreatedWorktree:
    """Result of a successful worktree creation.

    :param worktree_path: Absolute path of the created worktree
        directory, e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: The branch checked out in the worktree, e.g.
        ``"feature/login"``.
    """

    worktree_path: str
    branch: str


def create_worktree(
    *,
    repo_path: str,
    branch_name: str,
    base_branch: str | None = None,
) -> CreatedWorktree:
    """Create a git worktree with a new branch checked out.

    Resolves the repo root, picks a collision-free sibling directory,
    and runs ``git worktree add -b`` (fetching once if ``base_branch``
    isn't locally resolvable).

    :param repo_path: Absolute path inside the source repo — the
        directory the user picked, e.g. ``"/Users/alice/myrepo"``.
    :param branch_name: New branch to create and check out, e.g.
        ``"feature/login"``.
    :param base_branch: Optional base ref, e.g. ``"main"``. ``None``
        branches from the repo's current ``HEAD``.
    :returns: The created worktree's path and branch.
    :raises WorktreeError: If the branch name is invalid, the path is
        not a git repo, the base ref can't be resolved, or
        ``git worktree add`` fails (e.g. the branch already exists).
    """
    validate_branch_name(branch_name)
    # Always create the worktree off the MAIN work tree, even when
    # ``repo_path`` is itself a linked worktree (e.g. the fork-resume
    # picker prefilled a worktree as the source). Otherwise the new
    # worktree would nest under the picked worktree
    # (``…/feature-worktrees/<branch>``); resolving to the main repo keeps
    # all worktrees as siblings (``…/myrepo-worktrees/<branch>``).
    repo_root = _main_work_tree(repo_path)
    # Friendly pre-check before git's raw "branch already exists" error.
    # We don't reuse the existing worktree: two sessions sharing one
    # working tree would clobber each other (designs/SESSION_GIT_WORKTREE.md).
    if _local_branch_exists(repo_root, branch_name):
        raise WorktreeError(
            f"a branch named {branch_name!r} already exists; choose a different branch name"
        )
    if base_branch is not None:
        _ensure_base_resolvable(repo_root, base_branch)
    worktree_path = _resolve_worktree_path(repo_root, branch_name)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    add_args = ["worktree", "add", "-b", branch_name, str(worktree_path)]
    if base_branch is not None:
        # --end-of-options: treat base_branch as a rev, never a git flag, so a
        # user-supplied value starting with '-' can't inject an option.
        add_args += ["--end-of-options", base_branch]
    result = _run_git(add_args, cwd=repo_root)
    if result.returncode != 0:
        raise _git_error("git worktree add failed", result)
    return CreatedWorktree(worktree_path=str(worktree_path), branch=branch_name)


def _main_repo_for_worktree(worktree_path: str) -> str:
    """Find the main repository work tree for a linked worktree.

    Uses ``git rev-parse --git-common-dir`` (which points at the
    shared ``.git`` of the main work tree) and returns that directory's
    parent. Run from inside the worktree so the relative result
    resolves correctly.

    :param worktree_path: Absolute path of a linked worktree, e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :returns: Absolute path of the main repo work tree, e.g.
        ``"/Users/alice/myrepo"``.
    :raises WorktreeError: If ``worktree_path`` is missing or not part
        of a git repository.
    """
    if not Path(worktree_path).exists():
        raise WorktreeError(f"worktree path does not exist: {worktree_path}")
    result = _run_git(["rev-parse", "--git-common-dir"], cwd=worktree_path)
    if result.returncode != 0:
        raise WorktreeError(f"not a git worktree: {worktree_path}")
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (Path(worktree_path) / common_dir).resolve()
    return str(common_dir.parent)


def remove_worktree(
    *,
    worktree_path: str,
    branch: str | None = None,
    delete_branch: bool = False,
) -> None:
    """Remove a git worktree and optionally delete its branch.

    Removes the directory with ``--force``, then (if requested) deletes
    the branch — in that order, since git refuses to delete a branch
    still checked out in a linked worktree. ``git worktree remove``
    refuses to remove the main work tree.

    :param worktree_path: Absolute path of the worktree to remove,
        e.g. ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Branch to delete when ``delete_branch`` is
        ``True``, e.g. ``"feature/login"``. ``None`` skips branch
        deletion.
    :param delete_branch: When ``True``, run ``git branch -D`` on
        ``branch`` after removing the worktree directory.
    :raises WorktreeError: If the worktree path is missing/invalid, or
        a git command fails.
    """
    main_repo = _main_repo_for_worktree(worktree_path)
    remove_result = _run_git(
        ["worktree", "remove", "--force", worktree_path],
        cwd=main_repo,
    )
    if remove_result.returncode != 0:
        raise _git_error("git worktree remove failed", remove_result)
    if delete_branch and branch is not None:
        branch_result = _run_git(["branch", "-D", branch], cwd=main_repo)
        if branch_result.returncode != 0:
            raise _git_error("git branch -D failed", branch_result)


@dataclass
class DiffResult:
    """A racer worktree's change set relative to a base ref.

    :param diff: Unified diff text (tracked changes vs the base ref, plus
        the full contents of untracked new files). Empty when nothing
        changed.
    :param truncated: ``True`` when the diff exceeded
        :data:`_MAX_DIFF_BYTES` and was cut off.
    """

    diff: str
    truncated: bool


def git_diff(*, worktree_path: str, base_ref: str) -> DiffResult:
    """Read-only diff of a racer's worktree against a base ref.

    Combines committed *and* uncommitted tracked changes
    (``git diff <base_ref>`` from inside the worktree) with the full
    contents of untracked new files (each rendered as an add via
    ``git diff --no-index``). No index or working-tree mutation, so it is
    safe to run against a worktree an agent is still editing.

    :param worktree_path: Absolute path of the racer's worktree, e.g.
        ``"/Users/alice/myrepo-worktrees/arena-ab12-codex"``.
    :param base_ref: Ref to diff against, e.g. ``"main"`` (the arena's
        base branch) or ``"HEAD"`` (uncommitted changes only).
    :returns: A :class:`DiffResult`.
    :raises WorktreeError: If the path is not a git worktree or git fails.
    """
    if not Path(worktree_path).is_dir():
        raise WorktreeError(f"path is not a directory: {worktree_path}")
    verify = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=worktree_path)
    if verify.returncode != 0:
        raise WorktreeError(f"not a git worktree: {worktree_path}")

    tracked = _run_git(["diff", base_ref], cwd=worktree_path)
    if tracked.returncode != 0:
        raise _git_error("git diff failed", tracked)
    parts = [tracked.stdout]

    others = _run_git(["ls-files", "--others", "--exclude-standard"], cwd=worktree_path)
    if others.returncode == 0:
        untracked = [line for line in others.stdout.splitlines() if line]
        for rel in untracked[:_MAX_UNTRACKED_FILES]:
            # --no-index compares two paths without touching the index; a
            # difference exits 1, which is expected, not an error.
            new_file = _run_git(["diff", "--no-index", "--", "/dev/null", rel], cwd=worktree_path)
            if new_file.stdout:
                parts.append(new_file.stdout)

    text = "".join(parts)
    if len(text.encode("utf-8", "replace")) > _MAX_DIFF_BYTES:
        return DiffResult(
            diff=text.encode("utf-8", "replace")[:_MAX_DIFF_BYTES].decode("utf-8", "ignore"),
            truncated=True,
        )
    return DiffResult(diff=text, truncated=False)


@dataclass
class DiffStat:
    """Line/file counts of a change set, for a compact status readout.

    :param added: Total inserted lines across all changed files.
    :param removed: Total deleted lines across all changed files.
    :param files: Number of changed files. Binary files count toward
        ``files`` but contribute no lines (git reports ``-`` for them).
    """

    added: int
    removed: int
    files: int


def git_ahead_behind(*, worktree_path: str, base_ref: str) -> tuple[int, int]:
    """Count commits ``HEAD`` is ahead of and behind ``base_ref``.

    Uses a single ``git rev-list --left-right --count <base>...HEAD``,
    whose output is ``<behind>\\t<ahead>`` — the left side counts commits
    reachable only from the base, the right side only from ``HEAD``.

    :param worktree_path: Absolute path of the workspace, e.g.
        ``"/Users/alice/myrepo"``.
    :param base_ref: Ref to compare against, e.g. ``"origin/main"``.
    :returns: ``(ahead, behind)`` commit counts.
    :raises WorktreeError: If git fails or ``base_ref`` does not resolve.
    """
    result = _run_git(
        ["rev-list", "--left-right", "--count", "--end-of-options", f"{base_ref}...HEAD"],
        cwd=worktree_path,
        timeout=_GIT_READ_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise _git_error("git rev-list failed", result)
    fields = result.stdout.split()
    if len(fields) != 2:
        raise WorktreeError(f"unexpected git rev-list output: {result.stdout.strip()!r}")
    try:
        behind, ahead = int(fields[0]), int(fields[1])
    except ValueError as exc:
        raise WorktreeError(f"unexpected git rev-list output: {result.stdout.strip()!r}") from exc
    return ahead, behind


def git_diff_stat(*, worktree_path: str, base_ref: str) -> DiffStat:
    """Summarize the workspace's changes against ``base_ref``.

    Runs ``git diff --numstat <base_ref>``, which covers committed *and*
    uncommitted tracked changes. Untracked files are not counted — they
    are not part of the diff git reports.

    :param worktree_path: Absolute path of the workspace.
    :param base_ref: Ref to diff against, e.g. ``"origin/main"`` or
        ``"HEAD"`` for uncommitted changes only.
    :returns: A :class:`DiffStat` with the aggregate counts.
    :raises WorktreeError: If git fails or ``base_ref`` does not resolve.
    """
    result = _run_git(
        ["diff", "--numstat", "--end-of-options", base_ref],
        cwd=worktree_path,
        timeout=_GIT_READ_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise _git_error("git diff --numstat failed", result)
    added = removed = files = 0
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        files += 1
        # Binary files report "-" for both counts; they add no lines.
        if fields[0] != "-":
            added += int(fields[0])
        if fields[1] != "-":
            removed += int(fields[1])
    return DiffStat(added=added, removed=removed, files=files)


def git_upstream_ref(*, worktree_path: str) -> str | None:
    """Return the current branch's upstream ref, or ``None`` if unset.

    :param worktree_path: Absolute path of the workspace.
    :returns: The upstream ref name, e.g. ``"origin/main"``. ``None``
        when the branch tracks nothing or ``HEAD`` is detached.
    :raises WorktreeError: If git is not installed or times out.
    """
    result = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=worktree_path,
        timeout=_GIT_READ_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_remote_slug(*, worktree_path: str, remote: str = "origin") -> str | None:
    """Return the ``owner/name`` GitHub slug of a remote, if it is one.

    Handles both URL forms git writes: ``https://github.com/o/n.git`` and
    ``git@github.com:o/n.git``.

    :param worktree_path: Absolute path of the workspace.
    :param remote: Remote name to read, e.g. ``"origin"``.
    :returns: ``"owner/name"``, or ``None`` when the remote is missing or
        is not hosted on github.com.
    :raises WorktreeError: If git is not installed or times out.
    """
    result = _run_git(
        ["remote", "get-url", "--end-of-options", remote],
        cwd=worktree_path,
        timeout=_GIT_READ_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$", url)
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"


@dataclass
class MergeResult:
    """Outcome of promoting a racer's branch into the arena base branch.

    :param outcome: One of ``"merged"`` (base advanced),
        ``"conflict"`` (merge aborted cleanly, base untouched),
        ``"base_not_checked_out"`` (the main work tree is on another
        branch), or ``"base_dirty"`` (the main work tree has uncommitted
        changes). Every non-``merged`` outcome leaves the repo untouched.
    :param detail: Human-readable extra context, e.g. the merge commit
        subject, the conflicting files, or the blocking branch/state.
    """

    outcome: str
    detail: str | None = None


def merge_worktree(
    *,
    worktree_path: str,
    branch: str,
    base_branch: str,
    commit_message: str,
) -> MergeResult:
    """Commit a racer's work and merge its branch into the base branch.

    Conservative by design — it refuses to touch a main work tree that is
    not sitting cleanly on ``base_branch``, and aborts (never leaves a
    half-merged tree) on conflict:

    1. Stage and commit any uncommitted changes on the racer branch
       (skipped when the worktree is already clean).
    2. Require the main work tree to be checked out on ``base_branch`` and
       have no uncommitted changes — otherwise return without merging.
    3. ``git merge --no-ff`` the racer branch; on conflict
       ``git merge --abort`` and report, leaving the base untouched.

    :param worktree_path: Absolute path of the winning racer's worktree.
    :param branch: The racer's branch, e.g. ``"arena-ab12-codex"``.
    :param base_branch: Branch to merge into, e.g. ``"main"``.
    :param commit_message: Message for the racer's work commit + merge.
    :returns: A :class:`MergeResult` describing what happened.
    :raises WorktreeError: If a path is invalid or a git command errors
        in a way that isn't a normal merge conflict.
    """
    main_repo = _main_repo_for_worktree(worktree_path)

    # 1. Capture the agent's work as a commit on its own branch.
    status = _run_git(["status", "--porcelain"], cwd=worktree_path)
    if status.returncode != 0:
        raise _git_error("git status failed", status)
    if status.stdout.strip():
        add = _run_git(["add", "-A"], cwd=worktree_path)
        if add.returncode != 0:
            raise _git_error("git add failed", add)
        commit = _run_git(["commit", "-m", commit_message], cwd=worktree_path)
        if commit.returncode != 0:
            raise _git_error("git commit failed", commit)

    # 2. The base must be checked out cleanly in the main work tree.
    head = _run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=main_repo)
    current = head.stdout.strip() if head.returncode == 0 else ""
    if current != base_branch:
        return MergeResult(
            outcome="base_not_checked_out",
            detail=(
                f"a cópia principal está em {current or 'HEAD destacado'}, não em {base_branch}"
            ),
        )
    base_status = _run_git(["status", "--porcelain"], cwd=main_repo)
    if base_status.returncode != 0:
        raise _git_error("git status failed", base_status)
    if base_status.stdout.strip():
        return MergeResult(
            outcome="base_dirty",
            detail=f"{base_branch} tem alterações não commitadas na cópia principal",
        )

    # 3. Merge, aborting cleanly on conflict.
    merge = _run_git(["merge", "--no-ff", "-m", commit_message, branch], cwd=main_repo)
    if merge.returncode == 0:
        return MergeResult(outcome="merged", detail=merge.stdout.strip() or None)
    _run_git(["merge", "--abort"], cwd=main_repo)
    return MergeResult(outcome="conflict", detail=(merge.stdout or merge.stderr).strip() or None)


# ── Worktree snapshots (checkpoint / restore safety net) ─────────────

# Snapshots live under a private ref namespace so they never appear as
# branches, never move HEAD, and are cheap (they share objects with the repo).
_SNAPSHOT_REF_PREFIX = "refs/omnicraft/snapshots/"
_SNAPSHOT_ID_RE = re.compile(r"^[0-9A-Za-z._-]+$")
_SNAPSHOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "OmniCraft",
    "GIT_AUTHOR_EMAIL": "snapshots@omnicraft.local",
    "GIT_COMMITTER_NAME": "OmniCraft",
    "GIT_COMMITTER_EMAIL": "snapshots@omnicraft.local",
}


@dataclass
class SnapshotInfo:
    """One saved worktree checkpoint.

    :param id: Ref segment / handle, e.g. ``"1783000000-ab12cd"``. The
        leading number is the creation epoch, so ids sort chronologically.
    :param commit: The snapshot commit sha whose tree is the saved state.
    :param label: The user's note, e.g. ``"before the big refactor"``.
    :param created_at: Creation time, epoch seconds.
    """

    id: str
    commit: str
    label: str
    created_at: int


def _require_worktree(worktree_path: str) -> None:
    """Raise unless ``worktree_path`` is an existing git work tree."""
    if not Path(worktree_path).is_dir():
        raise WorktreeError(f"path is not a directory: {worktree_path}")
    if _run_git(["rev-parse", "--is-inside-work-tree"], cwd=worktree_path).returncode != 0:
        raise WorktreeError(f"not a git worktree: {worktree_path}")


def snapshot_worktree(*, worktree_path: str, label: str = "") -> SnapshotInfo:
    """Capture the FULL current worktree state as a restorable checkpoint.

    Records tracked edits, staged changes, and untracked (non-ignored) files
    into a commit under a private ref — without touching HEAD, the branch, or
    the working index (a scratch ``GIT_INDEX_FILE`` does the staging). The
    commit shares objects with the repo, so a snapshot is cheap.

    :param worktree_path: Absolute path of the worktree to checkpoint.
    :param label: Optional human note stored in the snapshot.
    :returns: The created :class:`SnapshotInfo`.
    :raises WorktreeError: If the path isn't a git worktree or git fails.
    """
    _require_worktree(worktree_path)
    head = _run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd=worktree_path)
    has_head = head.returncode == 0

    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        if has_head:
            seed = _run_git(["read-tree", "HEAD"], cwd=worktree_path, env=env)
            if seed.returncode != 0:
                raise _git_error("git read-tree failed", seed)
        added = _run_git(["add", "-A"], cwd=worktree_path, env=env)
        if added.returncode != 0:
            raise _git_error("git add failed", added)
        written = _run_git(["write-tree"], cwd=worktree_path, env=env)
        if written.returncode != 0:
            raise _git_error("git write-tree failed", written)
        tree = written.stdout.strip()
        commit_args = ["commit-tree", tree, "-m", f"snapshot: {label}" if label else "snapshot"]
        if has_head:
            commit_args += ["-p", head.stdout.strip()]
        committed = _run_git(commit_args, cwd=worktree_path, env={**env, **_SNAPSHOT_IDENTITY})
        if committed.returncode != 0:
            raise _git_error("git commit-tree failed", committed)
        commit = committed.stdout.strip()

    created = int(time.time())
    snap_id = f"{created}-{secrets.token_hex(3)}"
    updated = _run_git(
        ["update-ref", f"{_SNAPSHOT_REF_PREFIX}{snap_id}", commit], cwd=worktree_path
    )
    if updated.returncode != 0:
        raise _git_error("git update-ref failed", updated)
    return SnapshotInfo(id=snap_id, commit=commit, label=label, created_at=created)


def list_snapshots(*, worktree_path: str) -> list[SnapshotInfo]:
    """List a worktree's checkpoints, newest first.

    :param worktree_path: Absolute path of the worktree.
    :returns: The snapshots, most recent first.
    :raises WorktreeError: If the path isn't a git worktree or git fails.
    """
    _require_worktree(worktree_path)
    # A NUL between fields + newline between records survives labels with spaces.
    result = _run_git(
        [
            "for-each-ref",
            "--sort=-refname",
            "--format=%(refname)%00%(objectname)%00%(subject)",
            _SNAPSHOT_REF_PREFIX,
        ],
        cwd=worktree_path,
    )
    if result.returncode != 0:
        raise _git_error("git for-each-ref failed", result)
    snapshots: list[SnapshotInfo] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        refname, commit, subject = line.split("\x00")
        snap_id = refname[len(_SNAPSHOT_REF_PREFIX) :]
        label = subject[len("snapshot: ") :] if subject.startswith("snapshot: ") else ""
        try:
            created = int(snap_id.split("-", 1)[0])
        except ValueError:
            created = 0
        snapshots.append(SnapshotInfo(id=snap_id, commit=commit, label=label, created_at=created))
    return snapshots


@dataclass
class RestoreResult:
    """Outcome of restoring a snapshot.

    :param restored: The snapshot id that was applied.
    :param backup_id: The id of the auto-snapshot taken of the pre-restore
        state (so the restore itself is undoable), or ``None`` if skipped.
    """

    restored: str
    backup_id: str | None = None


def restore_snapshot(
    *, worktree_path: str, snapshot_id: str, auto_backup: bool = True
) -> RestoreResult:
    """Reset the worktree to exactly match a saved checkpoint.

    Snapshot files are rewritten, files created after the snapshot are removed,
    and files deleted since are recreated — an exact match. The current branch
    and HEAD are untouched (the restored state shows as ordinary uncommitted
    changes). By default the pre-restore state is itself snapshotted first, so
    a restore can be undone.

    :param worktree_path: Absolute path of the worktree.
    :param snapshot_id: The snapshot handle from :func:`list_snapshots`.
    :param auto_backup: When ``True``, snapshot the current state first.
    :returns: A :class:`RestoreResult`.
    :raises WorktreeError: If the id is invalid/unknown or git fails.
    """
    _require_worktree(worktree_path)
    if not _SNAPSHOT_ID_RE.match(snapshot_id):
        raise WorktreeError(f"invalid snapshot id: {snapshot_id!r}")
    ref = f"{_SNAPSHOT_REF_PREFIX}{snapshot_id}"
    resolved = _run_git(["rev-parse", "--verify", "-q", ref], cwd=worktree_path)
    if resolved.returncode != 0:
        raise WorktreeError(f"snapshot not found: {snapshot_id}")
    commit = resolved.stdout.strip()

    backup: SnapshotInfo | None = None
    if auto_backup:
        backup = snapshot_worktree(worktree_path=worktree_path, label="auto: antes de restaurar")

    # Files in the snapshot tree.
    listed = _run_git(["ls-tree", "-r", "-z", "--name-only", commit], cwd=worktree_path)
    if listed.returncode != 0:
        raise _git_error("git ls-tree failed", listed)
    snap_files = {f for f in listed.stdout.split("\x00") if f}

    # Files present now (tracked + untracked non-ignored) — anything here but
    # NOT in the snapshot was created after it and must go.
    tracked = _run_git(["ls-files", "-z"], cwd=worktree_path).stdout.split("\x00")
    untracked = _run_git(
        ["ls-files", "-z", "--others", "--exclude-standard"], cwd=worktree_path
    ).stdout.split("\x00")
    current = {f for f in [*tracked, *untracked] if f}
    for rel in current - snap_files:
        target = Path(worktree_path) / rel
        with contextlib.suppress(FileNotFoundError, IsADirectoryError, PermissionError):
            target.unlink()

    # Write every snapshot file back into the worktree (scratch index again).
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        read = _run_git(["read-tree", commit], cwd=worktree_path, env=env)
        if read.returncode != 0:
            raise _git_error("git read-tree failed", read)
        out = _run_git(["checkout-index", "-a", "-f"], cwd=worktree_path, env=env)
        if out.returncode != 0:
            raise _git_error("git checkout-index failed", out)

    # Point the real index back at HEAD so the restored state reads as ordinary
    # unstaged changes (intuitive: "your uncommitted work is back").
    if _run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd=worktree_path).returncode == 0:
        _run_git(["read-tree", "HEAD"], cwd=worktree_path)

    return RestoreResult(restored=snapshot_id, backup_id=backup.id if backup else None)


def delete_snapshot(*, worktree_path: str, snapshot_id: str) -> None:
    """Delete a checkpoint ref (objects are reclaimed by a later ``git gc``).

    :param worktree_path: Absolute path of the worktree.
    :param snapshot_id: The snapshot handle to remove.
    :raises WorktreeError: If the id is invalid or git fails.
    """
    _require_worktree(worktree_path)
    if not _SNAPSHOT_ID_RE.match(snapshot_id):
        raise WorktreeError(f"invalid snapshot id: {snapshot_id!r}")
    deleted = _run_git(
        ["update-ref", "-d", f"{_SNAPSHOT_REF_PREFIX}{snapshot_id}"], cwd=worktree_path
    )
    if deleted.returncode != 0:
        raise _git_error("git update-ref -d failed", deleted)
