# WHOOSH Parameter Tuning Guide

This document explains the parameters used in the WHOOSH tuning run in simple business terms.

The current tuning grid is configured in `parameter_tuning.yaml`:

```yaml
parameters:
  slop: [1, 2, 3, 4, 5]
  edit_distance: [1, 2]
  min_fuzzy_term_length: [1, 2, 3, 4, 5, 6, 7, 8, 9]
  keep_stopwords: [false, true]
  stem_words: true
  prefixlength: 0
```

This creates 180 WHOOSH runs:

```text
5 slop values x 2 edit-distance values x 9 minimum fuzzy length values x 2 stop-word settings = 180 runs
```

Each run uses a different matching strictness. The final report compares those runs against ground truth and ranks them by the configured metric.

## Quick Summary

| Parameter | Simple meaning | Lower value | Higher value |
|---|---|---|---|
| `slop` | How close words must be for multi-word keywords | Stricter phrase matching | More flexible phrase matching |
| `edit_distance` | How many character differences are allowed in a word | Fewer typo matches | More typo/OCR-error matches |
| `min_fuzzy_term_length` | Minimum word length before typo matching is allowed | More words can fuzzy match | Short words stay exact |
| `keep_stopwords` | Whether common words like `by`, `the`, `and` are kept | Ignores common words | Requires common words too |
| `stem_words` | Whether word forms are normalized | Always enabled here | Always enabled here |
| `prefixlength` | How much of the beginning of a word must match exactly | `0` means no fixed prefix | Larger values are stricter |

## Important Matching Rule

WHOOSH first breaks both OCR text and keywords into searchable word tokens.

Then it searches page by page.

For example:

```json
{
  "emergent": ["Emergency"]
}
```

If page 3 contains:

```text
Contact Type: EMERGENCY
```

WHOOSH can detect the keyword group `emergent` on page 3 through the variant `Emergency`.

## Single-Word vs Multi-Word Keywords

### Single-word keyword

Example:

```text
Emergency
```

For a single word, WHOOSH mainly uses:

- `edit_distance`
- `min_fuzzy_term_length`
- `keep_stopwords`, only if the word itself is a stop word
- `stem_words`
- `prefixlength`

`slop` does not matter for single-word keywords because there is only one word.

Example:

```text
Keyword: Emergency
OCR text: EMERGENCY
```

This should match.

With fuzzy matching:

```text
Keyword: Emergency
OCR text: Emergancy
```

This may still match if the configured `edit_distance` is high enough.

### Multi-word keyword

Example:

```text
Electronically signed by
```

For multi-word keywords, WHOOSH must find all important words near each other on the same page.

This is where `slop` matters.

The code uses unordered proximity matching, so word order can vary.

Example:

```text
Keyword: Electronically signed by
OCR text: electronically signed by provider
```

This is a normal phrase match.

Another OCR example:

```text
OCR text: signed electronically by provider
```

This can also match because the words are near each other, even though the order changed.

## Parameter: `slop`

`slop` controls how close words must be for a multi-word keyword.

It only affects multi-word keywords.

Current values:

```yaml
slop: [1, 2, 3, 4, 5]
```

### What `slop=1` means

`slop=1` is the strictest setting in this grid.

It means the words must occur right next to each other.

Example keyword:

```text
signed by
```

Matches with `slop=1`:

```text
signed by physician
```

May not match with `slop=1`:

```text
signed electronically by physician
```

Because there is an extra word between `signed` and `by`.

### What `slop=5` means

`slop=5` is more flexible.

It allows the keyword words to appear within a wider nearby window.

Example keyword:

```text
signed by
```

May match with `slop=5`:

```text
signed electronically after review by physician
```

### Business impact

Lower `slop`:

- Fewer false positives
- More strict phrase matching
- May miss OCR text where words are separated or reordered

Higher `slop`:

- Better recall
- Can catch OCR reflow issues
- May introduce more false positives if the words appear near each other by coincidence

## Parameter: `edit_distance`

`edit_distance` controls typo tolerance for each searchable word.

Current values:

```yaml
edit_distance: [1, 2]
```

### What `edit_distance=1` means

Allows one character-level difference.

Example:

```text
Keyword: signed
OCR text: signad
```

This can match because only one character changed.

### What `edit_distance=2` means

Allows up to two character-level differences.

Example:

```text
Keyword: emergency
OCR text: emergncy
```

This may match because the OCR word is close to the keyword.

### Business impact

Lower `edit_distance`:

- More conservative
- Fewer typo-based false positives
- May miss OCR spelling errors

Higher `edit_distance`:

- Better for noisy OCR
- Can recover more misspelled words
- May match words that are not truly the intended keyword

## Parameter: `min_fuzzy_term_length`

`min_fuzzy_term_length` decides when fuzzy typo matching is allowed.

Current values:

```yaml
min_fuzzy_term_length: [1, 2, 3, 4, 5, 6, 7, 8, 9]
```

If a word is shorter than this value, the word must match exactly.

If a word length is equal to or greater than this value, `edit_distance` can be used.

### Example with short words

Keyword:

```text
by
```

If `min_fuzzy_term_length=5`, then `by` is too short for fuzzy matching.

So it must match exactly as:

```text
by
```

This prevents very short words from matching too broadly.

### Example with longer words

Keyword:

```text
Emergency
```

If `min_fuzzy_term_length=5`, then `Emergency` is long enough for fuzzy matching.

It may match OCR variants like:

```text
Emergancy
Emergncy
```

depending on `edit_distance`.

### Business impact

Lower `min_fuzzy_term_length`:

- More words can use typo tolerance
- More sensitive to OCR errors
- Higher risk of false positives on short words

Higher `min_fuzzy_term_length`:

- Short words stay exact
- Usually safer for business reporting
- May miss OCR typos in shorter keywords

## Parameter: `keep_stopwords`

Stop words are common words such as:

```text
the, and, or, by, of, to, in
```

Current values:

```yaml
keep_stopwords: [false, true]
```

### `keep_stopwords=false`

Common stop words are removed from both OCR text and keyword queries.

Example keyword:

```text
signed by
```

If `by` is treated as a stop word, the effective search may focus mostly on:

```text
signed
```

This can increase recall, but it can also make matching less specific.

### `keep_stopwords=true`

Common stop words are kept.

Example keyword:

```text
signed by
```

WHOOSH keeps both:

```text
signed
by
```

This makes the match more phrase-like and specific.

### Business impact

`keep_stopwords=false`:

- Can find more matches
- Better when OCR drops common words
- May over-match broad words

`keep_stopwords=true`:

- More exact phrase behavior
- Better for phrases where words like `by` matter
- May miss results if OCR omitted or damaged the stop word

## Parameter: `stem_words`

Stemming normalizes related word forms.

Current value:

```yaml
stem_words: true
```

This is always enabled for this tuning workflow.

Examples:

```text
sign, signed, signing -> similar searchable root
review, reviewed, reviewing -> similar searchable root
emergency, emergencies -> similar searchable root
```

### Business impact

Stemming helps WHOOSH match natural word variations without needing every exact form in the keyword file.

For example:

```text
Keyword: review
OCR text: reviewed by nurse
```

With stemming enabled, this is more likely to match.

## Parameter: `prefixlength`

`prefixlength` controls how many beginning characters must match exactly before fuzzy matching is allowed.

Current value:

```yaml
prefixlength: 0
```

`0` means WHOOSH does not require a fixed exact prefix.

Example:

```text
Keyword: emergency
OCR text: xmergency
```

With `prefixlength=0`, fuzzy matching can still consider this kind of beginning-character OCR error.

If `prefixlength` were larger, the beginning of the word would need to match exactly.

### Business impact

`prefixlength=0` is more flexible for OCR errors at the beginning of words.

It can improve recall, but it is less strict than requiring the first few letters to be exact.

## How These Parameters Work Together

Example keyword:

```text
Digitally signed by
```

Example OCR text:

```text
Digitally signad electronically by Dr. Smith
```

The run may match this if:

- `edit_distance` allows `signad` to match `signed`
- `slop` allows the words to be separated
- `keep_stopwords=true` keeps `by`, or `keep_stopwords=false` ignores it
- `min_fuzzy_term_length` allows fuzzy matching on `signed`
- `stem_words=true` normalizes related word forms

## Strict vs Broad Examples

### Strict configuration

```yaml
slop: 1
edit_distance: 1
min_fuzzy_term_length: 9
keep_stopwords: true
stem_words: true
prefixlength: 0
```

Expected behavior:

- Words need to be very close
- Short and medium words mostly need exact matching
- Stop words are included
- Lower false-positive risk
- Higher false-negative risk

### Broad configuration

```yaml
slop: 5
edit_distance: 2
min_fuzzy_term_length: 1
keep_stopwords: false
stem_words: true
prefixlength: 0
```

Expected behavior:

- Words can be farther apart
- More OCR typos can match
- Stop words are ignored
- Higher recall potential
- Higher false-positive risk

## How To Explain This To Business

The tuning process is testing different levels of strictness.

Strict settings answer:

```text
How well does WHOOSH perform when we only accept close, clean matches?
```

Broad settings answer:

```text
How well does WHOOSH perform when we allow OCR noise, spelling mistakes, and word spacing issues?
```

The best parameter set is the one that gives the best balance between:

- finding true keyword pages and keywords
- avoiding false detections
- minimizing missed detections

## Metric Connection

The final report calculates two levels of performance.

### Keyword level

Checks whether the correct keyword was detected on the correct page.

Example:

```text
GT page 3: emergent
WHOOSH page 3: emergent
```

This is a keyword true positive.

### Page level

Checks whether a page has at least one keyword, regardless of which keyword.

Example:

```text
GT keyword pages: 1, 2, 3
WHOOSH detected pages: 2, 3, 4
```

Counts:

```text
TP = 2  pages 2 and 3
FP = 1  page 4
FN = 1  page 1
TN = all remaining pages with no GT keyword and no WHOOSH detection
```

Page-level metrics are useful for understanding whether the model finds the right pages.

Keyword-level metrics are stricter because they also require the right keyword label.
