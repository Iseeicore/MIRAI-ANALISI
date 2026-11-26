import argparse
import csv
import json
from contextlib import ExitStack
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from tqdm import tqdm


VIEWS = [
    "path_L_CC",
    "path_R_CC",
    "path_L_MLO",
    "path_R_MLO",
]

REQUIRED_COLUMNS = ["StudyInstanceUID", *VIEWS]


def process_exam(
    exam_id,
    dicom_paths,
    mirai_url,
    timeout=600,
):
    """
    Submit one mammography exam to the MIRAI API.
    """

    with ExitStack() as stack:

        files = [
            (
                "dicom",
                (
                    Path(path).name,
                    stack.enter_context(open(path, "rb")),
                    "application/dicom",
                ),
            )
            for path in dicom_paths
        ]

        response = requests.post(
            mirai_url,
            data={
                "metadata": json.dumps({
                    "exam_id": exam_id
                })
            },
            files=files,
            timeout=(120, timeout),
        )

        response.raise_for_status()

        return response.json()


def load_completed_exams(output_csv):
    """
    Identify exams already present in the results file.
    """

    if not output_csv.exists():
        return set()

    with output_csv.open(newline="") as f:
        return {
            row["exam_id"]
            for row in csv.DictReader(f)
            if row.get("exam_id")
        }


def run_mirai(
    input_csv: Path,
    output_csv: Path,
    mirai_url: str = "http://localhost:5000/dicom/files",
    max_workers: int = 4,
    timeout: int = 600,
):

    input_csv = Path(input_csv)
    output_csv = Path(output_csv)

    output_csv.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    completed = load_completed_exams(output_csv)

    print(f"Previously completed exams: {len(completed):,}")

    pending = []
    seen = set()
    invalid = 0
    skipped = 0

    # Load and validate input CSV.
    with input_csv.open(newline="") as f:
        reader = csv.DictReader(f)

        missing = set(REQUIRED_COLUMNS) - set(
            reader.fieldnames or []
        )

        if missing:
            raise ValueError(
                f"Missing columns: {sorted(missing)}"
            )

        for row in reader:

            exam_id = (row["StudyInstanceUID"] or "").strip()

            if not exam_id:
                invalid += 1
                continue

            if exam_id in completed or exam_id in seen:
                skipped += 1
                continue

            paths = [
                (row.get(view) or "").strip()
                for view in VIEWS
            ]

            if not all(
                path and Path(path).is_file()
                for path in paths
            ):
                invalid += 1
                continue

            pending.append((exam_id, paths))
            seen.add(exam_id)

    print(f"Exams to process: {len(pending):,}")
    print(f"Skipped exams: {skipped:,}")
    print(f"Invalid exams: {invalid:,}")
    print(f"MIRAI endpoint: {mirai_url}")
    print(f"Concurrent requests: {max_workers}")

    write_header = (
        not output_csv.exists()
        or output_csv.stat().st_size == 0
    )

    successful = 0
    failed = 0

    failed_csv = output_csv.with_name(
        output_csv.stem + "_failed.csv"
    )

    with (
        output_csv.open("a", newline="") as f,
        failed_csv.open("w", newline="") as error_f,
        ThreadPoolExecutor(max_workers=max_workers) as executor,
    ):

        writer = csv.DictWriter(
            f,
            fieldnames=["exam_id", "result_json"],
        )

        error_writer = csv.DictWriter(
            error_f,
            fieldnames=["exam_id", "error"],
        )

        if write_header:
            writer.writeheader()
            f.flush()

        error_writer.writeheader()

        futures = {
            executor.submit(
                process_exam,
                exam_id,
                paths,
                mirai_url,
                timeout,
            ): exam_id
            for exam_id, paths in pending
        }

        iterator = tqdm(
            as_completed(futures),
            total=len(futures),
            desc="Running MIRAI",
        )

        for future in iterator:

            exam_id = futures[future]

            try:
                result = future.result()

                writer.writerow({
                    "exam_id": exam_id,
                    "result_json": json.dumps(result),
                })

                f.flush()
                successful += 1

            except Exception as e:

                error_writer.writerow({
                    "exam_id": exam_id,
                    "error": str(e),
                })

                error_f.flush()
                failed += 1

            iterator.set_postfix({
                "successful": successful,
                "failed": failed,
            })

    print("\nMIRAI inference summary")
    print("-" * 40)
    print(f"Previously completed: {len(completed):,}")
    print(f"New successful exams: {successful:,}")
    print(f"Failed requests:      {failed:,}")
    print(f"Invalid exams:        {invalid:,}")
    print(f"Results: {output_csv}")

    if failed:
        print(f"Failed exams: {failed_csv}")

    return failed + invalid == 0

def parse_results(output_csv, output_summary):
    """
    Parse the MIRAI results CSV and generate a summary CSV.
    """

    with output_csv.open(newline="") as f:
        reader = csv.DictReader(f)

        summary_rows = []

        for row in reader:
            exam_id = row["exam_id"]
            result_json = row.get("result_json")

            if not result_json:
                continue

            try:
                result = json.loads(result_json)
            except json.JSONDecodeError:
                continue

            predictions = (result.get("data") or {}).get("predictions") or {}
            summary_row = {
                "exam_id": exam_id,
                "Year 1": predictions.get("Year 1"),
                "Year 2": predictions.get("Year 2"),
                "Year 3": predictions.get("Year 3"),
                "Year 4": predictions.get("Year 4"),
                "Year 5": predictions.get("Year 5"),
                "metadata": result.get("metadata"),
                "runtime": result.get("runtime"),
                "statusCode": result.get("statusCode"),
            }

            summary_rows.append(summary_row)

    with output_summary.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "exam_id",
                "Year 1",
                "Year 2",
                "Year 3",
                "Year 4",
                "Year 5",
                "metadata",
                "runtime",
                "statusCode",
            ]
        )
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"Summary saved to {output_summary}")

if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Run MIRAI on prepared mammography exams."
    )

    parser.add_argument("input_csv", type=Path)
    parser.add_argument("output_csv", type=Path)

    parser.add_argument(
        "--mirai_url",
        default="http://localhost:5000/dicom/files",
    )

    parser.add_argument(
        "--max_workers",
        type=int,
        default=4,
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
    )

    args = parser.parse_args()

    success = run_mirai(
        input_csv=args.input_csv,
        output_csv=args.output_csv,
        mirai_url=args.mirai_url,
        max_workers=args.max_workers,
        timeout=args.timeout,
    )


    parse_results(
        output_csv=args.output_csv,
        output_summary=args.output_csv.with_name(
            args.output_csv.stem + "_summary.csv"
        )
    )

    raise SystemExit(0 if success else 1)
