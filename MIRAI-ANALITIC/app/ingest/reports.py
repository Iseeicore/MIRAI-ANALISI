"""Batch reports built on demand from the repository (decision 0003); nothing is written to disk.

Shared by the API (GET /batches/{id}/fallos.txt and /report.xlsx) and the Streamlit panel. User-facing
labels are Spanish, like the existing report; styling and formula-injection sanitizing come from
pipeline.py.
"""
import io
from typing import Optional

import pandas as pd

from pipeline import _autosize_columns, _sanitize_for_excel, _style_header_row

from .domain import SCORE_YEARS, StudyStatus

FAILURES_HEADER = ("carpeta", "seudonimo", "estado", "motivo")
SKIPPED_STATUS = "IGNORADO"
NO_PSEUDONYM = "-"
PROBLEM_STATUSES = (StudyStatus.INCOMPLETE.value, StudyStatus.FAILED.value)
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SCORE_LABELS = [f"Score_Año_{n}" for n in range(1, 6)]
STATUS_LABELS = {
    "COLLECTING": "Estudios reuniendo vistas (COLLECTING)",
    "DISCOVERED": "Estudios en cola de descarga (DISCOVERED)",
    "FETCHING": "Estudios descargandose (FETCHING)",
    "READY": "Estudios listos para MIRAI (READY)",
    "SCORING": "Estudios en MIRAI (SCORING)",
    "DONE": "Estudios completados (DONE)",
    "INCOMPLETE": "Estudios incompletos (INCOMPLETE)",
    "FAILED": "Estudios con error (FAILED)",
}
LEGEND_ROWS = [
    ("Seudonimo", "Identificador corto del estudio: primeros 12 caracteres del hash SHA-256 del StudyInstanceUID. "
                  "No permite recuperar el UID original."),
    ("Carpeta", "Carpeta comun de las imagenes del estudio (o del archivo ignorado), relativa a la raiz del lote. "
                "Sirve para ubicar el estudio en el origen."),
    ("Estado", "COLLECTING: reuniendo las 4 vistas. DISCOVERED/FETCHING/READY/SCORING: en proceso. "
               "DONE: con score MIRAI. INCOMPLETE: faltan vistas o imagenes ilegibles. FAILED: error al descargar "
               "o en MIRAI. IGNORADO: archivo que no se uso."),
    ("Score_Año_N", "Probabilidad de cancer de mama a N anios segun MIRAI (0 a 1). Solo para estudios DONE."),
    ("Motivo", "Razon de INCOMPLETE, FAILED o IGNORADO (para archivos ignorados: nombre del archivo y razon)."),
]


def _clean(value) -> str:
    """Single-line text: tabs and line breaks become spaces."""
    text = "" if value is None else str(value)
    return text.replace("\r\n", " ").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def failure_rows(data: dict) -> list:
    """(carpeta, seudonimo, estado, motivo): INCOMPLETE/FAILED studies first, then skipped files."""
    rows = [(s["folder"], s["pseudonym"], s["status"], s["error"])
            for s in data["studies"] if s["status"] in PROBLEM_STATUSES]
    rows += [(f["folder"], NO_PSEUDONYM, SKIPPED_STATUS, f"{f['file_name']}: {f['reason']}") for f in data["skipped"]]
    return [tuple(_clean(v) for v in row) for row in rows]


def build_failures_txt(data: dict) -> bytes:
    lines = ["\t".join(FAILURES_HEADER)] + ["\t".join(row) for row in failure_rows(data)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_sheet(writer, df: pd.DataFrame, name: str, wrap=None):
    safe = _sanitize_for_excel(df)
    # The sanitizer escapes every leading "-"; the lone placeholder "-" is not a formula, keep it readable.
    safe = safe.mask(safe == f"'{NO_PSEUDONYM}", NO_PSEUDONYM)
    safe.to_excel(writer, sheet_name=name, index=False)
    ws = writer.sheets[name]
    _style_header_row(ws, 1)
    _autosize_columns(ws, wrap_header_values=wrap or set())


def build_report_xlsx(data: dict) -> bytes:
    batch, counts = data["batch"], data["counts"]
    summary = [("Lote", batch["batch_id"]), ("Estado del lote", batch["status"]),
               ("Estudios", batch["total_studies"])]
    summary += [(STATUS_LABELS[status], counts.get(status, 0)) for status in STATUS_LABELS]
    summary += [("Imagenes registradas", batch["total_images"]), ("Archivos ignorados", batch["total_skipped"])]

    studies = pd.DataFrame(
        [[s["pseudonym"], s["folder"], s["status"], *[s["scores"].get(y) for y in SCORE_YEARS], s["error"]]
         for s in data["studies"]],
        columns=["Seudonimo", "Carpeta", "Estado", *SCORE_LABELS, "Motivo"])
    failures = pd.DataFrame(failure_rows(data), columns=["Carpeta", "Seudonimo", "Estado", "Motivo"])

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        _write_sheet(writer, pd.DataFrame(summary, columns=["Indicador", "Cantidad"]), "Resumen")
        _write_sheet(writer, studies, "Estudios", wrap={"Motivo"})
        _write_sheet(writer, failures, "Fallos", wrap={"Motivo"})
        _write_sheet(writer, pd.DataFrame(LEGEND_ROWS, columns=["Columna", "Significado"]), "Leyenda",
                     wrap={"Significado"})
    return output.getvalue()


def load_report_data(repo, batch_id: int) -> Optional[dict]:
    return repo.report_data(batch_id)
