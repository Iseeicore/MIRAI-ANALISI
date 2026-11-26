"""HTTP ingestion endpoint: one POST that receives a .zip and runs the whole
pipeline (anonymize -> scan -> sanitize -> MIRAI score -> SQLite) in blocks,
so a large batch doesn't need to fit in memory or in a single MIRAI call."""
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from pipeline import extract_zip_safely, sanitize, scan_folder, submit_exams_to_mirai  # noqa: E402 (adds RETROSPECTIVE/ to sys.path)
from anonymize_dicoms import anonymize_folder
from db import record_exams

BLOCK_SIZE = int(os.environ.get("INGEST_BLOCK_SIZE", "50"))

logger = logging.getLogger(__name__)
app = FastAPI(title="dicom-analyzer ingest API")


@app.get("/health")
def health():
    return {"status": "ok", "block_size": BLOCK_SIZE}


@app.post("/ingest")
async def ingest(file: UploadFile = File(...), multiple_view_choice: str = "first"):
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted.")
    if multiple_view_choice not in ("first", "last"):
        raise HTTPException(status_code=400, detail="multiple_view_choice must be 'first' or 'last'.")

    raw_dir = tempfile.mkdtemp(prefix="api_zip_raw_")
    anon_dir = tempfile.mkdtemp(prefix="api_zip_anon_")
    try:
        zip_path = Path(raw_dir) / "upload.zip"
        with open(zip_path, "wb") as out:
            shutil.copyfileobj(file.file, out)

        try:
            with zipfile.ZipFile(zip_path) as zf:
                extract_zip_safely(zf, raw_dir)
            zip_path.unlink()
            anonymize_folder(Path(raw_dir), Path(anon_dir))
        except (zipfile.BadZipFile, ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=f"Could not process the .zip: {e}")

        raw_df = scan_folder(str(anon_dir))
        exams_df, dropped_df, stats = sanitize(raw_df, multiple_view_choice)

        full_mask = exams_df["full_exam"] == 1
        full_exams = exams_df[full_mask]
        blocks = [full_exams.iloc[i:i + BLOCK_SIZE] for i in range(0, len(full_exams), BLOCK_SIZE)]

        scored_blocks = []
        for block in blocks:
            scored_block = submit_exams_to_mirai(block)
            try:
                record_exams(scored_block)
            except Exception:
                logger.exception("No se pudo registrar un bloque en la base de datos local")
            scored_blocks.append(scored_block)

        if scored_blocks:
            scored_all = pd.concat(scored_blocks)
            exams_df.update(scored_all)

        incomplete = exams_df[~full_mask]
        if len(incomplete):
            try:
                record_exams(incomplete)
            except Exception:
                logger.exception("No se pudo registrar los estudios incompletos en la base de datos local")

        return JSONResponse({
            "total_exams": stats["total_exams"],
            "full_exams": stats["full_exams"],
            "incomplete_exams": stats["total_exams"] - stats["full_exams"],
            "duplicates_removed": stats["duplicates_removed"],
            "blocks_processed": len(blocks),
            "block_size": BLOCK_SIZE,
            "mirai_errors": int(exams_df["mirai_error"].notna().sum()) if "mirai_error" in exams_df else 0,
        })
    finally:
        shutil.rmtree(raw_dir, ignore_errors=True)
        shutil.rmtree(anon_dir, ignore_errors=True)
