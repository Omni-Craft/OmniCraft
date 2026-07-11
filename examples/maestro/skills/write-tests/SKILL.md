---
name: write-tests
description: Turn a change, function, or module into a focused test suite — one behavior per test, clear arrange/act/assert, edge cases covered. Use when the user asks for tests, unit tests, or coverage for new or changed code.
---

# write-tests — turn a change into a focused test suite

Turn a change or a function into tests that actually pin its behavior and would
fail if it regressed.

## 1. Understand what to test

Establish the target: a diff, a function, a module. Collect it yourself with
`sys_os_shell` (`git diff`, `git show`) and read the code under test. Find a
sibling test file and match the repo's framework, layout, and naming — do not
invent a new convention.

## 2. Enumerate behaviors before writing

List the behaviors the code promises before you write anything:
- The happy path — the main contract, with realistic inputs.
- The edges — boundaries (0, 1, max), empty and null inputs, duplicates.
- The error paths — what should raise, reject, or return an error, and how.

## 3. Write one behavior per test

- One behavior per test, with a name that states it (`test_rejects_empty_token`).
- Clear arrange / act / assert: set up the input, call the code once, assert the
  outcome.
- Assert the actual behavior, never a tautology — no `assert True`, no
  re-asserting a literal you just set, no mocking the thing under test so the
  test can't fail.
- Prefer real inputs over mocks; mock only true external boundaries (network,
  clock, filesystem you don't own).

## 4. Run them (dispatch the runner)

Hand the runner the test command (`purpose: explore` / `search`). It runs the
suite and returns pass/fail evidence. A new test that passes on the first run
without ever having been seen to fail is suspect — confirm it fails against
broken behavior when it matters.

## 5. Optional design review

When the suite will be trusted as a regression net, route it through the
`reviewer` (`purpose: review`, codex, different vendor) to catch weak or
tautological assertions before finalizing.
