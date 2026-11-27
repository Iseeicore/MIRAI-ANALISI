"""Streamlit "Lotes" view: live progress of the background ingestion batches and on-demand reports.

The table refreshes every few seconds with st.fragment(run_every=...); reports are built through
ingest/reports.py (the same code as GET /batches/{id}/fallos.txt and /report.xlsx).
"""
import datetime as dt

import pandas as pd
import streamlit as st

from ingest import reports, service
from ingest.config import build_repository, load_settings
from ingest.domain import StudyStatus

REFRESH_SECONDS = 5


def _repository():
    """A fresh repository per use (PyMySQL connections are not thread-safe); None if not configured."""
    try:
        return build_repository(load_settings())
    except SystemExit:
        return None


def _close(repo):
    close = getattr(repo, "close", None)
    if close:
        close()


def _batches_table(repo) -> pd.DataFrame:
    rows = []
    for b in repo.status_report(None)["batches"]:
        status = service.batch_status(repo, b["batch_id"])
        rows.append({"Lote": status["batch_id"], "Estado": status["status"], "Estudios": status["total_studies"],
                     "Imagenes": status["total_images"], "Ignorados": status["total_skipped"],
                     **{s.value: status["counts"].get(s.value, 0) for s in StudyStatus}})
    return pd.DataFrame(rows)


@st.fragment(run_every=REFRESH_SECONDS)
def _live_progress():
    repo = _repository()
    try:
        table = _batches_table(repo)
    finally:
        _close(repo)
    if table.empty:
        st.info("Todavia no hay lotes. Crealos con POST /batches en la API (puerto 8889).")
    else:
        st.dataframe(table, hide_index=True, width="stretch")
    st.caption(f"Actualizado {dt.datetime.now():%H:%M:%S} (cada {REFRESH_SECONDS} s)")


def _downloads():
    repo = _repository()
    try:
        batch_ids = [b["batch_id"] for b in repo.status_report(None)["batches"]]
        if not batch_ids:
            return
        st.subheader("Reportes")
        batch_id = st.selectbox("Lote", batch_ids, index=len(batch_ids) - 1)
        st.button("Regenerar reportes con el estado actual")
        data = reports.load_report_data(repo, batch_id)
    finally:
        _close(repo)
    st.download_button("Descargar fallos.txt", data=reports.build_failures_txt(data),
                       file_name=f"fallos_batch_{batch_id}.txt", mime="text/plain")
    st.download_button("Descargar reporte Excel", data=reports.build_report_xlsx(data),
                       file_name=f"report_batch_{batch_id}.xlsx", mime=reports.XLSX_MEDIA_TYPE)


def render_batches():
    st.subheader("Lotes en proceso")
    if _repository() is None:
        st.warning("El procesamiento por lotes no esta configurado (faltan las variables MYSQL_*).")
        return
    _live_progress()
    _downloads()
