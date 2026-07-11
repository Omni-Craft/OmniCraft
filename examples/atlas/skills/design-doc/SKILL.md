---
name: design-doc
description: Turn a feature request into a design document — context, goals and non-goals, proposed approach, alternatives considered, and rollout. Use when the user asks for a design doc, an RFC, an architecture proposal, or "how should we build X". Plan only — never write code.
---

# design-doc — turn a feature request into a design document

Produce a design document that lets a reader agree on the approach BEFORE any
code is written. Plan only; you describe the design, you never implement it.

## 1. Collect context

Establish what is being asked and the ground it stands on. Gather the state
yourself with `sys_os_*` / git — current behavior, related modules, existing
patterns. This is plumbing, not investigation.

## 2. Dispatch the surveyor (purpose: explore / search)

When you need to know how the affected code works or where a change lands, hand
the surveyor the question; it reads source, history, and manifests and returns
file:line evidence. Do NOT sprawl across the repo yourself.

## 3. Write the design document

Use these sections, dropping any that are truly empty:

    # <Feature> — Design Doc

    ## Context
    Why this exists now; the problem and who it affects.

    ## Goals / Non-goals
    What success is; what is explicitly out of scope.

    ## Proposed approach
    The design, grounded in the real code (modules, data flow, interfaces).
    Describe the changes — never patch files.

    ## Alternatives considered
    Other approaches and why they were not chosen.

    ## Rollout
    How this ships: sequencing, flags, migrations, backward-compat, fallback.

## 4. Cross-vendor critique (purpose: review)

Route the draft through the `reviewer` (codex, different vendor) to catch an
unsound approach, a missed alternative, or a rollout hole. Fold in its verdicts.

## 5. Deliver

Present the final design doc. You PLAN; you never write, edit, or patch code.
