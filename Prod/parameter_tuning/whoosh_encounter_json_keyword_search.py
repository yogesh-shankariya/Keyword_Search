from __future__ import annotations

import csv
import json
import os
import re
import shutil
import tempfile
import traceback
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

try:
    from whoosh import index, query
    from whoosh.analysis import LowercaseFilter, RegexTokenizer, StemmingAnalyzer
    from whoosh.fields import ID, NUMERIC, STORED, Schema, TEXT
    from whoosh.query import spans
except ModuleNotFoundError as exc:
    if exc.name != "whoosh":
        raise
    raise SystemExit("Whoosh is not installed. Install it first using: pip install Whoosh") from exc


PAGE_TAG_RE = re.compile(
    r"<ocr_service_page_start>\s*(\d+)\s*<ocr_service_page_start>",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class DetectionSettings:
    slop: int
    edit_distance: int
    min_fuzzy_term_length: int
    keep_stopwords: bool
    stem_words: bool = True
    prefixlength: int = 0
    include_cover: bool = False
    matched_only: bool = False
    include_file_path: bool = True
    log_preview_chars: int = 0
    stop_on_error: bool = False
    max_file_retries: int = 3


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def elapsed_ms(start_time: float) -> float:
    return round((perf_counter() - start_time) * 1000, 3)


def preview_text(value: Any, max_chars: int = 300) -> str:
    if max_chars <= 0:
        return ""

    text = str(value or "").replace("\n", "\\n")
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}..."


def format_log_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    return str(value)


class TextDebugLogger:
    def __init__(self, log_path: Path, file_id: str, input_path: Path):
        self.log_path = Path(log_path)
        self.file_id = file_id
        self.input_path = str(input_path)
        self.started_at = perf_counter()

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.log_path.open("w", encoding="utf-8")
        self.log("log_opened", input_path=self.input_path, log_path=str(self.log_path))

    def log(self, event: str, **payload: Any) -> None:
        self.file.write(
            f"[{utc_timestamp()}] "
            f"file_id={self.file_id} "
            f"event={event} "
            f"elapsed_ms={elapsed_ms(self.started_at)}\n"
        )

        for key, value in payload.items():
            formatted_value = format_log_value(value)
            if "\n" in formatted_value:
                self.file.write(f"  {key}:\n")
                for line in formatted_value.splitlines():
                    self.file.write(f"    {line}\n")
            else:
                self.file.write(f"  {key}: {formatted_value}\n")

        self.file.write("\n")
        self.file.flush()

    def close(self) -> None:
        if not self.file.closed:
            self.log("log_closed")
            self.file.close()


def log_event(logger: TextDebugLogger | None, event: str, **payload: Any) -> None:
    if logger is not None:
        logger.log(event, **payload)


def read_json_file(file_path: Path) -> Any:
    with Path(file_path).open("r", encoding="utf-8") as file:
        return json.load(file)


def flatten_keyword_json(data: Any, parent_key: str = "") -> list[dict[str, str]]:
    keyword_records: list[dict[str, str]] = []

    if isinstance(data, dict):
        for key, value in data.items():
            full_key = f"{parent_key}.{key}" if parent_key else str(key)

            if isinstance(value, dict):
                keyword_records.extend(flatten_keyword_json(value, full_key))
            elif isinstance(value, list):
                for item in value:
                    if item is None:
                        continue
                    variant = str(item).strip()
                    if variant:
                        keyword_records.append({"group": full_key, "variant": variant})
            elif isinstance(value, str):
                variant = value.strip()
                if variant:
                    keyword_records.append({"group": full_key, "variant": variant})

    return keyword_records


def split_encounter_text_into_pages(encounter_text: str) -> list[dict[str, Any]]:
    if not encounter_text:
        return []

    matches = list(PAGE_TAG_RE.finditer(encounter_text))
    if not matches:
        return [{"page_number": 1, "content": encounter_text.strip()}]

    pages: list[dict[str, Any]] = []
    for idx, match in enumerate(matches):
        page_number = int(match.group(1))
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(encounter_text)
        pages.append({"page_number": page_number, "content": encounter_text[start:end].strip()})

    return pages


def extract_pages_from_ocr_json(ocr_json: Any, include_cover: bool = False) -> list[dict[str, Any]]:
    if not isinstance(ocr_json, dict):
        raise ValueError("OCR JSON must be an object.")

    page_records: list[dict[str, Any]] = []

    if include_cover and isinstance(ocr_json.get("cover"), dict):
        cover_text = ocr_json["cover"].get("text", "") or ""
        cover_file_path = ocr_json["cover"].get("file_path", "") or ""
        for page in split_encounter_text_into_pages(cover_text):
            page_records.append(
                {
                    "encounter_id": "cover",
                    "file_path": cover_file_path,
                    "page_number": page["page_number"],
                    "content": page["content"],
                }
            )

    encounters = ocr_json.get("encounters", [])
    if not isinstance(encounters, list):
        raise ValueError("OCR JSON must contain 'encounters' as a list.")

    for encounter_index, encounter in enumerate(encounters, start=1):
        if not isinstance(encounter, dict):
            continue
        encounter_id = str(
            encounter.get("id")
            or encounter.get("encounter_id")
            or encounter.get("encounterId")
            or encounter_index
        )
        encounter_text = encounter.get("text", "") or ""
        file_path = encounter.get("file_path", "") or encounter.get("filepath", "") or ""
        for page in split_encounter_text_into_pages(encounter_text):
            page_records.append(
                {
                    "encounter_id": encounter_id,
                    "file_path": file_path,
                    "page_number": page["page_number"],
                    "content": page["content"],
                }
            )

    return page_records


def analyzer_mode(keep_stopwords: bool = False, stem_words: bool = False) -> str:
    if stem_words:
        if keep_stopwords:
            return "stemming_keep_stopwords"
        return "stemming"

    if keep_stopwords:
        return "keep_stopwords"

    return "default"


def create_or_replace_index(index_dir: Path, keep_stopwords: bool = False, stem_words: bool = False):
    if os.path.exists(index_dir):
        shutil.rmtree(index_dir)
    os.makedirs(index_dir, exist_ok=True)

    if stem_words:
        analyzer = (
            StemmingAnalyzer(minsize=1, stoplist=None)
            if keep_stopwords
            else StemmingAnalyzer(minsize=1)
        )
        content_field = TEXT(stored=True, phrase=True, analyzer=analyzer)
    elif keep_stopwords:
        analyzer = RegexTokenizer() | LowercaseFilter()
        content_field = TEXT(stored=True, phrase=True, analyzer=analyzer)
    else:
        content_field = TEXT(stored=True, phrase=True)

    schema = Schema(
        doc_id=ID(stored=True, unique=True),
        encounter_id=ID(stored=True),
        file_path=STORED,
        page_number=NUMERIC(stored=True),
        content=content_field,
    )
    return index.create_in(index_dir, schema)


def index_pages(
    ix: Any,
    page_records: list[dict[str, Any]],
    logger: TextDebugLogger | None = None,
    log_preview_chars: int = 300,
) -> None:
    writer = ix.writer()

    for idx, page in enumerate(page_records, start=1):
        encounter_id = str(page["encounter_id"])
        page_number = int(page["page_number"])
        doc_id = f"{encounter_id}::page_{page_number}::{idx}"
        page["doc_id"] = doc_id

        writer.add_document(
            doc_id=doc_id,
            encounter_id=encounter_id,
            file_path=page.get("file_path", ""),
            page_number=page_number,
            content=page.get("content", ""),
        )

        if log_preview_chars > 0:
            log_event(
                logger,
                "index_page_added",
                doc_id=doc_id,
                encounter_id=encounter_id,
                page_number=page_number,
                file_path=page.get("file_path", ""),
                content_chars=len(page.get("content", "") or ""),
                content_preview=preview_text(page.get("content", ""), log_preview_chars),
            )

    writer.commit()
    log_event(logger, "index_commit_finished", indexed_page_count=len(page_records))


def analyze_keyword_terms(schema: Any, fieldname: str, keyword: str) -> list[str]:
    field = schema[fieldname]
    terms = list(field.process_text(keyword, mode="query"))
    seen: set[str] = set()
    unique_terms: list[str] = []

    for term in terms:
        if term not in seen:
            unique_terms.append(term)
            seen.add(term)

    return unique_terms


def build_fuzzy_unordered_near_all_query(
    schema: Any,
    ixreader: Any,
    fieldname: str,
    keyword: str,
    slop: int,
    edit_distance: int,
    prefixlength: int,
    min_fuzzy_term_length: int,
    log_query_details: bool = False,
    logger: TextDebugLogger | None = None,
):
    terms = analyze_keyword_terms(schema, fieldname, keyword)
    if log_query_details:
        log_event(
            logger,
            "keyword_analyzed",
            keyword=keyword,
            analyzed_terms=terms,
            analyzed_term_count=len(terms),
            min_fuzzy_term_length=min_fuzzy_term_length,
        )

    if not terms:
        log_event(logger, "keyword_query_null", keyword=keyword, reason="no_analyzed_terms")
        return query.NullQuery

    field = schema[fieldname]
    fuzzy_terms = []
    for term in terms:
        if min_fuzzy_term_length > 0 and len(term) < min_fuzzy_term_length:
            fuzzy_terms.append(query.Term(fieldname, term))
            if log_query_details:
                log_event(
                    logger,
                    "exact_term_used",
                    keyword=keyword,
                    term=term,
                    term_length=len(term),
                    min_fuzzy_term_length=min_fuzzy_term_length,
                    reason="short_term",
                )
            continue

        expanded_terms = sorted(
            {
                field.from_bytes(candidate) if isinstance(candidate, bytes) else candidate
                for candidate in ixreader.terms_within(
                    fieldname,
                    term,
                    edit_distance,
                    prefix=prefixlength,
                )
            }
        )
        if log_query_details:
            log_event(
                logger,
                "fuzzy_term_expanded",
                keyword=keyword,
                term=term,
                edit_distance=edit_distance,
                prefixlength=prefixlength,
                min_fuzzy_term_length=min_fuzzy_term_length,
                expanded_term_count=len(expanded_terms),
            )

        if not expanded_terms:
            log_event(
                logger,
                "keyword_query_null",
                keyword=keyword,
                reason="no_expanded_terms",
                missing_term=term,
            )
            return query.NullQuery

        term_queries = [query.Term(fieldname, candidate) for candidate in expanded_terms]
        fuzzy_terms.append(term_queries[0] if len(term_queries) == 1 else spans.SpanOr(term_queries))

    if len(fuzzy_terms) == 1:
        if log_query_details:
            log_event(
                logger,
                "whoosh_query_built",
                keyword=keyword,
                query_type=type(fuzzy_terms[0]).__name__,
                analyzed_term_count=len(terms),
            )
        return fuzzy_terms[0]

    near_query = spans.SpanNear2(fuzzy_terms, slop=slop, ordered=False, mindist=1)
    if log_query_details:
        log_event(
            logger,
            "whoosh_query_built",
            keyword=keyword,
            query_type=type(near_query).__name__,
            analyzed_term_count=len(terms),
            slop=slop,
            ordered=False,
            mindist=1,
        )
    return near_query


def run_keyword_detection(
    ocr_json_file: Path,
    keywords_json_file: Path,
    settings: DetectionSettings,
    index_dir: Path | None = None,
    logger: TextDebugLogger | None = None,
) -> list[dict[str, Any]]:
    run_started_at = perf_counter()
    log_event(
        logger,
        "keyword_detection_started",
        ocr_json_file=str(ocr_json_file),
        keywords_json_file=str(keywords_json_file),
        slop=settings.slop,
        edit_distance=settings.edit_distance,
        prefixlength=settings.prefixlength,
        min_fuzzy_term_length=settings.min_fuzzy_term_length,
        index_dir=str(index_dir) if index_dir else None,
        keep_stopwords=settings.keep_stopwords,
        stem_words=settings.stem_words,
        analyzer_mode=analyzer_mode(settings.keep_stopwords, settings.stem_words),
        include_cover=settings.include_cover,
        include_empty_pages=not settings.matched_only,
        include_file_path=settings.include_file_path,
    )

    step_started_at = perf_counter()
    ocr_json = read_json_file(ocr_json_file)
    log_event(
        logger,
        "ocr_json_read_finished",
        duration_ms=elapsed_ms(step_started_at),
        top_level_keys=list(ocr_json.keys()) if isinstance(ocr_json, dict) else [],
        encounter_count=len(ocr_json.get("encounters", [])) if isinstance(ocr_json, dict) else 0,
        has_cover=isinstance(ocr_json.get("cover"), dict) if isinstance(ocr_json, dict) else False,
    )

    step_started_at = perf_counter()
    keywords_json = read_json_file(keywords_json_file)
    log_event(
        logger,
        "keywords_json_read_finished",
        duration_ms=elapsed_ms(step_started_at),
        top_level_keys=list(keywords_json.keys()) if isinstance(keywords_json, dict) else [],
    )

    step_started_at = perf_counter()
    page_records = extract_pages_from_ocr_json(ocr_json, include_cover=settings.include_cover)
    pages_extracted_payload: dict[str, Any] = {
        "duration_ms": elapsed_ms(step_started_at),
        "page_count": len(page_records),
        "non_empty_page_count": sum(1 for page in page_records if page.get("content", "")),
        "total_content_chars": sum(len(page.get("content", "") or "") for page in page_records),
        "unique_encounter_count": len({str(page.get("encounter_id", "")) for page in page_records}),
    }
    if settings.log_preview_chars > 0:
        pages_extracted_payload["pages"] = [
            {
                "encounter_id": str(page.get("encounter_id", "")),
                "file_path": page.get("file_path", ""),
                "page_number": page.get("page_number"),
                "content_chars": len(page.get("content", "") or ""),
                "content_preview": preview_text(page.get("content", ""), settings.log_preview_chars),
            }
            for page in page_records
        ]
    log_event(logger, "pages_extracted", **pages_extracted_payload)

    step_started_at = perf_counter()
    keyword_records = flatten_keyword_json(keywords_json)
    keywords_flattened_payload: dict[str, Any] = {
        "duration_ms": elapsed_ms(step_started_at),
        "keyword_variant_count": len(keyword_records),
        "keyword_group_count": len({record["group"] for record in keyword_records}),
    }
    if settings.log_preview_chars > 0:
        keywords_flattened_payload["keyword_records"] = keyword_records
    log_event(logger, "keywords_flattened", **keywords_flattened_payload)

    temp_dir = None
    if index_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="whoosh_encounter_keyword_index_")
        index_dir = Path(temp_dir)
        log_event(logger, "temporary_index_dir_created", index_dir=str(index_dir))

    try:
        step_started_at = perf_counter()
        ix = create_or_replace_index(
            Path(index_dir),
            keep_stopwords=settings.keep_stopwords,
            stem_words=settings.stem_words,
        )
        log_event(
            logger,
            "whoosh_index_created",
            duration_ms=elapsed_ms(step_started_at),
            index_dir=str(index_dir),
            keep_stopwords=settings.keep_stopwords,
            stem_words=settings.stem_words,
            analyzer_mode=analyzer_mode(settings.keep_stopwords, settings.stem_words),
        )

        step_started_at = perf_counter()
        index_pages(ix, page_records, logger=logger, log_preview_chars=settings.log_preview_chars)
        log_event(logger, "pages_indexed", duration_ms=elapsed_ms(step_started_at))

        output_by_doc_id: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for page in page_records:
            record: dict[str, Any] = {
                "page_number": int(page["page_number"]),
                "encounter_id": str(page["encounter_id"]),
                "matches": [],
            }
            if settings.include_file_path:
                record["file_path"] = page.get("file_path", "")
            output_by_doc_id[page["doc_id"]] = record

        with ix.searcher() as searcher:
            ixreader = searcher.reader()
            for keyword_record in keyword_records:
                keyword_started_at = perf_counter()
                group = keyword_record["group"]
                variant = keyword_record["variant"]
                log_event(logger, "keyword_search_started", group=group, variant=variant)

                q = build_fuzzy_unordered_near_all_query(
                    schema=ix.schema,
                    ixreader=ixreader,
                    fieldname="content",
                    keyword=variant,
                    slop=settings.slop,
                    edit_distance=settings.edit_distance,
                    prefixlength=settings.prefixlength,
                    min_fuzzy_term_length=settings.min_fuzzy_term_length,
                    log_query_details=settings.log_preview_chars > 0,
                    logger=logger,
                )

                results = searcher.search(q, limit=None)
                hit_count = 0
                added_match_count = 0
                duplicate_match_count = 0
                hit_records: list[dict[str, Any]] = []

                for hit in results:
                    hit_count += 1
                    doc_id = hit["doc_id"]
                    match_record = {"group": group, "variant": variant}
                    hit_record = {
                        "doc_id": doc_id,
                        "encounter_id": hit["encounter_id"],
                        "page_number": hit["page_number"],
                        "file_path": hit["file_path"],
                    }
                    hit_records.append(hit_record)

                    if match_record not in output_by_doc_id[doc_id]["matches"]:
                        output_by_doc_id[doc_id]["matches"].append(match_record)
                        added_match_count += 1
                        if settings.log_preview_chars > 0:
                            log_event(logger, "match_added", group=group, variant=variant, **hit_record)
                    else:
                        duplicate_match_count += 1
                        if settings.log_preview_chars > 0:
                            log_event(
                                logger,
                                "duplicate_match_skipped",
                                group=group,
                                variant=variant,
                                **hit_record,
                            )

                log_event(
                    logger,
                    "keyword_search_finished",
                    group=group,
                    variant=variant,
                    duration_ms=elapsed_ms(keyword_started_at),
                    hit_count=hit_count,
                    added_match_count=added_match_count,
                    duplicate_match_count=duplicate_match_count,
                    hits=hit_records,
                )

        output = [
            record
            for record in output_by_doc_id.values()
            if not settings.matched_only or record["matches"]
        ]
        log_event(
            logger,
            "keyword_detection_finished",
            duration_ms=elapsed_ms(run_started_at),
            output_record_count=len(output),
            matched_record_count=sum(1 for record in output if record["matches"]),
            total_match_count=sum(len(record["matches"]) for record in output),
        )
        return output

    finally:
        if temp_dir is not None and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            log_event(logger, "temporary_index_dir_removed", index_dir=temp_dir)


def collect_ocr_json_files(ocr_path: Path) -> list[Path]:
    ocr_path = Path(ocr_path)
    if ocr_path.is_file():
        return [ocr_path]
    if ocr_path.is_dir():
        return sorted(path for path in ocr_path.rglob("*.json") if path.is_file())
    raise FileNotFoundError(f"OCR path does not exist: {ocr_path}")


def relative_path_for_output(ocr_json_file: Path, ocr_root: Path) -> Path:
    ocr_json_file = Path(ocr_json_file)
    ocr_root = Path(ocr_root)
    if ocr_root.is_dir():
        return ocr_json_file.relative_to(ocr_root)
    return Path(ocr_json_file.name)


def output_file_for_ocr_json(ocr_json_file: Path, ocr_root: Path, output_root: Path) -> Path:
    relative_path = relative_path_for_output(ocr_json_file, ocr_root)
    return Path(output_root) / relative_path.parent / f"{relative_path.stem}_keyword_output.json"


def log_file_for_ocr_json(ocr_json_file: Path, ocr_root: Path, logs_root: Path) -> Path:
    relative_path = relative_path_for_output(ocr_json_file, ocr_root)
    return Path(logs_root) / relative_path.parent / f"{relative_path.stem}.log"


def retry_log_file_for_ocr_json(
    ocr_json_file: Path,
    ocr_root: Path,
    logs_root: Path,
    attempt_number: int,
) -> Path:
    relative_path = relative_path_for_output(ocr_json_file, ocr_root)
    return Path(logs_root) / relative_path.parent / f"{relative_path.stem}_attempt_{attempt_number}.log"


def cleanup_previous_file_artifacts(
    ocr_json_file: Path,
    ocr_root: Path,
    output_path: Path,
    logs_root: Path,
) -> None:
    relative_path = relative_path_for_output(ocr_json_file, ocr_root)
    stem = relative_path.stem

    output_path = Path(output_path)
    if output_path.exists():
        output_path.unlink()

    log_dir = Path(logs_root) / relative_path.parent
    failed_dir = Path(logs_root) / "failed" / relative_path.parent
    for directory in (log_dir, failed_dir):
        if not directory.exists():
            continue
        for candidate in directory.iterdir():
            if not candidate.is_file():
                continue
            if candidate.name == f"{stem}.log":
                candidate.unlink()
            elif candidate.name.startswith(f"{stem}_attempt_") and candidate.suffix == ".log":
                candidate.unlink()
            elif candidate.name == f"{stem}_failure_summary.json":
                candidate.unlink()


def write_batch_timing_csv(csv_path: Path, summaries: list[dict[str, Any]]) -> None:
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["filename", "processing_time_seconds"])
        for summary in summaries:
            duration_ms = summary.get("total_duration_ms", summary["duration_ms"])
            seconds = round(float(duration_ms) / 1000, 3)
            writer.writerow([summary["file_id"], f"{seconds:.3f}"])


def process_ocr_json_file(
    ocr_json_file: Path,
    keywords_json_file: Path,
    settings: DetectionSettings,
    output_path: Path,
    log_path: Path,
    index_dir: Path,
    attempt_number: int = 1,
    max_attempts: int = 1,
) -> dict[str, Any]:
    file_started_at = perf_counter()
    ocr_json_file = Path(ocr_json_file)
    file_id = ocr_json_file.name
    logger = TextDebugLogger(log_path, file_id=file_id, input_path=ocr_json_file)

    try:
        log_event(
            logger,
            "file_processing_started",
            output_path=str(output_path),
            keywords_json_file=str(keywords_json_file),
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )
        output = run_keyword_detection(
            ocr_json_file=ocr_json_file,
            keywords_json_file=keywords_json_file,
            settings=settings,
            index_dir=index_dir,
            logger=logger,
        )

        output_json = json.dumps(output, indent=2, ensure_ascii=False)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_json, encoding="utf-8")
        log_event(logger, "output_written", output_path=str(output_path), output_bytes=output_path.stat().st_size)

        summary = {
            "file_id": file_id,
            "input_path": str(ocr_json_file),
            "output_path": str(output_path),
            "log_path": str(log_path),
            "status": "success",
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
            "duration_ms": elapsed_ms(file_started_at),
            "output_record_count": len(output),
            "matched_record_count": sum(1 for record in output if record["matches"]),
            "total_match_count": sum(len(record["matches"]) for record in output),
        }
        log_event(logger, "file_processing_finished", **summary)
        return summary

    except Exception as exc:
        summary = {
            "file_id": file_id,
            "input_path": str(ocr_json_file),
            "output_path": str(output_path),
            "log_path": str(log_path),
            "status": "failed",
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
            "duration_ms": elapsed_ms(file_started_at),
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
        log_event(logger, "file_processing_failed", **summary, traceback=traceback.format_exc())
        return summary

    finally:
        logger.close()


def process_ocr_json_file_with_retries(
    ocr_json_file: Path,
    ocr_root: Path,
    keywords_json_file: Path,
    settings: DetectionSettings,
    output_path: Path,
    logs_root: Path,
    index_dir: Path,
) -> dict[str, Any]:
    wrapper_started_at = perf_counter()
    max_retries = max(settings.max_file_retries, 0)
    max_attempts = max_retries + 1
    failed_attempts: list[dict[str, Any]] = []
    cleanup_previous_file_artifacts(
        ocr_json_file=ocr_json_file,
        ocr_root=ocr_root,
        output_path=output_path,
        logs_root=logs_root,
    )

    for attempt_number in range(1, max_attempts + 1):
        log_path = (
            log_file_for_ocr_json(ocr_json_file, ocr_root, logs_root)
            if attempt_number == 1
            else retry_log_file_for_ocr_json(ocr_json_file, ocr_root, logs_root, attempt_number)
        )
        summary = process_ocr_json_file(
            ocr_json_file=ocr_json_file,
            keywords_json_file=keywords_json_file,
            settings=settings,
            output_path=output_path,
            log_path=log_path,
            index_dir=index_dir,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )
        summary["retry_count"] = attempt_number - 1

        if summary["status"] == "success":
            summary["failed_attempts"] = failed_attempts
            summary["total_duration_ms"] = elapsed_ms(wrapper_started_at)
            return summary

        failed_attempts.append(
            {
                "attempt_number": attempt_number,
                "log_path": summary.get("log_path"),
                "error_type": summary.get("error_type"),
                "error_message": summary.get("error_message"),
                "duration_ms": summary.get("duration_ms"),
            }
        )

    summary["failed_attempts"] = failed_attempts
    summary["total_duration_ms"] = elapsed_ms(wrapper_started_at)
    write_failed_file_artifacts(
        summary=summary,
        ocr_json_file=ocr_json_file,
        ocr_root=ocr_root,
        logs_root=logs_root,
    )
    return summary


def write_failed_file_artifacts(
    summary: dict[str, Any],
    ocr_json_file: Path,
    ocr_root: Path,
    logs_root: Path,
) -> None:
    relative_path = relative_path_for_output(ocr_json_file, ocr_root)
    failed_dir = Path(logs_root) / "failed" / relative_path.parent
    failed_dir.mkdir(parents=True, exist_ok=True)

    copied_logs: list[str] = []
    for attempt in summary.get("failed_attempts", []):
        log_path = Path(str(attempt.get("log_path", "")))
        if log_path.exists():
            copied_log_path = failed_dir / log_path.name
            shutil.copy2(log_path, copied_log_path)
            copied_logs.append(str(copied_log_path))

    failure_summary = {
        "file_id": summary.get("file_id"),
        "input_path": summary.get("input_path"),
        "output_path": summary.get("output_path"),
        "status": summary.get("status"),
        "attempt_count": summary.get("attempt_number"),
        "max_attempts": summary.get("max_attempts"),
        "retry_count": summary.get("retry_count"),
        "error_type": summary.get("error_type"),
        "error_message": summary.get("error_message"),
        "failed_attempts": summary.get("failed_attempts", []),
        "copied_log_paths": copied_logs,
    }
    failure_summary_path = failed_dir / f"{relative_path.stem}_failure_summary.json"
    failure_summary_path.write_text(
        json.dumps(failure_summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    summary["failed_logs_dir"] = str(failed_dir)
    summary["failed_summary_path"] = str(failure_summary_path)


def process_ocr_path(
    ocr_root: Path,
    keywords_json_file: Path,
    output_root: Path,
    logs_root: Path,
    index_dir: Path,
    settings: DetectionSettings,
) -> dict[str, Any]:
    batch_started_at = perf_counter()
    ocr_root = Path(ocr_root)
    output_root = Path(output_root)
    logs_root = Path(logs_root)
    index_dir = Path(index_dir)

    ocr_json_files = collect_ocr_json_files(ocr_root)
    if not ocr_json_files:
        raise FileNotFoundError(f"No OCR JSON files found under: {ocr_root}")

    summaries: list[dict[str, Any]] = []
    for ocr_json_file in ocr_json_files:
        output_path = output_file_for_ocr_json(ocr_json_file, ocr_root, output_root)
        summary = process_ocr_json_file_with_retries(
            ocr_json_file=ocr_json_file,
            ocr_root=ocr_root,
            keywords_json_file=keywords_json_file,
            settings=settings,
            output_path=output_path,
            logs_root=logs_root,
            index_dir=index_dir,
        )
        summaries.append(summary)
        if summary["status"] != "success" and settings.stop_on_error:
            break

    batch_summary = OrderedDict(
        [
            ("timestamp_utc", utc_timestamp()),
            ("ocr_root", str(ocr_root)),
            ("keywords_json_file", str(keywords_json_file)),
            ("output_root", str(output_root)),
            ("logs_root", str(logs_root)),
            ("index_dir", str(index_dir)),
            ("slop", settings.slop),
            ("edit_distance", settings.edit_distance),
            ("prefixlength", settings.prefixlength),
            ("min_fuzzy_term_length", settings.min_fuzzy_term_length),
            ("keep_stopwords", settings.keep_stopwords),
            ("stem_words", settings.stem_words),
            ("analyzer_mode", analyzer_mode(settings.keep_stopwords, settings.stem_words)),
            ("max_file_retries", settings.max_file_retries),
            ("duration_ms", elapsed_ms(batch_started_at)),
            ("file_count", len(summaries)),
            ("success_count", sum(1 for item in summaries if item["status"] == "success")),
            ("failed_count", sum(1 for item in summaries if item["status"] != "success")),
            ("files", summaries),
        ]
    )

    logs_root.mkdir(parents=True, exist_ok=True)
    batch_summary_path = logs_root / "batch_summary.json"
    batch_summary_path.write_text(json.dumps(batch_summary, indent=2, ensure_ascii=False), encoding="utf-8")
    write_batch_timing_csv(logs_root / "batch_summary.csv", summaries)
    return dict(batch_summary)


if __name__ == "__main__":
    from config_loader import load_config

    app_config = load_config()
    settings = DetectionSettings(
        slop=app_config.parameters.slop[0],
        edit_distance=app_config.parameters.edit_distance[0],
        min_fuzzy_term_length=app_config.parameters.min_fuzzy_term_length[0],
        keep_stopwords=app_config.parameters.keep_stopwords[0],
        stem_words=app_config.parameters.stem_words,
        prefixlength=app_config.parameters.prefixlength,
        include_cover=app_config.runtime.include_cover,
        matched_only=app_config.runtime.matched_only,
        include_file_path=app_config.runtime.include_file_path,
        log_preview_chars=app_config.runtime.log_preview_chars,
        stop_on_error=app_config.runtime.stop_on_error,
        max_file_retries=app_config.runtime.max_file_retries,
    )
    process_ocr_path(
        ocr_root=app_config.paths.ocr_json,
        keywords_json_file=app_config.paths.keywords_json,
        output_root=app_config.paths.base_output_dir / "single_config_run",
        logs_root=app_config.paths.base_logs_dir / "single_config_run",
        index_dir=app_config.paths.base_index_dir / "single_config_run",
        settings=settings,
    )
