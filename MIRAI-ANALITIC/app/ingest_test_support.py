"""Helpers shared by the test_ingest_*.py scripts: synthetic mammography DICOMs (no real data)."""
from pathlib import Path

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

MG_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.1.2"
TEST_SECRET = "test-secret-" + "0123456789abcdef" * 3  # >= 32 chars; for tests only
VIEW_TAGS = {"L_CC": ("L", "CC"), "L_MLO": ("L", "MLO"), "R_CC": ("R", "CC"), "R_MLO": ("R", "MLO")}


def write_dicom(path, study_uid, view=None, laterality=None, view_position=None, modality="MG",
                patient_name="REAL^NAME", padding_bytes=0, acquisition_time="100000"):
    """Write a tiny but valid DICOM file. `view` ("L_CC"...) sets laterality and ViewPosition.
    padding_bytes adds a private OB element before the view tags (to make the header larger)."""
    if view is not None:
        laterality, view_position = VIEW_TAGS[view]
    path = Path(path)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MG_SOP_CLASS
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\x00" * 128)
    ds.SOPClassUID = MG_SOP_CLASS
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientName = patient_name
    ds.PatientID = patient_name
    ds.PatientBirthDate = "19700101"
    if modality is not None:
        ds.Modality = modality
    if laterality is not None:
        ds.ImageLaterality = laterality
    if view_position is not None:
        ds.ViewPosition = view_position
    ds.StudyDate = "20260101"
    ds.AcquisitionDate = "20260101"
    ds.AcquisitionTime = acquisition_time
    if padding_bytes:
        block = ds.private_block(0x0011, "TEST PADDING", create=True)
        block.add_new(0x01, "OB", b"\x00" * padding_bytes)
    ds.Rows, ds.Columns = 4, 4
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
    ds.PixelData = b"\x01\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(path), enforce_file_format=True)
    return path


def new_uid():
    return generate_uid()
