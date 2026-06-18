from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from whoosh import index, query
    from whoosh.analysis import LowercaseFilter, RegexTokenizer, StemmingAnalyzer
    from whoosh.fields import ID, NUMERIC, STORED, Schema, TEXT
    from whoosh.query import spans
except ModuleNotFoundError as exc:
    if exc.name != "whoosh":
        raise
    raise SystemExit("Whoosh is not installed. Install dependencies with: pip install -r requirements.txt") from exc


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
    include_file_path: bool = False
    matched_only: bool = False


def read_json_file(file_path: Path) -> Any:
    return json.loads(Path(file_path).read_text(encoding="utf-8"))


def write_json_file(file_path: Path, data: Any) -> None:
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def make_demo_ocr_json(pages: dict[int, str]) -> dict[str, Any]:
    text = "\n".join(
        f"<ocr_service_page_start>{page_number}<ocr_service_page_start>\n{content}"
        for page_number, content in pages.items()
    )
    return {
        "encounters": [
            {
                "id": "demo_encounter",
                "file_path": "demo.txt",
                "text": text,
            }
        ]
    }


def flatten_keyword_json(data: Any, parent_key: str = "") -> list[dict[str, str]]:
    keyword_records: list[dict[str, str]] = []
    if not isinstance(data, dict):
        return keyword_records

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


def extract_pages_from_ocr_json(ocr_json: Any) -> list[dict[str, Any]]:
    if not isinstance(ocr_json, dict):
        raise ValueError("OCR JSON must be an object.")

    encounters = ocr_json.get("encounters", [])
    if not isinstance(encounters, list):
        raise ValueError("OCR JSON must contain 'encounters' as a list.")

    page_records: list[dict[str, Any]] = []
    for encounter_index, encounter in enumerate(encounters, start=1):
        if not isinstance(encounter, dict):
            continue
        encounter_id = str(encounter.get("id") or encounter.get("encounter_id") or encounter_index)
        file_path = encounter.get("file_path", "") or ""
        for page in split_encounter_text_into_pages(encounter.get("text", "") or ""):
            page_records.append(
                {
                    "encounter_id": encounter_id,
                    "file_path": file_path,
                    "page_number": page["page_number"],
                    "content": page["content"],
                }
            )
    return page_records


def analyzer_mode(keep_stopwords: bool, stem_words: bool) -> str:
    if stem_words and keep_stopwords:
        return "stemming_keep_stopwords"
    if stem_words:
        return "stemming"
    if keep_stopwords:
        return "keep_stopwords"
    return "default"


def create_or_replace_index(index_dir: Path, keep_stopwords: bool, stem_words: bool):
    index_dir = Path(index_dir)
    if index_dir.exists():
        shutil.rmtree(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)

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


def index_pages(ix: Any, page_records: list[dict[str, Any]]) -> None:
    writer = ix.writer()
    for idx, page in enumerate(page_records, start=1):
        doc_id = f"{page['encounter_id']}::page_{int(page['page_number'])}::{idx}"
        page["doc_id"] = doc_id
        writer.add_document(
            doc_id=doc_id,
            encounter_id=str(page["encounter_id"]),
            file_path=page.get("file_path", ""),
            page_number=int(page["page_number"]),
            content=page.get("content", ""),
        )
    writer.commit()


def analyze_keyword_terms(schema: Any, fieldname: str, keyword: str) -> list[str]:
    field = schema[fieldname]
    seen: set[str] = set()
    unique_terms: list[str] = []
    for term in field.process_text(keyword, mode="query"):
        if term not in seen:
            unique_terms.append(term)
            seen.add(term)
    return unique_terms


def build_fuzzy_unordered_near_all_query(
    schema: Any,
    ixreader: Any,
    fieldname: str,
    keyword: str,
    settings: DetectionSettings,
):
    terms = analyze_keyword_terms(schema, fieldname, keyword)
    if not terms:
        return query.NullQuery

    field = schema[fieldname]
    fuzzy_terms = []
    for term in terms:
        if settings.min_fuzzy_term_length > 0 and len(term) < settings.min_fuzzy_term_length:
            fuzzy_terms.append(query.Term(fieldname, term))
            continue

        expanded_terms = sorted(
            {
                field.from_bytes(candidate) if isinstance(candidate, bytes) else candidate
                for candidate in ixreader.terms_within(
                    fieldname,
                    term,
                    settings.edit_distance,
                    prefix=settings.prefixlength,
                )
            }
        )
        if not expanded_terms:
            return query.NullQuery

        term_queries = [query.Term(fieldname, candidate) for candidate in expanded_terms]
        fuzzy_terms.append(term_queries[0] if len(term_queries) == 1 else spans.SpanOr(term_queries))

    if len(fuzzy_terms) == 1:
        return fuzzy_terms[0]

    return spans.SpanNear2(fuzzy_terms, slop=settings.slop, ordered=False, mindist=1)


def run_keyword_detection(
    ocr_json_file: Path,
    keywords_json_file: Path,
    settings: DetectionSettings,
    index_dir: Path | None = None,
) -> list[dict[str, Any]]:
    ocr_json = read_json_file(ocr_json_file)
    keywords_json = read_json_file(keywords_json_file)
    page_records = extract_pages_from_ocr_json(ocr_json)
    keyword_records = flatten_keyword_json(keywords_json)

    temp_dir = None
    if index_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="whoosh_demo_index_")
        index_dir = Path(temp_dir)

    try:
        ix = create_or_replace_index(
            Path(index_dir),
            keep_stopwords=settings.keep_stopwords,
            stem_words=settings.stem_words,
        )
        index_pages(ix, page_records)

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
                q = build_fuzzy_unordered_near_all_query(
                    schema=ix.schema,
                    ixreader=ixreader,
                    fieldname="content",
                    keyword=keyword_record["variant"],
                    settings=settings,
                )
                for hit in searcher.search(q, limit=None):
                    match_record = {
                        "group": keyword_record["group"],
                        "variant": keyword_record["variant"],
                    }
                    matches = output_by_doc_id[hit["doc_id"]]["matches"]
                    if match_record not in matches:
                        matches.append(match_record)

        return [
            record
            for record in output_by_doc_id.values()
            if not settings.matched_only or record["matches"]
        ]
    finally:
        if temp_dir is not None and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

