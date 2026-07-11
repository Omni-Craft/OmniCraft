---
name: dead-code
description: Identify and remove unused code safely — prove a symbol or file is unreferenced before deleting it. Use when the user asks to remove dead code, delete unused functions, or clean up after a feature was removed.
---

# dead-code — prove code is unused, then remove it

Remove code that nothing references. The risk is not the deletion — it is being
wrong about "nothing references it." Prove the target is dead before you delete.

## 1. Name the suspects

List the specific symbols or files suspected to be dead (a function, a class, a
whole module, a config flag). Removing dead code is one kind of change — don't
also rename or restructure what stays in the same pass.

## 2. Prove it is unreferenced (dispatch the explorer)

Dispatch the `explorer` (`purpose: explore` or `search`) to hunt for ANY
reference to each suspect and report `file:line` for what it finds — or state
that it found none. Insist it check the paths a grep misses:
- Dynamic usage: reflection, string-based dispatch, plugin/entry-point registries.
- Public API surface: an exported symbol may have no in-repo caller yet still be
  part of a published contract — that is NOT dead code.
- Tests, docs, config, and build files that name the symbol.

A suspect is safe to remove ONLY when the explorer confirms zero live
references. If usage is dynamic or the symbol is public, treat it as live unless
the user explicitly confirms otherwise.

## 3. Remove in small edits

With your `sys_os_*` tools, delete each proven-dead symbol and its now-orphaned
imports, fixtures, and docs. Keep deletions grouped and reviewable; do not
delete anything the explorer could not clear.

## 4. Verify (dispatch the verifier)

Route the diff through the `verifier` (`purpose: review`, different vendor) to
confirm nothing you removed is still referenced and no behavior was lost. Fold
in anything BLOCKING before presenting the result.
