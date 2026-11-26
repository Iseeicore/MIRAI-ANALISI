"""Operator CLI.

  python -m ingest.cli create-batch <root_inside_container>
  python -m ingest.cli status [batch_id]

Status shows counts per study status, scores of DONE studies and reasons for INCOMPLETE/FAILED
ones, by internal study id only (no patient data).
"""
import argparse
import sys

from . import service
from .config import build_repository, load_settings, load_source_policy
from .sources import SourceRejected


def _print_status(report: dict):
    for b in report.get("batches", []):
        print(f"batch {b['batch_id']}: {b['status']} (studies={b['total_studies']}, images={b['total_images']},"
              f" skipped={b['total_skipped']})")
    print("studies by status:")
    for status, n in report["counts"].items():
        print(f"  {status:<11} {n}")
    if report["done"]:
        print("DONE scores:")
        print("  study_id  pseudonym     " + "  ".join(f"{y:>7}" for y in report["done"][0]["scores"]) + "  folder")
        for row in report["done"]:
            values = "  ".join(f"{v:>7.4f}" if v is not None else f"{'-':>7}" for v in row["scores"].values())
            print(f"  {row['study_id']:<8}  {row['pseudonym']:<12}  {values}  {row['folder']}")
    if report["problems"]:
        print("INCOMPLETE / FAILED:")
        for row in report["problems"]:
            print(f"  {row['study_id']:<8}  {row['pseudonym']:<12}  {row['status']:<10}  {row['folder']}  {row['error']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Batch ingestion CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-batch", help="queue a folder tree for crawling")
    create.add_argument("root", help="folder under /dicom inside the containers (e.g. /dicom/e2e) "
                                     "or an http(s) index URL on an INGEST_ALLOWED_HOSTS host")
    status = sub.add_parser("status", help="show progress and scores")
    status.add_argument("batch_id", nargs="?", type=int)
    args = parser.parse_args(argv)

    repo = build_repository(load_settings())
    repo.apply_schema()
    if args.command == "create-batch":
        try:
            batch_id = service.create_batch(repo, args.root, load_source_policy())
        except SourceRejected as exc:
            sys.exit(f"rejected: {exc}")
        print(f"batch {batch_id} created")
    elif args.batch_id is not None:
        status = service.batch_status(repo, args.batch_id)
        if status is None:
            sys.exit(f"batch {args.batch_id} not found")
        _print_status(dict(status, batches=[status]))
    else:
        _print_status(repo.status_report(None))


if __name__ == "__main__":
    main()
