#!/usr/bin/env python3
"""Join retrospective.sh's batch outputs (metadata + MIRAI results) into the
same multi-sheet Excel report dicom-analyzer's GUI produces.

Run after retrospective.sh's steps 0-3 (needs the metadata and results CSVs
it writes under DESTINATION_DIRECTORY). Reuses pipeline.py's sanitize() and
build_excel_report() directly, so the batch report always matches the
interactive GUI's format.
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from pipeline import sanitize, build_excel_report, SCORE_YEARS  # noqa: E402


def build_batch_report(
    metadata_csv: Path,
    results_csv: Path,
    output_xlsx: Path,
    multiple_view_choice: str = "first",
    anonymized_dir: Path = None,
) -> bytes:
    raw_df = pd.read_csv(metadata_csv, dtype=str)
    exams_df, dropped_df, stats = sanitize(raw_df, multiple_view_choice)

    for year in SCORE_YEARS:
        exams_df[year] = None
    exams_df["mirai_error"] = None

    if results_csv.exists():
        results_df = pd.read_csv(results_csv, dtype=str)
        for _, row in results_df.iterrows():
            try:
                result = json.loads(row["result_json"])
                predictions = (result.get("data") or {}).get("predictions") or {}
            except (TypeError, ValueError, json.JSONDecodeError):
                predictions = {}
            mask = exams_df["studyuid"] == row["exam_id"]
            for year in SCORE_YEARS:
                exams_df.loc[mask, year] = predictions.get(year)

    failed_csv = results_csv.with_name(results_csv.stem + "_failed.csv")
    if failed_csv.exists():
        failed_df = pd.read_csv(failed_csv, dtype=str)
        for _, row in failed_df.iterrows():
            exams_df.loc[exams_df["studyuid"] == row["exam_id"], "mirai_error"] = row["error"]

    if anonymized_dir is not None and anonymized_dir.is_dir():
        scanned_total_files = sum(1 for _ in anonymized_dir.rglob("*") if _.is_file())
    else:
        scanned_total_files = len(raw_df)

    excel_bytes = build_excel_report(raw_df, exams_df, dropped_df, stats, scanned_total_files)
    output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    output_xlsx.write_bytes(excel_bytes)
    return excel_bytes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata_csv", type=Path, help="dicom_metadata_*.csv from collect_dicoms.py")
    parser.add_argument("results_csv", type=Path, help="mirai_results_*.csv from run_mirai.py")
    parser.add_argument("output_xlsx", type=Path)
    parser.add_argument("--multiple_view_choice", choices=["first", "last"], default="first")
    parser.add_argument(
        "--anonymized_dir",
        type=Path,
        default=None,
        help="anonymized_dicoms_*/ folder, to count total scanned files (optional).",
    )
    args = parser.parse_args()

    build_batch_report(
        args.metadata_csv,
        args.results_csv,
        args.output_xlsx,
        args.multiple_view_choice,
        args.anonymized_dir,
    )
    print(f"Report saved to {args.output_xlsx}")
