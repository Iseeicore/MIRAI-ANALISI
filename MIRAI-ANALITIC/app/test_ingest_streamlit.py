"""Standalone check for the Streamlit "Lotes" view (run with: python test_ingest_streamlit.py).

No pytest in this project. Runs streamlit_app.py with Streamlit's AppTest against the in-memory
repository (no MySQL needed): the live table shows the batch and both report downloads are offered.
"""
from streamlit.testing.v1 import AppTest

import batches_view
from ingest.adapters.memory_repository import MemoryRepository
from ingest_test_support import TEST_SECRET
from ingest.domain import VIEWS, hash_uid

repo = MemoryRepository()
batch_id = repo.create_batch("http://e2e-files:8080/")
for view in VIEWS[:3]:
    repo.register_image(batch_id, hash_uid("1.2.3", TEST_SECRET), f"2026-11/0013/{view}.dcm", 1, view)
repo.register_skipped(batch_id, "leeme.txt", "not DICOM")
batch = repo.claim_batch("c", 600)
repo.finish_batch(batch.id, "c")
batches_view._repository = lambda: repo

at = AppTest.from_file("streamlit_app.py", default_timeout=60)
at.run()
assert not at.exception, at.exception
analysis_buttons = len(at.button)

lotes = [o for o in at.sidebar.radio[0].options if o.startswith("Lotes")][0]
at.sidebar.radio[0].set_value(lotes).run()
assert not at.exception, at.exception
table = at.dataframe[0].value
assert list(table["Lote"]) == [batch_id], table
assert int(table["INCOMPLETE"].iloc[0]) == 1 and int(table["Ignorados"].iloc[0]) == 1, table
labels = [b.proto.label for b in at.get("download_button")]
assert labels == ["Descargar fallos.txt", "Descargar reporte Excel"], labels

# The existing analysis view still renders.
at.sidebar.radio[0].set_value(at.sidebar.radio[0].options[0]).run()
assert not at.exception and len(at.button) == analysis_buttons

print("OK: Streamlit batches view checks passed")
