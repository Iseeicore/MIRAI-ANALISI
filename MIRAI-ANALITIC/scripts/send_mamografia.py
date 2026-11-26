#!/usr/bin/env python3
"""Send one mammography exam (4 DICOM views) to MIRAI via Newman.

Reuses postman/MIRAI.postman_collection.json as the single source of truth
for the request shape, injecting the client's file paths and metadata into
a throwaway copy before handing it to Newman.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

COLLECTION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "postman",
    "MIRAI.postman_collection.json",
)
REQUEST_NAME = "Send Mammogram (4 DICOM views)"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ccr", required=True, help="Path to the CC-R .dcm file")
    parser.add_argument("--ccl", required=True, help="Path to the CC-L .dcm file")
    parser.add_argument("--mlor", required=True, help="Path to the MLO-R .dcm file")
    parser.add_argument("--mlol", required=True, help="Path to the MLO-L .dcm file")
    parser.add_argument("--mrn", required=True, help="Patient medical record number")
    parser.add_argument("--accession", required=True, help="Accession number")
    parser.add_argument("--patient-name", required=True, help="Patient name")
    parser.add_argument(
        "--base-url",
        default=None,
        help="MIRAI base URL (defaults to the collection's baseUrl variable)",
    )
    return parser.parse_args()


def validate_dicom_path(path):
    if not os.path.isfile(path):
        sys.exit(f"Error: '{path}' no es un archivo válido (¿existe y no es una carpeta?).")


def main():
    args = parse_args()
    dicom_paths = [args.ccr, args.ccl, args.mlor, args.mlol]
    for path in dicom_paths:
        validate_dicom_path(path)

    with open(COLLECTION_PATH) as f:
        collection = json.load(f)

    if args.base_url:
        for var in collection["variable"]:
            if var["key"] == "baseUrl":
                var["value"] = args.base_url

    request_item = next(
        item for item in collection["item"] if item["name"] == REQUEST_NAME
    )
    metadata = {
        "mrn": args.mrn,
        "accession": args.accession,
        "patient_name": args.patient_name,
    }
    for field in request_item["request"]["body"]["formdata"]:
        if field["key"] == "data":
            field["value"] = json.dumps({"metadata": metadata})
        elif field["key"] == "dicom":
            field["src"] = [os.path.abspath(p) for p in dicom_paths]

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".postman_collection.json", delete=False
    ) as tmp:
        json.dump(collection, tmp)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["npx", "--yes", "newman", "run", tmp_path, "--folder", REQUEST_NAME]
        )
        return result.returncode
    finally:
        os.remove(tmp_path)


if __name__ == "__main__":
    sys.exit(main())
