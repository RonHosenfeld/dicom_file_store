"""FastAPI web app: receiver control, settings, and file browser."""

from __future__ import annotations

import logging
import logging.handlers
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config as config_mod
from . import generator as generator_mod
from . import scanner as scanner_mod
from . import sender as sender_mod
from .jobs import JobRegistry
from .receiver import ReceiverManager

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(threadName)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    logs_dir = PACKAGE_DIR.parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "dicomstore.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    app.state.config = config_mod.load()
    app.state.receiver = ReceiverManager()
    app.state.jobs = JobRegistry()
    app.state.scans = {}
    yield


templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def _safe_path(storage_root: Path, folder: str, filename: str) -> Path:
    """Resolve folder/filename and require the result to stay inside storage_root."""
    root = storage_root.resolve()
    path = (root / folder / filename).resolve()
    if not path.is_relative_to(root):
        raise HTTPException(status_code=404, detail="Not found")
    return path


def _read_study_json(folder: Path) -> dict | None:
    try:
        import json
        with open(folder / "study.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _list_studies(storage_root: Path) -> list[dict]:
    studies = []
    if not storage_root.is_dir():
        return studies
    for entry in sorted(storage_root.iterdir()):
        if not entry.is_dir():
            continue
        doc = _read_study_json(entry)
        studies.append({
            "folder": entry.name,
            "name": entry.name,
            "doc": doc,
            "study_date": (doc or {}).get("StudyDate", ""),
            "patient_name": (doc or {}).get("PatientName", ""),
            "instance_count": (doc or {}).get("InstanceCount", 0),
        })
    studies.sort(key=lambda s: s["study_date"], reverse=True)
    return studies


def create_app() -> FastAPI:
    app = FastAPI(title="DICOM File Store", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        cfg: config_mod.Config = request.app.state.config
        receiver: ReceiverManager = request.app.state.receiver
        return templates.TemplateResponse(request, "index.html", {
            "cfg": cfg,
            "running": receiver.running,
            "studies": _list_studies(Path(cfg.storage_path))[:20],
        })

    @app.get("/status")
    async def status(request: Request):
        receiver: ReceiverManager = request.app.state.receiver
        return {"running": receiver.running}

    @app.post("/receiver/start")
    async def receiver_start(request: Request):
        cfg: config_mod.Config = request.app.state.config
        receiver: ReceiverManager = request.app.state.receiver
        try:
            Path(cfg.storage_path).mkdir(parents=True, exist_ok=True)
            receiver.start(cfg)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Cannot start receiver: {exc}")
        return RedirectResponse("/", status_code=303)

    @app.post("/receiver/stop")
    async def receiver_stop(request: Request):
        receiver: ReceiverManager = request.app.state.receiver
        receiver.stop()
        return RedirectResponse("/", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_form(request: Request):
        cfg: config_mod.Config = request.app.state.config
        receiver: ReceiverManager = request.app.state.receiver
        return templates.TemplateResponse(request, "settings.html", {
            "cfg": cfg, "running": receiver.running,
        })

    @app.post("/settings")
    async def settings_save(
        request: Request,
        port: int = Form(...),
        ae_title: str = Form(...),
        storage_path: str = Form(...),
        web_port: int = Form(...),
    ):
        receiver: ReceiverManager = request.app.state.receiver
        if receiver.running:
            raise HTTPException(status_code=400, detail="Stop the receiver before changing settings")
        if not (1 <= port <= 65535) or not (1 <= web_port <= 65535):
            raise HTTPException(status_code=400, detail="Port must be 1-65535")
        ae_title = ae_title.strip()
        if not ae_title or len(ae_title) > 16:
            raise HTTPException(status_code=400, detail="AE title must be 1-16 characters")
        if not storage_path.strip():
            raise HTTPException(status_code=400, detail="Storage path is required")
        cfg = config_mod.Config(
            port=port, ae_title=ae_title, storage_path=storage_path.strip(), web_port=web_port
        )
        try:
            Path(cfg.storage_path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Storage path not usable: {exc}")
        config_mod.save(cfg)
        request.app.state.config = cfg
        return RedirectResponse("/", status_code=303)

    @app.get("/studies", response_class=HTMLResponse)
    async def studies(request: Request):
        cfg: config_mod.Config = request.app.state.config
        return templates.TemplateResponse(request, "studies.html", {
            "studies": _list_studies(Path(cfg.storage_path)),
        })

    @app.get("/studies/{folder}", response_class=HTMLResponse)
    async def study_detail(request: Request, folder: str):
        cfg: config_mod.Config = request.app.state.config
        root = Path(cfg.storage_path).resolve()
        folder_path = _safe_path(root, folder, "")
        doc = _read_study_json(folder_path)
        if doc is None:
            # Folder exists (received data) but no study.json yet
            files = sorted(p.name for p in folder_path.iterdir() if p.is_file())
            doc = {"Instances": [{"File": f} for f in files]}
        return templates.TemplateResponse(request, "study.html", {
            "folder": folder, "doc": doc,
        })

    @app.get("/files/{folder}/{filename}")
    async def download(folder: str, filename: str, request: Request):
        cfg: config_mod.Config = request.app.state.config
        path = _safe_path(Path(cfg.storage_path).resolve(), folder, filename)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(path, media_type="application/dicom", filename=filename)

    @app.get("/files/{folder}/{filename}/preview", response_class=HTMLResponse)
    async def preview(request: Request, folder: str, filename: str):
        cfg: config_mod.Config = request.app.state.config
        path = _safe_path(Path(cfg.storage_path).resolve(), folder, filename)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        import pydicom
        ds = pydicom.dcmread(path, force=True)
        elements = []
        for elem in ds:
            value = str(elem.value)
            if len(value) > 120:
                value = value[:120] + "…"
            elements.append({"keyword": elem.keyword or f"({elem.tag})", "vr": elem.VR,
                             "value": value})
        return templates.TemplateResponse(request, "preview.html", {
            "folder": folder, "filename": filename, "elements": elements,
        })

    # ---- Send tab: test sender (generate / directory / load test) ----

    def _send_target(form, cfg: config_mod.Config) -> sender_mod.SendTarget:
        host = str(form.get("host", "")).strip() or "localhost"
        try:
            port = int(form.get("port", cfg.port))
            if not (1 <= port <= 65535):
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Port must be 1-65535")
        called_ae = str(form.get("called_ae", "")).strip() or cfg.ae_title
        calling_ae = str(form.get("calling_ae", "")).strip() or "DICOMSTORE-SCU"
        if len(calling_ae) > 16 or len(called_ae) > 16:
            raise HTTPException(status_code=400, detail="AE titles must be 1-16 characters")
        return sender_mod.SendTarget(host=host, port=port, called_ae=called_ae,
                                     calling_ae=calling_ae)

    def _load_controls(form) -> tuple[int, int, int | None]:
        def _int(name: str, default: int, lo: int, hi: int) -> int:
            try:
                value = int(form.get(name, default))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{name} must be an integer")
            if not (lo <= value <= hi):
                raise HTTPException(status_code=400, detail=f"{name} must be {lo}-{hi}")
            return value

        concurrency = _int("concurrency", 1, 1, 16)
        delay_ms = _int("delay_ms", 0, 0, 5000)
        max_per_second = _int("max_per_second", 0, 0, 10000)
        return concurrency, delay_ms, (max_per_second or None)

    @app.get("/send", response_class=HTMLResponse)
    async def send_page(request: Request):
        cfg: config_mod.Config = request.app.state.config
        jobs: JobRegistry = request.app.state.jobs
        scans: dict = request.app.state.scans
        scan_id = request.query_params.get("scan")
        scan = scans.get(scan_id) if scan_id else None
        return templates.TemplateResponse(request, "send.html", {
            "cfg": cfg,
            "modality_specs": generator_mod.MODALITY_SPECS,
            "jobs": list(reversed(jobs.all())),
            "scan": scan,
            "scan_id": scan_id,
        })

    @app.post("/send/echo")
    async def send_echo(request: Request):
        form = await request.form()
        cfg: config_mod.Config = request.app.state.config
        target = _send_target(form, cfg)
        ae = AE(ae_title=target.calling_ae)
        ae.add_requested_context("1.2.840.10008.1.1")
        assoc = ae.associate(target.host, target.port)
        try:
            if not assoc.is_established:
                raise HTTPException(status_code=400,
                                    detail=f"Association to {target.host}:{target.port} failed")
            result = assoc.send_c_echo()
            status = int(result.Status) if result is not None else 0xC000
        finally:
            if assoc.is_established:
                assoc.release()
        if status != 0x0000:
            raise HTTPException(status_code=400, detail=f"C-ECHO failed: 0x{status:04X}")
        return RedirectResponse("/send", status_code=303)

    @app.post("/send/generate")
    async def send_generate(request: Request):
        form = await request.form()
        cfg: config_mod.Config = request.app.state.config
        jobs: JobRegistry = request.app.state.jobs

        modalities = form.getlist("modalities")
        if not modalities:
            raise HTTPException(status_code=400, detail="Select at least one modality")
        try:
            plan = generator_mod.build_plan(
                modalities=modalities,
                series_count=int(form.get("series_count", 1)),
                images_per_series=int(form.get("images_per_series", 1)),
                patient_name=str(form.get("patient_name", "")),
                patient_id=str(form.get("patient_id", "")),
                accession=str(form.get("accession", "")),
                study_description=str(form.get("study_description", "")),
                study_date=str(form.get("study_date", "")),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        target = _send_target(form, cfg)
        concurrency, delay_ms, max_per_second = _load_controls(form)
        req = sender_mod.SendRequest(target=target, concurrency=concurrency,
                                     delay_ms=delay_ms, max_per_second=max_per_second)
        work = [sender_mod.GeneratedSeries(plan=plan, spec=s) for s in plan.series]
        label = (f"Generated {plan.patient_name} ({plan.accession}): "
                 f"{len(plan.series)} series × {plan.series[0].image_count} images")
        jobs.create(label, plan.instance_count, len(work),
                    lambda progress, cancel: sender_mod.send_job(work, req, progress, cancel))
        return RedirectResponse("/send", status_code=303)

    @app.post("/send/scan")
    async def send_scan(request: Request):
        form = await request.form()
        scans: dict = request.app.state.scans
        directory = str(form.get("directory", "")).strip()
        root = Path(directory).expanduser()
        if not directory or not root.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {directory}")
        result = scanner_mod.scan_directory(root)
        scan_id = uuid.uuid4().hex[:8]
        scans[scan_id] = result
        # Keep the most recent 5 scans.
        while len(scans) > 5:
            oldest = min(scans, key=lambda k: scans[k].root)
            del scans[oldest]
        return RedirectResponse(f"/send?scan={scan_id}", status_code=303)

    @app.post("/send/directory")
    async def send_directory(request: Request):
        form = await request.form()
        cfg: config_mod.Config = request.app.state.config
        jobs: JobRegistry = request.app.state.jobs
        scans: dict = request.app.state.scans
        scan_id = str(form.get("scan_id", ""))
        scan = scans.get(scan_id)
        if scan is None:
            raise HTTPException(status_code=400, detail="Scan expired — scan the directory again")

        selected = set(form.getlist("study_uid"))
        if not selected:
            raise HTTPException(status_code=400, detail="Select at least one study")
        work = [
            sender_mod.FileSeries(series_uid=s.series_uid, modality=s.modality,
                                  refs=s.instances)
            for study in scan.studies if study.study_uid in selected
            for s in study.series
        ]
        total = sum(w.instance_count for w in work)

        target = _send_target(form, cfg)
        concurrency, delay_ms, max_per_second = _load_controls(form)
        req = sender_mod.SendRequest(target=target, concurrency=concurrency,
                                     delay_ms=delay_ms, max_per_second=max_per_second)
        label = f"Directory send: {len(selected)} studies, {total} instances"
        jobs.create(label, total, len(work),
                    lambda progress, cancel: sender_mod.send_job(work, req, progress, cancel))
        return RedirectResponse("/send", status_code=303)

    @app.get("/send/jobs/{job_id}/progress")
    async def send_job_progress(job_id: str, request: Request):
        jobs: JobRegistry = request.app.state.jobs
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        return {
            "id": job.id,
            "label": job.label,
            "state": job.state,
            "progress": job.progress.snapshot(),
        }

    @app.post("/send/jobs/{job_id}/cancel")
    async def send_job_cancel(job_id: str, request: Request):
        jobs: JobRegistry = request.app.state.jobs
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        job.cancel()
        return RedirectResponse("/send", status_code=303)

    return app
