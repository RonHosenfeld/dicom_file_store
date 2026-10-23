# DICOM File Store

A DICOM receiver (C-STORE SCP) that accepts incoming associations with as few
restrictions as possible — all storage SOP classes, all transfer syntaxes, any
calling AE title — and stores each exam as one file per image/instance,
grouped into a folder per study.

## Features

- Accepts **all storage SOP classes** plus C-ECHO (connectivity checks), any calling AE title
- Handles **multiple simultaneous associations** (one thread per connection)
- Stores instances as `<storage_path>/<AccessionNumber>/<SOPInstanceUID>.dcm`
- Instances without an accession number go to
  `<storage_path>/_no_accession_<StudyInstanceUID>/` (still grouped per study)
- Writes a `study.json` per study folder with patient, accession, study info,
  modality list, and per-instance details — updated as instances arrive
- Duplicate instances (same SOPInstanceUID) overwrite silently
- Web UI (no auth — intended for a trusted LAN): start/stop the receiver,
  configure port / AE title / storage location, browse and download received files
- **Send tab** for testing: generate synthetic exams (choice of modalities,
  series/instance counts, overridable metadata) and send them over C-STORE,
  or scan a local directory of real DICOM files and send selected studies —
  with load-test controls (parallel associations, send-rate throttling) and
  live job progress

## Running

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python main.py
```

Then open http://localhost:8080, configure settings if needed, and click **Start**.
Point your modality/PACS at this host: port `11112` (default), AE title `DICOMSTORE`
(default). Configuration persists in `config.json`.

## File layout

```
<storage_path>/
├── <ACCESSION>/                # one folder per study (accession number)
│   ├── <SOPInstanceUID>.dcm    # one Part-10 file per instance
│   └── study.json              # study-level metadata
└── _no_accession_<StudyUID>/   # fallback when accession is missing
```

## Tests

```bash
.venv/bin/python -m pytest
```

Unit tests cover path sanitization and study.json merging; the E2E suite runs a
real DICOM SCU against the receiver over localhost (normal studies, missing
accession, duplicates, mixed modalities, concurrent senders). Further suites
cover the exam generator, directory scanner, and the send engine (throttling,
cancellation, concurrency, file-based sends), including the Send tab's web
endpoints.

## Notes

- The web UI binds to `0.0.0.0` and has **no authentication** — run it on a
  trusted network only.
- Logs go to the console and `logs/dicomstore.log` (rotating).
- Accession numbers are sanitized for the filesystem (invalid characters
  replaced) but preserve case and content otherwise.
