"""Wraps the RETROSPECTIVE scan/sanitize scripts with progress callbacks for the GUI."""
import csv
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from contextlib import ExitStack, redirect_stdout
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

RETROSPECTIVE_DIR = Path(__file__).resolve().parent.parent / "RETROSPECTIVE"
sys.path.insert(0, str(RETROSPECTIVE_DIR))


def extract_zip_safely(zf: zipfile.ZipFile, dest_dir: str) -> None:
    """Extract zf into dest_dir, rejecting entries that would land outside it (zip-slip guard)."""
    dest = Path(dest_dir).resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        if not target.is_relative_to(dest):
            raise ValueError(f"Entrada de zip fuera de destino: {member.filename}")
    zf.extractall(dest_dir)

from collect_dicoms import scan_file, OUTPUT_COLUMNS  # noqa: E402
from sanitize_dicoms import select_unique_views, VIEWS  # noqa: E402

MIRAI_URL = os.environ.get("MIRAI_URL", "http://mirai:5000/dicom/files")
SCORE_YEARS = ["Year 1", "Year 2", "Year 3", "Year 4", "Year 5"]

COLUMN_LABELS = {
    "file_path": "Ruta del archivo",
    "PatientID": "ID de Paciente",
    "StudyUID": "ID de Estudio",
    "SeriesUID": "ID de Serie",
    "ImageUID": "ID de Imagen",
    "study_date": "Fecha de Estudio",
    "Hospital": "Hospital",
    "Manufacturer": "Fabricante",
    "Machine": "Equipo",
    "Modality": "Modalidad",
    "laterality": "Lateralidad",
    "view_position": "Posicion de Vista",
    "acquisition_date": "Fecha de Adquisicion",
    "acquisition_time": "Hora de Adquisicion",
    "type": "Vista",
    "num_views": "Cantidad de Vistas",
    "full_exam": "Estudio Completo",
    "duplicates_removed": "Vistas Duplicadas Omitidas",
}

LEGEND_ROWS = [
    ("ID de Estudio", "Identificador unico del estudio (StudyInstanceUID) tal como viene en el DICOM."),
    ("ID de Paciente", "Identificador del paciente (PatientID) tal como viene en el DICOM."),
    ("Vista", "Combinacion de lateralidad y posicion: L_CC, L_MLO, R_CC o R_MLO."),
    ("Cantidad de Vistas", "Cuantas de las 4 vistas mamograficas se encontraron para ese estudio (0 a 4)."),
    ("Estudio Completo", "Si (1) cuando el estudio tiene las 4 vistas; No (0) cuando falta alguna."),
    ("Vistas Duplicadas Omitidas", "Cuantas vistas repetidas de ese estudio se descartaron al armar el exam (se conserva una sola, la mas antigua por defecto)."),
    ("Score_Año_N", "Probabilidad de cancer de mama a N anios segun el modelo MIRAI (0 a 1). Solo se calcula para estudios completos (4 vistas)."),
    ("Error MIRAI", "Motivo por el que no se pudo obtener el score de ese estudio (ej. timeout, error de conexion). Vacio si no hubo error."),
    ("Imagenes invalidas", "Archivos que no se pudieron leer como DICOM, o que dieron error al leer su cabecera."),
    ("Fecha/Hora de Adquisicion", "Cuando se tomo esa imagen puntual (hoja 'Vistas duplicadas omitidas'); sirve para ver por que se descarto frente a la que se conservo."),
]

# Columnas que si le sirven a un usuario final en la hoja de duplicados; se
# dejan afuera las rutas de archivo y los campos internos de DICOM (UIDs de
# serie/imagen, instance_number, image_type, original_view_position, Comment).
DROPPED_VIEW_COLUMNS = [
    "PatientID",
    "StudyUID",
    "type",
    "study_date",
    "acquisition_date",
    "acquisition_time",
    "Hospital",
    "Machine",
]


def scan_folder(path: str, max_workers: int = 16, progress_cb=None) -> pd.DataFrame:
    """Scan every file under `path`, reading DICOM headers in parallel.

    progress_cb(done, total) is called after each file, if provided.
    """
    root = Path(path)
    if not root.is_dir():
        raise NotADirectoryError(f"'{path}' no es una carpeta valida.")

    files = [Path(r) / f for r, _, fs in __import__("os").walk(root) for f in fs]
    total = len(files)
    if total == 0:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(scan_file, f) for f in files]
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                results.append(result)
            done += 1
            if progress_cb:
                progress_cb(done, total)

    return pd.DataFrame(results, columns=OUTPUT_COLUMNS)


def _filter_mammography(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "Modality" in df.columns:
        df = df[df["Modality"] == "MG"]
    df = df[df["type"].isin(VIEWS)]
    df = df.dropna(subset=["StudyUID", "file_path"])
    return df


def sanitize(df: pd.DataFrame, multiple_view_choice: str = "first"):
    """Dedupe views and build the one-row-per-exam table.

    Returns (exams_df, dropped_df, stats_dict). Reuses select_unique_views
    (the actual dedupe logic from sanitize_dicoms.py) directly on the
    lowercase-column convention it expects.
    """
    mammo = _filter_mammography(df)
    lower = mammo.rename(columns={c: c.lower() for c in mammo.columns})

    buf = io.StringIO()
    with redirect_stdout(buf):
        deduped = select_unique_views(lower, multiple_view_choice)
    dup_match = re.search(r"Removed ([\d,]+) duplicate views", buf.getvalue())
    duplicates_removed = int(dup_match.group(1).replace(",", "")) if dup_match else 0

    dropped = mammo[~mammo.index.isin(deduped.index)]

    pivotted = deduped.pivot(
        index="studyuid", columns="type", values="file_path"
    ).reindex(columns=VIEWS)

    meta_cols = [c for c in ["patientid", "study_date", "hospital", "machine"] if c in deduped.columns]
    metadata = deduped.groupby("studyuid")[meta_cols].first()

    exams = pivotted.join(metadata).reset_index()
    exams.columns.name = None
    exams["num_views"] = exams[VIEWS].notna().sum(axis=1)
    exams["full_exam"] = (exams["num_views"] == 4).astype(int)

    dup_counts = dropped.groupby("StudyUID").size() if len(dropped) else pd.Series(dtype=int)
    exams["duplicates_removed"] = exams["studyuid"].map(dup_counts).fillna(0).astype(int)

    stats = {
        "valid_dicom_files": int(len(df)),
        "mammography_images": int(len(mammo)),
        "duplicates_removed": duplicates_removed,
        "total_exams": int(len(exams)),
        "full_exams": int(exams["full_exam"].sum()),
        "views_histogram": exams["num_views"].value_counts().sort_index().to_dict(),
    }
    return exams, dropped, stats


def _submit_one_exam_to_mirai(exam_id, dicom_paths, mirai_url, timeout):
    with ExitStack() as stack:
        files = [
            (
                "dicom",
                (Path(p).name, stack.enter_context(open(p, "rb")), "application/dicom"),
            )
            for p in dicom_paths
        ]
        response = requests.post(
            mirai_url,
            data={"metadata": json.dumps({"exam_id": exam_id})},
            files=files,
            timeout=(120, timeout),
        )
        response.raise_for_status()
        return response.json()


def submit_exams_to_mirai(
    exams_df: pd.DataFrame,
    mirai_url: str = MIRAI_URL,
    max_workers: int = 2,
    timeout: int = 600,
    progress_cb=None,
) -> pd.DataFrame:
    """Submit every complete exam (4 views) to MIRAI and add Year 1-5 score columns.

    Incomplete exams are left with empty scores (nothing to submit).
    progress_cb(done, total) is called after each completed submission.
    """
    exams_df = exams_df.copy()
    for year in SCORE_YEARS:
        exams_df[year] = None
    exams_df["mirai_error"] = None

    full = exams_df[exams_df["full_exam"] == 1]
    if len(full) == 0:
        return exams_df

    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _submit_one_exam_to_mirai,
                row["studyuid"],
                [row[v] for v in VIEWS],
                mirai_url,
                timeout,
            ): idx
            for idx, row in full.iterrows()
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
                predictions = (result.get("data") or {}).get("predictions") or {}
                for year in SCORE_YEARS:
                    exams_df.at[idx, year] = predictions.get(year)
            except Exception as e:
                exams_df.at[idx, "mirai_error"] = str(e)
            done += 1
            if progress_cb:
                progress_cb(done, len(full))

    return exams_df


HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
TITLE_FONT = Font(bold=True, size=12)


def _style_header_row(ws, row_num):
    for cell in ws[row_num]:
        if cell.value is None:
            continue
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center")


def _autosize_columns(ws, max_width=60, wrap_header_values=None):
    """Size columns to their content and give every cell a bit of breathing
    room (indent) so text doesn't look glued to the cell borders."""
    wrap_header_values = wrap_header_values or set()
    header_row = next((c.row for col in ws.columns for c in col if c.value in wrap_header_values), 1)
    for col_cells in ws.columns:
        letter = get_column_letter(col_cells[0].column)
        lengths = [len(str(c.value)) for c in col_cells if c.value is not None]
        width = min(max(lengths, default=10) + 4, max_width)
        ws.column_dimensions[letter].width = width
        is_wrap_column = col_cells[0].value in wrap_header_values or any(
            c.value in wrap_header_values for c in col_cells
        )
        if is_wrap_column:
            ws.column_dimensions[letter].width = max_width
            for c in col_cells[header_row:]:
                c.alignment = Alignment(wrap_text=True, vertical="top", indent=1)
        else:
            for c in col_cells:
                if c.value is None:
                    continue
                c.alignment = Alignment(vertical="center", indent=1)
    for row in ws.iter_rows(min_row=1, max_row=1):
        for cell in row:
            if cell.value is not None:
                cell.alignment = Alignment(vertical="center", indent=1)
    ws.freeze_panes = "A2"


def _sanitize_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    """Neutralize spreadsheet formula injection: a cell value starting with
    =, +, -, or @ is interpreted as a formula when the file is opened. These
    values come straight from DICOM header fields that any uploader can set,
    so prefix them with a single quote to force plain text, same as Excel's
    own manual escape for a leading special character."""
    df = df.copy()
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].map(
            lambda v: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
        )
    return df


def build_excel_report(raw_scan_df, exams_df, dropped_df, stats, scanned_total_files) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        invalid = scanned_total_files - stats["valid_dicom_files"]
        resumen = pd.DataFrame(
            [
                ("Archivos encontrados en la carpeta", scanned_total_files),
                ("Archivos DICOM validos", stats["valid_dicom_files"]),
                ("Archivos invalidos / con error de lectura", invalid),
                ("Imagenes de mamografia (Modality=MG)", stats["mammography_images"]),
                ("Vistas duplicadas omitidas", stats["duplicates_removed"]),
                ("Total de estudios (exams)", stats["total_exams"]),
                ("Estudios completos (4 vistas)", stats["full_exams"]),
                ("Estudios incompletos", stats["total_exams"] - stats["full_exams"]),
            ],
            columns=["Indicador", "Cantidad"],
        )
        resumen.to_excel(writer, sheet_name="Resumen", index=False)

        hist_title_row = len(resumen) + 2  # 1-indexed blank row right above the histogram header
        hist_header_row = hist_title_row + 1
        hist = pd.DataFrame(
            [(f"{k} vista(s)", v) for k, v in stats["views_histogram"].items()],
            columns=["Vistas encontradas", "Cantidad de estudios"],
        )
        hist.to_excel(writer, sheet_name="Resumen", index=False, startrow=hist_header_row - 1)

        ws_resumen = writer.sheets["Resumen"]
        ws_resumen.cell(row=hist_title_row, column=1, value="Detalle: estudios por cantidad de vistas encontradas").font = TITLE_FONT
        _style_header_row(ws_resumen, 1)
        _style_header_row(ws_resumen, hist_header_row)
        _autosize_columns(ws_resumen)

        exams_out = exams_df.drop(
            columns=[v for v in VIEWS if v in exams_df.columns]
        ).rename(columns={
            "studyuid": "ID de Estudio",
            "patientid": "ID de Paciente",
            "study_date": "Fecha de Estudio",
            "hospital": "Hospital",
            "machine": "Equipo",
            "num_views": "Cantidad de Vistas",
            "full_exam": "Estudio Completo",
            "duplicates_removed": "Vistas Duplicadas Omitidas",
            "Year 1": "Score_Año_1",
            "Year 2": "Score_Año_2",
            "Year 3": "Score_Año_3",
            "Year 4": "Score_Año_4",
            "Year 5": "Score_Año_5",
            "mirai_error": "Error MIRAI",
        })
        exams_out = _sanitize_for_excel(exams_out)
        exams_out.to_excel(writer, sheet_name="Estudios", index=False)
        ws_estudios = writer.sheets["Estudios"]
        _style_header_row(ws_estudios, 1)
        _autosize_columns(ws_estudios, wrap_header_values={"Error MIRAI"})

        dropped_out = dropped_df[
            [c for c in DROPPED_VIEW_COLUMNS if c in dropped_df.columns]
        ].rename(columns=COLUMN_LABELS)
        dropped_out = _sanitize_for_excel(dropped_out)
        dropped_out.to_excel(writer, sheet_name="Vistas duplicadas omitidas", index=False)
        ws_dropped = writer.sheets["Vistas duplicadas omitidas"]
        _style_header_row(ws_dropped, 1)
        _autosize_columns(ws_dropped)

        legend = pd.DataFrame(LEGEND_ROWS, columns=["Columna", "Significado"])
        legend.to_excel(writer, sheet_name="Leyenda", index=False)
        ws_legend = writer.sheets["Leyenda"]
        _style_header_row(ws_legend, 1)
        _autosize_columns(ws_legend, wrap_header_values={"Significado"})

    return output.getvalue()
