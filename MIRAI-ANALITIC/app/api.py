"""HTTP ingestion endpoint: one POST that receives a .zip and runs the whole
pipeline (anonymize -> scan -> sanitize -> MIRAI score -> SQLite) in blocks,
so a large batch doesn't need to fit in memory or in a single MIRAI call."""
import hmac
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Security, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field

from pipeline import extract_zip_safely, sanitize, scan_folder, submit_exams_to_mirai  # noqa: E402 (adds RETROSPECTIVE/ to sys.path)
from anonymize_dicoms import MissingSecretError, anonymize_folder
from db import record_exams
from ingest import reports as ingest_reports
from ingest import service as ingest_service
from ingest.config import build_repository, load_settings, load_source_policy
from ingest.sources import SourceRejected, SourceUnavailable

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
            anonymize_folder(Path(raw_dir), Path(anon_dir))  # deterministic, keyed by PSEUDONYM_SECRET
        except MissingSecretError as e:
            raise HTTPException(status_code=503, detail=str(e))
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


# --- Batch analysis by URL or /dicom folder (queued; processed by the ingest workers) ---------------
# Every /batches endpoint requires X-API-Key (decision 0004): reports carry folder paths, and starting
# a batch makes the backend fetch from the network.

API_KEY_HEADER = "X-API-Key"
MIN_API_KEY_LENGTH = 32
INVALID_KEY_DETAIL = "invalid or missing API key"
api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False,
                              description="Clave de acceso entregada por el administrador (INGEST_API_KEYS).")


class ErrorDetail(BaseModel):
    detail: str


class BatchRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"source": "http://IP-DEL-SERVIDOR:PUERTO/ruta/"}})

    source: str = Field(
        description=("Reemplace el ejemplo por la URL real del listado de carpetas del servidor de imagenes "
                     "(formato http://IP-DEL-SERVIDOR:PUERTO/ruta/). Debe ser una direccion de la red interna del "
                     "hospital y mostrar las carpetas de los estudios. Tambien acepta una carpeta montada bajo "
                     "/dicom, por ejemplo /dicom/mirai_demo_data para una prueba."),
        examples=["http://IP-DEL-SERVIDOR:PUERTO/ruta/"],
    )


class BatchCreated(BaseModel):
    batch_id: int


def get_api_keys() -> list:
    """Valid keys from INGEST_API_KEYS (comma-separated); keys shorter than 32 chars are ignored."""
    return [k.strip() for k in os.environ.get("INGEST_API_KEYS", "").split(",") if len(k.strip()) >= MIN_API_KEY_LENGTH]


def require_api_key(key: str = Security(api_key_header), keys: list = Depends(get_api_keys)):
    if not keys:  # fail closed
        raise HTTPException(status_code=503, detail="Batch API disabled: INGEST_API_KEYS is not configured "
                                                    f"(comma-separated keys of at least {MIN_API_KEY_LENGTH} characters).")
    supplied = (key or "").encode("utf-8")
    if not any(hmac.compare_digest(supplied, k.encode("utf-8")) for k in keys):
        raise HTTPException(status_code=401, detail=INVALID_KEY_DETAIL)


_ingest_schema_ready = False


def get_ingest_repository():
    """One MySQL connection per request (PyMySQL connections are not thread-safe)."""
    global _ingest_schema_ready
    try:
        settings = load_settings()
    except SystemExit:
        raise HTTPException(status_code=503, detail="Batch ingestion is not configured (MySQL settings missing).")
    repo = build_repository(settings)
    try:
        if not _ingest_schema_ready:
            repo.apply_schema()
            _ingest_schema_ready = True
        yield repo
    finally:
        repo.close()


def get_source_policy():
    return load_source_policy()


batches = APIRouter(
    prefix="/batches",
    tags=["Lotes"],
    dependencies=[Security(require_api_key)],
    responses={401: {"model": ErrorDetail, "description": "Falta la cabecera X-API-Key o la clave no es valida."},
               503: {"model": ErrorDetail, "description": "Procesamiento por lotes no configurado."}},
)


@batches.post(
    "",
    status_code=201,
    response_model=BatchCreated,
    summary="Crear un lote a partir de una URL de imagenes",
    description=("Pegue la URL del listado de carpetas del servidor de imagenes y envie. El sistema verifica al "
                 "momento que la URL responda y muestre carpetas; si no, devuelve el motivo y no crea el lote. "
                 "Use el boton Authorize para cargar la clave X-API-Key."),
    responses={400: {"model": ErrorDetail, "description": "URL no permitida: esquema, red o ruta fuera de /dicom."},
               422: {"model": ErrorDetail, "description": "No se pudo conectar, o la URL no es un listado de "
                                                          "carpetas o esta vacia."}},
)
def create_batch(body: BatchRequest, repo=Depends(get_ingest_repository), policy=Depends(get_source_policy)):
    try:
        batch_id = ingest_service.create_batch(repo, body.source, policy)
    except SourceRejected as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SourceUnavailable as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"batch_id": batch_id}


@batches.get("/{batch_id}", summary="Progreso del lote", responses={404: {"model": ErrorDetail}})
def get_batch(batch_id: int, repo=Depends(get_ingest_repository)):
    status = ingest_service.batch_status(repo, batch_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return status


def _report_data_or_404(repo, batch_id: int) -> dict:
    data = ingest_reports.load_report_data(repo, batch_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return data


@batches.get("/{batch_id}/fallos.txt", summary="Reporte de fallos (texto)", responses={404: {"model": ErrorDetail}})
def get_batch_failures(batch_id: int, repo=Depends(get_ingest_repository)):
    content = ingest_reports.build_failures_txt(_report_data_or_404(repo, batch_id))
    return Response(content=content, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=fallos_batch_{batch_id}.txt"})


@batches.get("/{batch_id}/report.xlsx", summary="Reporte Excel del lote", responses={404: {"model": ErrorDetail}})
def get_batch_report(batch_id: int, repo=Depends(get_ingest_repository)):
    content = ingest_reports.build_report_xlsx(_report_data_or_404(repo, batch_id))
    return Response(content=content, media_type=ingest_reports.XLSX_MEDIA_TYPE,
                    headers={"Content-Disposition": f"attachment; filename=report_batch_{batch_id}.xlsx"})


app.include_router(batches)
