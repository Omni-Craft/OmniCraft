---
name: rename-symbol
description: Rename a function, class, method, or variable safely across the whole codebase — find every reference first, update them all, then verify nothing broke. Use when the user asks to rename a symbol, fix a name, or make a name consistent.
---

# rename-symbol — rename a symbol safely across the codebase

Rename a symbol so that every reference moves with it and behavior is unchanged.
A rename is the archetypal refactor: purely structural, and broken the instant a
single callsite is missed.

## 1. Pin the exact target

Establish precisely what is being renamed and to what: the old identifier, the
new one, and the scope (a single module, a class method, a package-wide public
name). Note any same-named-but-unrelated symbols that must NOT be touched.

## 2. Map every reference (dispatch the explorer)

Dispatch the `explorer` (`purpose: explore` or `search`) to find the COMPLETE
set of references — the definition, every callsite, imports, re-exports,
subclasses, plus indirect uses a plain search misses: string literals, dynamic
dispatch, config keys, test fixtures, and docs. Do not start editing until you
have its `file:line` list. A missed reference is a broken rename.

## 3. Apply the rename in small edits

Update every reference the explorer found, with your `sys_os_*` tools:
- Rename the definition first, then each callsite, import, and re-export.
- Watch for collisions and shadowing — the new name must not clash with an
  existing symbol in any scope where the old one was used.
- Change ONLY the name. If you notice a behavior bug along the way, leave it;
  a rename does not change behavior.

## 4. Verify (dispatch the verifier)

Route the diff through the `verifier` (`purpose: review`, different vendor) to
confirm no reference dangles, no signature drifted, and no semantics changed.
Fold in anything BLOCKING before presenting the result.
