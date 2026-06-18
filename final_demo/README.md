# Portable WHOOSH Parameter Demo

This folder is self-contained and portable. It does not use any absolute path from the original machine.

## Files

- `WHOOSH_PARAMETER_TUNING_PORTABLE_DEMO.ipynb` - runnable notebook for business and technical demos.
- `demo_config.yaml` - relative paths and demo parameter values.
- `whoosh_demo_engine.py` - local WHOOSH keyword-search demo engine.
- `metrics_demo.py` - local metric calculation helper.
- `sample_data/` - small OCR, keyword, GT, and method-output JSON examples.
- `requirements.txt` - Python packages needed for the notebook.

## Run

From this folder:

```bash
pip install -r requirements.txt
jupyter notebook WHOOSH_PARAMETER_TUNING_PORTABLE_DEMO.ipynb
```

If you already use a virtual environment, activate it first.

## Portability Rule

All paths in `demo_config.yaml` are relative to this `final_demo` folder.

You can move this folder to another device and run the notebook from there.

