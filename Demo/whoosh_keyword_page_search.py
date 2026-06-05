import argparse
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

try:
    from whoosh import index, query
    from whoosh.fields import ID, NUMERIC, Schema, TEXT
    from whoosh.query import spans
except ModuleNotFoundError as exc:
    if exc.name != "whoosh":
        raise
    raise SystemExit(
        "Whoosh is not installed. Install it first using: pip install Whoosh"
    ) from exc


PAGE_HEADER_RE = re.compile(r"(?im)^#\s*Page\s*0*(\d+)\s*$")


def parse_markdown_pages(markdown_text):
    """
    Splits a markdown document into page-level records.

    Expected page marker examples:
        # Page 01
        # Page 1

    If no page marker is found, the complete document is treated as page 1.
    """
    matches = list(PAGE_HEADER_RE.finditer(markdown_text))

    if not matches:
        return [{"page": 1, "content": markdown_text.strip()}]

    pages = []

    for idx, match in enumerate(matches):
        page_no = int(match.group(1))
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown_text)
        content = markdown_text[start:end].strip()
        pages.append({"page": page_no, "content": content})

    return pages


def create_or_replace_index(index_dir):
    """
    Creates a fresh Whoosh index directory.
    The content field must be TEXT with phrase=True because SpanNear2 requires positions.
    """
    if os.path.exists(index_dir):
        shutil.rmtree(index_dir)

    os.makedirs(index_dir, exist_ok=True)

    schema = Schema(
        page=NUMERIC(stored=True, unique=True),
        page_id=ID(stored=True, unique=True),
        content=TEXT(stored=True, phrase=True),
    )

    return index.create_in(index_dir, schema)


def index_pages(ix, pages):
    writer = ix.writer()

    for page in pages:
        writer.add_document(
            page=page["page"],
            page_id=f"page_{page['page']:04d}",
            content=page["content"],
        )

    writer.commit()


def analyze_keyword_terms(schema, fieldname, keyword):
    """
    Converts a keyword phrase into the exact terms Whoosh will search.
    Example: 'Electronically Signed' -> ['electronically', 'signed']
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
):
    """
    Builds a Whoosh query where:
      - all terms from the keyword phrase must match
      - terms must be near each other within slop
      - order does not matter
      - each term allows fuzzy typo tolerance using edit distance

    Example keyword:
        electronically signed

    Can detect:
        electronically signed
        signed electronically
        electronicaly signed
        electronically was signed
    """
    terms = analyze_keyword_terms(schema, fieldname, keyword)

    if not terms:
        return query.NullQuery

    field = schema[fieldname]
    fuzzy_terms = []

    for term in terms:
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

        if not expanded_terms:
            return query.NullQuery

        term_queries = [query.Term(fieldname, candidate) for candidate in expanded_terms]
        fuzzy_terms.append(
            term_queries[0] if len(term_queries) == 1 else spans.SpanOr(term_queries)
        )

    if len(fuzzy_terms) == 1:
        return fuzzy_terms[0]

    return spans.SpanNear2(
        fuzzy_terms,
        slop=slop,
        ordered=False,
    )


def search_keyword_pages(ix, keyword, slop=5, edit_distance=1, prefixlength=0, limit=None):
    """
    Returns a set of page numbers where the keyword phrase is detected.
    """
    matched_pages = set()

    with ix.searcher() as searcher:
        q = build_fuzzy_unordered_near_all_query(
            schema=ix.schema,
            ixreader=searcher.reader(),
            fieldname="content",
            keyword=keyword,
            slop=slop,
            edit_distance=edit_distance,
            prefixlength=prefixlength,
        )

        results = searcher.search(q, limit=limit)

        for hit in results:
            matched_pages.add(hit["page"])

    return matched_pages


def run_keyword_detection(markdown_file, keywords, slop=5, edit_distance=1, prefixlength=0, index_dir=None):
    markdown_text = Path(markdown_file).read_text(encoding="utf-8")
    pages = parse_markdown_pages(markdown_text)

    temp_dir = None

    if index_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="whoosh_keyword_index_")
        index_dir = temp_dir

    try:
        ix = create_or_replace_index(index_dir)
        index_pages(ix, pages)

        page_to_detected_keywords = {page["page"]: [] for page in pages}

        for keyword in keywords:
            matched_pages = search_keyword_pages(
                ix=ix,
                keyword=keyword,
                slop=slop,
                edit_distance=edit_distance,
                prefixlength=prefixlength,
                limit=None,
            )

            for page_no in matched_pages:
                page_to_detected_keywords[page_no].append(keyword)

        output = []

        for page in sorted(pages, key=lambda item: item["page"]):
            page_no = page["page"]
            output.append(
                {
                    "page": page_no,
                    "keyword_detected": page_to_detected_keywords[page_no],
                }
            )

        return output

    finally:
        if temp_dir is not None and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Detect fuzzy unordered near keyword phrases page-by-page using Whoosh."
    )

    parser.add_argument(
        "--input",
        required=True,
        help="Path to markdown file containing page markers like '# Page 01'.",
    )

    parser.add_argument(
        "--keyword",
        action="append",
        required=True,
        help="Keyword phrase to detect. Repeat this argument for multiple keyword phrases.",
    )

    parser.add_argument(
        "--slop",
        type=int,
        default=5,
        help="Maximum allowed distance/proximity window between keyword terms. Default: 5.",
    )

    parser.add_argument(
        "--edit-distance",
        type=int,
        default=1,
        help="Fuzzy typo tolerance for each term. Default: 1.",
    )

    parser.add_argument(
        "--prefixlength",
        type=int,
        default=0,
        help="Number of starting characters that must match exactly. Default: 0.",
    )

    parser.add_argument(
        "--index-dir",
        default=None,
        help="Optional Whoosh index directory. If not passed, a temporary index is used.",
    )

    parser.add_argument(
        "--output",
        default=None,
        help="Optional output JSON file path. If not passed, JSON is printed to console.",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    output = run_keyword_detection(
        markdown_file=args.input,
        keywords=args.keyword,
        slop=args.slop,
        edit_distance=args.edit_distance,
        prefixlength=args.prefixlength,
        index_dir=args.index_dir,
    )

    output_json = json.dumps(output, indent=2, ensure_ascii=False)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_json, encoding="utf-8")
        print(f"Output written to: {args.output}")
    else:
        print(output_json)


if __name__ == "__main__":
    main()
