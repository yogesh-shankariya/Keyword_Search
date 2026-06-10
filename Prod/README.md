# Encounter OCR Keyword Search

This script searches OCR encounter JSON files for configured keyword variants using Whoosh.

It supports:

- Single OCR JSON file processing
- OCR folder processing, one JSON file at a time
- Mirrored output folder hierarchy
- Per-file debug logs
- Per-file and per-keyword timing information
- Batch summary reporting
- In-script default configuration for no-argument runs
- Optional stemming and short-keyword exact matching to reduce false positives

## Step 1: Open The Prod Folder

Run all commands from inside the `Prod` folder:

```bash
cd Prod
```

If a Python virtual environment is used, activate it before running the script.

Example:

```bash
source path/to/venv/bin/activate
```

Whoosh must be installed in the active Python environment:

```bash
pip install Whoosh
```

## Script

The default input path, output path, logs path, index path, and matching configuration live in the `CONFIG_...` block near the top of `whoosh_encounter_json_keyword_search.py`.

Those paths are absolute `Path(...)` values, so you can paste the exact OCR, keyword, output, log, and index paths you want:

```python
CONFIG_OCR_JSON = Path("/absolute/path/to/ocr_json_folder")
CONFIG_KEYWORDS_JSON = Path("/absolute/path/to/keywords.json")
CONFIG_OUTPUT = Path("/absolute/path/to/output_folder")
CONFIG_LOGS_DIR = Path("/absolute/path/to/logs_folder")
CONFIG_INDEX_DIR = Path("/absolute/path/to/index_folder")
```

With those defaults, run:

```bash
python whoosh_encounter_json_keyword_search.py
```

Or from anywhere:

```bash
python /Users/mitulkanani/Desktop/Projects/Keyword_Search/Prod/whoosh_encounter_json_keyword_search.py
```

If the environment uses `python3` instead of `python`, replace `python` with `python3` in the commands below.

## Input Files

OCR JSON folder:

```text
Input/OCR
```

Keyword JSON file:

```text
Input/Keywords/sample_provider_role_keywords_flattened.json
```

The OCR input can be either:

- One JSON file
- A folder containing multiple JSON files

When a folder is passed, the script recursively finds all `*.json` files and processes them one by one.

## Recommended Batch Run

The no-argument script run already uses the configured OCR folder and output locations. CLI arguments are still available when you want to override the in-script defaults:

```bash
python whoosh_encounter_json_keyword_search.py \
  --ocr-json Input/OCR \
  --keywords-json Input/Keywords/sample_provider_role_keywords_flattened.json \
  --slop 5 \
  --edit-distance 1 \
  --output Output \
  --logs-dir Output/Logs \
  --include-file-path
```

## Single File Run

Use this command from inside the `Prod` folder to process only one OCR JSON file:

```bash
python whoosh_encounter_json_keyword_search.py \
  --ocr-json Input/OCR/sample_ocr_encounters_input.json \
  --keywords-json Input/Keywords/sample_provider_role_keywords_flattened.json \
  --slop 5 \
  --edit-distance 1 \
  --output Output/sample_encounter_keyword_output.json \
  --logs-dir Output/Logs \
  --include-file-path
```

## Output Files

For folder mode, output is generated under the output root using the same input hierarchy.

Example:

```text
Input:
Input/OCR/sample_ocr_encounters_input.json

Output:
Output/sample_ocr_encounters_input_keyword_output.json
```

Each output JSON contains page-level records:

```json
[
  {
    "page_number": 1,
    "encounter_id": "ENC_WRONG_ORDER_001",
    "matches": [
      {
        "group": "electronically_signed_provider.Electronically signed by",
        "variant": "Electronically signed by"
      }
    ],
    "file_path": "enc_wrong_order_001.txt"
  }
]
```

## Log Files

Each OCR JSON file gets its own log file.

Example:

```text
Output/Logs/sample_ocr_encounters_input.log
```

The log file is plain text. Each event is written as a readable block with the timestamp, file name, event name, elapsed time, and related details.

Each log event includes:

- `file_id`: OCR JSON file name
- `event`: what happened
- `elapsed_ms`: time elapsed for that file
- `timestamp_utc`: event timestamp

The `file_id` is the input file name, so logs can be traced back to the exact OCR file.

Example log events:

```text
log_opened
file_processing_started
ocr_json_read_finished
keywords_json_read_finished
pages_extracted
keywords_flattened
whoosh_index_created
index_page_added
keyword_search_started
keyword_analyzed
fuzzy_term_expanded
whoosh_query_built
match_added
keyword_search_finished
keyword_detection_finished
output_written
file_processing_finished
```

## Batch Summary

Folder mode also creates:

```text
Output/Logs/batch_summary.json
Output/Logs/batch_summary.csv
```

The JSON file shows:

- Total batch duration
- Number of files processed
- Success count
- Failed count
- Per-file duration
- Output path for each file
- Log path for each file
- Total match count per file

The CSV file is a simple timing summary with two columns:

```text
filename,processing_time_seconds
sample_ocr_encounters_input.json,0.043
```

## Useful Options

`--matched-only`

Returns only pages where at least one keyword matched.

`--include-file-path`

Includes the OCR source `file_path` in each output record.

`--include-cover`

Also searches `cover.text` if present in the OCR JSON.

`--log-preview-chars 0`

Uses compact logs. With the default value `0`, logs keep timing, counts, configuration, errors, matches, and hit records, but do not write OCR page text previews, full keyword dumps, full fuzzy expansion lists, or long query strings.

Set this above `0` only when you intentionally want detailed page text previews for debugging.

`--stop-on-error`

In folder mode, stops processing after the first failed JSON file.

`--keep-stopwords`

Keeps words like `and`, `by`, `to`, and `the` during keyword search. By default, the script uses the existing recall-first mode where Whoosh removes common stop words.

`--stem-words`

Stems OCR and keyword terms before fuzzy matching. This helps a keyword like `Priority` match OCR forms like `priorities`, `prioritize`, `prioritized`, and `prioritizing` when used with the existing fuzzy `--edit-distance` setting.

Example:

```bash
python whoosh_encounter_json_keyword_search.py \
  --ocr-json Input/OCR/sample_ocr_encounters_input.json \
  --keywords-json Input/Keywords/sample_provider_role_keywords_flattened.json \
  --stem-words \
  --edit-distance 1
```

If `--stem-words` and `--keep-stopwords` are both passed, `--stem-words` takes precedence and Whoosh's built-in `StemmingAnalyzer()` is used.

`--min-fuzzy-term-length 5`

Terms shorter than this value use exact matching instead of fuzzy matching. With the default value `5`, a short keyword like `STAT` must match `stat` exactly and will not fuzzy-match OCR words like `start` or `status`.

## Timing Information

The logs include timing for:

- Full file processing
- OCR JSON read
- Keyword JSON read
- Page extraction
- Keyword flattening
- Whoosh index creation
- Page indexing
- Each keyword search
- Final output writing

This helps identify which file or keyword is taking more time.

## Quick Validation Commands

After running, check the batch summary:

```bash
cat Output/Logs/batch_summary.json
```

Check logs for one file:

```bash
cat Output/Logs/sample_ocr_encounters_input.log
```
