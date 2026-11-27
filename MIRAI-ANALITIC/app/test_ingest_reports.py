"""Standalone checks for ingest.reports (run with: python test_ingest_reports.py). No pytest in this project."""
import io

from openpyxl import load_workbook

from ingest.reports import FAILURES_HEADER, build_failures_txt, build_report_xlsx

SCORES = {"Year 1": 0.0298, "Year 2": 0.0483, "Year 3": 0.0684, "Year 4": 0.09, "Year 5": 0.1016}
NO_SCORES = {f"Year {n}": None for n in range(1, 6)}

data = {
    "batch": {"batch_id": 7, "status": "CRAWLED", "total_studies": 3, "total_images": 11, "total_skipped": 2},
    "counts": {"COLLECTING": 0, "DISCOVERED": 0, "FETCHING": 0, "READY": 0, "SCORING": 0, "DONE": 1,
               "INCOMPLETE": 1, "FAILED": 1},
    "studies": [
        {"study_id": 1, "pseudonym": "aaaaaaaaaaaa", "folder": "2026-11", "status": "DONE",
         "scores": SCORES, "error": None},
        {"study_id": 2, "pseudonym": "bbbbbbbbbbbb", "folder": "2026-11/0013", "status": "INCOMPLETE",
         "scores": NO_SCORES, "error": "missing views: R_MLO"},
        {"study_id": 3, "pseudonym": "cccccccccccc", "folder": "=HYPERLINK(\"http://x\")\tbad\nfolder",
         "status": "FAILED", "scores": NO_SCORES, "error": "TransientError: line1\r\nline2"},
    ],
    "skipped": [
        {"folder": ".", "file_name": "leeme.txt", "reason": "not DICOM"},
        {"folder": "2026-11", "file_name": "+vacio.dcm", "reason": "not DICOM"},
    ],
}

# fallos.txt: tab-separated header, one line per INCOMPLETE/FAILED study and per skipped file, no raw tabs/newlines.
txt = build_failures_txt(data).decode("utf-8")
lines = txt.splitlines()
assert FAILURES_HEADER == ("carpeta", "seudonimo", "estado", "motivo")
assert lines[0] == "carpeta\tseudonimo\testado\tmotivo"
assert lines[1:] == [
    "2026-11/0013\tbbbbbbbbbbbb\tINCOMPLETE\tmissing views: R_MLO",
    "=HYPERLINK(\"http://x\") bad folder\tcccccccccccc\tFAILED\tTransientError: line1 line2",
    ".\t-\tIGNORADO\tleeme.txt: not DICOM",
    "2026-11\t-\tIGNORADO\t+vacio.dcm: not DICOM",
], lines
assert all(len(line.split("\t")) == 4 for line in lines)
assert txt.endswith("\n")

# Empty batch: just the header.
empty = dict(data, studies=[], skipped=[])
assert build_failures_txt(empty).decode("utf-8") == "carpeta\tseudonimo\testado\tmotivo\n"

# report.xlsx: sheets, rows and formula-injection sanitizing.
wb = load_workbook(io.BytesIO(build_report_xlsx(data)))
assert wb.sheetnames == ["Resumen", "Estudios", "Fallos", "Leyenda"], wb.sheetnames

estudios = [[c.value for c in row] for row in wb["Estudios"].iter_rows()]
assert estudios[0] == ["Seudonimo", "Carpeta", "Estado", "Score_Año_1", "Score_Año_2", "Score_Año_3",
                       "Score_Año_4", "Score_Año_5", "Motivo"], estudios[0]
assert estudios[1][:4] == ["aaaaaaaaaaaa", "2026-11", "DONE", 0.0298]
assert len(estudios) == 4

fallos = [[c.value for c in row] for row in wb["Fallos"].iter_rows()]
assert fallos[0] == ["Carpeta", "Seudonimo", "Estado", "Motivo"]
assert len(fallos) == 5
assert fallos[2][0].startswith("'="), f"formula injection not neutralized: {fallos[2][0]}"
assert fallos[4][3] == "'+vacio.dcm: not DICOM", fallos[4]
assert fallos[3][1] == "-" and fallos[3][2] == "IGNORADO", "a lone '-' placeholder is not a formula"
for ws in wb.worksheets:
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value != "-":
                assert cell.value[:1] not in "=+-@", (ws.title, cell.value)

resumen = {row[0].value: row[1].value for row in wb["Resumen"].iter_rows(min_row=2) if row[0].value}
assert resumen["Estudios completados (DONE)"] == 1
assert resumen["Estudios incompletos (INCOMPLETE)"] == 1
assert resumen["Archivos ignorados"] == 2
assert resumen["Imagenes registradas"] == 11

print("OK: report builder checks passed")
