from __future__ import annotations

import itertools
import json
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from config_loader import AppConfig, load_config
from whoosh_encounter_json_keyword_search import DetectionSettings, process_ocr_path


LOGGER = logging.getLogger(__name__)
RunStatus = Literal["success", "failed", "error", "skipped"]


@dataclass(frozen=True)
class ParameterCombination:
    slop: int
    edit_distance: int
    min_fuzzy_term_length: int
    keep_stopwords: bool


@dataclass(frozen=True)
class RunConfig:
    run_id: int
    run_name: str
    run_folder: str
    combination: ParameterCombination
    settings: DetectionSettings
    ocr_json: Path
    keywords_json: Path
    output_dir: Path
    logs_dir: Path
    index_dir: Path
    output_glob: str


@dataclass
class RunResult:
    run_id: int
    run_name: str
    status: RunStatus
    duration_seconds: float
    parameters: dict[str, Any]
    files_processed: int = 0
    output_dir: str | None = None
    logs_dir: str | None = None
    index_dir: str | None = None
    error: str | None = None


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
        force=True,
    )


def make_run_name(combination: ParameterCombination) -> str:
    return (
        f"slop_{combination.slop}"
        f"_ed_{combination.edit_distance}"
        f"_min_{combination.min_fuzzy_term_length}"
        f"_stop_{combination.keep_stopwords}"
    )


def generate_combinations(config: AppConfig) -> list[ParameterCombination]:
    parameters = config.parameters
    combinations = [
        ParameterCombination(
            slop=slop,
            edit_distance=edit_distance,
            min_fuzzy_term_length=min_fuzzy_term_length,
            keep_stopwords=keep_stopwords,
        )
        for slop, edit_distance, min_fuzzy_term_length, keep_stopwords in itertools.product(
            parameters.slop,
            parameters.edit_distance,
            parameters.min_fuzzy_term_length,
            parameters.keep_stopwords,
        )
    ]
    LOGGER.info("Generated %s parameter combinations", len(combinations))
    return combinations


def create_run_configs(config: AppConfig, combinations: list[ParameterCombination]) -> list[RunConfig]:
    run_configs: list[RunConfig] = []
    for index, combination in enumerate(combinations, start=1):
        run_name = make_run_name(combination)
        run_folder = f"run_{index:03d}_{run_name}"
        settings = DetectionSettings(
            slop=combination.slop,
            edit_distance=combination.edit_distance,
            min_fuzzy_term_length=combination.min_fuzzy_term_length,
            keep_stopwords=combination.keep_stopwords,
            stem_words=config.parameters.stem_words,
            prefixlength=config.parameters.prefixlength,
            include_cover=config.runtime.include_cover,
            matched_only=config.runtime.matched_only,
            include_file_path=config.runtime.include_file_path,
            log_preview_chars=config.runtime.log_preview_chars,
            stop_on_error=config.runtime.stop_on_error,
        )
        run_configs.append(
            RunConfig(
                run_id=index,
                run_name=run_name,
                run_folder=run_folder,
                combination=combination,
                settings=settings,
                ocr_json=config.paths.ocr_json,
                keywords_json=config.paths.keywords_json,
                output_dir=config.paths.base_output_dir / run_folder,
                logs_dir=config.paths.base_logs_dir / run_folder,
                index_dir=config.paths.base_index_dir / run_folder,
                output_glob=config.runtime.output_glob,
            )
        )
    return run_configs


def validate_runtime_paths(config: AppConfig) -> None:
    missing = [
        str(path)
        for path in (config.paths.ocr_json, config.paths.keywords_json)
        if not path.exists()
    ]
    if missing:
        raise FileNotFoundError("Missing required input path(s): " + ", ".join(missing))


def parameters_to_dict(run_config: RunConfig) -> dict[str, Any]:
    return {
        "slop": run_config.combination.slop,
        "edit_distance": run_config.combination.edit_distance,
        "min_fuzzy_term_length": run_config.combination.min_fuzzy_term_length,
        "keep_stopwords": run_config.combination.keep_stopwords,
        "stem_words": run_config.settings.stem_words,
        "prefixlength": run_config.settings.prefixlength,
        "analyzer_mode": (
            "stemming_keep_stopwords"
            if run_config.settings.stem_words and run_config.settings.keep_stopwords
            else "stemming"
            if run_config.settings.stem_words
            else "keep_stopwords"
            if run_config.settings.keep_stopwords
            else "default"
        ),
    }


def completed_result(run_config: RunConfig) -> RunResult | None:
    if not run_config.output_dir.exists():
        return None
    output_files = list(run_config.output_dir.glob(run_config.output_glob))
    if not output_files:
        return None
    return RunResult(
        run_id=run_config.run_id,
        run_name=run_config.run_name,
        status="success",
        duration_seconds=0.0,
        files_processed=len(output_files),
        output_dir=str(run_config.output_dir),
        logs_dir=str(run_config.logs_dir),
        index_dir=str(run_config.index_dir),
        parameters=parameters_to_dict(run_config),
    )


def execute_run(run_config: RunConfig) -> RunResult:
    start_time = perf_counter()
    try:
        batch_summary = process_ocr_path(
            ocr_root=run_config.ocr_json,
            keywords_json_file=run_config.keywords_json,
            output_root=run_config.output_dir,
            logs_root=run_config.logs_dir,
            index_dir=run_config.index_dir,
            settings=run_config.settings,
        )
        duration = perf_counter() - start_time
        output_files = list(run_config.output_dir.glob(run_config.output_glob))
        failed_count = int(batch_summary.get("failed_count", 0) or 0)
        if failed_count:
            return RunResult(
                run_id=run_config.run_id,
                run_name=run_config.run_name,
                status="failed",
                duration_seconds=duration,
                files_processed=len(output_files),
                output_dir=str(run_config.output_dir),
                logs_dir=str(run_config.logs_dir),
                index_dir=str(run_config.index_dir),
                parameters=parameters_to_dict(run_config),
                error=f"{failed_count} OCR file(s) failed. See logs_dir.",
            )

        return RunResult(
            run_id=run_config.run_id,
            run_name=run_config.run_name,
            status="success",
            duration_seconds=duration,
            files_processed=len(output_files),
            output_dir=str(run_config.output_dir),
            logs_dir=str(run_config.logs_dir),
            index_dir=str(run_config.index_dir),
            parameters=parameters_to_dict(run_config),
        )

    except Exception as exc:
        duration = perf_counter() - start_time
        return RunResult(
            run_id=run_config.run_id,
            run_name=run_config.run_name,
            status="error",
            duration_seconds=duration,
            parameters=parameters_to_dict(run_config),
            output_dir=str(run_config.output_dir),
            logs_dir=str(run_config.logs_dir),
            index_dir=str(run_config.index_dir),
            error=str(exc),
        )


def save_execution_summary(
    config: AppConfig,
    results: list[RunResult],
    total_wall_seconds: float,
) -> None:
    sorted_results = sorted(results, key=lambda item: item.run_id)
    successful_runs = sum(1 for result in sorted_results if result.status in {"success", "skipped"})
    failed_runs = sum(1 for result in sorted_results if result.status in {"failed", "error"})
    summary = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "config_path": str(config.config_path),
        "total_runs": len(sorted_results),
        "successful_runs": successful_runs,
        "failed_runs": failed_runs,
        "total_run_duration_seconds": sum(result.duration_seconds for result in sorted_results),
        "total_wall_seconds": total_wall_seconds,
        "results": [asdict(result) for result in sorted_results],
    }

    config.paths.execution_summary.parent.mkdir(parents=True, exist_ok=True)
    config.paths.execution_summary.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    LOGGER.info("Execution summary saved to: %s", config.paths.execution_summary)


def print_header() -> None:
    print("=" * 80)
    print("WHOOSH PARAMETER TUNING")
    print("=" * 80)


def print_configuration(config: AppConfig, total_runs: int, runs_to_execute: int) -> None:
    print("Configuration:")
    print(f"  Config: {config.config_path}")
    print(f"  OCR input: {config.paths.ocr_json}")
    print(f"  Keywords: {config.paths.keywords_json}")
    print(f"  Total parameter combinations: {total_runs}")
    print(f"  Runs to execute: {runs_to_execute}")
    print(f"  Parallel workers: {config.runtime.max_workers}")
    print(f"  Resume mode: {config.runtime.resume}")
    print(f"  Generate report after tuning: {config.runtime.generate_report_after_tuning}")
    print()


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, remaining_seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m"


def progress_bar(completed_count: int, total_runs: int, width: int = 28) -> str:
    if total_runs <= 0:
        return "[" + ("#" * width) + "]"
    filled = int(round(width * completed_count / total_runs))
    filled = max(0, min(width, filled))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def print_progress(
    completed_count: int,
    total_runs: int,
    result: RunResult,
    elapsed_seconds: float,
    success_count: int,
    failed_count: int,
) -> None:
    progress_pct = (completed_count / total_runs) * 100 if total_runs else 100.0
    avg_time_per_run = elapsed_seconds / completed_count if completed_count else 0.0
    remaining_runs = total_runs - completed_count
    eta_seconds = remaining_runs * avg_time_per_run
    status_text = "OK" if result.status in {"success", "skipped"} else "FAIL"
    print(
        f"{progress_bar(completed_count, total_runs)} {progress_pct:6.2f}% | "
        f"Completed {completed_count}/{total_runs} | "
        f"Success {success_count} | Failed {failed_count} | "
        f"Last run {result.run_id:03d} {status_text} "
        f"({result.files_processed} file(s), {format_duration(result.duration_seconds)}) | "
        f"Elapsed {format_duration(elapsed_seconds)} | ETA {format_duration(eta_seconds)}",
        flush=True,
    )


def run_parameter_tuning(config: AppConfig) -> list[RunResult]:
    if config.runtime.max_workers < 1:
        raise ValueError("runtime.max_workers must be >= 1")

    combinations = generate_combinations(config)
    run_configs = create_run_configs(config, combinations)
    total_runs = len(run_configs)

    if config.runtime.resume:
        to_execute = [run_config for run_config in run_configs if completed_result(run_config) is None]
        skipped_results = []
        for run_config in run_configs:
            result = completed_result(run_config)
            if result is not None:
                result.status = "skipped"
                skipped_results.append(result)
    else:
        to_execute = run_configs
        skipped_results = []

    print_configuration(config, total_runs=total_runs, runs_to_execute=len(to_execute))
    validate_runtime_paths(config)

    start_time = perf_counter()
    results: list[RunResult] = list(skipped_results)

    if to_execute:
        with ProcessPoolExecutor(max_workers=config.runtime.max_workers) as executor:
            futures = [executor.submit(execute_run, run_config) for run_config in to_execute]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                success_count = sum(1 for item in results if item.status in {"success", "skipped"})
                failed_count = sum(1 for item in results if item.status in {"failed", "error"})
                print_progress(
                    completed_count=len(results),
                    total_runs=total_runs,
                    result=result,
                    elapsed_seconds=perf_counter() - start_time,
                    success_count=success_count,
                    failed_count=failed_count,
                )

    total_wall_seconds = perf_counter() - start_time
    save_execution_summary(config, results, total_wall_seconds=total_wall_seconds)
    print()
    print("=" * 80)
    print("PARAMETER TUNING COMPLETE")
    print(f"Successful/skipped runs: {sum(1 for item in results if item.status in {'success', 'skipped'})}/{len(results)}")
    print(f"Failed runs: {sum(1 for item in results if item.status in {'failed', 'error'})}/{len(results)}")
    print(f"Execution summary: {config.paths.execution_summary}")
    print("=" * 80)
    return sorted(results, key=lambda item: item.run_id)


def main() -> int:
    configure_logging()
    print_header()
    config = load_config()
    results = run_parameter_tuning(config)

    failed = sum(1 for result in results if result.status in {"failed", "error"})
    if failed:
        LOGGER.error(
            "Skipping report generation because %s tuning run(s) failed. "
            "Fix failures before creating a business-facing report.",
            failed,
        )
        return 1

    if config.runtime.generate_report_after_tuning:
        from generate_final_comparison_report import generate_report_from_config

        generate_report_from_config(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
