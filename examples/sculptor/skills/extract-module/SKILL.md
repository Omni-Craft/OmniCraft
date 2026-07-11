---
name: extract-module
description: Pull code out of one file into a new module and rewire every import without changing behavior. Use when the user asks to split a file, extract a module or package, or move related code into its own unit.
---

# extract-module — extract code into a new module, rewire imports

Move a cohesive slice of code (functions, classes, constants) into a new module
and update every consumer to import it from its new home — with behavior
unchanged.

## 1. Decide the seam

Identify exactly what moves and where it lands: the symbols to extract, the new
module path, and the boundary between what leaves and what stays. A clean seam
minimizes the imports that have to cross it in both directions.

## 2. Map dependencies both ways (dispatch the explorer)

Dispatch the `explorer` (`purpose: explore`) to report, with `file:line`
evidence:
- Every external reference to the symbols being moved (who imports them today).
- What the moved code itself depends on (so the new module's imports are
  complete and you don't create a circular import).
Do not start moving code until you have the full picture.

## 3. Move and rewire in small edits

With your `sys_os_*` tools:
- Create the new module and move the code verbatim — do not "improve" it in the
  same pass; a move that also edits logic is impossible to review.
- Add the imports the moved code needs at its new location.
- Update every consumer the explorer found to import from the new path; drop
  now-unused imports at the old location.
- Preserve the public surface — if other code imported these symbols from the
  old path, keep a re-export there unless the user asked to update all callers.

## 4. Verify (dispatch the verifier)

Route the diff through the `verifier` (`purpose: review`, different vendor) to
confirm no import dangles, no circular import was introduced, and the moved code
behaves identically. Fold in anything BLOCKING before presenting the result.
