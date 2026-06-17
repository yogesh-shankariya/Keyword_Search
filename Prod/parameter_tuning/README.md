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
