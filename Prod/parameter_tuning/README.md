# WHOOSH Parameter Tuning

This folder is the clean YAML-driven parameter tuning workflow.

## Files

- `parameter_tuning.yaml` - all paths, tuning parameters, runtime settings, and report settings.
- `parameter_tuning_orchestrator.py` - runs all WHOOSH parameter combinations and optionally generates the report.
- `whoosh_encounter_json_keyword_search.py` - cleaned WHOOSH engine. It has no hardcoded input/output config block.
- `generate_final_comparison_report.py` - compares WHOOSH and RapidFuzz output against GT.
- `config_loader.py` - shared YAML loader and validation.

## Run

First open this folder:

```bash
cd /Users/mitulkanani/Desktop/Projects/Keyword_Search/Prod/parameter_tuning
```

Run parameter tuning:

```bash
python parameter_tuning_orchestrator.py
```

The orchestrator also generates the final report after tuning because `runtime.generate_report_after_tuning` is `true`.

You can rerun only the final report independently:

```bash
python generate_final_comparison_report.py
```

There are no command-line arguments. Edit `parameter_tuning.yaml`.

You can still run from the project root with the longer path if needed:

```bash
python Prod/parameter_tuning/parameter_tuning_orchestrator.py
python Prod/parameter_tuning/generate_final_comparison_report.py
```

## Path Rule

Use absolute paths in `parameter_tuning.yaml`.
For Windows paths, prefer single quotes so backslashes are read literally.
Python-style raw strings such as `r"C:\Users\name\input\ocr"` are also accepted.

Example:

```yaml
paths:
  ocr_json: 'C:\Users\name\input\ocr'
  keywords_json: 'C:\Users\name\input\keywords.json'
```

## Report

By default, `runtime.generate_report_after_tuning` is `true`, so the orchestrator generates the Excel report after tuning finishes. You can still run `python generate_final_comparison_report.py` independently to regenerate the report from an existing execution summary.

The report writer automatically uses `xlsxwriter` first, then `openpyxl`, when either package is installed. Those engines create a formatted workbook with frozen headers, filters, bold header rows, and sized columns.

If a run produces output for only part of the OCR set, comparison metrics are calculated only on files that have method output and matching GT. The report still exposes file counts such as `common_file_count` and `missing_method_file_count` so incomplete runs are visible.

For the `Best WHOOSH vs RapidFuzz`, `Ground Truth (LLM)`, and `Detailed Comparison` sheets, the workbook uses the best WHOOSH run's passed file set. For example, if GT has 200 files and the best WHOOSH run produced 190 valid output files, those sheets are scoped to those 190 files.

## Retry Behavior

`runtime.max_file_retries: 3` means a failed OCR JSON file is rerun up to three additional times before it is marked failed. This retry is per file, not for the whole parameter combination.

Each retry gets its own log file, such as:

```text
abc.log
abc_attempt_2.log
abc_attempt_3.log
abc_attempt_4.log
```

The first attempt uses the normal log filename; retry attempts use numbered log filenames.

If the file still fails after all retries, copies of those attempt logs plus a JSON failure summary are written under `logs/<run_folder>/failed/`. The failure summary includes the input path, output path, attempt count, final error type/message, and all failed attempts. Stale output files for failed files are removed before retrying, so failed files do not leak into the report.

RapidFuzz output should be a JSON page list with this shape:

```json
[
  {
    "page_number": 3,
    "encounter_id": 2,
    "matches": [
      {"group": "emergent", "variant": "Emergency"}
    ],
    "file_path": ""
  }
]
```

RapidFuzz filenames must match GT base filenames, for example:

- GT: `abc.json`
- RP: `abc.json` or `abc_rapidfuzz_output.json`
