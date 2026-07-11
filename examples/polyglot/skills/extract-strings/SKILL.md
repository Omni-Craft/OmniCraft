---
name: extract-strings
description: Locate the i18n resource files and identify which translatable strings or keys are missing or stale in each target locale. Use when the user asks what needs translating, to find untranslated strings, or to set up a new target language.
---

# extract-strings — find what needs translating

Establish the source of truth and the gap against each target locale before any
translation happens.

## 1. Find the resource files

Locate the locale directory and the file format yourself with `sys_os_shell`
(`rg`, `find`). Common layouts:
- One file per language: `locales/en.json`, `locales/pt-BR.json`, `de.yaml`.
- Framework conventions: `messages/<lang>.po`/`.pot`, Flutter `.arb`
  (`app_en.arb`), iOS `.strings`, Java `.properties`.
- Namespaced trees: `locales/<lang>/<namespace>.json`.

Identify the SOURCE language (usually `en`) — its file is the source of truth
for keys and structure.

## 2. Diff each target against the source

For each target locale, find the keys present in the source but missing (or
empty) in the target, and keys that are stale (present in the target but the
source value changed since they were last translated). Compare the key sets;
`git log`/`git diff` on the source file shows which source values changed
recently and therefore need re-translation.

For raw UI strings not yet in a resource file, `rg` for hardcoded literals in
the components (JSX text, `t("...")` / `gettext("...")` calls) and list the
strings that have no key yet.

## 3. Capture context per string

For each string to translate, note the data a translator needs: its key, the
source value, its interpolation placeholders, and — where the value alone is
ambiguous (a lone "Post", "Order", "Share") — a one-line note on how it is used,
read from the component.

## 4. Report the work list

Produce, per target language: the keys to translate, the keys to re-translate
(stale), and any placeholders/context notes. This is the input you hand the
translator.
