from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class MethodMetrics:
    gt_file_count: int = 0
    method_file_count: int = 0
    common_file_count: int = 0
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
            "gt_file_count": self.gt_file_count,
            "method_file_count": self.method_file_count,
            "common_file_count": self.common_file_count,
            "gt_total_keywords": self.gt_total_keywords,
            "method_total_detected": self.method_total_detected,
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


def read_json(filepath: Path) -> Any:
    return json.loads(Path(filepath).read_text(encoding="utf-8"))


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
    return re.sub(r"\s+", " ", str(keyword).strip().lower())


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
    if not Path(directory).exists():
        return mapping
    for path in Path(directory).rglob(glob_pattern):
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
        page_keywords = result.setdefault(page_num, [])
        seen = {
            normalize_keyword(item.get("keyword", ""))
            for item in page_keywords
            if normalize_keyword(item.get("keyword", ""))
        }
        for kw_item in page_info.get("keywords_detected", []) or []:
            keyword, reason = extract_keyword_from_gt_item(kw_item)
            normalized = normalize_keyword(keyword)
            if normalized and normalized not in seen:
                seen.add(normalized)
                page_keywords.append({"keyword": keyword, "reason": reason})

    return result, total_pages if total_pages > 0 else max_seen_page


def dedupe_preserve_order(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = normalize_keyword(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(str(value).strip())
    return result


def extract_method_keywords_from_page(page_info: Mapping[str, Any], preferred_match_field: str) -> list[str]:
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
        for key in ("rp_output", "pages", "pages_with_keywords", "results"):
            if isinstance(data.get(key), list):
                pages_raw = data[key]
                break
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
        page_keywords = result.setdefault(page_num, [])
        seen = {normalize_keyword(keyword) for keyword in page_keywords if normalize_keyword(keyword)}
        for keyword in extract_method_keywords_from_page(page_info, preferred_match_field):
            normalized = normalize_keyword(keyword)
            if normalized and normalized not in seen:
                seen.add(normalized)
                page_keywords.append(keyword)
    return result


def precision_recall_f1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    detected = tp + fp
    expected = tp + fn
    precision = (tp / detected * 100) if detected > 0 else 0.0
    recall = (tp / expected * 100) if expected > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall > 0 else 0.0
    return precision, recall, f1


def compare_output_dir_vs_gt(
    method_output_dir: Path,
    gt_output_dir: Path,
    method_glob: str = "*_keyword_output.json",
    preferred_match_field: str = "group",
) -> MethodMetrics:
    gt_files = build_file_mapping(gt_output_dir, "*.json")
    method_files = build_file_mapping(method_output_dir, method_glob)
    common_bases = set(gt_files) & set(method_files)
    if not common_bases:
        return MethodMetrics(
            gt_file_count=len(gt_files),
            method_file_count=len(method_files),
            common_file_count=0,
        )

    total_kw_tp = total_kw_fp = total_kw_fn = 0
    total_page_tp = total_page_fp = total_page_fn = total_page_tn = 0

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
        total_page_tn += len(set(range(1, total_pages + 1)) - (gt_pages_with_keywords | method_pages_with_keywords))

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

    kw_precision, kw_recall, kw_f1 = precision_recall_f1(total_kw_tp, total_kw_fp, total_kw_fn)
    page_precision, page_recall, page_f1 = precision_recall_f1(total_page_tp, total_page_fp, total_page_fn)
    total_pages_all_docs = total_page_tp + total_page_fp + total_page_fn + total_page_tn
    page_accuracy = (
        (total_page_tp + total_page_tn) / total_pages_all_docs * 100
        if total_pages_all_docs > 0
        else 0.0
    )

    return MethodMetrics(
        gt_file_count=len(gt_files),
        method_file_count=len(method_files),
        common_file_count=len(common_bases),
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

