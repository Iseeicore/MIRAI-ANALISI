
from pathlib import Path
from typing import Literal

import pandas as pd


VIEWS = ["L_CC", "L_MLO", "R_CC", "R_MLO"]


def check_all_good(df):
    if df.empty:
        raise ValueError("No data found in the DataFrame.")

def select_unique_views(
    df: pd.DataFrame,
    multiple_view_choice: Literal["first", "last"] = "first",
):
    df = df.copy()

    def get_column(name):
        return df.get(
            name,
            pd.Series(pd.NA, index=df.index, dtype="string")
        ).astype("string")

    # Prefer full acquisition datetime.
    acquisition_datetime = get_column("acquisition_datetime")

    # Fall back to separate date and time fields.
    acquisition_date = get_column("acquisition_date")
    acquisition_date = acquisition_date.fillna(
        get_column("study_date")
    )
    acquisition_time = get_column("acquisition_time")

    fallback_datetime = acquisition_date + acquisition_time

    df["_timestamp"] = acquisition_datetime.fillna(
        fallback_datetime
    )

    # Prefer native MLO over remapped ML.
    df["_remapped_view"] = (
        get_column("original_view_position")
        .eq("ML")
        .fillna(False)
    )

    # Instance numbers are numeric, not strings.
    df["_instance_number"] = pd.to_numeric(
        get_column("instance_number"),
        errors="coerce",
    )

    # Only compare instance numbers within the same series.
    series_uid = get_column("seriesuid")
    series_count = series_uid.groupby(
        [df["studyuid"], df["type"]]
    ).transform("nunique")

    missing_series = series_uid.isna().groupby(
        [df["studyuid"], df["type"]]
    ).transform("any")

    df.loc[
        (series_count != 1) | missing_series,
        "_instance_number"
    ] = float("nan")

    earliest = multiple_view_choice == "first"

    df = df.sort_values(
        by=[
            "studyuid",
            "type",
            "_remapped_view",
            "_timestamp",
            "_instance_number",
            "file_path",
        ],
        ascending=[
            True,
            True,
            True,
            earliest,
            earliest,
            earliest,
        ],
        na_position="last",
    )

    n_before = len(df)

    df = df.drop_duplicates(
        subset=["studyuid", "type"],
        keep="first",
    )

    print(f"Removed {n_before - len(df):,} duplicate views.")

    return df.drop(
        columns=[
            "_remapped_view",
            "_timestamp",
            "_instance_number",
        ]
    )

def prepare_mammography(
    input_csv: Path,
    output_csv: Path,
    full_exams_only: bool = True,
    multiple_view_choice: Literal["first", "last"] = "first",
):
    if multiple_view_choice not in {"first", "last"}:
        raise ValueError("Invalid multiple_view_choice.")

    df = pd.read_csv(input_csv, dtype=str)
    df.columns = df.columns.str.lower()

    if "modality" in df.columns:
        df = df[df["modality"] == "MG"].copy()

    check_all_good(df)

    if "type" not in df.columns:
        df["type"] = (
            df["laterality"] + "_" + df["view_position"]
        )

    df = df[df["type"].isin(VIEWS)].copy()

    df = df.dropna(subset=["studyuid", "file_path"])

    check_all_good(df)

    df = select_unique_views(df, multiple_view_choice)

    # One row per study
    pivotted = df.pivot(
        index="studyuid",
        columns="type",
        values="file_path",
    ).reindex(columns=VIEWS)

    # Study-level metadata
    metadata_columns = [
        col for col in [
            "patientid",
            "study_date",
            "hospital",
            "machine",
        ]
        if col in df.columns
    ]

    metadata = df.groupby("studyuid")[
        metadata_columns
    ].first()

    pivotted = pivotted.join(metadata).reset_index()
    pivotted.columns.name = None

    # Exam completeness
    pivotted["num_views"] = (
        pivotted[VIEWS].notna().sum(axis=1)
    )

    pivotted["full_exam"] = (
        pivotted["num_views"] == 4
    ).astype(int)

    print(f"Total exams: {len(pivotted):,}")
    print(f"Full exams: {pivotted['full_exam'].sum():,}")

    print("\nExam completeness:")
    print(
        pivotted["num_views"]
        .value_counts()
        .sort_index()
    )

    if full_exams_only:
        pivotted = pivotted[
            pivotted["full_exam"] == 1
        ].copy()

    print(f"Retained exams: {len(pivotted):,}")

    # MIRAI-compatible column names
    pivotted = pivotted.rename(
        columns={
            "patientid": "ClientID",
            "studyuid": "StudyInstanceUID",
            "study_date": "StudyDate",
            "hospital": "Hospital",
            "machine": "Machine",
            **{view: f"path_{view}" for view in VIEWS},
        }
    )
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    pivotted.to_csv(output_csv, index=False)

    return pivotted

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Prepare mammography dataset for MIRAI."
    )
    parser.add_argument(
        "--input_csv",
        type=Path,
        help="Path to the input CSV file containing DICOM metadata.",
    )
    parser.add_argument(
        "--output_csv",
        type=Path,
        help="Path to the output CSV file for the prepared dataset.",
    )
    parser.add_argument(
        "--full_exams_only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include only full exams (all four views).",
    )
    parser.add_argument(
        "--multiple_view_choice",
        choices=["first", "last"],
        default="first",
        help="How to handle duplicate views per patient: 'first' or 'last'.",
    )

    args = parser.parse_args()

    prepare_mammography(
        input_csv=args.input_csv,
        output_csv=args.output_csv,
        full_exams_only=args.full_exams_only,
        multiple_view_choice=args.multiple_view_choice,
    )