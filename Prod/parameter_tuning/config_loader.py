from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


CONFIG_PATH = Path(__file__).resolve().parent / "parameter_tuning.yaml"


@dataclass(frozen=True)
class PathConfig:
    ocr_json: Path
    keywords_json: Path
    gt_output: Path
    rp_output: Path | None
    base_output_dir: Path
    base_logs_dir: Path
    base_index_dir: Path
    execution_summary: Path
    report_output: Path | None


@dataclass(frozen=True)
class ParameterConfig:
    slop: list[int]
    edit_distance: list[int]
    min_fuzzy_term_length: list[int]
    keep_stopwords: list[bool]
    stem_words: bool
    prefixlength: int


@dataclass(frozen=True)
class RuntimeConfig:
    max_workers: int
    resume: bool
    include_cover: bool
    matched_only: bool
    include_file_path: bool
    log_preview_chars: int
    stop_on_error: bool
    output_glob: str
    generate_report_after_tuning: bool


@dataclass(frozen=True)
class ReportConfig:
    rank_by: str
    method_key_field: str
    whoosh_glob: str
    rp_glob: str


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    paths: PathConfig
    parameters: ParameterConfig
    runtime: RuntimeConfig
    report: ReportConfig


def _require_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"YAML section '{key}' must be a mapping")
    return value


def _require_path(data: dict[str, Any], key: str) -> Path:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"YAML path '{key}' is required")
    return Path(str(value))


def _optional_path(data: dict[str, Any], key: str) -> Path | None:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        return None
    return Path(str(value))


def _list_of_ints(data: dict[str, Any], key: str) -> list[int]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"YAML parameter '{key}' must be a non-empty list")
    result: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError(f"YAML parameter '{key}' must contain only integers")
        result.append(item)
    return result


def _list_of_bools(data: dict[str, Any], key: str) -> list[bool]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"YAML parameter '{key}' must be a non-empty list")
    result: list[bool] = []
    for item in value:
        if not isinstance(item, bool):
            raise ValueError(f"YAML parameter '{key}' must contain only booleans")
        result.append(item)
    return result


def _bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"YAML value '{key}' must be true or false")
    return value


def _int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"YAML value '{key}' must be an integer")
    return value


def _str(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if value is None:
        return default
    return str(value)


def load_config(config_path: Path = CONFIG_PATH) -> AppConfig:
    config_path = Path(config_path).resolve()
    with config_path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file)

    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a YAML mapping: {config_path}")

    paths_raw = _require_mapping(raw, "paths")
    parameters_raw = _require_mapping(raw, "parameters")
    runtime_raw = _require_mapping(raw, "runtime")
    report_raw = _require_mapping(raw, "report")

    return AppConfig(
        config_path=config_path,
        paths=PathConfig(
            ocr_json=_require_path(paths_raw, "ocr_json"),
            keywords_json=_require_path(paths_raw, "keywords_json"),
            gt_output=_require_path(paths_raw, "gt_output"),
            rp_output=_optional_path(paths_raw, "rp_output"),
            base_output_dir=_require_path(paths_raw, "base_output_dir"),
            base_logs_dir=_require_path(paths_raw, "base_logs_dir"),
            base_index_dir=_require_path(paths_raw, "base_index_dir"),
            execution_summary=_require_path(paths_raw, "execution_summary"),
            report_output=_optional_path(paths_raw, "report_output"),
        ),
        parameters=ParameterConfig(
            slop=_list_of_ints(parameters_raw, "slop"),
            edit_distance=_list_of_ints(parameters_raw, "edit_distance"),
            min_fuzzy_term_length=_list_of_ints(parameters_raw, "min_fuzzy_term_length"),
            keep_stopwords=_list_of_bools(parameters_raw, "keep_stopwords"),
            stem_words=_bool(parameters_raw, "stem_words", True),
            prefixlength=_int(parameters_raw, "prefixlength", 0),
        ),
        runtime=RuntimeConfig(
            max_workers=_int(runtime_raw, "max_workers", 1),
            resume=_bool(runtime_raw, "resume", False),
            include_cover=_bool(runtime_raw, "include_cover", False),
            matched_only=_bool(runtime_raw, "matched_only", False),
            include_file_path=_bool(runtime_raw, "include_file_path", True),
            log_preview_chars=_int(runtime_raw, "log_preview_chars", 0),
            stop_on_error=_bool(runtime_raw, "stop_on_error", False),
            output_glob=_str(runtime_raw, "output_glob", "*_keyword_output.json"),
            generate_report_after_tuning=_bool(runtime_raw, "generate_report_after_tuning", True),
        ),
        report=ReportConfig(
            rank_by=_str(report_raw, "rank_by", "kw_f1"),
            method_key_field=_str(report_raw, "method_key_field", "group"),
            whoosh_glob=_str(report_raw, "whoosh_glob", "*_keyword_output.json"),
            rp_glob=_str(report_raw, "rp_glob", "*.json"),
        ),
    )
