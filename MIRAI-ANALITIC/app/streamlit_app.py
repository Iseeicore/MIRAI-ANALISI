"""Simple GUI to scan a DICOM folder, flag duplicates/incomplete exams, and
export a formatted Excel report. Wraps the RETROSPECTIVE scan+sanitize steps."""
import datetime as dt
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

from pipeline import (
    scan_folder, sanitize, build_excel_report, submit_exams_to_mirai, extract_zip_safely, pseudonymize_exams,
    pseudonymize_dropped,
)
from anonymize_dicoms import MissingSecretError, anonymize_folder, load_secret
from db import record_exams

DICOM_DATA_DIR = os.environ.get("DICOM_DATA_DIR", "/dicom")

st.set_page_config(page_title="Analisis de Mamografias", layout="wide")
st.title("Analisis de Mamografias (pre-MIRAI)")
st.caption(
    "Escanea archivos DICOM, arma los estudios de 4 vistas, "
    "detecta duplicados e incompletos, y genera un reporte Excel."
)

VIEW_ANALYSIS = "Analizar carpeta o archivos"
VIEW_BATCHES = "Lotes (procesamiento en segundo plano)"
if st.sidebar.radio("Vista", options=[VIEW_ANALYSIS, VIEW_BATCHES], index=0) == VIEW_BATCHES:
    from batches_view import render_batches

    render_batches()
    st.stop()

mode = st.radio(
    "Como queres cargar las imagenes",
    options=[
        "Ruta en el servidor (lotes grandes, ej. 10.000 mamografias)",
        "Subir archivos desde el navegador (lotes chicos)",
        "Subir un .zip (se anonimiza antes de analizar)",
    ],
    index=0,
)
use_upload = mode.startswith("Subir archivos")
use_zip = mode.startswith("Subir un .zip")

with st.form("analysis_form"):
    folder = None
    uploaded_files = None
    uploaded_zip = None
    if use_upload:
        uploaded_files = st.file_uploader(
            "Subir archivos DICOM (.dcm)",
            type=["dcm"],
            accept_multiple_files=True,
        )
    elif use_zip:
        uploaded_zip = st.file_uploader(
            "Subir un .zip con las carpetas/series DICOM del estudio",
            type=["zip"],
            accept_multiple_files=False,
        )
    else:
        folder = st.text_input(
            "Ruta de la carpeta con las imagenes DICOM",
            value=f"{DICOM_DATA_DIR}/mirai_demo_data",
            help=f"Debe estar dentro de la carpeta montada en el contenedor ({DICOM_DATA_DIR}).",
        )
    choice_label = st.selectbox(
        "Si hay vistas duplicadas, conservar",
        options=["La mas antigua", "La mas reciente"],
        index=0,
    )
    submitted = st.form_submit_button("Analizar")

if submitted:
    multiple_view_choice = "first" if choice_label == "La mas antigua" else "last"

    cleanup_dirs = []
    if use_upload:
        if not uploaded_files:
            st.error("Subi al menos un archivo .dcm antes de analizar.")
            st.stop()
        scan_dir = tempfile.mkdtemp(prefix="mirai_upload_")
        cleanup_dirs.append(scan_dir)
        for f in uploaded_files:
            with open(Path(scan_dir) / f.name, "wb") as out:
                out.write(f.getbuffer())
    elif use_zip:
        if not uploaded_zip:
            st.error("Subi un archivo .zip antes de analizar.")
            st.stop()
        raw_dir = tempfile.mkdtemp(prefix="mirai_zip_raw_")
        anonymized_dir = tempfile.mkdtemp(prefix="mirai_zip_anon_")
        cleanup_dirs.extend([raw_dir, anonymized_dir])
        with st.spinner("Descomprimiendo y anonimizando..."):
            try:
                with zipfile.ZipFile(uploaded_zip) as zf:
                    extract_zip_safely(zf, raw_dir)
                anonymize_folder(Path(raw_dir), Path(anonymized_dir))  # deterministic, keyed by PSEUDONYM_SECRET
            except MissingSecretError as e:
                for d in cleanup_dirs:
                    shutil.rmtree(d, ignore_errors=True)
                st.error(str(e))
                st.stop()
            except (zipfile.BadZipFile, ValueError, FileNotFoundError) as e:
                for d in cleanup_dirs:
                    shutil.rmtree(d, ignore_errors=True)
                st.error(f"No se pudo procesar el .zip: {e}")
                st.stop()
        scan_dir = anonymized_dir
    else:
        root = Path(folder)
        if not root.is_dir():
            st.error(f"'{folder}' no es una carpeta valida o no esta montada en el contenedor.")
            st.stop()
        scan_dir = folder

    try:
        progress = st.progress(0, text="Escaneando archivos...")

        def on_progress(done, total):
            progress.progress(done / total, text=f"Escaneando archivos... {done:,}/{total:,}")

        raw_df = scan_folder(scan_dir, progress_cb=on_progress)
        scanned_total_files = sum(1 for _ in Path(scan_dir).rglob("*") if _.is_file())
        progress.progress(1.0, text="Escaneo completo.")

        with st.spinner("Armando estudios y detectando duplicados..."):
            exams_df, dropped_df, stats = sanitize(raw_df, multiple_view_choice)

        full_count = int((exams_df["full_exam"] == 1).sum())
        if full_count > 0:
            mirai_progress = st.progress(0, text=f"Calculando Score MIRAI... 0/{full_count}")

            def on_mirai_progress(done, total):
                mirai_progress.progress(
                    done / total,
                    text=f"Calculando Score MIRAI... {done}/{total} estudios",
                )

            exams_df = submit_exams_to_mirai(exams_df, progress_cb=on_mirai_progress)

        if not use_zip:
            # Server-path and upload modes scan the original files: replace StudyInstanceUID and
            # PatientID with the anonymizer's deterministic pseudonyms before anything is shown,
            # exported or stored. Without the secret, nothing is shown.
            try:
                secret = load_secret()
            except MissingSecretError as e:
                st.error(f"No se puede mostrar el resultado sin seudonimizar: {e}")
                st.stop()
            exams_df = pseudonymize_exams(exams_df, secret)
            dropped_df = pseudonymize_dropped(dropped_df, secret)

        excel_bytes = build_excel_report(raw_df, exams_df, dropped_df, stats, scanned_total_files)

        try:
            record_exams(exams_df)
        except Exception as e:
            st.warning(f"No se pudo registrar en la base de datos local: {e}")

        st.session_state["results"] = {
            "exams_df": exams_df,
            "dropped_df": dropped_df,
            "stats": stats,
            "scanned_total_files": scanned_total_files,
            "excel_bytes": excel_bytes,
        }
    finally:
        for d in cleanup_dirs:
            shutil.rmtree(d, ignore_errors=True)

if "results" in st.session_state:
    r = st.session_state["results"]
    stats = r["stats"]

    st.subheader("Resumen")
    cols = st.columns(4)
    cols[0].metric("Archivos encontrados", f"{r['scanned_total_files']:,}")
    cols[1].metric("Archivos DICOM validos", f"{stats['valid_dicom_files']:,}")
    cols[2].metric("Vistas duplicadas omitidas", f"{stats['duplicates_removed']:,}")
    cols[3].metric("Total de estudios", f"{stats['total_exams']:,}")

    cols2 = st.columns(4)
    cols2[0].metric("Estudios completos (4 vistas)", f"{stats['full_exams']:,}")
    cols2[1].metric("Estudios incompletos", f"{stats['total_exams'] - stats['full_exams']:,}")

    st.subheader("Estudios detectados")
    st.dataframe(r["exams_df"], use_container_width=True)

    if len(r["dropped_df"]):
        st.subheader("Vistas duplicadas omitidas")
        st.dataframe(r["dropped_df"], use_container_width=True)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        "Descargar reporte Excel",
        data=r["excel_bytes"],
        file_name=f"analisis_mamografias_{timestamp}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
