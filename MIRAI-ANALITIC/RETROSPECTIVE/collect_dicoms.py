
#!/usr/bin/env python3

import argparse
import os
from pathlib import Path
from functools import partial
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pydicom
from tqdm import tqdm


DICOM_TAGS = [
    "PatientID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "StudyDate",
    "AcquisitionDateTime",
    "AcquisitionDate",
    "AcquisitionTime",
    "InstanceNumber",
    "ImageType",
    "ImageLaterality",
    "Laterality",
    "ViewPosition",
    "InstitutionName",
    "Manufacturer",
    "ManufacturerModelName",
    "Modality",
]


OUTPUT_COLUMNS = [
    "file_path",
    "PatientID",
    "StudyUID",
    "SeriesUID",
    "ImageUID",
    "study_date",
    "acquisition_datetime",
    "acquisition_date",
    "acquisition_time",
    "instance_number",
    "image_type",
    "Hospital",
    "Manufacturer",
    "Machine",
    "Modality",
    "laterality",
    "view_position",
    "original_view_position",
    "Comment",
    "type",
]


def scan_file(
    file_path: Path,
    patient_id_depth=None,
    hospital_tag=None,
):
    """
    Extract DICOM metadata without loading pixel data.
    """

    file_path = Path(file_path)

    tags = DICOM_TAGS.copy()

    if hospital_tag is not None:
        tags.append(hospital_tag)

    try:
        ds = pydicom.dcmread(
            file_path,
            stop_before_pixels=True,
            specific_tags=tags,
        )

        patient_id = ds.get("PatientID", None)

        # Optional override using a parent directory.
        if patient_id_depth is not None:
            patient_id = file_path.parents[
                patient_id_depth
            ].name

        hospital = ds.get("InstitutionName", None)

        # Optional institution-specific private tag.
        if hospital_tag is not None:
            element = ds.get(hospital_tag)
            if element is not None:
                hospital = str(element.value)

        laterality = (
            ds.get("ImageLaterality")
            or ds.get("Laterality")
        )

        original_view = ds.get("ViewPosition", None)
        view_position = original_view
        comment = ""

        if view_position == "ML":
            view_position = "MLO"
            comment = "ML view"

        image_type = (
            f"{laterality}_{view_position}"
            if laterality and view_position
            else None
        )

        return {
            "file_path": str(file_path),
            "PatientID": patient_id,
            "StudyUID": ds.get("StudyInstanceUID"),
            "SeriesUID": ds.get("SeriesInstanceUID"),
            "ImageUID": ds.get("SOPInstanceUID"),
            "study_date": ds.get("StudyDate"),
            "acquisition_datetime": ds.get("AcquisitionDateTime"),
            "acquisition_date": ds.get("AcquisitionDate"),
            "acquisition_time": ds.get("AcquisitionTime"),
            "instance_number": ds.get("InstanceNumber"),
            "image_type": "|".join(ds.get("ImageType") or []),
            "Hospital": hospital,
            "Manufacturer": ds.get("Manufacturer"),
            "Machine": ds.get("ManufacturerModelName"),
            "Modality": ds.get("Modality"),
            "laterality": laterality,
            "view_position": view_position,
            "original_view_position": original_view,
            "Comment": comment,
            "type": image_type,
        }

    except pydicom.errors.InvalidDicomError:
        return None

    except Exception as e:
        tqdm.write(f"Error reading {file_path}: {e}")
        return None


def collect_dicoms(
    path: Path,
    output_path: Path,
    max_workers: int = 16,
    extensions=None,
    patient_id_depth=None,
    hospital_tag=None,
):

    path = Path(path).resolve()
    output_path = Path(output_path)

    if not path.is_dir():
        raise NotADirectoryError(path)

    if extensions:
        extensions = {
            "." + ext.lower().lstrip(".")
            for ext in extensions
        }

    print(f"Finding candidate files in {path}")

    to_scan = []

    for root, _, files in os.walk(path):
        for filename in files:
            file_path = Path(root) / filename

            if (
                extensions is None
                or file_path.suffix.lower() in extensions
            ):
                to_scan.append(file_path)

    print(f"Found {len(to_scan):,} candidate files.")

    scanner = partial(
        scan_file,
        patient_id_depth=patient_id_depth,
        hospital_tag=hospital_tag,
    )

    results = []

    with ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:

        iterator = tqdm(
            executor.map(scanner, to_scan),
            total=len(to_scan),
            desc="Scanning DICOM headers",
        )

        for result in iterator:
            if result is not None:
                results.append(result)

    df = pd.DataFrame(
        results,
        columns=OUTPUT_COLUMNS,
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    df.to_csv(output_path, index=False)

    print(f"\nSaved results to {output_path}")
    print(f"Valid DICOM files: {len(df):,}")
    print(f"Skipped files: {len(to_scan) - len(df):,}")

    return df


def check_results(df):

    print("\nDataset summary")
    print("-" * 40)

    print(f"Images:    {len(df):,}")
    print(f"Patients:  {df['PatientID'].nunique():,}")
    print(f"Studies:   {df['StudyUID'].nunique():,}")
    print(f"Hospitals: {df['Hospital'].nunique():,}")
    print(f"Machines:  {df['Machine'].nunique():,}")

    print("\nModalities:")
    print(df["Modality"].value_counts(dropna=False))

    print("\nImage types:")
    print(df["type"].value_counts(dropna=False))

    print("\nStudies per patient:")
    print(
        df.groupby("PatientID")["StudyUID"]
        .nunique()
        .value_counts()
        .sort_index()
    )

    print("\nMissing metadata (%):")
    print(
        df.isna()
        .mean()
        .mul(100)
        .round(2)
        .to_string()
    )


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Collect DICOM metadata from a directory."
    )

    parser.add_argument(
        "--input_path",
        type=Path,
        required=True,
    )

    parser.add_argument(
        "--output_path",
        type=Path,
        default=Path("dicom_metadata.csv"),
    )

    parser.add_argument(
        "--max_workers",
        type=int,
        default=16,
    )

    parser.add_argument(
        "--extensions",
        nargs="+",
        default=None,
        help="Optional file extension filter.",
    )

    parser.add_argument(
        "--patient_id_depth",
        type=int,
        default=None,
        help="Extract PatientID from a parent directory.",
    )

    parser.add_argument(
        "--hospital_tag",
        nargs=2,
        type=lambda x: int(x, 16),
        default=None,
        help="Optional private DICOM tag for hospital.",
    )

    args = parser.parse_args()

    df = collect_dicoms(
        path=args.input_path,
        output_path=args.output_path,
        max_workers=args.max_workers,
        extensions=args.extensions,
        patient_id_depth=args.patient_id_depth,
        hospital_tag=(
            tuple(args.hospital_tag)
            if args.hospital_tag else None
        ),
    )

    check_results(df)