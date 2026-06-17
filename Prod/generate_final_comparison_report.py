#!/usr/bin/env python3
"""
Final Comparison Report Generator for WHOOSH parameter tuning.

This module compares all successful WHOOSH tuning outputs against Ground Truth
(GT) JSON files and generates a comprehensive Excel report.

What it creates:
1. Parameter Tuning Results
   - One row per successful WHOOSH run
   - Keyword-level metrics
   - Page-level metrics
   - Ranked by a selected metric, default keyword F1

2. Best WHOOSH vs RapidFuzz
   - Summary comparison of best WHOOSH config vs RapidFuzz vs GT

3. Ground Truth (LLM)
   - Flattened GT keyword detections with file, page, keyword, and reason

4. Detailed Comparison
   - Per-file/per-page TP, FP, FN details for best WHOOSH and RapidFuzz

Expected config format:
{
  "FIXED_PATHS": {
    "gt_output": "path/to/gt/output",
    "rp_output": "path/to/rapidfuzz/output"
  }
}

Usage:
    python generate_final_comparison_report.py

Optional examples:
    python generate_final_comparison_report.py \
      --config config/parameter_grid.json \
      --execution-summary execution_summary.json \
      --output parameter_tuning_results.xlsx

    python generate_final_comparison_report.py --rank-by kw_recall

Notes:
- WHOOSH output files are expected to end with *_keyword_output.json.
- GT files are expected to contain pages_with_keywords.
- Method output files are expected to contain page rows with matches.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import pandas as pd

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixedPaths:
    """Directories required for report generation."""

    gt_output_dir: Path
    rp_output_dir: Optional[Path] = None


@dataclass(frozen=True)
class RunParameters:
    """WHOOSH tuning parameters for one run."""

    slop: Any = ""
    edit_distance: Any = ""
    min_fuzzy_term_length: Any = ""
    keep_stopwords: Any = ""
    stem_words: Any = ""


@dataclass(frozen=True)
class SuccessfulRun:
    """One successful WHOOSH run from execution_summary.json."""

    run_id: int
    run_name: str
    output_dir: Path
    duration_seconds: float
    parameters: RunParameters


@dataclass(frozen=True)
class MethodMetrics:
    """Aggregated keyword-level and page-level metrics."""

    files_processed: int = 0
    total_pages_analyzed: int = 0
    gt_total_keywords: int = 0
    method_total_detected: int = 0
    kw_tp: int = 0
    kw_fp: int = 0
    kw_fn: int = 0
    kw_precision: float = 0.0
    kw_recall: float = 0.0
    kw_f1: float = 0.0
    page_tp: int = 0
    page_fp: int = 0
    page_fn: int = 0
    page_tn: int = 0
    page_precision: float = 0.0
    page_recall: float = 0.0
    page_f1: float = 0.0
    page_accuracy: float = 0.0
    gt_pages_with_keywords_total: int = 0
    method_pages_detected: int = 0
    total_pages_all_docs: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Return metrics as a dictionary, including WHOOSH-compatible alias."""
        data = {
            "files_processed": self.files_processed,
            "total_pages_analyzed": self.total_pages_analyzed,
            "gt_total_keywords": self.gt_total_keywords,
            "method_total_detected": self.method_total_detected,
            # Backward-compatible alias used by the earlier script.
            "whoosh_total_detected": self.method_total_detected,
            "kw_tp": self.kw_tp,
            "kw_fp": self.kw_fp,
            "kw_fn": self.kw_fn,
            "kw_precision": round(self.kw_precision, 2),
            "kw_recall": round(self.kw_recall, 2),
            "kw_f1": round(self.kw_f1, 2),
            "page_tp": self.page_tp,
            "page_fp": self.page_fp,
            "page_fn": self.page_fn,
            "page_tn": self.page_tn,
            "page_precision": round(self.page_precision, 2),
            "page_recall": round(self.page_recall, 2),
            "page_f1": round(self.page_f1, 2),
            "page_accuracy": round(self.page_accuracy, 2),
            "gt_pages_with_keywords_total": self.gt_pages_with_keywords_total,
            "method_pages_detected": self.method_pages_detected,
            "total_pages_all_docs": self.total_pages_all_docs,
        }
        return data


# ---------------------------------------------------------------------------
# Logging / CLI helpers
# ---------------------------------------------------------------------------


def configure_logging(verbose: bool = False, log_file: Optional[Path] = None) -> None:
    """Configure console and optional file logging."""
    level = logging.DEBUG if verbose else logging.INFO
    handlers: List[logging.Handler] = [logging.StreamHandler()]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Generate final WHOOSH parameter tuning comparison Excel report."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=script_dir / "config" / "parameter_grid.json",
        help="Path to parameter_grid.json. Default: ./config/parameter_grid.json",
    )
    parser.add_argument(
        "--execution-summary",
        type=Path,
        default=script_dir / "execution_summary.json",
        help="Path to execution_summary.json generated by parameter_tuning_orchestrator.py.",
    )
    parser.add_argument(
        "--gt-output-dir",
        type=Path,
        default=None,
        help="Override GT output directory. If omitted, uses FIXED_PATHS.gt_output from config.",
    )
    parser.add_argument(
        "--rp-output-dir",
        type=Path,
        default=None,
        help="Override RapidFuzz output directory. If omitted, uses FIXED_PATHS.rp_output from config when available.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output Excel path. Default: ./parameter_tuning_results_<timestamp>.xlsx",
    )
    parser.add_argument(
        "--rank-by",
        default="kw_f1",
        choices=["kw_f1", "kw_recall", "kw_precision", "page_f1", "page_recall", "page_precision", "page_accuracy"],
        help="Metric used to rank parameter combinations. Default: kw_f1",
    )
    parser.add_argument(
        "--method-key-field",
        default="group",
        help="Preferred field inside each method match object to compare with GT. Default: group",
    )
    parser.add_argument(
        "--whoosh-glob",
        default="*_keyword_output.json",
        help="Glob pattern for WHOOSH output files. Default: *_keyword_output.json",
    )
    parser.add_argument(
        "--rp-glob",
        default="*.json",
        help="Glob pattern for RapidFuzz output files. Default: *.json",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Optional log file path.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# JSON loading / normalization helpers
# ---------------------------------------------------------------------------


def normalize_keyword(keyword: Any) -> str:
    """
    Normalize keyword text for comparison.

    Lowercases, strips leading/trailing whitespace, and collapses repeated spaces.
    """
    if keyword is None:
        return ""
    text = str(keyword).strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def read_json(filepath: Path) -> Any:
    """Read a JSON file with UTF-8 encoding."""
    with filepath.open("r", encoding="utf-8") as file:
        return json.load(file)


def safe_int(value: Any, default: int = 0) -> int:
    """Convert value to int safely."""
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def get_base_filename(filename: str) -> str:
    """
    Extract comparable base filename from GT/method output names.

    Examples:
        ABC.json -> ABC
        ABC_keyword_output.json -> ABC
    """
    base = Path(filename).name
    if base.endswith(".json"):
        base = base[:-5]
    for suffix in ("_keyword_output", "_whoosh_output", "_rapidfuzz_output"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


def dedupe_preserve_order(values: Iterable[Any]) -> List[str]:
    """Deduplicate strings using normalized text while preserving original order."""
    seen: Set[str] = set()
    result: List[str] = []
    for value in values:
        normalized = normalize_keyword(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(str(value).strip())
    return result


def extract_keyword_from_gt_item(item: Any) -> Tuple[str, str]:
    """
    Extract keyword and reason from one GT item.

    Supports both:
    - {"keyword": "...", "reason": "..."}
    - "keyword text"
    """
    if isinstance(item, Mapping):
        keyword = str(item.get("keyword", "")).strip()
        reason = str(item.get("reason", "")).strip()
        return keyword, reason
    return str(item).strip(), ""


def load_gt_keywords(filepath: Path) -> Tuple[Dict[int, List[Dict[str, str]]], int]:
    """
    Load ground-truth keywords from one JSON file.

    Expected schema:
        {
          "total_pages": 10,
          "pages_with_keywords": [
            {
              "page_number": 1,
              "keywords_detected": [
                {"keyword": "Reviewer", "reason": "..."}
              ]
            }
          ]
        }

    Returns:
        (page_number -> [{"keyword": str, "reason": str}], total_pages)
    """
    try:
        data = read_json(filepath)
    except Exception as exc:
        LOGGER.error("Error loading GT file %s: %s", filepath.name, exc)
        return {}, 0

    pages_raw: List[Any]
    if isinstance(data, Mapping):
        pages_raw = list(data.get("pages_with_keywords", []))
        total_pages = safe_int(data.get("total_pages"), 0)
    elif isinstance(data, list):
        # Defensive fallback if GT has already been flattened by page.
        pages_raw = data
        total_pages = 0
    else:
        LOGGER.warning("Unsupported GT JSON structure in %s", filepath.name)
        return {}, 0

    result: Dict[int, List[Dict[str, str]]] = {}
    max_seen_page = 0

    for page_info in pages_raw:
        if not isinstance(page_info, Mapping):
            continue
        page_num = safe_int(page_info.get("page_number"), 0)
        if page_num <= 0:
            continue
        max_seen_page = max(max_seen_page, page_num)

        keywords_raw = page_info.get("keywords_detected", [])
        if keywords_raw is None:
            keywords_raw = []

        seen: Set[str] = set()
        deduplicated: List[Dict[str, str]] = []
        for kw_item in keywords_raw:
            keyword, reason = extract_keyword_from_gt_item(kw_item)
            normalized = normalize_keyword(keyword)
            if normalized and normalized not in seen:
                seen.add(normalized)
                deduplicated.append({"keyword": keyword, "reason": reason})

        result[page_num] = deduplicated

    if total_pages <= 0:
        total_pages = max_seen_page

    return result, total_pages


def extract_method_keywords_from_page(
    page_info: Mapping[str, Any],
    preferred_match_field: str = "group",
) -> List[str]:
    """
    Extract method keywords from one method-output page.

    Primary expected schema:
        {
          "page_number": 1,
          "matches": [{"group": "Reviewer", "variant": "Reviewer"}]
        }

    Defensive fallback schemas supported:
    - keyword_detected: ["..."]
    - keywords_detected: ["..."] or [{"keyword": "..."}]
    - matches: ["..."]
    """
    keywords: List[Any] = []

    matches = page_info.get("matches")
    if isinstance(matches, list):
        for match in matches:
            if isinstance(match, Mapping):
                value = match.get(preferred_match_field)
                if value is None or value == "":
                    # Fallback order keeps current project output compatible.
                    for fallback_field in ("group", "variant", "keyword", "text", "matched_keyword"):
                        value = match.get(fallback_field)
                        if value:
                            break
                if value:
                    keywords.append(value)
            elif isinstance(match, str):
                keywords.append(match)

    for field in ("keyword_detected", "keywords_detected", "detected_keywords"):
        raw_values = page_info.get(field)
        if isinstance(raw_values, list):
            for item in raw_values:
                if isinstance(item, Mapping):
                    keyword, _ = extract_keyword_from_gt_item(item)
                    keywords.append(keyword)
                else:
                    keywords.append(item)

    return dedupe_preserve_order(keywords)


def load_method_keywords(
    filepath: Path,
    preferred_match_field: str = "group",
) -> Dict[int, List[str]]:
    """
    Load keywords from a WHOOSH/RapidFuzz output file.

    Returns:
        page_number -> [unique keyword strings]
    """
    try:
        data = read_json(filepath)
    except Exception as exc:
        LOGGER.error("Error loading method file %s: %s", filepath.name, exc)
        return {}

    if isinstance(data, Mapping):
        # Common defensive possibilities.
        if isinstance(data.get("pages"), list):
            pages_raw = data["pages"]
        elif isinstance(data.get("pages_with_keywords"), list):
            pages_raw = data["pages_with_keywords"]
        elif isinstance(data.get("results"), list):
            pages_raw = data["results"]
        else:
            LOGGER.warning("Unsupported method JSON object structure in %s", filepath.name)
            return {}
    elif isinstance(data, list):
        pages_raw = data
    else:
        LOGGER.warning("Unsupported method JSON structure in %s", filepath.name)
        return {}

    result: Dict[int, List[str]] = {}
    for page_info in pages_raw:
        if not isinstance(page_info, Mapping):
            continue
        page_num = safe_int(page_info.get("page_number"), 0)
        if page_num <= 0:
            continue
        result[page_num] = extract_method_keywords_from_page(
            page_info,
            preferred_match_field=preferred_match_field,
        )

    return result


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def calculate_page_level_counts(
    gt_pages: Set[int],
    method_pages: Set[int],
    total_pages: int,
) -> Dict[str, int]:
    """Calculate page-level TP, FP, FN, TN counts."""
    page_tp = len(gt_pages & method_pages)
    page_fp = len(method_pages - gt_pages)
    page_fn = len(gt_pages - method_pages)

    all_pages = set(range(1, total_pages + 1)) if total_pages > 0 else gt_pages | method_pages
    pages_with_any_detection = gt_pages | method_pages
    page_tn = len(all_pages - pages_with_any_detection)

    return {
        "page_tp": page_tp,
        "page_fp": page_fp,
        "page_fn": page_fn,
        "page_tn": page_tn,
    }


def precision_recall_f1(tp: int, fp: int, fn: int) -> Tuple[float, float, float]:
    """Calculate precision, recall, and F1 percentage values."""
    detected = tp + fp
    expected = tp + fn
    precision = (tp / detected * 100) if detected > 0 else 0.0
    recall = (tp / expected * 100) if expected > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def build_file_mapping(directory: Path, glob_pattern: str) -> Dict[str, Path]:
    """Map comparable base filename to JSON file path."""
    return {get_base_filename(path.name): path for path in directory.glob(glob_pattern)}


def compare_output_dir_vs_gt(
    method_output_dir: Path,
    gt_output_dir: Path,
    method_name: str = "Method",
    method_glob: str = "*.json",
    preferred_match_field: str = "group",
) -> MethodMetrics:
    """
    Compare one method output directory against GT.

    Works for WHOOSH, RapidFuzz, or any method that follows the same page-level
    JSON structure.
    """
    if not method_output_dir.exists():
        LOGGER.warning("%s output directory does not exist: %s", method_name, method_output_dir)
        return MethodMetrics()

    if not gt_output_dir.exists():
        LOGGER.warning("GT output directory does not exist: %s", gt_output_dir)
        return MethodMetrics()

    gt_files = build_file_mapping(gt_output_dir, "*.json")
    method_files = build_file_mapping(method_output_dir, method_glob)
    common_bases = set(gt_files) & set(method_files)

    if not common_bases:
        LOGGER.warning("No common files between %s and GT outputs", method_name)
        LOGGER.debug("GT files: %s", sorted(gt_files)[:10])
        LOGGER.debug("%s files: %s", method_name, sorted(method_files)[:10])
        return MethodMetrics()

    total_kw_tp = 0
    total_kw_fp = 0
    total_kw_fn = 0
    total_page_tp = 0
    total_page_fp = 0
    total_page_fn = 0
    total_page_tn = 0
    total_pages_analyzed = 0
    files_processed = 0

    for base_name in sorted(common_bases):
        gt_filepath = gt_files[base_name]
        method_filepath = method_files[base_name]

        gt_pages, total_pages = load_gt_keywords(gt_filepath)
        method_pages = load_method_keywords(method_filepath, preferred_match_field=preferred_match_field)

        if total_pages <= 0:
            total_pages = max(set(gt_pages) | set(method_pages), default=0)
        if total_pages <= 0:
            LOGGER.warning("Skipping %s because total pages is 0", base_name)
            continue

        gt_pages_with_keywords = {page for page, keywords in gt_pages.items() if keywords}
        method_pages_with_keywords = {page for page, keywords in method_pages.items() if keywords}

        page_counts = calculate_page_level_counts(
            gt_pages_with_keywords,
            method_pages_with_keywords,
            total_pages,
        )
        total_page_tp += page_counts["page_tp"]
        total_page_fp += page_counts["page_fp"]
        total_page_fn += page_counts["page_fn"]
        total_page_tn += page_counts["page_tn"]

        all_pages_with_any_keyword_record = set(gt_pages) | set(method_pages)
        for page_num in all_pages_with_any_keyword_record:
            gt_kw_list = [kw["keyword"] for kw in gt_pages.get(page_num, [])]
            method_kw_list = method_pages.get(page_num, [])

            gt_kw_normalized = {normalize_keyword(keyword) for keyword in gt_kw_list if normalize_keyword(keyword)}
            method_kw_normalized = {
                normalize_keyword(keyword) for keyword in method_kw_list if normalize_keyword(keyword)
            }

            total_kw_tp += len(gt_kw_normalized & method_kw_normalized)
            total_kw_fp += len(method_kw_normalized - gt_kw_normalized)
            total_kw_fn += len(gt_kw_normalized - method_kw_normalized)

        total_pages_analyzed += total_pages
        files_processed += 1

    kw_precision, kw_recall, kw_f1 = precision_recall_f1(total_kw_tp, total_kw_fp, total_kw_fn)
    page_precision, page_recall, page_f1 = precision_recall_f1(
        total_page_tp,
        total_page_fp,
        total_page_fn,
    )

    total_pages_all_docs = total_page_tp + total_page_fp + total_page_fn + total_page_tn
    page_accuracy = (
        (total_page_tp + total_page_tn) / total_pages_all_docs * 100
        if total_pages_all_docs > 0
        else 0.0
    )

    return MethodMetrics(
        files_processed=files_processed,
        total_pages_analyzed=total_pages_analyzed,
        gt_total_keywords=total_kw_tp + total_kw_fn,
        method_total_detected=total_kw_tp + total_kw_fp,
        kw_tp=total_kw_tp,
        kw_fp=total_kw_fp,
        kw_fn=total_kw_fn,
        kw_precision=kw_precision,
        kw_recall=kw_recall,
        kw_f1=kw_f1,
        page_tp=total_page_tp,
        page_fp=total_page_fp,
        page_fn=total_page_fn,
        page_tn=total_page_tn,
        page_precision=page_precision,
        page_recall=page_recall,
        page_f1=page_f1,
        page_accuracy=page_accuracy,
        gt_pages_with_keywords_total=total_page_tp + total_page_fn,
        method_pages_detected=total_page_tp + total_page_fp,
        total_pages_all_docs=total_pages_all_docs,
    )


def compare_whoosh_vs_gt(
    whoosh_output_dir: Path,
    gt_output_dir: Path,
    whoosh_glob: str = "*_keyword_output.json",
    preferred_match_field: str = "group",
) -> Dict[str, Any]:
    """Backward-compatible wrapper for comparing WHOOSH output to GT."""
    return compare_output_dir_vs_gt(
        method_output_dir=whoosh_output_dir,
        gt_output_dir=gt_output_dir,
        method_name="WHOOSH",
        method_glob=whoosh_glob,
        preferred_match_field=preferred_match_field,
    ).to_dict()


def compare_method_vs_gt(
    method_output_dir: Path,
    gt_output_dir: Path,
    method_name: str = "Method",
    method_glob: str = "*.json",
    preferred_match_field: str = "group",
) -> Dict[str, Any]:
    """Backward-compatible wrapper for comparing a generic method output to GT."""
    return compare_output_dir_vs_gt(
        method_output_dir=method_output_dir,
        gt_output_dir=gt_output_dir,
        method_name=method_name,
        method_glob=method_glob,
        preferred_match_field=preferred_match_field,
    ).to_dict()


def create_empty_metrics() -> Dict[str, Any]:
    """Backward-compatible empty metrics dictionary."""
    return MethodMetrics().to_dict()


# ---------------------------------------------------------------------------
# Config / summary loading
# ---------------------------------------------------------------------------


def load_fixed_paths(config_path: Path) -> FixedPaths:
    """Load GT and RapidFuzz directories from parameter grid config."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    config = read_json(config_path)
    if not isinstance(config, Mapping):
        raise ValueError(f"Config must be a JSON object: {config_path}")

    fixed_paths = config.get("FIXED_PATHS")
    if not isinstance(fixed_paths, Mapping):
        raise KeyError("Config must contain FIXED_PATHS object")

    gt_output = fixed_paths.get("gt_output")
    if not gt_output:
        raise KeyError("Config FIXED_PATHS must contain gt_output")

    rp_output = fixed_paths.get("rp_output")
    return FixedPaths(
        gt_output_dir=Path(str(gt_output)).expanduser(),
        rp_output_dir=Path(str(rp_output)).expanduser() if rp_output else None,
    )


def parse_run_parameters(parameters: Mapping[str, Any]) -> RunParameters:
    """Parse run parameters from one execution summary result."""
    return RunParameters(
        slop=parameters.get("slop", ""),
        edit_distance=parameters.get("edit_distance", ""),
        min_fuzzy_term_length=parameters.get("min_fuzzy_term_length", ""),
        keep_stopwords=parameters.get("keep_stopwords", ""),
        stem_words=parameters.get("stem_words", ""),
    )


def load_successful_runs(execution_summary_path: Path) -> List[SuccessfulRun]:
    """Load successful runs from execution_summary.json."""
    if not execution_summary_path.exists():
        raise FileNotFoundError(
            f"Execution summary not found: {execution_summary_path}. "
            "Run parameter_tuning_orchestrator.py first."
        )

    summary = read_json(execution_summary_path)
    if not isinstance(summary, Mapping):
        raise ValueError("execution_summary.json must contain a JSON object")

    raw_results = summary.get("results", [])
    if not isinstance(raw_results, list):
        raise ValueError("execution_summary.json must contain a results list")

    successful_runs: List[SuccessfulRun] = []
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("status") != "success":
            continue

        parameters = raw.get("parameters")
        if not isinstance(parameters, Mapping):
            parameters = {}

        output_dir = raw.get("output_dir")
        if not output_dir:
            LOGGER.warning("Skipping successful run %s because output_dir is missing", raw.get("run_id"))
            continue

        successful_runs.append(
            SuccessfulRun(
                run_id=safe_int(raw.get("run_id"), 0),
                run_name=str(raw.get("run_name", "")),
                output_dir=Path(str(output_dir)).expanduser(),
                duration_seconds=float(raw.get("duration_seconds", 0.0) or 0.0),
                parameters=parse_run_parameters(parameters),
            )
        )

    return successful_runs


# ---------------------------------------------------------------------------
# Data extraction for Excel sheets
# ---------------------------------------------------------------------------


def extract_ground_truth_data(gt_output_dir: Path) -> List[Dict[str, Any]]:
    """
    Extract flattened ground truth data for the Ground Truth sheet.

    Returns rows with:
        file_name, page_number, keyword, reason
    """
    LOGGER.info("Extracting ground truth data...")
    gt_data: List[Dict[str, Any]] = []

    if not gt_output_dir.exists():
        LOGGER.warning("GT directory does not exist: %s", gt_output_dir)
        return gt_data

    for filepath in sorted(gt_output_dir.glob("*.json")):
        page_keywords, total_pages = load_gt_keywords(filepath)
        for page_num, keywords in sorted(page_keywords.items()):
            for kw_info in keywords:
                gt_data.append(
                    {
                        "file_name": filepath.name,
                        "base_name": get_base_filename(filepath.name),
                        "page_number": page_num,
                        "total_pages": total_pages,
                        "keyword": kw_info.get("keyword", ""),
                        "reason": kw_info.get("reason", ""),
                    }
                )

    LOGGER.info("Extracted %s ground-truth keyword detections", len(gt_data))
    return gt_data


def get_original_keywords_by_normalized(values: Iterable[str]) -> Dict[str, str]:
    """Map normalized keyword to original keyword text."""
    mapping: Dict[str, str] = {}
    for value in values:
        normalized = normalize_keyword(value)
        if normalized and normalized not in mapping:
            mapping[normalized] = value
    return mapping


def join_keywords(values: Iterable[str]) -> str:
    """Join keywords as a stable comma-separated string."""
    return ", ".join(sorted((str(value) for value in values if str(value).strip()), key=str.lower))


def extract_detailed_comparison_data(
    best_whoosh_output_dir: Path,
    rp_output_dir: Path,
    gt_output_dir: Path,
    whoosh_glob: str = "*_keyword_output.json",
    rp_glob: str = "*.json",
    preferred_match_field: str = "group",
) -> List[Dict[str, Any]]:
    """
    Extract per-file, per-page comparison rows for Best WHOOSH vs RapidFuzz.
    """
    LOGGER.info("Extracting detailed comparison data...")

    gt_files = build_file_mapping(gt_output_dir, "*.json")
    whoosh_files = build_file_mapping(best_whoosh_output_dir, whoosh_glob)
    rp_files = build_file_mapping(rp_output_dir, rp_glob)

    common_bases = set(gt_files) & set(whoosh_files) & set(rp_files)
    if not common_bases:
        LOGGER.warning("No common files between WHOOSH, RapidFuzz, and GT outputs")
        return []

    comparison_rows: List[Dict[str, Any]] = []

    for base_name in sorted(common_bases):
        gt_pages, total_pages = load_gt_keywords(gt_files[base_name])
        whoosh_pages = load_method_keywords(
            whoosh_files[base_name],
            preferred_match_field=preferred_match_field,
        )
        rp_pages = load_method_keywords(
            rp_files[base_name],
            preferred_match_field=preferred_match_field,
        )

        if total_pages <= 0:
            total_pages = max(set(gt_pages) | set(whoosh_pages) | set(rp_pages), default=0)

        all_pages = set(range(1, total_pages + 1)) | set(gt_pages) | set(whoosh_pages) | set(rp_pages)

        for page_num in sorted(all_pages):
            gt_kw_original = [kw["keyword"] for kw in gt_pages.get(page_num, [])]
            whoosh_kw_original = whoosh_pages.get(page_num, [])
            rp_kw_original = rp_pages.get(page_num, [])

            gt_map = get_original_keywords_by_normalized(gt_kw_original)
            whoosh_map = get_original_keywords_by_normalized(whoosh_kw_original)
            rp_map = get_original_keywords_by_normalized(rp_kw_original)

            gt_norm = set(gt_map)
            whoosh_norm = set(whoosh_map)
            rp_norm = set(rp_map)

            whoosh_tp = gt_norm & whoosh_norm
            whoosh_fp = whoosh_norm - gt_norm
            whoosh_fn = gt_norm - whoosh_norm

            rp_tp = gt_norm & rp_norm
            rp_fp = rp_norm - gt_norm
            rp_fn = gt_norm - rp_norm

            # Keep rows useful but avoid creating a giant all-empty sheet.
            if not (gt_norm or whoosh_norm or rp_norm):
                continue

            comparison_rows.append(
                {
                    "file_name": f"{base_name}.json",
                    "page_number": page_num,
                    "total_pages": total_pages,
                    "gt_keywords": join_keywords(gt_kw_original),
                    "whoosh_keywords": join_keywords(whoosh_kw_original),
                    "rp_keywords": join_keywords(rp_kw_original),
                    "whoosh_tp_count": len(whoosh_tp),
                    "whoosh_fp_count": len(whoosh_fp),
                    "whoosh_fn_count": len(whoosh_fn),
                    "rp_tp_count": len(rp_tp),
                    "rp_fp_count": len(rp_fp),
                    "rp_fn_count": len(rp_fn),
                    "whoosh_tp_keywords": join_keywords(whoosh_map[item] for item in whoosh_tp),
                    "whoosh_fp_keywords": join_keywords(whoosh_map[item] for item in whoosh_fp),
                    "whoosh_fn_keywords": join_keywords(gt_map[item] for item in whoosh_fn),
                    "rp_tp_keywords": join_keywords(rp_map[item] for item in rp_tp),
                    "rp_fp_keywords": join_keywords(rp_map[item] for item in rp_fp),
                    "rp_fn_keywords": join_keywords(gt_map[item] for item in rp_fn),
                }
            )

    LOGGER.info("Extracted %s detailed comparison rows", len(comparison_rows))
    return comparison_rows


def generate_best_whoosh_vs_rp_comparison(
    best_whoosh_output_dir: Path,
    rp_output_dir: Path,
    gt_output_dir: Path,
    best_run_params: Mapping[str, Any],
    whoosh_glob: str = "*_keyword_output.json",
    rp_glob: str = "*.json",
    preferred_match_field: str = "group",
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Generate summary comparison tables for Best WHOOSH vs RapidFuzz vs GT.

    Returns:
        (keyword_level_dataframe, page_level_dataframe)
    """
    LOGGER.info("=" * 80)
    LOGGER.info("GENERATING BEST WHOOSH VS RAPIDFUZZ COMPARISON")
    LOGGER.info("=" * 80)

    LOGGER.info("Comparing Best WHOOSH vs GT...")
    whoosh_metrics = compare_method_vs_gt(
        best_whoosh_output_dir,
        gt_output_dir,
        method_name="WHOOSH (Best)",
        method_glob=whoosh_glob,
        preferred_match_field=preferred_match_field,
    )

    LOGGER.info("Comparing RapidFuzz vs GT...")
    rp_metrics = compare_method_vs_gt(
        rp_output_dir,
        gt_output_dir,
        method_name="RapidFuzz",
        method_glob=rp_glob,
        preferred_match_field=preferred_match_field,
    )

    config_text = (
        "WHOOSH (Best Config: "
        f"slop={best_run_params.get('slop')}, "
        f"ed={best_run_params.get('edit_distance')}, "
        f"min_fuzzy={best_run_params.get('min_fuzzy_term_length')}, "
        f"stopwords={best_run_params.get('keep_stopwords')}, "
        f"stem={best_run_params.get('stem_words')})"
    )

    df_keyword = pd.DataFrame(
        {
            "Method": ["Ground Truth", config_text, "RapidFuzz"],
            "GT Total Keywords": [
                whoosh_metrics["gt_total_keywords"],
                whoosh_metrics["gt_total_keywords"],
                rp_metrics["gt_total_keywords"],
            ],
            "Keywords Detected": [
                whoosh_metrics["gt_total_keywords"],
                whoosh_metrics["method_total_detected"],
                rp_metrics["method_total_detected"],
            ],
            "True Positives (TP)": ["-", whoosh_metrics["kw_tp"], rp_metrics["kw_tp"]],
            "False Positives (FP)": ["-", whoosh_metrics["kw_fp"], rp_metrics["kw_fp"]],
            "False Negatives (FN)": ["-", whoosh_metrics["kw_fn"], rp_metrics["kw_fn"]],
            "Precision (%)": ["-", whoosh_metrics["kw_precision"], rp_metrics["kw_precision"]],
            "Recall (%)": ["-", whoosh_metrics["kw_recall"], rp_metrics["kw_recall"]],
            "F1-Score": ["-", whoosh_metrics["kw_f1"], rp_metrics["kw_f1"]],
        }
    )

    df_page = pd.DataFrame(
        {
            "Method": ["Ground Truth", config_text, "RapidFuzz"],
            "Total Pages": [
                whoosh_metrics["total_pages_all_docs"],
                whoosh_metrics["total_pages_all_docs"],
                rp_metrics["total_pages_all_docs"],
            ],
            "GT Pages with Keywords": [
                whoosh_metrics["gt_pages_with_keywords_total"],
                whoosh_metrics["gt_pages_with_keywords_total"],
                rp_metrics["gt_pages_with_keywords_total"],
            ],
            "Pages Detected": [
                whoosh_metrics["gt_pages_with_keywords_total"],
                whoosh_metrics["method_pages_detected"],
                rp_metrics["method_pages_detected"],
            ],
            "Page TP": ["-", whoosh_metrics["page_tp"], rp_metrics["page_tp"]],
            "Page FP": ["-", whoosh_metrics["page_fp"], rp_metrics["page_fp"]],
            "Page FN": ["-", whoosh_metrics["page_fn"], rp_metrics["page_fn"]],
            "Page TN": ["-", whoosh_metrics["page_tn"], rp_metrics["page_tn"]],
            "Precision (%)": ["-", whoosh_metrics["page_precision"], rp_metrics["page_precision"]],
            "Recall (%)": ["-", whoosh_metrics["page_recall"], rp_metrics["page_recall"]],
            "F1-Score": ["-", whoosh_metrics["page_f1"], rp_metrics["page_f1"]],
            "Accuracy (%)": ["-", whoosh_metrics["page_accuracy"], rp_metrics["page_accuracy"]],
        }
    )

    LOGGER.info("Keyword-Level: WHOOSH F1=%s, RapidFuzz F1=%s", whoosh_metrics["kw_f1"], rp_metrics["kw_f1"])
    LOGGER.info("Page-Level: WHOOSH F1=%s, RapidFuzz F1=%s", whoosh_metrics["page_f1"], rp_metrics["page_f1"])

    return df_keyword, df_page


# ---------------------------------------------------------------------------
# Excel writing
# ---------------------------------------------------------------------------


def autosize_columns(worksheet: Any, max_width: int = 80, sample_rows: int = 500) -> None:
    """Auto-size worksheet columns with a maximum width."""
    for column in worksheet.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for idx, cell in enumerate(column):
            if idx > sample_rows:
                break
            try:
                if cell.value is not None:
                    max_length = max(max_length, len(str(cell.value)))
            except Exception:
                continue
        worksheet.column_dimensions[column_letter].width = min(max(max_length + 2, 10), max_width)


def style_worksheet(worksheet: Any, freeze_cell: str = "A2") -> None:
    """Apply basic usability styling to an Excel worksheet."""
    worksheet.freeze_panes = freeze_cell
    worksheet.auto_filter.ref = worksheet.dimensions

    try:
        from openpyxl.styles import Font

        for cell in worksheet[1]:
            cell.font = Font(bold=True)
    except Exception:
        # Styling is nice-to-have; do not fail report generation.
        pass


def write_excel_report(
    output_excel_path: Path,
    parameter_results_df: pd.DataFrame,
    best_vs_rp_keyword_df: Optional[pd.DataFrame] = None,
    best_vs_rp_page_df: Optional[pd.DataFrame] = None,
    gt_data: Optional[List[Dict[str, Any]]] = None,
    detailed_comparison_data: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Write all report sheets to Excel and return generated sheet names."""
    output_excel_path.parent.mkdir(parents=True, exist_ok=True)
    generated_sheets: List[str] = []

    with pd.ExcelWriter(output_excel_path, engine="openpyxl") as writer:
        sheet_name = "Parameter Tuning Results"
        parameter_results_df.to_excel(writer, sheet_name=sheet_name, index=False)
        worksheet = writer.sheets[sheet_name]
        autosize_columns(worksheet, max_width=60)
        style_worksheet(worksheet)
        generated_sheets.append(sheet_name)

        if best_vs_rp_keyword_df is not None and best_vs_rp_page_df is not None:
            sheet_name = "Best WHOOSH vs RapidFuzz"
            best_vs_rp_keyword_df.to_excel(writer, sheet_name=sheet_name, startrow=0, index=False)
            startrow = len(best_vs_rp_keyword_df) + 3
            best_vs_rp_page_df.to_excel(writer, sheet_name=sheet_name, startrow=startrow, index=False)
            worksheet = writer.sheets[sheet_name]
            autosize_columns(worksheet, max_width=80)
            worksheet.freeze_panes = "A2"
            generated_sheets.append(sheet_name)

        if gt_data:
            sheet_name = "Ground Truth (LLM)"
            pd.DataFrame(gt_data).to_excel(writer, sheet_name=sheet_name, index=False)
            worksheet = writer.sheets[sheet_name]
            autosize_columns(worksheet, max_width=80)
            style_worksheet(worksheet)
            generated_sheets.append(sheet_name)

        if detailed_comparison_data:
            sheet_name = "Detailed Comparison"
            pd.DataFrame(detailed_comparison_data).to_excel(writer, sheet_name=sheet_name, index=False)
            worksheet = writer.sheets[sheet_name]
            autosize_columns(worksheet, max_width=80)
            style_worksheet(worksheet)
            generated_sheets.append(sheet_name)

    return generated_sheets


# ---------------------------------------------------------------------------
# Main report generation
# ---------------------------------------------------------------------------


def run_to_row(run: SuccessfulRun, metrics: Dict[str, Any], status: str = "success") -> Dict[str, Any]:
    """Build one parameter-results row."""
    return {
        "run_id": run.run_id,
        "run_name": run.run_name,
        "status": status,
        "slop": run.parameters.slop,
        "edit_distance": run.parameters.edit_distance,
        "min_fuzzy_term_length": run.parameters.min_fuzzy_term_length,
        "keep_stopwords": run.parameters.keep_stopwords,
        "stem_words": run.parameters.stem_words,
        "execution_time_sec": round(run.duration_seconds, 2),
        **metrics,
    }


def get_column_order() -> List[str]:
    """Preferred column order for Parameter Tuning Results."""
    return [
        "rank",
        "run_id",
        "run_name",
        "status",
        "slop",
        "edit_distance",
        "min_fuzzy_term_length",
        "keep_stopwords",
        "stem_words",
        "execution_time_sec",
        "files_processed",
        "total_pages_analyzed",
        "gt_total_keywords",
        "method_total_detected",
        "whoosh_total_detected",
        "kw_tp",
        "kw_fp",
        "kw_fn",
        "kw_precision",
        "kw_recall",
        "kw_f1",
        "page_tp",
        "page_fp",
        "page_fn",
        "page_tn",
        "page_precision",
        "page_recall",
        "page_f1",
        "page_accuracy",
        "gt_pages_with_keywords_total",
        "method_pages_detected",
        "total_pages_all_docs",
    ]


def generate_comparison_report(
    execution_summary_path: Path,
    gt_output_dir: Path,
    rp_output_dir: Optional[Path],
    output_excel_path: Path,
    rank_by: str = "kw_f1",
    whoosh_glob: str = "*_keyword_output.json",
    rp_glob: str = "*.json",
    preferred_match_field: str = "group",
) -> Path:
    """
    Generate comprehensive Excel report comparing all successful parameter runs.

    Args:
        execution_summary_path: Path to execution_summary.json.
        gt_output_dir: Directory containing GT JSON files.
        rp_output_dir: Optional directory containing RapidFuzz JSON files.
        output_excel_path: Path for output Excel file.
        rank_by: Metric column used for ranking.
        whoosh_glob: Glob for WHOOSH output files.
        rp_glob: Glob for RapidFuzz output files.
        preferred_match_field: Preferred field in method matches, default group.

    Returns:
        Path to generated Excel file.
    """
    LOGGER.info("=" * 80)
    LOGGER.info("GENERATING FINAL COMPARISON REPORT")
    LOGGER.info("=" * 80)
    LOGGER.info("Execution summary: %s", execution_summary_path)
    LOGGER.info("GT output directory: %s", gt_output_dir)
    LOGGER.info("RapidFuzz output directory: %s", rp_output_dir or "Not provided")
    LOGGER.info("Rank by: %s", rank_by)

    successful_runs = load_successful_runs(execution_summary_path)
    LOGGER.info("Successful runs found: %s", len(successful_runs))

    if not successful_runs:
        raise ValueError("No successful runs found in execution summary.")

    comparison_rows: List[Dict[str, Any]] = []

    for idx, run in enumerate(successful_runs, start=1):
        LOGGER.info("[%s/%s] Processing run %s: %s", idx, len(successful_runs), run.run_id, run.run_name)
        try:
            metrics = compare_whoosh_vs_gt(
                run.output_dir,
                gt_output_dir,
                whoosh_glob=whoosh_glob,
                preferred_match_field=preferred_match_field,
            )
            comparison_rows.append(run_to_row(run, metrics, status="success"))
            LOGGER.info(
                "  Files=%s | Kw F1=%s | Kw Recall=%s | Page F1=%s",
                metrics["files_processed"],
                metrics["kw_f1"],
                metrics["kw_recall"],
                metrics["page_f1"],
            )
        except Exception as exc:
            LOGGER.exception("Error comparing run %s: %s", run.run_id, exc)
            comparison_rows.append(run_to_row(run, create_empty_metrics(), status="comparison_error"))

    df = pd.DataFrame(comparison_rows)
    if df.empty:
        raise ValueError("No comparison rows were generated.")

    if rank_by not in df.columns:
        raise ValueError(f"rank_by column not found: {rank_by}")

    # Ranking: primary selected metric, then keyword recall, then page recall.
    sort_columns = [rank_by]
    for tie_breaker in ("kw_recall", "page_recall", "kw_precision", "page_precision"):
        if tie_breaker not in sort_columns and tie_breaker in df.columns:
            sort_columns.append(tie_breaker)

    df = df.sort_values(sort_columns, ascending=[False] * len(sort_columns)).reset_index(drop=True)
    df.insert(0, "rank", range(1, len(df) + 1))

    column_order = [column for column in get_column_order() if column in df.columns]
    remaining_columns = [column for column in df.columns if column not in column_order]
    df = df[column_order + remaining_columns]

    best_run_row = df.iloc[0]
    best_run_id = int(best_run_row["run_id"])
    best_run = next((run for run in successful_runs if run.run_id == best_run_id), None)

    LOGGER.info("")
    LOGGER.info("Best WHOOSH configuration by %s: Run %s", rank_by, best_run_id)
    LOGGER.info(
        "  slop=%s, edit_distance=%s, min_fuzzy_term_length=%s, keep_stopwords=%s, stem_words=%s",
        best_run_row.get("slop"),
        best_run_row.get("edit_distance"),
        best_run_row.get("min_fuzzy_term_length"),
        best_run_row.get("keep_stopwords"),
        best_run_row.get("stem_words"),
    )
    LOGGER.info(
        "  Kw F1=%s | Kw Recall=%s | Page F1=%s | Page Recall=%s",
        best_run_row.get("kw_f1"),
        best_run_row.get("kw_recall"),
        best_run_row.get("page_f1"),
        best_run_row.get("page_recall"),
    )

    best_vs_rp_keyword_df: Optional[pd.DataFrame] = None
    best_vs_rp_page_df: Optional[pd.DataFrame] = None
    gt_data: List[Dict[str, Any]] = []
    detailed_comparison_data: List[Dict[str, Any]] = []

    if best_run is not None and rp_output_dir is not None and rp_output_dir.exists():
        try:
            best_params = {
                "slop": best_run.parameters.slop,
                "edit_distance": best_run.parameters.edit_distance,
                "min_fuzzy_term_length": best_run.parameters.min_fuzzy_term_length,
                "keep_stopwords": best_run.parameters.keep_stopwords,
                "stem_words": best_run.parameters.stem_words,
            }
            best_vs_rp_keyword_df, best_vs_rp_page_df = generate_best_whoosh_vs_rp_comparison(
                best_whoosh_output_dir=best_run.output_dir,
                rp_output_dir=rp_output_dir,
                gt_output_dir=gt_output_dir,
                best_run_params=best_params,
                whoosh_glob=whoosh_glob,
                rp_glob=rp_glob,
                preferred_match_field=preferred_match_field,
            )
            detailed_comparison_data = extract_detailed_comparison_data(
                best_whoosh_output_dir=best_run.output_dir,
                rp_output_dir=rp_output_dir,
                gt_output_dir=gt_output_dir,
                whoosh_glob=whoosh_glob,
                rp_glob=rp_glob,
                preferred_match_field=preferred_match_field,
            )
        except Exception as exc:
            LOGGER.exception("Failed to generate WHOOSH vs RapidFuzz comparison: %s", exc)
    elif rp_output_dir is not None:
        LOGGER.warning("RapidFuzz output directory not found: %s", rp_output_dir)
    else:
        LOGGER.warning("RapidFuzz output directory not provided; skipping RapidFuzz comparison sheets")

    gt_data = extract_ground_truth_data(gt_output_dir)

    LOGGER.info("Saving Excel report to: %s", output_excel_path)
    generated_sheets = write_excel_report(
        output_excel_path=output_excel_path,
        parameter_results_df=df,
        best_vs_rp_keyword_df=best_vs_rp_keyword_df,
        best_vs_rp_page_df=best_vs_rp_page_df,
        gt_data=gt_data,
        detailed_comparison_data=detailed_comparison_data,
    )

    LOGGER.info("")
    LOGGER.info("=" * 80)
    LOGGER.info("TOP 10 PARAMETER COMBINATIONS by %s", rank_by)
    LOGGER.info("=" * 80)
    for _, row in df.head(10).iterrows():
        LOGGER.info(
            "%s. Run %3s | Slop=%s ED=%s MinFuzz=%s Stop=%s Stem=%s | KwF1=%6.2f | KwRecall=%6.2f | PageF1=%6.2f",
            int(row["rank"]),
            int(row["run_id"]),
            row["slop"],
            row["edit_distance"],
            row["min_fuzzy_term_length"],
            row["keep_stopwords"],
            row["stem_words"],
            float(row["kw_f1"]),
            float(row["kw_recall"]),
            float(row["page_f1"]),
        )

    LOGGER.info("")
    LOGGER.info("=" * 80)
    LOGGER.info("SUMMARY STATISTICS")
    LOGGER.info("=" * 80)
    LOGGER.info("Best Keyword F1-Score: %.2f", float(df["kw_f1"].max()))
    LOGGER.info("Best Keyword Recall: %.2f", float(df["kw_recall"].max()))
    LOGGER.info("Best Page F1-Score: %.2f", float(df["page_f1"].max()))
    LOGGER.info("Best Page Recall: %.2f", float(df["page_recall"].max()))
    LOGGER.info("Average Keyword F1-Score: %.2f", float(df["kw_f1"].mean()))
    LOGGER.info("Average Page F1-Score: %.2f", float(df["page_f1"].mean()))
    LOGGER.info("Average Execution Time: %.2f seconds", float(df["execution_time_sec"].mean()))
    LOGGER.info("Generated sheets: %s", ", ".join(generated_sheets))
    LOGGER.info("Report saved to: %s", output_excel_path)
    LOGGER.info("=" * 80)

    return output_excel_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    configure_logging(verbose=args.verbose, log_file=args.log_file)

    try:
        fixed_paths = load_fixed_paths(args.config)

        gt_output_dir = args.gt_output_dir or fixed_paths.gt_output_dir
        rp_output_dir = args.rp_output_dir if args.rp_output_dir is not None else fixed_paths.rp_output_dir

        if args.output is not None:
            output_excel_path = args.output
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_excel_path = Path(__file__).resolve().parent / f"parameter_tuning_results_{timestamp}.xlsx"

        generate_comparison_report(
            execution_summary_path=args.execution_summary,
            gt_output_dir=gt_output_dir,
            rp_output_dir=rp_output_dir,
            output_excel_path=output_excel_path,
            rank_by=args.rank_by,
            whoosh_glob=args.whoosh_glob,
            rp_glob=args.rp_glob,
            preferred_match_field=args.method_key_field,
        )
        return 0
    except Exception as exc:
        LOGGER.exception("Report generation failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
