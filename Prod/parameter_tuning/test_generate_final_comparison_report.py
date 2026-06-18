from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_final_comparison_report import compare_output_dir_vs_gt  # noqa: E402


class ComparisonMetricsTest(unittest.TestCase):
    def write_json(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def compare_case(self, gt_data: object, method_data: object) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            gt_dir = root / "gt"
            method_dir = root / "method"
            self.write_json(gt_dir / "case.json", gt_data)
            self.write_json(method_dir / "case_keyword_output.json", method_data)

            return compare_output_dir_vs_gt(
                method_output_dir=method_dir,
                gt_output_dir=gt_dir,
                method_glob="*_keyword_output.json",
                preferred_match_field="group",
            ).to_dict()

    def test_page_overlap_counts_tp_fp_fn_tn(self) -> None:
        metrics = self.compare_case(
            {
                "total_pages": 5,
                "pages_with_keywords": [
                    {"page_number": 1, "keywords_detected": ["alpha"]},
                    {"page_number": 2, "keywords_detected": ["alpha"]},
                    {"page_number": 3, "keywords_detected": ["alpha"]},
                ],
            },
            [
                {"page_number": 2, "matches": [{"group": "alpha", "variant": "alpha"}]},
                {"page_number": 3, "matches": [{"group": "alpha", "variant": "alpha"}]},
                {"page_number": 4, "matches": [{"group": "alpha", "variant": "alpha"}]},
            ],
        )

        self.assertEqual(metrics["page_tp"], 2)
        self.assertEqual(metrics["page_fp"], 1)
        self.assertEqual(metrics["page_fn"], 1)
        self.assertEqual(metrics["page_tn"], 1)
        self.assertEqual(metrics["kw_tp"], 2)
        self.assertEqual(metrics["kw_fp"], 1)
        self.assertEqual(metrics["kw_fn"], 1)

    def test_duplicate_method_page_rows_are_merged(self) -> None:
        metrics = self.compare_case(
            {
                "total_pages": 2,
                "pages_with_keywords": [
                    {"page_number": 1, "keywords_detected": ["alpha"]},
                ],
            },
            [
                {"page_number": 1, "matches": [{"group": "alpha", "variant": "alpha"}]},
                {"page_number": 1, "matches": [{"group": "beta", "variant": "beta"}]},
            ],
        )

        self.assertEqual(metrics["page_tp"], 1)
        self.assertEqual(metrics["page_fp"], 0)
        self.assertEqual(metrics["page_fn"], 0)
        self.assertEqual(metrics["page_tn"], 1)
        self.assertEqual(metrics["kw_tp"], 1)
        self.assertEqual(metrics["kw_fp"], 1)
        self.assertEqual(metrics["kw_fn"], 0)

    def test_duplicate_gt_page_rows_are_merged(self) -> None:
        metrics = self.compare_case(
            {
                "total_pages": 2,
                "pages_with_keywords": [
                    {"page_number": 1, "keywords_detected": ["alpha"]},
                    {"page_number": 1, "keywords_detected": ["beta"]},
                ],
            },
            [
                {
                    "page_number": 1,
                    "matches": [
                        {"group": "alpha", "variant": "alpha"},
                        {"group": "beta", "variant": "beta"},
                    ],
                },
            ],
        )

        self.assertEqual(metrics["page_tp"], 1)
        self.assertEqual(metrics["page_fp"], 0)
        self.assertEqual(metrics["page_fn"], 0)
        self.assertEqual(metrics["page_tn"], 1)
        self.assertEqual(metrics["kw_tp"], 2)
        self.assertEqual(metrics["kw_fp"], 0)
        self.assertEqual(metrics["kw_fn"], 0)


if __name__ == "__main__":
    unittest.main()
