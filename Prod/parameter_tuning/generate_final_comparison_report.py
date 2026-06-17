from __future__ import annotations

import importlib.util
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from xml.sax.saxutils import escape

import pandas as pd

from config_loader import AppConfig, load_config


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MethodMetrics:
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_processed": self.files_processed,
            "total_pages_analyzed": self.total_pages_analyzed,
            "gt_total_keywords": self.gt_total_keywords,
            "method_total_detected": self.method_total_detected,
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


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
        force=True,
    )


def read_json(filepath: Path) -> Any:
    with filepath.open("r", encoding="utf-8") as file:
        return json.load(file)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_keyword(keyword: Any) -> str:
    if keyword is None:
        return ""
    text = str(keyword).strip().lower()
    return re.sub(r"\s+", " ", text)


def get_base_filename(filename: str) -> str:
    base = Path(filename).name
    if base.endswith(".json"):
        base = base[:-5]
    for suffix in ("_keyword_output", "_whoosh_output", "_rapidfuzz_output"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


def extract_declared_base_filename(data: Any) -> str | None:
    if not isinstance(data, Mapping):
        return None
    for field in ("file_name", "filename"):
        value = data.get(field)
        if value:
            return get_base_filename(str(value))
    return None


def build_file_mapping(directory: Path, glob_pattern: str) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    if not directory.exists():
        return mapping

    for path in directory.glob(glob_pattern):
        base_name = get_base_filename(path.name)
        try:
            declared_base_name = extract_declared_base_filename(read_json(path))
        except Exception:
            declared_base_name = None
        mapping[declared_base_name or base_name] = path
    return mapping


def extract_keyword_from_gt_item(item: Any) -> tuple[str, str]:
    if isinstance(item, Mapping):
        return str(item.get("keyword", "")).strip(), str(item.get("reason", "")).strip()
    return str(item).strip(), ""


def load_gt_keywords(filepath: Path) -> tuple[dict[int, list[dict[str, str]]], int]:
    data = read_json(filepath)
    if isinstance(data, Mapping):
        pages_raw = list(data.get("pages_with_keywords", []))
        total_pages = safe_int(data.get("total_pages"), 0)
    elif isinstance(data, list):
        pages_raw = data
        total_pages = 0
    else:
        return {}, 0

    result: dict[int, list[dict[str, str]]] = {}
    max_seen_page = 0
    for page_info in pages_raw:
        if not isinstance(page_info, Mapping):
            continue
        page_num = safe_int(page_info.get("page_number"), 0)
        if page_num <= 0:
            continue
        max_seen_page = max(max_seen_page, page_num)
        keywords_raw = page_info.get("keywords_detected", []) or []
        seen: set[str] = set()
        deduplicated: list[dict[str, str]] = []
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


def dedupe_preserve_order(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = normalize_keyword(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(str(value).strip())
    return result


def extract_method_keywords_from_page(
    page_info: Mapping[str, Any],
    preferred_match_field: str,
) -> list[str]:
    keywords: list[Any] = []
    matches = page_info.get("matches")
    if isinstance(matches, list):
        for match in matches:
            if isinstance(match, Mapping):
                value = match.get(preferred_match_field)
                if value is None or value == "":
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


def load_method_keywords(filepath: Path, preferred_match_field: str) -> dict[int, list[str]]:
    data = read_json(filepath)
    if isinstance(data, Mapping):
        if isinstance(data.get("rp_output"), list):
            pages_raw = data["rp_output"]
        elif isinstance(data.get("pages"), list):
            pages_raw = data["pages"]
        elif isinstance(data.get("pages_with_keywords"), list):
            pages_raw = data["pages_with_keywords"]
        elif isinstance(data.get("results"), list):
            pages_raw = data["results"]
        else:
            return {}
    elif isinstance(data, list):
        pages_raw = data
    else:
        return {}

    result: dict[int, list[str]] = {}
    for page_info in pages_raw:
        if not isinstance(page_info, Mapping):
            continue
        page_num = safe_int(page_info.get("page_number"), 0)
        if page_num <= 0:
            continue
        result[page_num] = extract_method_keywords_from_page(page_info, preferred_match_field)
    return result


def precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    detected = tp + fp
    expected = tp + fn
    precision = (tp / detected * 100) if detected > 0 else 0.0
    recall = (tp / expected * 100) if expected > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def compare_output_dir_vs_gt(
    method_output_dir: Path,
    gt_output_dir: Path,
    method_glob: str,
    preferred_match_field: str,
) -> MethodMetrics:
    gt_files = build_file_mapping(gt_output_dir, "*.json")
    method_files = build_file_mapping(method_output_dir, method_glob)
    common_bases = set(gt_files) & set(method_files)
    if not common_bases:
        LOGGER.warning("No common files between %s and GT", method_output_dir)
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
        gt_pages, total_pages = load_gt_keywords(gt_files[base_name])
        method_pages = load_method_keywords(method_files[base_name], preferred_match_field)
        if total_pages <= 0:
            total_pages = max(set(gt_pages) | set(method_pages), default=0)
        if total_pages <= 0:
            continue

        gt_pages_with_keywords = {page for page, keywords in gt_pages.items() if keywords}
        method_pages_with_keywords = {page for page, keywords in method_pages.items() if keywords}
        total_page_tp += len(gt_pages_with_keywords & method_pages_with_keywords)
        total_page_fp += len(method_pages_with_keywords - gt_pages_with_keywords)
        total_page_fn += len(gt_pages_with_keywords - method_pages_with_keywords)
        all_pages = set(range(1, total_pages + 1))
        total_page_tn += len(all_pages - (gt_pages_with_keywords | method_pages_with_keywords))

        for page_num in set(gt_pages) | set(method_pages):
            gt_kw = {
                normalize_keyword(item["keyword"])
                for item in gt_pages.get(page_num, [])
                if normalize_keyword(item["keyword"])
            }
            method_kw = {
                normalize_keyword(item)
                for item in method_pages.get(page_num, [])
                if normalize_keyword(item)
            }
            total_kw_tp += len(gt_kw & method_kw)
            total_kw_fp += len(method_kw - gt_kw)
            total_kw_fn += len(gt_kw - method_kw)

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


def load_successful_runs(execution_summary_path: Path) -> list[dict[str, Any]]:
    summary = read_json(execution_summary_path)
    raw_results = summary.get("results", []) if isinstance(summary, Mapping) else []
    successful_runs: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("status") not in {"success", "skipped"}:
            continue
        if not raw.get("output_dir"):
            continue
        successful_runs.append(dict(raw))
    return successful_runs


def run_to_row(run: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    parameters = run.get("parameters") if isinstance(run.get("parameters"), Mapping) else {}
    return {
        "run_id": run.get("run_id"),
        "run_name": run.get("run_name"),
        "status": run.get("status"),
        "slop": parameters.get("slop"),
        "edit_distance": parameters.get("edit_distance"),
        "min_fuzzy_term_length": parameters.get("min_fuzzy_term_length"),
        "keep_stopwords": parameters.get("keep_stopwords"),
        "stem_words": parameters.get("stem_words"),
        "prefixlength": parameters.get("prefixlength"),
        "analyzer_mode": parameters.get("analyzer_mode"),
        "execution_time_sec": round(float(run.get("duration_seconds", 0.0) or 0.0), 2),
        **metrics,
    }


def extract_ground_truth_data(gt_output_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for filepath in sorted(gt_output_dir.glob("*.json")):
        page_keywords, total_pages = load_gt_keywords(filepath)
        for page_num, keywords in sorted(page_keywords.items()):
            for kw_info in keywords:
                rows.append(
                    {
                        "file_name": filepath.name,
                        "base_name": get_base_filename(filepath.name),
                        "page_number": page_num,
                        "total_pages": total_pages,
                        "keyword": kw_info.get("keyword", ""),
                        "reason": kw_info.get("reason", ""),
                    }
                )
    return rows


def join_keywords(values: Iterable[str]) -> str:
    return ", ".join(sorted((str(value) for value in values if str(value).strip()), key=str.lower))


def original_by_normalized(values: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        normalized = normalize_keyword(value)
        if normalized and normalized not in result:
            result[normalized] = value
    return result


def extract_detailed_comparison_data(
    best_whoosh_output_dir: Path,
    rp_output_dir: Path,
    gt_output_dir: Path,
    whoosh_glob: str,
    rp_glob: str,
    preferred_match_field: str,
) -> list[dict[str, Any]]:
    gt_files = build_file_mapping(gt_output_dir, "*.json")
    whoosh_files = build_file_mapping(best_whoosh_output_dir, whoosh_glob)
    rp_files = build_file_mapping(rp_output_dir, rp_glob)
    common_bases = set(gt_files) & set(whoosh_files) & set(rp_files)
    rows: list[dict[str, Any]] = []

    for base_name in sorted(common_bases):
        gt_pages, total_pages = load_gt_keywords(gt_files[base_name])
        whoosh_pages = load_method_keywords(whoosh_files[base_name], preferred_match_field)
        rp_pages = load_method_keywords(rp_files[base_name], preferred_match_field)
        if total_pages <= 0:
            total_pages = max(set(gt_pages) | set(whoosh_pages) | set(rp_pages), default=0)
        all_pages = set(range(1, total_pages + 1)) | set(gt_pages) | set(whoosh_pages) | set(rp_pages)

        for page_num in sorted(all_pages):
            gt_original = [kw["keyword"] for kw in gt_pages.get(page_num, [])]
            whoosh_original = whoosh_pages.get(page_num, [])
            rp_original = rp_pages.get(page_num, [])
            gt_map = original_by_normalized(gt_original)
            whoosh_map = original_by_normalized(whoosh_original)
            rp_map = original_by_normalized(rp_original)
            gt_norm = set(gt_map)
            whoosh_norm = set(whoosh_map)
            rp_norm = set(rp_map)
            if not (gt_norm or whoosh_norm or rp_norm):
                continue

            whoosh_tp = gt_norm & whoosh_norm
            whoosh_fp = whoosh_norm - gt_norm
            whoosh_fn = gt_norm - whoosh_norm
            rp_tp = gt_norm & rp_norm
            rp_fp = rp_norm - gt_norm
            rp_fn = gt_norm - rp_norm
            rows.append(
                {
                    "file_name": f"{base_name}.json",
                    "page_number": page_num,
                    "total_pages": total_pages,
                    "gt_keywords": join_keywords(gt_original),
                    "whoosh_keywords": join_keywords(whoosh_original),
                    "rp_keywords": join_keywords(rp_original),
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
    return rows


def excel_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def clean_sheet_name(name: str, used_names: set[str]) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", " ", name).strip()[:31] or "Sheet"
    candidate = cleaned
    suffix = 2
    while candidate in used_names:
        suffix_text = f" {suffix}"
        candidate = f"{cleaned[:31 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def normalize_excel_value(value: Any) -> Any:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    return value


def dataframe_rows(dataframe: pd.DataFrame) -> list[list[Any]]:
    rows: list[list[Any]] = [list(dataframe.columns)]
    for row in dataframe.itertuples(index=False, name=None):
        rows.append([normalize_excel_value(value) for value in row])
    return rows


def worksheet_xml(rows: list[list[Any]]) -> str:
    row_xml: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(row, start=1):
            cell_ref = f"{excel_column_name(column_index)}{row_index}"
            value = normalize_excel_value(value)
            if isinstance(value, bool):
                cells.append(f'<c r="{cell_ref}" t="b"><v>{1 if value else 0}</v></c>')
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{cell_ref}"><v>{value}</v></c>')
            else:
                text = escape(str(value))
                cells.append(f'<c r="{cell_ref}" t="inlineStr"><is><t>{text}</t></is></c>')
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        + "".join(row_xml)
        + "</sheetData>"
        "</worksheet>"
    )


def write_xlsx_workbook(output_excel_path: Path, sheets: list[tuple[str, list[list[Any]]]]) -> None:
    used_names: set[str] = set()
    safe_sheets = [(clean_sheet_name(name, used_names), rows) for name, rows in sheets if rows]
    if not safe_sheets:
        safe_sheets = [("Report", [["message"], ["No report data generated"]])]

    workbook_sheets = []
    workbook_rels = []
    content_overrides = [
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    ]

    for index, (sheet_name, _) in enumerate(safe_sheets, start=1):
        workbook_sheets.append(
            f'<sheet name="{escape(sheet_name)}" sheetId="{index}" r:id="rId{index}"/>'
        )
        workbook_rels.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
        content_overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        + "".join(workbook_sheets)
        + "</sheets>"
        "</workbook>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(workbook_rels)
        + "</Relationships>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(content_overrides)
        + "</Types>"
    )

    output_excel_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_excel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        for index, (_, rows) in enumerate(safe_sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", worksheet_xml(rows))


def available_excel_engine() -> str | None:
    if importlib.util.find_spec("xlsxwriter") is not None:
        return "xlsxwriter"
    if importlib.util.find_spec("openpyxl") is not None:
        return "openpyxl"
    return None


def dataframe_column_widths(
    dataframe: pd.DataFrame,
    max_width: int = 80,
    sample_rows: int = 500,
) -> list[int]:
    widths: list[int] = []
    sampled = dataframe.head(sample_rows)
    for column in dataframe.columns:
        values = [column]
        if column in sampled:
            values.extend(sampled[column].astype(str).tolist())
        max_length = max((len(str(value)) for value in values if value is not None), default=8)
        widths.append(min(max(max_length + 2, 10), max_width))
    return widths


def style_xlsxwriter_sheet(writer: pd.ExcelWriter, sheet_name: str, dataframe: pd.DataFrame) -> None:
    workbook = writer.book
    worksheet = writer.sheets[sheet_name]
    header_format = workbook.add_format(
        {
            "bold": True,
            "bg_color": "#D9EAF7",
            "border": 1,
            "text_wrap": True,
            "valign": "top",
        }
    )

    for col_idx, column_name in enumerate(dataframe.columns):
        worksheet.write(0, col_idx, column_name, header_format)

    worksheet.freeze_panes(1, 0)
    if len(dataframe.columns) > 0:
        worksheet.autofilter(0, 0, max(len(dataframe), 1), len(dataframe.columns) - 1)
    for col_idx, width in enumerate(dataframe_column_widths(dataframe)):
        worksheet.set_column(col_idx, col_idx, width)
    worksheet.set_zoom(90)


def style_openpyxl_sheet(writer: pd.ExcelWriter, sheet_name: str, dataframe: pd.DataFrame) -> None:
    from openpyxl.styles import Border, Font, PatternFill, Side

    worksheet = writer.sheets[sheet_name]
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions

    header_fill = PatternFill("solid", fgColor="D9EAF7")
    header_font = Font(bold=True)
    border = Border(bottom=Side(style="thin", color="B7C9D6"))

    for cell in worksheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border

    for col_idx, width in enumerate(dataframe_column_widths(dataframe), start=1):
        worksheet.column_dimensions[excel_column_name(col_idx)].width = width


def write_dataframe_sheet(
    writer: pd.ExcelWriter,
    engine: str,
    sheet_name: str,
    dataframe: pd.DataFrame,
) -> None:
    dataframe.to_excel(writer, sheet_name=sheet_name, index=False)
    if engine == "xlsxwriter":
        style_xlsxwriter_sheet(writer, sheet_name, dataframe)
    elif engine == "openpyxl":
        style_openpyxl_sheet(writer, sheet_name, dataframe)


def write_excel_report_with_engine(
    output_excel_path: Path,
    sheets: list[tuple[str, pd.DataFrame]],
    engine: str,
) -> None:
    output_excel_path.parent.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    with pd.ExcelWriter(output_excel_path, engine=engine) as writer:
        for sheet_name, dataframe in sheets:
            safe_sheet_name = clean_sheet_name(sheet_name, used_names)
            write_dataframe_sheet(writer, engine, safe_sheet_name, dataframe)


def write_excel_report(
    output_excel_path: Path,
    parameter_results_df: pd.DataFrame,
    best_vs_rp_df: pd.DataFrame | None,
    gt_data: list[dict[str, Any]],
    detailed_data: list[dict[str, Any]],
) -> None:
    dataframe_sheets: list[tuple[str, pd.DataFrame]] = [
        ("Parameter Tuning Results", parameter_results_df)
    ]
    if best_vs_rp_df is not None:
        dataframe_sheets.append(("Best WHOOSH vs RapidFuzz", best_vs_rp_df))
    if gt_data:
        dataframe_sheets.append(("Ground Truth (LLM)", pd.DataFrame(gt_data)))
    if detailed_data:
        dataframe_sheets.append(("Detailed Comparison", pd.DataFrame(detailed_data)))

    engine = available_excel_engine()
    if engine is not None:
        write_excel_report_with_engine(output_excel_path, dataframe_sheets, engine)
        return

    LOGGER.warning(
        "Neither xlsxwriter nor openpyxl is installed; using basic XLSX fallback writer."
    )
    fallback_sheets = [
        (sheet_name, dataframe_rows(dataframe))
        for sheet_name, dataframe in dataframe_sheets
    ]
    write_xlsx_workbook(output_excel_path, fallback_sheets)


def output_path_for_report(config: AppConfig) -> Path:
    if config.paths.report_output is not None:
        return config.paths.report_output
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return config.paths.execution_summary.parent / f"parameter_tuning_results_{timestamp}.xlsx"


def generate_report_from_config(config: AppConfig) -> Path:
    successful_runs = load_successful_runs(config.paths.execution_summary)
    if not successful_runs:
        raise ValueError(f"No successful runs found in {config.paths.execution_summary}")

    rows: list[dict[str, Any]] = []
    for index, run in enumerate(successful_runs, start=1):
        LOGGER.info("[%s/%s] Comparing run %s", index, len(successful_runs), run.get("run_id"))
        metrics = compare_output_dir_vs_gt(
            method_output_dir=Path(str(run["output_dir"])),
            gt_output_dir=config.paths.gt_output,
            method_glob=config.report.whoosh_glob,
            preferred_match_field=config.report.method_key_field,
        ).to_dict()
        rows.append(run_to_row(run, metrics))

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError("No comparison rows were generated.")
    if config.report.rank_by not in df.columns:
        raise ValueError(f"Report rank_by column not found: {config.report.rank_by}")

    sort_columns = [config.report.rank_by]
    for tie_breaker in ("kw_recall", "page_recall", "kw_precision", "page_precision"):
        if tie_breaker not in sort_columns and tie_breaker in df.columns:
            sort_columns.append(tie_breaker)
    df = df.sort_values(sort_columns, ascending=[False] * len(sort_columns)).reset_index(drop=True)
    df.insert(0, "rank", range(1, len(df) + 1))

    best_run_id = int(df.iloc[0]["run_id"])
    best_run = next(run for run in successful_runs if int(run["run_id"]) == best_run_id)
    best_vs_rp_df: pd.DataFrame | None = None
    detailed_data: list[dict[str, Any]] = []

    if config.paths.rp_output is not None and config.paths.rp_output.exists():
        whoosh_metrics = compare_output_dir_vs_gt(
            method_output_dir=Path(str(best_run["output_dir"])),
            gt_output_dir=config.paths.gt_output,
            method_glob=config.report.whoosh_glob,
            preferred_match_field=config.report.method_key_field,
        ).to_dict()
        rp_metrics = compare_output_dir_vs_gt(
            method_output_dir=config.paths.rp_output,
            gt_output_dir=config.paths.gt_output,
            method_glob=config.report.rp_glob,
            preferred_match_field=config.report.method_key_field,
        ).to_dict()
        best_vs_rp_df = pd.DataFrame(
            [
                {"method": "Ground Truth", **{key: whoosh_metrics[key] for key in whoosh_metrics}},
                {"method": "WHOOSH Best", **whoosh_metrics},
                {"method": "RapidFuzz", **rp_metrics},
            ]
        )
        detailed_data = extract_detailed_comparison_data(
            best_whoosh_output_dir=Path(str(best_run["output_dir"])),
            rp_output_dir=config.paths.rp_output,
            gt_output_dir=config.paths.gt_output,
            whoosh_glob=config.report.whoosh_glob,
            rp_glob=config.report.rp_glob,
            preferred_match_field=config.report.method_key_field,
        )
    elif config.paths.rp_output is not None:
        LOGGER.warning("RapidFuzz output directory does not exist: %s", config.paths.rp_output)

    output_excel_path = output_path_for_report(config)
    write_excel_report(
        output_excel_path=output_excel_path,
        parameter_results_df=df,
        best_vs_rp_df=best_vs_rp_df,
        gt_data=extract_ground_truth_data(config.paths.gt_output),
        detailed_data=detailed_data,
    )
    LOGGER.info("Report saved to: %s", output_excel_path)
    return output_excel_path


def main() -> int:
    configure_logging()
    config = load_config()
    generate_report_from_config(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
