import argparse
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

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

# Edit this block when you want to run the script without passing CLI arguments.
# Use absolute paths here so the script can be launched from any folder.
CONFIG_OCR_JSON = Path("/Users/mitulkanani/Desktop/Projects/Keyword_Search/Prod/Input/OCR")
CONFIG_KEYWORDS_JSON = Path(
    "/Users/mitulkanani/Desktop/Projects/Keyword_Search/Prod/Input/Keywords/sample_provider_role_keywords_flattened.json"
)
CONFIG_OUTPUT = Path("/Users/mitulkanani/Desktop/Projects/Keyword_Search/Prod/Output")
CONFIG_LOGS_DIR = Path("/Users/mitulkanani/Desktop/Projects/Keyword_Search/Prod/Output/Logs")
CONFIG_INDEX_DIR = Path("/Users/mitulkanani/Desktop/Projects/Keyword_Search/Prod/Output/Index")

CONFIG_SLOP = 5
CONFIG_EDIT_DISTANCE = 1
CONFIG_PREFIXLENGTH = 0
CONFIG_MIN_FUZZY_TERM_LENGTH = 5

CONFIG_KEEP_STOPWORDS = False
CONFIG_STEM_WORDS = True
CONFIG_INCLUDE_COVER = False
CONFIG_MATCHED_ONLY = False
CONFIG_INCLUDE_FILE_PATH = True
CONFIG_LOG_PREVIEW_CHARS = 0
CONFIG_STOP_ON_ERROR = False


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat()


def elapsed_ms(start_time):
    return round((perf_counter() - start_time) * 1000, 3)


def preview_text(value, max_chars=300):
    if max_chars <= 0:
        return ""

    text = str(value or "").replace("\n", "\\n")

    if len(text) <= max_chars:
        return text

    return f"{text[:max_chars]}..."


def log_event(logger, event, **payload):
    if logger is not None:
        logger.log(event, **payload)


def format_log_value(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)

    return str(value)


class TextDebugLogger:
    def __init__(self, log_path, file_id, input_path):
        self.log_path = Path(log_path)
        self.file_id = file_id
        self.input_path = str(input_path)
        self.started_at = perf_counter()

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.log_path, "w", encoding="utf-8")
        self.log(
            "log_opened",
            input_path=self.input_path,
            log_path=str(self.log_path),
        )

    def log(self, event, **payload):
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

    def close(self):
        if not self.file.closed:
            self.log("log_closed")
            self.file.close()


def read_json_file(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


def flatten_keyword_json(data, parent_key=""):
    """
    Converts keyword JSON into a flat list of records.

    Supported formats:

    1) Already flattened:
       {
         "legal_authenticator.Last reviewed by": ["Last reviewed by"],
         "electronically_signed_provider.Electronically signed by": ["Electronically signed by"]
       }

    2) Nested:
       {
         "legal_authenticator": {
           "Last reviewed by": ["Last reviewed by"]
         }
       }

    Returns:
       [
         {"group": "legal_authenticator.Last reviewed by", "variant": "Last reviewed by"}
       ]
    """
    keyword_records = []

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


def split_encounter_text_into_pages(encounter_text):
    """
    Splits encounter OCR text using tags like:

        <ocr_service_page_start>1<ocr_service_page_start>
        <ocr_service_page_start>10<ocr_service_page_start>

    If no tag exists, complete encounter text is treated as page 1.
    """
    if not encounter_text:
        return []

    matches = list(PAGE_TAG_RE.finditer(encounter_text))

    if not matches:
        return [{"page_number": 1, "content": encounter_text.strip()}]

    pages = []

    for idx, match in enumerate(matches):
        page_number = int(match.group(1))
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(encounter_text)
        content = encounter_text[start:end].strip()
        pages.append({"page_number": page_number, "content": content})

    return pages


def extract_pages_from_ocr_json(ocr_json, include_cover=False):
    """
    Converts OCR encounter JSON into page-level records.

    Expected input shape:
       {
         "number_of_encounters": 3,
         "cover": {"text": "", "file_path": ""},
         "encounters": [
           {"id": "1", "text": "...", "file_path": "enc1.txt"}
         ]
       }
    """
    page_records = []

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


def analyzer_mode(keep_stopwords=False, stem_words=False):
    if stem_words:
        if keep_stopwords:
            return "stemming_keep_stopwords"
        return "stemming"

    if keep_stopwords:
        return "keep_stopwords"

    return "default"


def create_or_replace_index(index_dir, keep_stopwords=False, stem_words=False):
    """
    Creates a fresh Whoosh index.

    Default mode is recall-first:
      - default TEXT analyzer is used
      - common stop words like 'and' and 'by' are removed

    Strict mode:
      - pass keep_stopwords=True
      - stop words are kept using RegexTokenizer + LowercaseFilter

    Stemming mode:
      - pass stem_words=True
      - Whoosh StemmingAnalyzer is used
      - if keep_stopwords is also true, stemming is used without a stoplist
    """
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


def index_pages(ix, page_records, logger=None, log_preview_chars=300):
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


def analyze_keyword_terms(schema, fieldname, keyword):
    """
    Converts a keyword phrase into indexed query terms using the same field analyzer.

    Example in recall-first mode:
      'reported and signed by' -> ['reported', 'signed']

    Example in keep-stopwords mode:
      'reported and signed by' -> ['reported', 'and', 'signed', 'by']
    """
    field = schema[fieldname]
    terms = list(field.process_text(keyword, mode="query"))

    seen = set()
    unique_terms = []

    for term in terms:
        if term not in seen:
            unique_terms.append(term)
            seen.add(term)

    return unique_terms


def build_fuzzy_unordered_near_all_query(
    schema,
    ixreader,
    fieldname,
    keyword,
    slop=5,
    edit_distance=1,
    prefixlength=0,
    min_fuzzy_term_length=5,
    log_query_details=False,
    logger=None,
):
    """
    Builds a Whoosh query where:
      - all analyzed keyword terms must match
      - terms must be near each other within slop
      - order does not matter
      - short terms use exact matching to avoid false positives
      - longer terms allow fuzzy typo tolerance using edit distance
    """
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
            exact_term = query.Term(fieldname, term)
            fuzzy_terms.append(exact_term)
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
        fuzzy_terms.append(
            term_queries[0] if len(term_queries) == 1 else spans.SpanOr(term_queries)
        )

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

    near_query = spans.SpanNear2(
        fuzzy_terms,
        slop=slop,
        ordered=False,
        mindist=1,
    )
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
    ocr_json_file,
    keywords_json_file,
    slop=5,
    edit_distance=1,
    prefixlength=0,
    min_fuzzy_term_length=5,
    index_dir=None,
    keep_stopwords=False,
    stem_words=False,
    include_cover=False,
    include_empty_pages=True,
    include_file_path=False,
    logger=None,
    log_preview_chars=300,
):
    run_started_at = perf_counter()
    log_event(
        logger,
        "keyword_detection_started",
        ocr_json_file=str(ocr_json_file),
        keywords_json_file=str(keywords_json_file),
        slop=slop,
        edit_distance=edit_distance,
        prefixlength=prefixlength,
        min_fuzzy_term_length=min_fuzzy_term_length,
        index_dir=index_dir,
        keep_stopwords=keep_stopwords,
        stem_words=stem_words,
        analyzer_mode=analyzer_mode(keep_stopwords=keep_stopwords, stem_words=stem_words),
        include_cover=include_cover,
        include_empty_pages=include_empty_pages,
        include_file_path=include_file_path,
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
    page_records = extract_pages_from_ocr_json(ocr_json, include_cover=include_cover)
    pages_extracted_payload = {
        "duration_ms": elapsed_ms(step_started_at),
        "page_count": len(page_records),
        "non_empty_page_count": sum(1 for page in page_records if page.get("content", "")),
        "total_content_chars": sum(len(page.get("content", "") or "") for page in page_records),
        "unique_encounter_count": len({str(page.get("encounter_id", "")) for page in page_records}),
    }

    if log_preview_chars > 0:
        pages_extracted_payload["pages"] = [
            {
                "encounter_id": str(page.get("encounter_id", "")),
                "file_path": page.get("file_path", ""),
                "page_number": page.get("page_number"),
                "content_chars": len(page.get("content", "") or ""),
                "content_preview": preview_text(page.get("content", ""), log_preview_chars),
            }
            for page in page_records
        ]

    log_event(logger, "pages_extracted", **pages_extracted_payload)

    step_started_at = perf_counter()
    keyword_records = flatten_keyword_json(keywords_json)
    keywords_flattened_payload = {
        "duration_ms": elapsed_ms(step_started_at),
        "keyword_variant_count": len(keyword_records),
        "keyword_group_count": len({record["group"] for record in keyword_records}),
    }

    if log_preview_chars > 0:
        keywords_flattened_payload["keyword_records"] = keyword_records

    log_event(logger, "keywords_flattened", **keywords_flattened_payload)

    temp_dir = None

    if index_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="whoosh_encounter_keyword_index_")
        index_dir = temp_dir
        log_event(logger, "temporary_index_dir_created", index_dir=index_dir)

    try:
        step_started_at = perf_counter()
        ix = create_or_replace_index(
            index_dir,
            keep_stopwords=keep_stopwords,
            stem_words=stem_words,
        )
        log_event(
            logger,
            "whoosh_index_created",
            duration_ms=elapsed_ms(step_started_at),
            index_dir=index_dir,
            keep_stopwords=keep_stopwords,
            stem_words=stem_words,
            analyzer_mode=analyzer_mode(keep_stopwords=keep_stopwords, stem_words=stem_words),
        )

        step_started_at = perf_counter()
        index_pages(ix, page_records, logger=logger, log_preview_chars=log_preview_chars)
        log_event(logger, "pages_indexed", duration_ms=elapsed_ms(step_started_at))

        output_by_doc_id = OrderedDict()

        for page in page_records:
            record = {
                "page_number": int(page["page_number"]),
                "encounter_id": str(page["encounter_id"]),
                "matches": [],
            }

            if include_file_path:
                record["file_path"] = page.get("file_path", "")

            output_by_doc_id[page["doc_id"]] = record

        with ix.searcher() as searcher:
            ixreader = searcher.reader()

            for keyword_record in keyword_records:
                keyword_started_at = perf_counter()
                group = keyword_record["group"]
                variant = keyword_record["variant"]
                log_event(
                    logger,
                    "keyword_search_started",
                    group=group,
                    variant=variant,
                )

                q = build_fuzzy_unordered_near_all_query(
                    schema=ix.schema,
                    ixreader=ixreader,
                    fieldname="content",
                    keyword=variant,
                    slop=slop,
                    edit_distance=edit_distance,
                    prefixlength=prefixlength,
                    min_fuzzy_term_length=min_fuzzy_term_length,
                    log_query_details=log_preview_chars > 0,
                    logger=logger,
                )

                results = searcher.search(q, limit=None)
                hit_count = 0
                added_match_count = 0
                duplicate_match_count = 0
                hit_records = []

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
                        if log_preview_chars > 0:
                            log_event(
                                logger,
                                "match_added",
                                group=group,
                                variant=variant,
                                **hit_record,
                            )
                    else:
                        duplicate_match_count += 1
                        if log_preview_chars > 0:
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

        output = []

        for record in output_by_doc_id.values():
            if include_empty_pages or record["matches"]:
                output.append(record)

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


def collect_ocr_json_files(ocr_path):
    ocr_path = Path(ocr_path)

    if ocr_path.is_file():
        return [ocr_path]

    if ocr_path.is_dir():
        return sorted(path for path in ocr_path.rglob("*.json") if path.is_file())

    raise FileNotFoundError(f"OCR path does not exist: {ocr_path}")


def relative_path_for_output(ocr_json_file, ocr_root):
    ocr_json_file = Path(ocr_json_file)
    ocr_root = Path(ocr_root)

    if ocr_root.is_dir():
        return ocr_json_file.relative_to(ocr_root)

    return Path(ocr_json_file.name)


def output_file_for_ocr_json(ocr_json_file, ocr_root, output_root):
    relative_path = relative_path_for_output(ocr_json_file, ocr_root)
    return Path(output_root) / relative_path.parent / f"{relative_path.stem}_keyword_output.json"


def log_file_for_ocr_json(ocr_json_file, ocr_root, logs_root):
    relative_path = relative_path_for_output(ocr_json_file, ocr_root)
    return Path(logs_root) / relative_path.parent / f"{relative_path.stem}.log"


def default_output_root():
    return CONFIG_OUTPUT


def output_path_for_single_file(ocr_json_file, ocr_root, output_path):
    if not output_path:
        return None

    output_path = Path(output_path)

    if output_path.suffix:
        return output_path

    return output_file_for_ocr_json(ocr_json_file, ocr_root, output_path)


def resolve_logs_root(output_path, logs_dir):
    if logs_dir:
        return Path(logs_dir)

    if output_path:
        output_path = Path(output_path)
        if output_path.suffix:
            return output_path.parent / "Logs"
        return output_path / "Logs"

    return default_output_root() / "Logs"


def write_batch_timing_csv(csv_path, summaries):
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    with open(csv_path, "w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["filename", "processing_time_seconds"])

        for summary in summaries:
            seconds = round(float(summary["duration_ms"]) / 1000, 3)
            writer.writerow([summary["file_id"], f"{seconds:.3f}"])


def process_ocr_json_file(
    ocr_json_file,
    keywords_json_file,
    args,
    output_path=None,
    log_path=None,
):
    file_started_at = perf_counter()
    ocr_json_file = Path(ocr_json_file)
    file_id = ocr_json_file.name
    logger = TextDebugLogger(log_path, file_id=file_id, input_path=ocr_json_file) if log_path else None

    try:
        log_event(
            logger,
            "file_processing_started",
            output_path=str(output_path) if output_path else None,
            keywords_json_file=str(keywords_json_file),
        )

        output = run_keyword_detection(
            ocr_json_file=ocr_json_file,
            keywords_json_file=keywords_json_file,
            slop=args.slop,
            edit_distance=args.edit_distance,
            prefixlength=args.prefixlength,
            min_fuzzy_term_length=args.min_fuzzy_term_length,
            index_dir=args.index_dir,
            keep_stopwords=args.keep_stopwords,
            stem_words=args.stem_words,
            include_cover=args.include_cover,
            include_empty_pages=not args.matched_only,
            include_file_path=args.include_file_path,
            logger=logger,
            log_preview_chars=args.log_preview_chars,
        )

        output_json = json.dumps(output, indent=2, ensure_ascii=False)

        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(output_json, encoding="utf-8")
            print(f"Output written to: {output_path}")
            log_event(
                logger,
                "output_written",
                output_path=str(output_path),
                output_bytes=output_path.stat().st_size,
            )
        else:
            print(output_json)
            log_event(logger, "output_printed_to_stdout")

        summary = {
            "file_id": file_id,
            "input_path": str(ocr_json_file),
            "output_path": str(output_path) if output_path else None,
            "log_path": str(log_path) if log_path else None,
            "status": "success",
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
            "output_path": str(output_path) if output_path else None,
            "log_path": str(log_path) if log_path else None,
            "status": "failed",
            "duration_ms": elapsed_ms(file_started_at),
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
        log_event(
            logger,
            "file_processing_failed",
            **summary,
            traceback=traceback.format_exc(),
        )
        return summary

    finally:
        if logger is not None:
            logger.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Detect keyword variants from encounter OCR JSON using Whoosh.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--ocr-json",
        default=CONFIG_OCR_JSON,
        help="Path to one OCR JSON file, or a folder containing OCR JSON files.",
    )

    parser.add_argument(
        "--keywords-json",
        default=CONFIG_KEYWORDS_JSON,
        help="Path to flattened/nested keyword JSON file.",
    )

    parser.add_argument(
        "--slop",
        type=int,
        default=CONFIG_SLOP,
        help="Maximum proximity/slop distance between keyword terms. Default: 5.",
    )

    parser.add_argument(
        "--edit-distance",
        type=int,
        default=CONFIG_EDIT_DISTANCE,
        help="Fuzzy typo tolerance for each term. Default: 1.",
    )

    parser.add_argument(
        "--prefixlength",
        type=int,
        default=CONFIG_PREFIXLENGTH,
        help="Number of starting characters that must match exactly. Default: 0.",
    )

    parser.add_argument(
        "--min-fuzzy-term-length",
        type=int,
        default=CONFIG_MIN_FUZZY_TERM_LENGTH,
        help=(
            "Minimum analyzed term length allowed to use fuzzy matching. "
            "Shorter terms use exact matching to reduce false positives."
        ),
    )

    parser.add_argument(
        "--index-dir",
        default=CONFIG_INDEX_DIR,
        help="Optional Whoosh index directory. If not passed, a temporary index is used.",
    )

    parser.add_argument(
        "--output",
        default=CONFIG_OUTPUT,
        help=(
            "For one OCR JSON file, optional output JSON file path. "
            "For an OCR folder, output root folder where the input hierarchy is mirrored. "
            "If folder mode is used without this value, Prod/Output is used."
        ),
    )

    parser.add_argument(
        "--logs-dir",
        default=CONFIG_LOGS_DIR,
        help="Optional log root folder. Defaults to a Logs folder under the output folder.",
    )

    parser.add_argument(
        "--keep-stopwords",
        action=argparse.BooleanOptionalAction,
        default=CONFIG_KEEP_STOPWORDS,
        help="Keep stop words like 'and' and 'by'. Default is recall-first mode where stop words are removed.",
    )

    parser.add_argument(
        "--stem-words",
        action=argparse.BooleanOptionalAction,
        default=CONFIG_STEM_WORDS,
        help=(
            "Stem OCR and keyword terms before fuzzy matching. "
            "If used with --keep-stopwords, stop words are kept and stemmed."
        ),
    )

    parser.add_argument(
        "--include-cover",
        action=argparse.BooleanOptionalAction,
        default=CONFIG_INCLUDE_COVER,
        help="Also process cover.text if present in OCR JSON.",
    )

    parser.add_argument(
        "--matched-only",
        action=argparse.BooleanOptionalAction,
        default=CONFIG_MATCHED_ONLY,
        help="Return only pages where at least one keyword variant matched.",
    )

    parser.add_argument(
        "--include-file-path",
        action=argparse.BooleanOptionalAction,
        default=CONFIG_INCLUDE_FILE_PATH,
        help="Include source file_path in each output record.",
    )

    parser.add_argument(
        "--log-preview-chars",
        type=int,
        default=CONFIG_LOG_PREVIEW_CHARS,
        help=(
            "Maximum OCR content preview characters written to logs per page. "
            "Use 0 for compact logs with summary, timing, errors, and keyword hits only."
        ),
    )

    parser.add_argument(
        "--stop-on-error",
        action=argparse.BooleanOptionalAction,
        default=CONFIG_STOP_ON_ERROR,
        help="In folder mode, stop after the first failed OCR JSON file.",
    )

    return parser.parse_args()


def main():
    args = parse_args()
    ocr_root = Path(args.ocr_json)
    ocr_json_files = collect_ocr_json_files(ocr_root)

    if not ocr_json_files:
        raise SystemExit(f"No OCR JSON files found under: {ocr_root}")

    batch_mode = ocr_root.is_dir()
    logs_root = resolve_logs_root(args.output, args.logs_dir)

    if batch_mode:
        batch_started_at = perf_counter()
        output_root = Path(args.output) if args.output else default_output_root()
        summaries = []

        print(f"Found {len(ocr_json_files)} OCR JSON file(s) under: {ocr_root}")
        print(f"Output root: {output_root}")
        print(f"Logs root: {logs_root}")

        for ocr_json_file in ocr_json_files:
            output_path = output_file_for_ocr_json(ocr_json_file, ocr_root, output_root)
            log_path = log_file_for_ocr_json(ocr_json_file, ocr_root, logs_root)
            summary = process_ocr_json_file(
                ocr_json_file=ocr_json_file,
                keywords_json_file=args.keywords_json,
                args=args,
                output_path=output_path,
                log_path=log_path,
            )
            summaries.append(summary)
            print(
                f"{summary['status'].upper()}: {summary['file_id']} "
                f"({summary['duration_ms']} ms)"
            )

            if summary["status"] != "success" and args.stop_on_error:
                break

        batch_summary = OrderedDict(
            [
                ("timestamp_utc", utc_timestamp()),
                ("ocr_root", str(ocr_root)),
                ("keywords_json_file", str(args.keywords_json)),
                ("output_root", str(output_root)),
                ("logs_root", str(logs_root)),
                ("index_dir", str(args.index_dir) if args.index_dir else None),
                ("slop", args.slop),
                ("edit_distance", args.edit_distance),
                ("prefixlength", args.prefixlength),
                ("min_fuzzy_term_length", args.min_fuzzy_term_length),
                ("keep_stopwords", args.keep_stopwords),
                ("stem_words", args.stem_words),
                ("analyzer_mode", analyzer_mode(args.keep_stopwords, args.stem_words)),
                ("duration_ms", elapsed_ms(batch_started_at)),
                ("file_count", len(summaries)),
                ("success_count", sum(1 for item in summaries if item["status"] == "success")),
                ("failed_count", sum(1 for item in summaries if item["status"] != "success")),
                ("files", summaries),
            ]
        )
        batch_summary_path = Path(logs_root) / "batch_summary.json"
        batch_summary_path.parent.mkdir(parents=True, exist_ok=True)
        batch_summary_path.write_text(
            json.dumps(batch_summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        batch_timing_csv_path = Path(logs_root) / "batch_summary.csv"
        write_batch_timing_csv(batch_timing_csv_path, summaries)
        print(f"Batch summary written to: {batch_summary_path}")
        print(f"Batch timing CSV written to: {batch_timing_csv_path}")

        if batch_summary["failed_count"]:
            sys.exit(1)

        return

    output_path = output_path_for_single_file(ocr_json_files[0], ocr_root, args.output)

    log_path = log_file_for_ocr_json(ocr_json_files[0], ocr_root, logs_root)
    summary = process_ocr_json_file(
        ocr_json_file=ocr_json_files[0],
        keywords_json_file=args.keywords_json,
        args=args,
        output_path=output_path,
        log_path=log_path,
    )

    if summary["status"] != "success":
        print(
            f"Failed processing {summary['file_id']}: {summary.get('error_message', '')}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
