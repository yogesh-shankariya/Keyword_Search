"""
WHOOSH Parameter Tuning Orchestrator.

Runs the WHOOSH encounter keyword-search script across every parameter
combination from a JSON parameter grid. Each combination writes to its own
output/log/index folder, so runs can be compared later against ground truth.

Expected parameter grid shape:

{
  "CONFIG_SLOP": [1, 2, 3, 4, 5],
  "CONFIG_EDIT_DISTANCE": [0, 1],
  "CONFIG_MIN_FUZZY_TERM_LENGTH": [3, 4, 5],
  "CONFIG_KEEP_STOPWORDS": [true, false],
  "CONFIG_STEM_WORDS": false,
  "FIXED_PATHS": {
    "ocr_json": "path/to/ocr.json",
    "keywords_json": "path/to/keywords.json",
    "base_output_dir": "outputs/parameter_runs",
    "base_logs_dir": "logs/parameter_runs",
    "base_index_dir": "indexes/parameter_runs"
  }
}

Usage:
    python parameter_tuning_orchestrator.py

Common options:
    python parameter_tuning_orchestrator.py --max-workers 8 --resume
    python parameter_tuning_orchestrator.py --config config/parameter_grid.json
    python parameter_tuning_orchestrator.py --whoosh-script whoosh_encounter_json_keyword_search.py
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Literal, Sequence

LOGGER = logging.getLogger(__name__)

RunStatus = Literal["success", "failed", "timeout", "error", "skipped"]


@dataclass(frozen=True)
class ParameterCombination:
    """One WHOOSH parameter combination."""

    slop: int
    edit_distance: int
    min_fuzzy_term_length: int
    keep_stopwords: bool


@dataclass(frozen=True)
class FixedPaths:
    """Input and base output paths loaded from the parameter grid."""

    ocr_json: Path
    keywords_json: Path
    base_output_dir: Path
    base_logs_dir: Path
    base_index_dir: Path


@dataclass(frozen=True)
class ParameterGrid:
    """Validated parameter grid."""

    slop_values: list[int]
    edit_distance_values: list[int]
    min_fuzzy_term_length_values: list[int]
    keep_stopwords_values: list[bool]
    stem_words: bool
    fixed_paths: FixedPaths


@dataclass(frozen=True)
class RunConfig:
    """Configuration for a single subprocess execution."""

    run_id: int
    run_name: str
    run_folder: str
    combination: ParameterCombination
    stem_words: bool
    ocr_json: Path
    keywords_json: Path
    output_dir: Path
    logs_dir: Path
    index_dir: Path
    whoosh_script: Path
    timeout_seconds: int
    output_glob: str = "*_keyword_output.json"


@dataclass
class RunResult:
    """Serializable result for one run."""

    run_id: int
    run_name: str
    status: RunStatus
    duration_seconds: float
    parameters: dict[str, Any]
    files_processed: int = 0
    output_dir: str | None = None
    error: str | None = None
    command: list[str] | None = None


def configure_logging(log_file: Path | None = None, verbose: bool = False) -> None:
    """Configure console and optional file logging."""

    log_level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )


def load_json(path: Path) -> dict[str, Any]:
    """Load JSON from disk and return a dictionary."""

    if not path.exists():
        raise FileNotFoundError(f"Config file does not exist: {path}")

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in config file: {path}")

    return data


def _require_key(data: dict[str, Any], key: str) -> Any:
    """Return required key from a dictionary with a clear error message."""

    if key not in data:
        raise KeyError(f"Missing required key in parameter grid: {key}")
    return data[key]


def _as_list_of_ints(value: Any, key: str) -> list[int]:
    """Validate and normalize a list of integers."""

    if not isinstance(value, list) or not value:
        raise ValueError(f"{key} must be a non-empty list of integers")

    normalized: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError(f"{key} must contain only integers. Invalid value: {item!r}")
        normalized.append(item)

    return normalized


def _as_list_of_bools(value: Any, key: str) -> list[bool]:
    """Validate and normalize a list of booleans."""

    if not isinstance(value, list) or not value:
        raise ValueError(f"{key} must be a non-empty list of booleans")

    normalized: list[bool] = []
    for item in value:
        if not isinstance(item, bool):
            raise ValueError(f"{key} must contain only booleans. Invalid value: {item!r}")
        normalized.append(item)

    return normalized


def parse_parameter_grid(config_path: Path) -> ParameterGrid:
    """Load and validate the WHOOSH tuning parameter grid."""

    raw = load_json(config_path)
    paths_raw = _require_key(raw, "FIXED_PATHS")

    if not isinstance(paths_raw, dict):
        raise ValueError("FIXED_PATHS must be a JSON object")

    fixed_paths = FixedPaths(
        ocr_json=Path(_require_key(paths_raw, "ocr_json")),
        keywords_json=Path(_require_key(paths_raw, "keywords_json")),
        base_output_dir=Path(_require_key(paths_raw, "base_output_dir")),
        base_logs_dir=Path(_require_key(paths_raw, "base_logs_dir")),
        base_index_dir=Path(_require_key(paths_raw, "base_index_dir")),
    )

    stem_words = _require_key(raw, "CONFIG_STEM_WORDS")
    if not isinstance(stem_words, bool):
        raise ValueError("CONFIG_STEM_WORDS must be true or false")

    return ParameterGrid(
        slop_values=_as_list_of_ints(_require_key(raw, "CONFIG_SLOP"), "CONFIG_SLOP"),
        edit_distance_values=_as_list_of_ints(
            _require_key(raw, "CONFIG_EDIT_DISTANCE"), "CONFIG_EDIT_DISTANCE"
        ),
        min_fuzzy_term_length_values=_as_list_of_ints(
            _require_key(raw, "CONFIG_MIN_FUZZY_TERM_LENGTH"),
            "CONFIG_MIN_FUZZY_TERM_LENGTH",
        ),
        keep_stopwords_values=_as_list_of_bools(
            _require_key(raw, "CONFIG_KEEP_STOPWORDS"), "CONFIG_KEEP_STOPWORDS"
        ),
        stem_words=stem_words,
        fixed_paths=fixed_paths,
    )


def generate_combinations(grid: ParameterGrid) -> list[ParameterCombination]:
    """Generate all parameter combinations from the validated grid."""

    combinations = [
        ParameterCombination(
            slop=slop,
            edit_distance=edit_distance,
            min_fuzzy_term_length=min_fuzzy_term_length,
            keep_stopwords=keep_stopwords,
        )
        for slop, edit_distance, min_fuzzy_term_length, keep_stopwords in itertools.product(
            grid.slop_values,
            grid.edit_distance_values,
            grid.min_fuzzy_term_length_values,
            grid.keep_stopwords_values,
        )
    ]

    LOGGER.info("Generated %s parameter combinations", len(combinations))
    return combinations


def make_run_name(combination: ParameterCombination) -> str:
    """Create a readable run name from one parameter combination."""

    return (
        f"slop_{combination.slop}"
        f"_ed_{combination.edit_distance}"
        f"_min_{combination.min_fuzzy_term_length}"
        f"_stop_{combination.keep_stopwords}"
    )


def create_run_configs(
    combinations: Sequence[ParameterCombination],
    grid: ParameterGrid,
    whoosh_script: Path,
    timeout_seconds: int,
    output_glob: str,
) -> list[RunConfig]:
    """Create one run configuration per parameter combination."""

    run_configs: list[RunConfig] = []

    for index, combination in enumerate(combinations, start=1):
        run_name = make_run_name(combination)
        run_folder = f"run_{index:03d}_{run_name}"

        run_configs.append(
            RunConfig(
                run_id=index,
                run_name=run_name,
                run_folder=run_folder,
                combination=combination,
                stem_words=grid.stem_words,
                ocr_json=grid.fixed_paths.ocr_json,
                keywords_json=grid.fixed_paths.keywords_json,
                output_dir=grid.fixed_paths.base_output_dir / run_folder,
                logs_dir=grid.fixed_paths.base_logs_dir / run_folder,
                index_dir=grid.fixed_paths.base_index_dir / run_folder,
                whoosh_script=whoosh_script,
                timeout_seconds=timeout_seconds,
                output_glob=output_glob,
            )
        )

    return run_configs


def validate_runtime_paths(run_configs: Sequence[RunConfig]) -> None:
    """Validate common input paths before parallel execution starts."""

    if not run_configs:
        return

    first = run_configs[0]
    required_files = [first.whoosh_script, first.ocr_json, first.keywords_json]

    missing = [str(path) for path in required_files if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing required file(s): " + ", ".join(missing))


def run_already_completed(run_config: RunConfig) -> bool:
    """Return True when expected output JSON files already exist for a run."""

    if not run_config.output_dir.exists():
        return False

    output_files = list(run_config.output_dir.glob(run_config.output_glob))
    if output_files:
        LOGGER.info("Run %s already completed; skipping", run_config.run_id)
        return True

    return False


def filter_resume_runs(run_configs: Sequence[RunConfig], resume: bool) -> list[RunConfig]:
    """Skip completed runs when resume mode is enabled."""

    if not resume:
        return list(run_configs)

    LOGGER.info("Resume mode enabled; checking completed runs")
    return [run_config for run_config in run_configs if not run_already_completed(run_config)]


def build_whoosh_command(run_config: RunConfig) -> list[str]:
    """Build the subprocess command for one WHOOSH run."""

    command = [
        sys.executable,
        str(run_config.whoosh_script),
        "--ocr-json",
        str(run_config.ocr_json),
        "--keywords-json",
        str(run_config.keywords_json),
        "--output",
        str(run_config.output_dir),
        "--logs-dir",
        str(run_config.logs_dir),
        "--index-dir",
        str(run_config.index_dir),
        "--slop",
        str(run_config.combination.slop),
        "--edit-distance",
        str(run_config.combination.edit_distance),
        "--min-fuzzy-term-length",
        str(run_config.combination.min_fuzzy_term_length),
        "--log-preview-chars",
        "0",
    ]

    command.append("--keep-stopwords" if run_config.combination.keep_stopwords else "--no-keep-stopwords")
    command.append("--stem-words" if run_config.stem_words else "--no-stem-words")

    return command


def parameters_to_dict(run_config: RunConfig) -> dict[str, Any]:
    """Return run parameters in a flat dictionary for JSON summary output."""

    return {
        "slop": run_config.combination.slop,
        "edit_distance": run_config.combination.edit_distance,
        "min_fuzzy_term_length": run_config.combination.min_fuzzy_term_length,
        "keep_stopwords": run_config.combination.keep_stopwords,
        "stem_words": run_config.stem_words,
    }


def execute_whoosh_run(run_config: RunConfig) -> RunResult:
    """Execute WHOOSH keyword detection for one parameter combination."""

    start_time = perf_counter()
    command = build_whoosh_command(run_config)

    LOGGER.info("Starting run %s: %s", run_config.run_id, run_config.run_name)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=run_config.timeout_seconds,
            check=False,
        )
        duration = perf_counter() - start_time

        if result.returncode == 0:
            output_files = list(run_config.output_dir.glob(run_config.output_glob))
            LOGGER.info(
                "Run %s completed in %.2fs; files processed: %s",
                run_config.run_id,
                duration,
                len(output_files),
            )
            return RunResult(
                run_id=run_config.run_id,
                run_name=run_config.run_name,
                status="success",
                duration_seconds=duration,
                files_processed=len(output_files),
                output_dir=str(run_config.output_dir),
                parameters=parameters_to_dict(run_config),
                command=command,
            )

        error_text = (result.stderr or result.stdout or "Unknown subprocess failure").strip()
        LOGGER.error(
            "Run %s failed with return code %s. Error preview: %s",
            run_config.run_id,
            result.returncode,
            error_text[:500],
        )
        return RunResult(
            run_id=run_config.run_id,
            run_name=run_config.run_name,
            status="failed",
            duration_seconds=duration,
            error=error_text[:4000],
            parameters=parameters_to_dict(run_config),
            command=command,
        )

    except subprocess.TimeoutExpired as exc:
        duration = perf_counter() - start_time
        LOGGER.error("Run %s timed out after %.2fs", run_config.run_id, duration)
        return RunResult(
            run_id=run_config.run_id,
            run_name=run_config.run_name,
            status="timeout",
            duration_seconds=duration,
            error=f"Execution exceeded timeout of {run_config.timeout_seconds} seconds: {exc}",
            parameters=parameters_to_dict(run_config),
            command=command,
        )

    except Exception as exc:  # noqa: BLE001 - return error details to summary JSON
        duration = perf_counter() - start_time
        LOGGER.exception("Run %s raised an exception", run_config.run_id)
        return RunResult(
            run_id=run_config.run_id,
            run_name=run_config.run_name,
            status="error",
            duration_seconds=duration,
            error=str(exc),
            parameters=parameters_to_dict(run_config),
            command=command,
        )


def result_to_json(result: RunResult) -> dict[str, Any]:
    """Convert RunResult dataclass to JSON-safe dictionary."""

    return asdict(result)


def build_execution_summary(results: Sequence[RunResult], total_wall_seconds: float) -> dict[str, Any]:
    """Build final JSON summary for all completed runs."""

    sorted_results = sorted(results, key=lambda item: item.run_id)
    successful_runs = sum(1 for result in sorted_results if result.status == "success")
    failed_runs = sum(1 for result in sorted_results if result.status in {"failed", "timeout", "error"})

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total_runs": len(sorted_results),
        "successful_runs": successful_runs,
        "failed_runs": failed_runs,
        "total_run_duration_seconds": sum(result.duration_seconds for result in sorted_results),
        "total_wall_seconds": total_wall_seconds,
        "results": [result_to_json(result) for result in sorted_results],
    }


def save_execution_summary(results: Sequence[RunResult], output_path: Path, total_wall_seconds: float) -> None:
    """Save execution summary to JSON."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = build_execution_summary(results, total_wall_seconds=total_wall_seconds)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(summary, file, indent=2, ensure_ascii=False)

    LOGGER.info("Execution summary saved to: %s", output_path)


def print_header() -> None:
    """Print startup banner."""

    print("=" * 80)
    print("WHOOSH PARAMETER TUNING ORCHESTRATOR")
    print("=" * 80)
    print()


def print_configuration(total_runs: int, runs_to_execute: int, max_workers: int, resume: bool) -> None:
    """Print human-readable run configuration."""

    print("Configuration:")
    print(f"  Total parameter combinations: {total_runs}")
    print(f"  Runs to execute: {runs_to_execute}")
    print(f"  Parallel workers: {max_workers}")
    print(f"  Resume mode: {resume}")
    print()


def print_progress(completed_count: int, total_runs: int, result: RunResult, elapsed_seconds: float) -> None:
    """Print one-line progress update after each completed run."""

    progress_pct = (completed_count / total_runs) * 100 if total_runs else 100.0
    avg_time_per_run = elapsed_seconds / completed_count if completed_count else 0.0
    remaining_runs = total_runs - completed_count
    eta_seconds = remaining_runs * avg_time_per_run
    status_symbol = "✓" if result.status == "success" else "✗"

    print(
        f"{status_symbol} [{completed_count}/{total_runs}] ({progress_pct:.1f}%) | "
        f"Run {result.run_id}: {result.status.upper()} | "
        f"Duration: {result.duration_seconds:.1f}s | "
        f"ETA: {eta_seconds / 60:.1f}m"
    )


def print_final_summary(results: Sequence[RunResult], total_wall_seconds: float, summary_path: Path) -> None:
    """Print final execution summary."""

    successful = sum(1 for result in results if result.status == "success")
    failed = len(results) - successful
    average_seconds = total_wall_seconds / len(results) if results else 0.0

    print()
    print("=" * 80)
    print("PARAMETER TUNING COMPLETE")
    print("=" * 80)
    print(f"Total wall duration: {total_wall_seconds / 60:.1f} minutes ({total_wall_seconds / 3600:.2f} hours)")
    print(f"Successful runs: {successful}/{len(results)}")
    print(f"Failed runs: {failed}/{len(results)}")
    print(f"Average wall time per run: {average_seconds:.1f} seconds")
    print(f"Execution summary: {summary_path}")
    print()
    print("Next step: run generate_final_comparison_report.py to compare outputs against GT.")
    print("=" * 80)


def run_orchestrator(
    run_configs: Sequence[RunConfig],
    max_workers: int,
    summary_output: Path,
) -> list[RunResult]:
    """Run all configurations in parallel and save a summary."""

    if max_workers < 1:
        raise ValueError("max_workers must be >= 1")

    start_time = perf_counter()
    results: list[RunResult] = []

    print(f"Starting parallel execution with {max_workers} worker(s)...")
    print()

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(execute_whoosh_run, run_config) for run_config in run_configs]

        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            elapsed = perf_counter() - start_time
            print_progress(
                completed_count=len(results),
                total_runs=len(run_configs),
                result=result,
                elapsed_seconds=elapsed,
            )

    total_wall_seconds = perf_counter() - start_time
    save_execution_summary(results, summary_output, total_wall_seconds=total_wall_seconds)
    print_final_summary(results, total_wall_seconds=total_wall_seconds, summary_path=summary_output)

    return sorted(results, key=lambda item: item.run_id)


def resolve_default_config_path() -> Path:
    """Return default config path relative to this module."""

    return Path(__file__).resolve().parent / "config" / "parameter_grid.json"


def resolve_default_whoosh_script_path() -> Path:
    """Return default WHOOSH script path relative to this module.

    The original project layout keeps this orchestrator inside a subfolder and
    keeps whoosh_encounter_json_keyword_search.py one level above it.
    """

    return Path(__file__).resolve().parent.parent / "whoosh_encounter_json_keyword_search.py"


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(description="WHOOSH Parameter Tuning Orchestrator")
    parser.add_argument(
        "--config",
        type=Path,
        default=resolve_default_config_path(),
        help="Path to parameter_grid.json",
    )
    parser.add_argument(
        "--whoosh-script",
        type=Path,
        default=resolve_default_whoosh_script_path(),
        help="Path to whoosh_encounter_json_keyword_search.py",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=30,
        help="Number of parallel workers",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=3600,
        help="Timeout for each WHOOSH subprocess run",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip already completed runs",
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        default=Path("execution_summary.json"),
        help="Where to save execution summary JSON",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path("parameter_tuning_orchestrator.log"),
        help="Where to save orchestrator logs. Use --no-log-file to disable file logging.",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="Disable file logging",
    )
    parser.add_argument(
        "--output-glob",
        default="*_keyword_output.json",
        help="Glob pattern used to count/validate run output files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the run count and exit without executing subprocesses",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    """CLI entrypoint."""

    args = parse_args(argv)
    configure_logging(log_file=None if args.no_log_file else args.log_file, verbose=args.verbose)

    print_header()

    config_path = args.config.resolve()
    whoosh_script = args.whoosh_script.resolve()
    summary_output = args.summary_output.resolve()

    LOGGER.info("Loading parameter grid from: %s", config_path)
    grid = parse_parameter_grid(config_path)

    combinations = generate_combinations(grid)
    run_configs = create_run_configs(
        combinations=combinations,
        grid=grid,
        whoosh_script=whoosh_script,
        timeout_seconds=args.timeout_seconds,
        output_glob=args.output_glob,
    )

    total_runs = len(run_configs)
    run_configs = filter_resume_runs(run_configs, resume=args.resume)

    print_configuration(
        total_runs=total_runs,
        runs_to_execute=len(run_configs),
        max_workers=args.max_workers,
        resume=args.resume,
    )

    if args.dry_run:
        LOGGER.info("Dry run enabled; no subprocesses executed")
        return 0

    if not run_configs:
        LOGGER.info("No runs to execute")
        return 0

    validate_runtime_paths(run_configs)

    results = run_orchestrator(
        run_configs=run_configs,
        max_workers=args.max_workers,
        summary_output=summary_output,
    )

    failed = sum(1 for result in results if result.status != "success")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
