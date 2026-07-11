---
name: repro-bug
description: Turn a bug report into a minimal failing test that reproduces it, then hand it to the runner to confirm the failure. Use when the user reports a bug, a regression, or "this doesn't work" and wants it captured as a test.
---

# repro-bug — turn a bug report into a minimal failing test

Turn a bug report into the smallest test that fails because of the bug, so the
fix has a target and the regression is caught forever after.

## 1. Pin down the bug

Extract the exact conditions from the report: the input, the steps, the
observed wrong behavior, and the expected behavior. If the report is vague,
gather what you can yourself (`git log`, the code path) — but do not guess the
inputs; a repro built on the wrong inputs proves nothing.

## 2. Write the minimal failing test

- Reduce to the smallest arrange/act/assert that triggers the bug — one input,
  one call, one assertion on the expected (correct) behavior.
- Assert what SHOULD happen, so the test fails now and passes once the bug is
  fixed. Do not assert the buggy behavior.
- Match the repo's test framework and put it next to the related tests.

## 3. Confirm it fails for the right reason (dispatch the runner)

Hand the runner the new test (`purpose: explore` / `search`). It runs the test
and returns the output. Confirm it fails, and that the failure is the reported
bug — the expected assertion not met — not a typo, import error, or unrelated
crash. A repro that fails for the wrong reason is not a repro.

## 4. Deliver

Report the failing test and the runner's captured output as the reproduction.
You author the test and prove the failure; you do not fix the product source —
the failing test is the handoff to whoever fixes it.
