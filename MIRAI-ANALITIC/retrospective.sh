#!/usr/bin/env bash
which python
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="gen_eval_env"

SOURCE_DIRECTORY="${1:?Usage: ./retrospective.sh SOURCE_DIRECTORY [DESTINATION_DIRECTORY]}"
DESTINATION_DIRECTORY="${2:-./IN_PROGRESS}"
# Derived from the source folder's name so two different cohorts never share
# the same cached anonymized/metadata/sanitized/results files.
COHORT="$(basename "$SOURCE_DIRECTORY")"

SCAN_WORKERS="${SCAN_WORKERS:-16}"
MIRAI_WORKERS="${MIRAI_WORKERS:-4}"
MIRAI_URL="${MIRAI_URL:-http://localhost:8885/dicom/files}"

printf '%s\n' \
    'WARNING: This pipeline handles potentially protected health information (PHI).' \
    'Do not commit its inputs or outputs or use an unapproved inference endpoint.' \
    >&2

mkdir -p "$DESTINATION_DIRECTORY"

SOURCE_DIRECTORY="$(realpath "$SOURCE_DIRECTORY")"
DESTINATION_DIRECTORY="$(realpath "$DESTINATION_DIRECTORY")"

# --------------------------------------------------
# Setup
# --------------------------------------------------

ANONYMIZED_DIR="$DESTINATION_DIRECTORY/anonymized_dicoms_${COHORT}"
METADATA="$DESTINATION_DIRECTORY/dicom_metadata_${COHORT}.csv"
SANITIZED="$DESTINATION_DIRECTORY/sanitized_dicoms_${COHORT}.csv"
RESULTS="$DESTINATION_DIRECTORY/mirai_results_${COHORT}.csv"

# --------------------------------------------------
# Step 0: Anonymize DICOM files
# --------------------------------------------------

if [[ ! -d "$ANONYMIZED_DIR" ]]; then

    echo "[0/4] Anonymizing DICOM files..."

    python "$REPO_DIR/RETROSPECTIVE/anonymize_dicoms.py" \
        "$SOURCE_DIRECTORY" \
        "$ANONYMIZED_DIR"

else
    echo "[0/4] Reusing existing anonymized copies."
fi

# --------------------------------------------------
# Step 1: Collect DICOM metadata
# --------------------------------------------------

if [[ ! -f "$METADATA" ]]; then

    echo "[1/4] Collecting DICOM metadata..."

    python "$REPO_DIR/RETROSPECTIVE/collect_dicoms.py" \
        --input_path "$ANONYMIZED_DIR" \
        --output_path "$METADATA" \
        --max_workers "$SCAN_WORKERS"

else
    echo "[1/4] Reusing existing metadata."
fi

# --------------------------------------------------
# Step 2: Prepare mammography exams
# --------------------------------------------------

if [[ ! -f "$SANITIZED" ]]; then

    echo "[2/4] Preparing mammography exams..."

    python "$REPO_DIR/RETROSPECTIVE/sanitize_dicoms.py" \
        --input_csv "$METADATA" \
        --output_csv "$SANITIZED" \
        --full_exams_only \
        --multiple_view_choice first

else
    echo "[2/4] Reusing existing sanitized exams."
fi

# --------------------------------------------------
# Step 3: MIRAI inference
# --------------------------------------------------

echo "[3/4] Running MIRAI inference..."

python "$REPO_DIR/RETROSPECTIVE/run_mirai.py" \
    "$SANITIZED" \
    "$RESULTS" \
    --mirai_url "$MIRAI_URL" \
    --max_workers "$MIRAI_WORKERS"

echo "Retrospective inference complete."
echo "Results saved to $RESULTS"
