"""Tests for the inner sandbox launcher."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import subprocess
import sys

from omnicraft.inner.sandbox import (
    SandboxPolicy,
    _adopt_or_mint_scratch,
    _is_own_scratch_tmpdir,
    cleanup_private_tmpdir,
    create_exec_launcher,
    create_private_tmpdir,
    run_launcher,
    with_additional_write_roots,
)
from omnicraft.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR


def _noop_policy() -> SandboxPolicy:
    """A ``backend_type="none"`` / ``active=False`` policy.

    Skips kernel-level activation so tests don't need bwrap.

    :returns: An inactive :class:`SandboxPolicy`.
    """
    return SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def _noop_policy_arg() -> str:
    """The no-op policy in ``run_launcher``'s wire format
    (base64-encoded JSON, matching ``_encode_json_arg``).
    """
    raw = json.dumps(_noop_policy().to_jsonable(), separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii")


def test_run_launcher_emits_logger_checkpoints(caplog) -> None:
    """
    ``run_launcher`` logs three INFO records around the
    sandbox-activation and target-spawn steps. Dropping any of them
    erases the diagnostic signal the claude-sdk leg relies on to
    pinpoint a silent connect hang.
    """
    with caplog.at_level(logging.INFO, logger="omnicraft.inner.sandbox"):
        rc = run_launcher(_noop_policy_arg(), sys.executable, ["-c", "pass"])
    assert rc == 0

    messages = [record.getMessage() for record in caplog.records]
    # Absent: wrapper never entered the body.
    assert any(
        "[omnicraft-sandbox] activating backend=none active=False" in m for m in messages
    ), messages
    # Absent on an active policy: activation hung.
    assert any("[omnicraft-sandbox] activated; spawning target=" in m for m in messages), messages
    # Absent: spawned target hung (the claude-sdk symptom).
    assert any("[omnicraft-sandbox] target exited rc=0" in m for m in messages), messages


def test_run_launcher_strips_runner_binding_token_from_target_env(monkeypatch) -> None:
    """The runner tunnel binding token never reaches the spawned target.

    The launcher inherited the runner's full environment and
    ``subprocess.run`` passed it straight to the sandboxed target, so
    agent code could read the runner's control-plane auth secret and
    impersonate the runner. The probe target exits 0 only when the
    token is ABSENT from its inherited ``os.environ``; a non-zero rc
    means the leak is back.

    Spawns a real subprocess (no mocking of ``subprocess.run``) so the
    assertion covers the actual env inheritance the vulnerability used.

    :param monkeypatch: Pytest monkeypatch fixture used to seed the
        binding token into the launcher process's environment.
    """
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "bug-binding-token-secret")
    probe = (
        "import os, sys; "
        f"sys.exit(3 if {RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR!r} in os.environ else 0)"
    )

    rc = run_launcher(_noop_policy_arg(), sys.executable, ["-c", probe])

    assert rc == 0, "binding token leaked into the sandboxed target environment"
    # The launcher also drops it from its own process env so any later
    # inheritor (bwrap re-exec) is covered too.
    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in os.environ


def test_run_launcher_propagates_target_returncode(caplog) -> None:
    """``run_launcher`` returns the spawned target's exit code verbatim."""
    with caplog.at_level(logging.INFO, logger="omnicraft.inner.sandbox"):
        rc = run_launcher(
            _noop_policy_arg(),
            sys.executable,
            ["-c", "raise SystemExit(7)"],
        )
    assert rc == 7
    messages = [record.getMessage() for record in caplog.records]
    assert any("[omnicraft-sandbox] target exited rc=7" in m for m in messages), messages


def test_exec_launcher_wrapper_subprocess_emits_markers_to_stderr() -> None:
    """
    The generated wrapper script must configure logging so
    ``run_launcher``'s INFO records reach stderr when invoked as a
    subprocess. ``caplog`` alone can't catch a regression in the
    ``basicConfig`` line of the generated script.
    """
    wrapper_path = create_exec_launcher(sys.executable, _noop_policy())
    try:
        result = subprocess.run(
            [wrapper_path, "-c", "pass"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(wrapper_path)

    assert result.returncode == 0, result
    assert "[omnicraft-sandbox] activating backend=none active=False" in result.stderr, (
        result.stderr
    )
    assert "[omnicraft-sandbox] activated; spawning target=" in result.stderr, result.stderr
    assert "[omnicraft-sandbox] target exited rc=0" in result.stderr, result.stderr


def test_run_launcher_wraps_target_with_strace_when_env_set(monkeypatch, caplog) -> None:
    """
    ``OMNICRAFT_SANDBOX_STRACE=1`` must prepend ``strace -f -y -e
    trace=file`` to the spawned target's argv so the wrapper's stderr
    captures file-syscall denials (EACCES) from the sandbox.
    """
    from omnicraft.inner import sandbox as sb

    captured: list[list[str]] = []

    class _FakeCompleted:
        returncode = 0

    def _fake_run(argv, **kwargs):
        captured.append(list(argv))
        return _FakeCompleted()

    monkeypatch.setattr(sb.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        sb.shutil,
        "which",
        lambda name: "/usr/bin/strace" if name == "strace" else None,
    )
    monkeypatch.setenv("OMNICRAFT_SANDBOX_STRACE", "1")

    with caplog.at_level(logging.WARNING, logger="omnicraft.inner.sandbox"):
        rc = sb.run_launcher(_noop_policy_arg(), "/bin/echo", ["hi"])

    assert rc == 0
    assert captured == [
        ["/usr/bin/strace", "-f", "-y", "-e", "trace=file", "--", "/bin/echo", "hi"]
    ], captured
    # WARNING emitted so CI logs unambiguously confirm strace was active.
    assert any("strace active" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_run_launcher_skips_strace_when_binary_missing(monkeypatch, caplog) -> None:
    """If ``OMNICRAFT_SANDBOX_STRACE`` is set but strace is not on
    PATH, the wrapper must log a warning and run the target unwrapped
    rather than failing the spawn.
    """
    from omnicraft.inner import sandbox as sb

    captured: list[list[str]] = []

    class _FakeCompleted:
        returncode = 0

    def _fake_run(argv, **kwargs):
        captured.append(list(argv))
        return _FakeCompleted()

    monkeypatch.setattr(sb.subprocess, "run", _fake_run)
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    monkeypatch.setenv("OMNICRAFT_SANDBOX_STRACE", "1")

    with caplog.at_level(logging.WARNING, logger="omnicraft.inner.sandbox"):
        rc = sb.run_launcher(_noop_policy_arg(), "/bin/echo", ["hi"])

    assert rc == 0
    # Target ran unwrapped (no strace prefix), proving the missing-binary
    # path falls back gracefully instead of erroring or hanging.
    assert captured == [["/bin/echo", "hi"]], captured
    assert any("strace is not on PATH" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


# ---------------------------------------------------------------------------
# spawn_env_allowlist tests
# ---------------------------------------------------------------------------


def test_sandbox_policy_round_trips_spawn_env_allowlist() -> None:
    """``spawn_env_allowlist`` survives the launcher wire encoding.

    The policy crosses the parent/launcher boundary as JSON baked into
    the generated wrapper script; a field missing from ``to_jsonable``
    or ``from_jsonable`` silently decodes as ``None`` and the launcher
    prune becomes a no-op — the sandbox would quietly lose its env
    containment layer.
    """
    policy = _noop_policy()
    policy.spawn_env_allowlist = ["HOME", "PATH", "PI_CODING_AGENT_DIR"]

    decoded = SandboxPolicy.from_jsonable(policy.to_jsonable())

    assert decoded.spawn_env_allowlist == ["HOME", "PATH", "PI_CODING_AGENT_DIR"]
    # Old payloads (no key) and unset policies must decode to None —
    # "no prune", not "prune everything".
    assert SandboxPolicy.from_jsonable(_noop_policy().to_jsonable()).spawn_env_allowlist is None


def test_sandbox_policy_round_trips_deny_unix_socket_paths() -> None:
    """``deny_unix_socket_paths`` survives the launcher wire encoding.

    The seatbelt backend reads this list from the *decoded* policy to
    emit its ``(deny network-outbound (remote unix-socket ...))`` rules.
    If the field dropped out of ``to_jsonable`` / ``from_jsonable`` it
    would decode as ``None``, the deny rule would never be emitted, and
    the sandboxed pane could reach the unsandboxed tmux control socket.
    """
    from pathlib import Path

    policy = _noop_policy()
    policy.deny_unix_socket_paths = [Path("/tmp/inst/tmux.sock")]

    decoded = SandboxPolicy.from_jsonable(policy.to_jsonable())

    assert decoded.deny_unix_socket_paths == [Path("/tmp/inst/tmux.sock")]
    # Old payloads (no key) and unset policies decode to None — "no
    # deny", not an empty-but-present list.
    assert SandboxPolicy.from_jsonable(_noop_policy().to_jsonable()).deny_unix_socket_paths is None


def test_with_denied_unix_sockets_resolves_dedupes_and_is_pure() -> None:
    """``with_denied_unix_sockets`` resolves + de-duplicates the socket
    paths and never mutates the input policy.

    The terminal calls this once per instance with the single tmux
    socket, but the helper must be safe to chain (it sits next to the
    other ``with_*`` policy builders) — so we assert it returns a fresh
    policy and leaves the source untouched, and that a repeated path
    collapses to one entry (a duplicate /dev/null mask is harmless but
    a duplicate seatbelt deny line is noise).
    """
    from pathlib import Path

    from omnicraft.inner.sandbox import with_denied_unix_sockets

    policy = _noop_policy()
    sock = Path("/tmp/inst/tmux.sock")

    augmented = with_denied_unix_sockets(policy, [sock, sock])

    assert augmented.deny_unix_socket_paths == [sock.resolve(strict=False)]
    # Source policy is not mutated — builders are chained off a shared
    # base policy.
    assert policy.deny_unix_socket_paths is None
    assert augmented is not policy


def test_with_spawn_env_allowlist_sets_sorted_deduped_copy() -> None:
    """``with_spawn_env_allowlist`` normalizes names and never mutates
    the input policy; ``None`` means "didn't opt in" and returns the
    policy unchanged rather than attaching an empty allowlist (which
    would prune EVERYTHING in the launcher).
    """
    from omnicraft.inner.sandbox import with_spawn_env_allowlist

    policy = _noop_policy()

    untouched = with_spawn_env_allowlist(policy, None)
    # Same object back: None must not be coerced into a prune-all list.
    assert untouched is policy
    assert policy.spawn_env_allowlist is None

    augmented = with_spawn_env_allowlist(policy, ["PATH", "HOME", "PATH"])
    assert augmented.spawn_env_allowlist == ["HOME", "PATH"]
    # The source policy is not mutated — executors reuse it across
    # with_additional_* chains.
    assert policy.spawn_env_allowlist is None


def test_exec_launcher_prunes_inherited_env_to_spawn_allowlist(tmp_path) -> None:
    """The launcher chain drops env vars outside ``spawn_env_allowlist``
    before the target runs (defense in depth).

    Simulates the regression the field guards against: the launcher
    subprocess is spawned with the FULL host environment (plus a seeded
    ``FAKE_HOST_SECRET``). The real generated wrapper script must hand
    the target only the allowlisted names — if the prune in
    ``run_launcher`` is dropped, the secret shows up in the target's
    dumped environment and this test fails.
    """
    policy = _noop_policy()
    policy.spawn_env_allowlist = ["HOME", "PATH", "KEEP_ME"]
    out_file = tmp_path / "child_env.json"

    wrapper_path = create_exec_launcher(sys.executable, policy)
    dump_code = "import json,os,sys; open(sys.argv[1],'w').write(json.dumps(dict(os.environ)))"
    try:
        result = subprocess.run(
            [wrapper_path, "-c", dump_code, str(out_file)],
            capture_output=True,
            text=True,
            timeout=30,
            # The full-inheritance regression: everything the test
            # process has, plus a recognizable secret.
            env={**os.environ, "FAKE_HOST_SECRET": "PWNED", "KEEP_ME": "yes"},
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(wrapper_path)

    assert result.returncode == 0, result
    child_env = json.loads(out_file.read_text())
    # The seeded secret was pruned — the launcher enforced containment
    # even though its own environment carried the full host env.
    assert "FAKE_HOST_SECRET" not in child_env, sorted(child_env)
    # Allowlisted names still pass with their values intact (an empty
    # child env would also satisfy the absence check above).
    assert child_env.get("KEEP_ME") == "yes"
    assert child_env.get("PATH") == os.environ["PATH"]


def test_exec_launcher_without_allowlist_keeps_inherited_env(tmp_path) -> None:
    """A policy with ``spawn_env_allowlist=None`` (spawner didn't opt
    in) must NOT prune — existing launcher users (claude-sdk, terminal)
    deliberately pass rich environments at spawn time, and a prune-by-
    default would strip them and break those harnesses.
    """
    out_file = tmp_path / "child_env.json"

    wrapper_path = create_exec_launcher(sys.executable, _noop_policy())
    dump_code = "import json,os,sys; open(sys.argv[1],'w').write(json.dumps(dict(os.environ)))"
    try:
        result = subprocess.run(
            [wrapper_path, "-c", dump_code, str(out_file)],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "DELIBERATE_EXTRA": "kept"},
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(wrapper_path)

    assert result.returncode == 0, result
    child_env = json.loads(out_file.read_text())
    # Opt-in contract: no allowlist, no prune.
    assert child_env.get("DELIBERATE_EXTRA") == "kept"


# ---------------------------------------------------------------------------
# Private scratch tmpdir: ownership validation + adopt-vs-mint safety.
# Regression for the marker-driven arbitrary-path rmtree (#2759 round 2).
# ---------------------------------------------------------------------------


def _active_policy(backend_type: str) -> SandboxPolicy:
    """An active policy on *backend_type* (kernel activation not exercised)."""
    return SandboxPolicy(
        backend_type=backend_type,
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def test_is_own_scratch_tmpdir_accepts_freshly_minted() -> None:
    d = create_private_tmpdir()
    try:
        assert _is_own_scratch_tmpdir(d) is True
    finally:
        cleanup_private_tmpdir(d)


def test_is_own_scratch_tmpdir_rejects_wrong_prefix(tmp_path) -> None:
    d = tmp_path / "not-osenv"
    d.mkdir(mode=0o700)
    assert _is_own_scratch_tmpdir(d) is False


def test_is_own_scratch_tmpdir_rejects_wrong_mode() -> None:
    d = create_private_tmpdir()
    try:
        os.chmod(d, 0o755)
        assert _is_own_scratch_tmpdir(d) is False
    finally:
        cleanup_private_tmpdir(d)


def test_is_own_scratch_tmpdir_rejects_symlink(tmp_path) -> None:
    real = create_private_tmpdir()
    try:
        # A symlink whose NAME carries the prefix but whose lstat is a
        # symlink, not a dir, must be refused (lstat, not stat).
        link = tmp_path / "omnicraft-osenv-link"
        link.symlink_to(real)
        assert _is_own_scratch_tmpdir(link) is False
    finally:
        cleanup_private_tmpdir(real)


def test_is_own_scratch_tmpdir_rejects_missing(tmp_path) -> None:
    assert _is_own_scratch_tmpdir(tmp_path / "omnicraft-osenv-gone") is False


# ---------------------------------------------------------------------------
# adopt-vs-mint: scratch identity comes from the host-controlled inline argv
# (run_launcher's ``adopted_scratch``), never the environment. Regression for
# the marker-driven arbitrary-path rmtree (#2759 rounds 2-4).
# ---------------------------------------------------------------------------


def test_adopt_or_mint_none_mints() -> None:
    """No handoff (single-pass backend, or the host pass before the
    re-exec): ``adopted_scratch=None`` → mint a fresh dir."""
    tmpdir, minted = _adopt_or_mint_scratch(_active_policy("windows_jobobject"), None)
    try:
        assert minted is True
        assert _is_own_scratch_tmpdir(tmpdir)
    finally:
        cleanup_private_tmpdir(tmpdir)


def test_adopt_or_mint_adopts_host_delivered_argv_scratch() -> None:
    """Happy path: the host names its minted scratch over argv AND grants
    it in the policy → adopt it verbatim (``minted=False``)."""
    owned = create_private_tmpdir()
    try:
        policy = with_additional_write_roots(_active_policy("darwin_seatbelt"), [owned])
        tmpdir, minted = _adopt_or_mint_scratch(policy, str(owned))
        assert minted is False
        assert tmpdir == owned
    finally:
        cleanup_private_tmpdir(owned)


def test_adopt_or_mint_adopts_only_argv_scratch_not_spec_writeroot() -> None:
    """
    The root-cause vector: the spec grants ANOTHER valid ``omnicraft-osenv-*``
    dir as a write root, but the argv identity names ``ours``. The in-wrap
    must adopt ONLY the argv ``adopted_scratch`` — never the spec's
    scratch-shaped write root.
    """
    spec_other = create_private_tmpdir()  # spec-granted, valid scratch shape
    (spec_other / "in-use.txt").write_text("another run's data")
    ours = create_private_tmpdir()
    try:
        # Both are write roots of this policy; only ``ours`` is the argv id.
        policy = with_additional_write_roots(_active_policy("darwin_seatbelt"), [spec_other, ours])
        tmpdir, minted = _adopt_or_mint_scratch(policy, str(ours))
        assert minted is False
        assert tmpdir == ours, "must adopt the argv-named dir, not the spec write root"
        assert tmpdir != spec_other
    finally:
        cleanup_private_tmpdir(spec_other)
        cleanup_private_tmpdir(ours)


def test_adopt_or_mint_none_never_adopts_spec_scratch_writeroot() -> None:
    """
    With no argv handoff, a scratch-shaped spec write root must NOT be
    adopted (the old env marker could point at it; now nothing can). Mint
    fresh; the spec dir is never returned and so never rmtree'd.
    """
    spec_other = create_private_tmpdir()
    (spec_other / "in-use.txt").write_text("precious")
    try:
        policy = with_additional_write_roots(_active_policy("darwin_seatbelt"), [spec_other])
        tmpdir, minted = _adopt_or_mint_scratch(policy, None)
        try:
            assert minted is True
            assert tmpdir != spec_other
            cleanup_private_tmpdir(tmpdir)  # simulate exit cleanup on the returned dir
            assert spec_other.exists() and (spec_other / "in-use.txt").exists(), (
                "a spec-granted scratch-shaped dir must never be rmtree'd"
            )
        finally:
            cleanup_private_tmpdir(tmpdir)
    finally:
        cleanup_private_tmpdir(spec_other)


def test_adopt_or_mint_argv_scratch_not_in_writeroots_refused() -> None:
    """
    Defense in depth: an ``adopted_scratch`` that is a valid owned scratch
    but is NOT granted by this policy (a host-side inconsistency) is
    refused by the write-root consistency check → mint fresh.
    """
    stray = create_private_tmpdir()  # valid shape, but not in the policy
    minted_dir = None
    try:
        policy = _active_policy("darwin_seatbelt")  # empty write_roots
        minted_dir, minted = _adopt_or_mint_scratch(policy, str(stray))
        assert minted is True
        assert minted_dir != stray
    finally:
        cleanup_private_tmpdir(stray)
        cleanup_private_tmpdir(minted_dir)


def test_adopt_or_mint_argv_scratch_bad_ownership_refused(tmp_path) -> None:
    """
    An ``adopted_scratch`` that is granted by the policy but fails the
    ownership shape (wrong prefix / mode) is refused → mint fresh, never
    rmtree the non-scratch path.
    """
    external = tmp_path / "some-shared-dir"  # not omnicraft-osenv-, mode 0755
    external.mkdir(mode=0o755)
    (external / "keep.txt").write_text("precious")
    minted_dir = None
    try:
        policy = with_additional_write_roots(_active_policy("darwin_seatbelt"), [external])
        minted_dir, minted = _adopt_or_mint_scratch(policy, str(external))
        assert minted is True
        assert minted_dir != external
        cleanup_private_tmpdir(minted_dir)
        minted_dir = None
        assert external.exists() and (external / "keep.txt").exists()
    finally:
        cleanup_private_tmpdir(minted_dir)
        cleanup_private_tmpdir(external)
