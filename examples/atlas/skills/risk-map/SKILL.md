---
name: risk-map
description: Enumerate the risks of a proposed change — blast radius, migrations, backward-compat, unknowns — and pair each with a mitigation. Use when the user asks what could go wrong, for a risk assessment, a pre-mortem, or the risks of a plan or migration. Plan only — never write code.
---

# risk-map — enumerate the risks of a proposed change and mitigate them

Map what could go wrong with a proposed change before it ships, and pair every
risk with a concrete mitigation. Plan only; you assess risk, you never implement
the change or its mitigation.

## 1. Frame the change

Establish exactly what is changing — a plan, a diff, a migration. Gather the
surrounding state yourself with `sys_os_*` / git. This is plumbing, not
investigation.

## 2. Dispatch the surveyor (purpose: explore / search)

To size the blast radius, hand the surveyor the question: who calls this, what
depends on the data shape, what tests cover it. It returns file:line evidence.
Do NOT sprawl across the repo yourself.

## 3. Write the risk map

Cover these risk categories, dropping any that genuinely do not apply:
- **Blast radius** — what else touches this code / data; who breaks if it does.
- **Migrations** — schema/data changes, ordering, reversibility, downtime.
- **Backward-compat** — existing callers, on-disk formats, public APIs, configs.
- **Unknowns** — assumptions not yet confirmed; where the plan is guessing.

For each risk:

    ### <Category>: <short title>
    - **Risk**: <what could go wrong and the impact>
    - **Likelihood / Impact**: low | medium | high  /  low | medium | high
    - **Mitigation**: <how to prevent, detect, or recover — described, not applied>

## 4. Cross-vendor critique (purpose: review)

Route the risk map through the `reviewer` (codex, different vendor) to surface a
risk you missed or a mitigation that would not actually hold. Fold in its
verdicts.

## 5. Deliver

Present the risk map, highest likelihood×impact first. You PLAN; you never
write, edit, or patch code.
