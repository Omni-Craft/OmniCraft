---
name: breakdown-tasks
description: Decompose a plan or feature into ordered, independently-shippable tasks with their dependencies. Use when the user asks to break a plan into tasks, split work into PRs, sequence the work, or build an implementation checklist. Plan only — never write code.
---

# breakdown-tasks — decompose a plan into ordered, shippable tasks

Turn a plan or feature into a sequence of tasks small enough to ship on their
own, ordered so dependencies come first. Plan only; you describe the tasks, you
never implement them.

## 1. Establish the plan

Start from an agreed plan or design doc. If none exists, collect scope yourself
with `sys_os_*` / git and load the design-doc skill first — you cannot break
down work that is not yet defined.

## 2. Dispatch the surveyor (purpose: explore / search)

When you need to know where each task lands or what it depends on, hand the
surveyor the question; it returns file:line evidence and existing seams to
follow. Do NOT sprawl across the repo yourself.

## 3. Write the task breakdown

For each task:

    ### Task <n>: <short title>
    - **Deliverable**: <the shippable unit — ideally one PR>
    - **Depends on**: <task numbers, or "none">
    - **Files to touch**: <paths, functions, modules>
    - **Done when**: <the acceptance check — test, behavior, review>

Rules:
- Order tasks so every dependency precedes its dependents; number them in ship
  order.
- Prefer tasks that are independently shippable and independently revertible —
  each should leave the tree working.
- Split anything that touches unrelated areas or would make a reviewer's eyes
  glaze; merge tasks too small to matter.
- Call out tasks that can proceed in parallel (no dependency between them).

## 4. Cross-vendor critique (purpose: review)

Route the breakdown through the `reviewer` (codex, different vendor) to catch a
task that depends on something a later task introduces, or a "shippable" unit
that actually breaks the build. Fold in its verdicts.

## 5. Deliver

Present the ordered task list. You PLAN; you never write, edit, or patch code.
