# Retrospective MIRAI Pipeline

Collect DICOM metadata, select complete four-view mammography exams, and submit
them to a MIRAI inference service.

## Setup

```bash
conda env create -f environment.yml
conda activate gen_eval
```

## Run

Start the MIRAI service using docker, then run:

```bash
./retrospective.sh /path/to/dicoms [output_directory]
```

The service defaults to `http://localhost:5000/dicom/files`. Set `MIRAI_URL` to
use another endpoint.

> [!WARNING]
> This pipeline processes DICOM images and writes patient identifiers, dates,
> study UIDs, institution names, and local file paths to CSV files. Treat its
> inputs and outputs as potentially protected health information (PHI). Do not
> commit them or send them to an unapproved inference endpoint.
