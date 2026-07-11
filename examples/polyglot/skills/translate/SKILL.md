---
name: translate
description: Translate a set of source strings into a target language while preserving every interpolation placeholder, the source's tone, and glossary terminology. Use when the user asks to translate strings or fill in a locale for a language.
---

# translate — translate source strings into a target language

Turn a work list of source strings into natural, accurate translations for one
target language, then write them into the locale file.

## 1. Assemble the input

Gather the source strings (with keys), the target language, and any glossary or
context — the output of the extract-strings skill. If no glossary exists, note
the domain terms that must stay consistent (product name, feature names, terms
of art) so they translate the same way everywhere.

## 2. Dispatch the translator (purpose: explore / search)

Hand the translator the source strings, the target language, and the
glossary/context. It returns natural translations with every placeholder
preserved and each value attached to its original key. Do NOT translate the
strings yourself — that is the sub-agent's job.

## 3. Preserve placeholders and structure — non-negotiable

Every interpolation token in the source MUST appear unchanged in the
translation: `{count}`, `{{name}}`, `%s`/`%1$s`, `%(user)s`, `:name`, ICU
plurals/selects, and HTML tags. Keep the target file's KEYS and nesting
IDENTICAL to the source — translate only the values, never add/drop/rename a
key. Match the target language's plural categories (some need more than
English's one/other).

## 4. Write the locale file

Fold the translations into the target locale file with your `sys_os_*` tools,
matching its exact format, key order, indentation, and quoting. Edit in place;
do not restructure the file.

## 5. Verify

Route the finished locale through the review-locale skill (the `reviewer`,
`purpose: review`) when it will ship — placeholder integrity, terminology
consistency, and untranslated strings are exactly what a cross-vendor QA catches.
