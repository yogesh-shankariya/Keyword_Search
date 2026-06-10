# Stemming and Keyword Matching Flow

This document explains how `whoosh_encounter_json_keyword_search.py` matches OCR text against keyword JSON when stemming is enabled.

## Current Defaults

The script defaults are configured near the top of `whoosh_encounter_json_keyword_search.py`:

```python
CONFIG_STEM_WORDS = True
CONFIG_EDIT_DISTANCE = 1
CONFIG_SLOP = 5
CONFIG_MIN_FUZZY_TERM_LENGTH = 5
```

Meaning:

- Stemming is enabled.
- Fuzzy matching allows edit distance `1` for longer terms.
- Multi-word keyword terms must appear near each other within slop `5`.
- Short analyzed terms shorter than `5` characters use exact matching only.

## Does Stemming Apply To OCR, Keyword, Or Both?

Stemming applies to both.

Whoosh uses the same analyzer for:

1. OCR text when pages are indexed.
2. Keyword text when the query is built.

So if `CONFIG_STEM_WORDS = True`, both sides go through `StemmingAnalyzer()`.

This is important because Whoosh does not compare the original raw words directly. It compares the analyzed terms produced by the analyzer.

## Step-By-Step Flow

### 1. OCR Text Is Indexed

Example OCR text:

```text
The nurse prioritized discharge planning.
```

With `StemmingAnalyzer()`, Whoosh tokenizes, lowercases, removes stop words, and stems the text.

Conceptually:

```text
nurse        -> nurs
prioritized  -> prioritiz
discharge    -> discharg
planning     -> plan
```

These stemmed terms are what get stored in the Whoosh index.

### 2. Keyword JSON Is Flattened

Example keyword JSON:

```json
{
  "test.Priority": ["Priority"]
}
```

The script converts it into a record:

```json
{"group": "test.Priority", "variant": "Priority"}
```

The output still reports this original `variant`. Stemming does not change the JSON output value.

### 3. Keyword Text Is Analyzed

Keyword:

```text
Priority
```

With `StemmingAnalyzer()`, the keyword becomes:

```text
Priority -> prioriti
```

So the query is built from `prioriti`, not raw `Priority`.

### 4. Short Terms Use Exact Matching

Before fuzzy expansion, the script checks the analyzed term length:

```python
CONFIG_MIN_FUZZY_TERM_LENGTH = 5
```

If the analyzed term is shorter than `5`, it uses exact matching only.

Example:

```text
STAT -> stat
```

`stat` has length `4`, so it must match exactly.

This prevents false positives like:

```text
STAT matching start
STAT matching status
```

### 5. Longer Terms Use Fuzzy Expansion

For analyzed terms with length `5` or more, the script uses Whoosh `terms_within(...)` with:

```python
CONFIG_EDIT_DISTANCE = 1
```

Example:

```text
Priority -> prioriti
```

The index may contain related stems:

```text
prioriti   # priority, priorities
priorit    # prioritize
prioritiz  # prioritized, prioritizing
```

With edit distance `1`, `prioriti` can expand to nearby indexed stems like:

```text
prioriti
priorit
prioritiz
```

That is why one keyword, `Priority`, can match OCR words like:

```text
priority
priorities
prioritize
prioritized
prioritizing
```

### 6. Single-Word Keywords Match Directly

For a single analyzed keyword term, the script searches for that one exact or fuzzy-expanded term.

Example:

```text
Keyword: Priority
Analyzed term: prioriti
Expanded terms: prioriti, priorit, prioritiz
```

Any page containing one of those expanded terms is considered a match.

### 7. Multi-Word Keywords Use SpanNear2

For multi-word keywords, each analyzed term is prepared first:

1. Short terms become exact `Term` queries.
2. Longer terms become fuzzy-expanded term groups.

Then `SpanNear2` checks that all terms appear near each other:

```python
slop = 5
ordered = False
```

Example keyword:

```text
High Priority
```

Conceptually:

```text
High     -> high
Priority -> prioriti
```

Then the query requires both terms to appear close together, in any order, within slop `5`.

## Example: Priority

Keyword JSON:

```json
{
  "test.Priority": ["Priority"]
}
```

OCR text examples:

```text
priority
priorities
prioritize
prioritized
prioritizing
```

With stemming and edit distance `1`, all of these can match the keyword `Priority`.

The output still reports:

```json
{
  "group": "test.Priority",
  "variant": "Priority"
}
```

## Example: STAT

Keyword JSON:

```json
{
  "test.STAT": ["STAT"]
}
```

OCR text:

```text
stat
```

This matches because `STAT -> stat`, and it is an exact token match.

OCR text:

```text
start
status
```

These do not match because `stat` is shorter than `CONFIG_MIN_FUZZY_TERM_LENGTH`, so fuzzy matching is not used for it.

## Summary

Stemming is applied to both OCR text and keyword text. The script does not stem only OCR or only keywords.

The current matching pipeline is:

```text
OCR text -> Whoosh analyzer/stemming -> index terms
Keyword JSON variant -> Whoosh analyzer/stemming -> query terms
Short query terms -> exact match
Long query terms -> fuzzy expansion with edit distance 1
Multi-word query -> SpanNear2 proximity check with slop 5
Matched page -> output original group and variant
```
