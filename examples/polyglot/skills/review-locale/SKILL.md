---
name: review-locale
description: QA a translated locale file against the source for key parity, placeholder integrity, terminology consistency, and fluency. Use when the user asks to review, check, or QA a translation before it ships. Report only — never edit.
---

# review-locale — QA a translated locale against the source

## 1. Collect the pair

Identify the translated locale and its source file. Gather both yourself with
sys_os_* / git — this is plumbing, not investigation.

## 2. Dispatch the reviewer (purpose: review)

Hand the reviewer the translated locale and the source strings; it reads both
(and the code that renders a string when meaning is in doubt) and returns
per-key findings. The reviewer is a DIFFERENT vendor than the translator, so it
catches problems the author's own model would wave through. Do NOT sprawl across
the repo yourself.

## 3. What the review must cover

- **Placeholder integrity** — every `{count}`, `{{name}}`, `%s`/`%1$s`,
  `:name`, ICU plural/select, and HTML tag from the source is present and
  unchanged. Broken/dropped placeholder → BLOCKING.
- **Key parity** — exactly the source's keys and structure: none missing, extra,
  or renamed.
- **Meaning** — the translation says what the source says; a mistranslation →
  BLOCKING.
- **Terminology** — glossary/domain terms translated correctly and consistently.
- **Untranslated / overflow** — nothing left in the source language, nothing so
  much longer than the source that it overflows its UI.

## 4. Findings template

    ### <BLOCKING | NIT>: <key>
    - **Problem**: <what is wrong>
    - **Source**: <source value>
    - **Translation**: <translated value>
    - **Fix**: <corrected translation or guidance>

## 5. Deliver

Fold the reviewer's verdicts into the fix list and hand it back. You REPORT; you
never edit the locale — the orchestrator applies the corrections.
