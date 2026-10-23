from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from rasterio.errors import RasterioIOError

APP_VERSION = "0.1.0"
DATA_DIR = Path(os.environ.get("BHU_DRISHTI_DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "bhu_drishti.sqlite3"
MAX_UPLOAD_BYTES = int(os.environ.get("BHU_DRISHTI_MAX_UPLOAD_BYTES", 100 * 1024 * 1024))
ALLOWED_EXTENSIONS = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".webp"}

for directory in (UPLOAD_DIR, OUTPUT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Bhu-Drishti API", version=APP_VERSION, description="Evidence-oriented geospatial research workspace")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("BHU_DRISHTI_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS datasets (
          id TEXT PRIMARY KEY, filename TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE,
          sha256 TEXT NOT NULL, format TEXT NOT NULL, width INTEGER, height INTEGER,
          band_count INTEGER, dtypes TEXT, crs TEXT, bounds TEXT, resolution TEXT,
          nodata TEXT, georeferenced INTEGER NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(sha256)
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
          kind TEXT NOT NULL, status TEXT NOT NULL, parameters TEXT NOT NULL,
          result TEXT, warnings TEXT NOT NULL DEFAULT '[]', error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents (
          id TEXT PRIMARY KEY, filename TEXT NOT NULL, content TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS exemplars (
          id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
          descriptor TEXT NOT NULL, method TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS verifications (
          id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), status TEXT NOT NULL,
          explanation TEXT NOT NULL, evidence_dataset_id TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports (
          id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
          content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)


@app.on_event("startup")
def startup() -> None:
    init_db()
    # A process restart must not make interrupted work look successful.
    with connect() as con:
        con.execute("UPDATE jobs SET status='FAILED', error='Interrupted by application restart', updated_at=? WHERE status IN ('QUEUED','RUNNING')", (now(),))


def dataset_path(row: sqlite3.Row) -> Path:
    path = (UPLOAD_DIR / row["stored_name"]).resolve()
    if path.parent != UPLOAD_DIR.resolve():
        raise HTTPException(500, "Stored dataset path failed safety validation")
    return path


def raster_metadata(path: Path, original_name: str, digest: str) -> dict[str, Any]:
    ext = path.suffix.lower()
    if ext in {".tif", ".tiff"}:
        try:
            with rasterio.open(path) as ds:
                bounds = list(ds.bounds)
                return {
                    "format": "GeoTIFF/raster", "width": ds.width, "height": ds.height,
                    "band_count": ds.count, "dtypes": list(ds.dtypes),
                    "crs": str(ds.crs) if ds.crs else None,
                    "bounds": bounds if ds.crs else None,
                    "resolution": list(ds.res) if ds.transform else None,
                    "nodata": list(ds.nodatavals),
                    "georeferenced": bool(ds.crs and ds.transform and not ds.transform.is_identity),
                }
        except RasterioIOError as exc:
            raise HTTPException(422, f"File has a GeoTIFF extension but could not be read as a raster: {exc}") from exc
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return {"format": f"ordinary image ({image.format})", "width": image.width,
                    "height": image.height, "band_count": len(image.getbands()),
                    "dtypes": ["uint8"], "crs": None, "bounds": None,
                    "resolution": None, "nodata": None, "georeferenced": False}
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(415, "Unsupported or unreadable image. Upload a valid GeoTIFF or ordinary RGB image.") from exc


def row_to_dataset(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "filename": row["filename"], "sha256": row["sha256"],
            "format": row["format"], "width": row["width"], "height": row["height"],
            "band_count": row["band_count"], "dtypes": json.loads(row["dtypes"] or "[]"),
            "crs": row["crs"], "bounds": json.loads(row["bounds"]) if row["bounds"] else None,
            "resolution": json.loads(row["resolution"]) if row["resolution"] else None,
            "nodata": json.loads(row["nodata"]) if row["nodata"] else None,
            "georeferenced": bool(row["georeferenced"]), "created_at": row["created_at"]}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION, "storage": "local"}


@app.get("/api/datasets")
def list_datasets() -> list[dict[str, Any]]:
    with connect() as con:
        return [row_to_dataset(r) for r in con.execute("SELECT * FROM datasets ORDER BY created_at DESC")]


@app.post("/api/datasets", status_code=201)
async def upload_dataset(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = Path(file.filename or "upload").name
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, "Supported formats: GeoTIFF (.tif/.tiff), PNG, JPEG, BMP, and WebP.")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise HTTPException(400, "Uploaded file is empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MiB upload limit")
    digest = hashlib.sha256(raw).hexdigest()
    with connect() as con:
        existing = con.execute("SELECT * FROM datasets WHERE sha256=?", (digest,)).fetchone()
        if existing:
            raise HTTPException(409, f"This exact file is already in the library (dataset {existing['id']})")
    dataset_id = str(uuid.uuid4())
    stored_name = f"{dataset_id}{ext}"
    target = (UPLOAD_DIR / stored_name).resolve()
    if target.parent != UPLOAD_DIR.resolve():
        raise HTTPException(400, "Invalid filename")
    target.write_bytes(raw)
    try:
        meta = raster_metadata(target, filename, digest)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    created = now()
    try:
        with connect() as con:
            con.execute("""INSERT INTO datasets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (dataset_id, filename, stored_name, digest, meta["format"], meta["width"], meta["height"],
                 meta["band_count"], json.dumps(meta["dtypes"]), meta["crs"],
                 json.dumps(meta["bounds"]) if meta["bounds"] is not None else None,
                 json.dumps(meta["resolution"]) if meta["resolution"] is not None else None,
                 json.dumps(meta["nodata"]) if meta["nodata"] is not None else None,
                 int(meta["georeferenced"]), created))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    with connect() as con:
        return row_to_dataset(con.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone())


@app.get("/api/datasets/{dataset_id}")
def get_dataset(dataset_id: str) -> dict[str, Any]:
    with connect() as con:
        row = con.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Dataset not found")
    return row_to_dataset(row)


@app.get("/api/datasets/{dataset_id}/preview")
def preview_dataset(dataset_id: str) -> Response:
    with connect() as con:
        row = con.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Dataset not found")
    path = dataset_path(row)
    try:
        if path.suffix.lower() in {".tif", ".tiff"}:
            with rasterio.open(path) as ds:
                count = min(ds.count, 3)
                if count == 0:
                    raise HTTPException(422, "Raster has no readable bands")
                arr = ds.read(list(range(1, count + 1)), out_shape=(count, min(ds.height, 1200), min(ds.width, 1200)), masked=True)
                channels = []
                for band in arr:
                    data = np.asarray(band.astype(np.float32).filled(np.nan), dtype=np.float32)
                    valid = np.isfinite(data)
                    if not valid.any():
                        channels.append(np.zeros(data.shape, dtype=np.uint8)); continue
                    lo, hi = np.nanpercentile(data[valid], [2, 98])
                    scaled = np.zeros(data.shape, dtype=np.uint8) if hi <= lo else np.clip((np.nan_to_num(data, nan=lo)-lo)/(hi-lo)*255, 0, 255).astype(np.uint8)
                    channels.append(scaled)
                if count == 1:
                    image = Image.fromarray(channels[0], mode="L").convert("RGB")
                else:
                    while len(channels) < 3: channels.append(channels[-1])
                    image = Image.fromarray(np.stack(channels[:3], axis=-1), mode="RGB")
        else:
            with Image.open(path) as src:
                image = src.convert("RGB")
                image.thumbnail((1200, 1200))
        buf = io.BytesIO(); image.save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})
    except (RasterioIOError, OSError, ValueError) as exc:
        raise HTTPException(422, f"Could not render preview: {exc}") from exc


class AnalysisRequest(BaseModel):
    dataset_id: str
    kind: str = Field(default="statistics", pattern="^(statistics|difference_screening)$")
    reference_dataset_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


def compute_job(job_id: str) -> None:
    with connect() as con:
        job = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job: return
        con.execute("UPDATE jobs SET status='RUNNING',updated_at=? WHERE id=?", (now(), job_id))
        primary = con.execute("SELECT * FROM datasets WHERE id=?", (job["dataset_id"],)).fetchone()
        reference_id = json.loads(job["parameters"]).get("reference_dataset_id")
        reference = con.execute("SELECT * FROM datasets WHERE id=?", (reference_id,)).fetchone() if reference_id else None
    try:
        path = dataset_path(primary)
        warnings: list[str] = []
        if path.suffix.lower() in {".tif", ".tiff"}:
            with rasterio.open(path) as ds:
                bands = ds.read(masked=True)
                result_bands = []
                for index, band in enumerate(bands, start=1):
                    values = np.asarray(band.compressed(), dtype=np.float64)
                    if values.size == 0:
                        stats = {"band": index, "valid_pixels": 0, "min": None, "max": None, "mean": None, "std": None}
                    else:
                        stats = {"band": index, "valid_pixels": int(values.size), "min": float(values.min()), "max": float(values.max()), "mean": float(values.mean()), "std": float(values.std())}
                    result_bands.append(stats)
                result: dict[str, Any] = {"kind": job["kind"], "dataset_id": primary["id"], "bands": result_bands,
                    "dimensions": [ds.height, ds.width], "crs": str(ds.crs) if ds.crs else None,
                    "georeferenced": bool(ds.crs and not ds.transform.is_identity)}
                if job["kind"] == "difference_screening":
                    if not reference:
                        raise ValueError("difference_screening requires reference_dataset_id")
                    refpath = dataset_path(reference)
                    if refpath.suffix.lower() not in {".tif", ".tiff"}:
                        raise ValueError("Difference screening requires two raster datasets")
                    with rasterio.open(refpath) as other:
                        if (ds.width, ds.height, ds.count) != (other.width, other.height, other.count):
                            raise ValueError("Raster dimensions or band counts differ; resampling/alignment is not silently performed")
                        if ds.crs != other.crs or ds.transform != other.transform:
                            raise ValueError("CRS or affine transform differs; register/align datasets before pixel comparison")
                        diffs = []
                        for i in range(1, ds.count+1):
                            a = ds.read(i, masked=True).astype("float64")
                            b = other.read(i, masked=True).astype("float64")
                            valid = ~(np.ma.getmaskarray(a) | np.ma.getmaskarray(b))
                            d = np.abs(np.asarray(a.filled(0))-np.asarray(b.filled(0)))[valid]
                            diffs.append({"band": i, "valid_pixels": int(d.size), "mean_absolute_difference": float(d.mean()) if d.size else None, "max_absolute_difference": float(d.max()) if d.size else None})
                        result["reference_dataset_id"] = reference["id"]
                        result["absolute_difference"] = diffs
                        warnings.append("Pixel differences are screening evidence, not proof of environmental change; illumination, season, sensor, and registration may affect them.")
        else:
            with Image.open(path) as im:
                arr = np.asarray(im.convert("RGB"), dtype=np.float64)
            result = {"kind": job["kind"], "dataset_id": primary["id"], "dimensions": [int(arr.shape[0]), int(arr.shape[1])],
                      "channels": ["red", "green", "blue"], "statistics": [{"channel": c, "min": float(arr[:,:,i].min()), "max": float(arr[:,:,i].max()), "mean": float(arr[:,:,i].mean()), "std": float(arr[:,:,i].std())} for i,c in enumerate(["red","green","blue"])],
                      "georeferenced": False}
            if job["kind"] == "difference_screening":
                raise ValueError("Difference screening currently requires georeferenced raster inputs; ordinary RGB images are not silently treated as satellite data")
            warnings.append("Ordinary image: no CRS, geographic bounds, acquisition date, or sensor identity is inferred.")
        result["created_at"] = now()
        result["warnings"] = warnings
        output_path = OUTPUT_DIR / f"{job_id}.json"
        output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        with connect() as con:
            con.execute("UPDATE jobs SET status='SUCCEEDED',result=?,warnings=?,updated_at=? WHERE id=?", (json.dumps(result), json.dumps(warnings), now(), job_id))
    except Exception as exc:
        with connect() as con:
            con.execute("UPDATE jobs SET status='FAILED',error=?,updated_at=? WHERE id=?", (str(exc), now(), job_id))


@app.post("/api/analyses", status_code=202)
def create_analysis(req: AnalysisRequest, background: BackgroundTasks) -> dict[str, Any]:
    with connect() as con:
        dataset = con.execute("SELECT id FROM datasets WHERE id=?", (req.dataset_id,)).fetchone()
        if not dataset: raise HTTPException(404, "Input dataset not found")
        if req.kind == "difference_screening":
            if not req.reference_dataset_id: raise HTTPException(422, "reference_dataset_id is required for difference screening")
            ref = con.execute("SELECT id FROM datasets WHERE id=?", (req.reference_dataset_id,)).fetchone()
            if not ref: raise HTTPException(404, "Reference dataset not found")
            if req.reference_dataset_id == req.dataset_id: raise HTTPException(422, "Input and reference must be different datasets")
        job_id = str(uuid.uuid4()); created = now()
        params = {**req.parameters, "reference_dataset_id": req.reference_dataset_id}
        con.execute("INSERT INTO jobs (id,dataset_id,kind,status,parameters,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (job_id, req.dataset_id, req.kind, "QUEUED", json.dumps(params), created, created))
    background.add_task(compute_job, job_id)
    return {"id": job_id, "status": "QUEUED", "created_at": created}


@app.get("/api/analyses")
def list_analyses() -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [{"id": r["id"], "dataset_id": r["dataset_id"], "kind": r["kind"], "status": r["status"], "created_at": r["created_at"], "updated_at": r["updated_at"], "error": r["error"]} for r in rows]


@app.get("/api/analyses/{job_id}")
def get_analysis(job_id: str) -> dict[str, Any]:
    with connect() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r: raise HTTPException(404, "Analysis job not found")
    return {"id": r["id"], "dataset_id": r["dataset_id"], "kind": r["kind"], "status": r["status"],
            "parameters": json.loads(r["parameters"]), "result": json.loads(r["result"]) if r["result"] else None,
            "warnings": json.loads(r["warnings"]), "error": r["error"], "created_at": r["created_at"], "updated_at": r["updated_at"]}


class DocumentRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=2_000_000)


@app.post("/api/retrieval/documents", status_code=201)
def add_document(req: DocumentRequest) -> dict[str, Any]:
    doc_id = str(uuid.uuid4())
    with connect() as con: con.execute("INSERT INTO documents VALUES (?,?,?,?)", (doc_id, Path(req.filename).name, req.text, now()))
    return {"id": doc_id, "filename": Path(req.filename).name, "characters": len(req.text), "created_at": now()}


@app.get("/api/retrieval/documents")
def list_documents() -> list[dict[str, Any]]:
    with connect() as con: rows = con.execute("SELECT id,filename,content,created_at FROM documents ORDER BY created_at DESC").fetchall()
    return [{"id": r["id"], "filename": r["filename"], "characters": len(r["content"]), "created_at": r["created_at"]} for r in rows]


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=5, ge=1, le=20)


@app.post("/api/retrieval/text")
def retrieve_text(req: QueryRequest) -> dict[str, Any]:
    terms = list(dict.fromkeys(t.lower() for t in req.query.split() if len(t) > 1))
    with connect() as con: rows = con.execute("SELECT * FROM documents").fetchall()
    found = []
    for row in rows:
        text = row["content"]
        lower = text.lower(); score = sum(lower.count(term) for term in terms)
        if score:
            positions = [lower.find(term) for term in terms if lower.find(term) >= 0]
            pos = min(positions) if positions else 0
            found.append({"document_id": row["id"], "filename": row["filename"], "score": score,
                          "method": "lexical term-frequency; not semantic retrieval", "passage": text[max(0,pos-220):min(len(text),pos+600)], "source_type": "user-provided text", "created_at": row["created_at"]})
    found.sort(key=lambda x: x["score"], reverse=True)
    return {"query": req.query, "method": "lexical term-frequency", "results": found[:req.limit], "indexed_documents": len(rows), "message": None if found else "No matching passages. Add a source document or try different terms."}


def visual_descriptor(path: Path) -> list[float]:
    if path.suffix.lower() in {".tif", ".tiff"}:
        with rasterio.open(path) as ds:
            count = min(ds.count, 3)
            arr = ds.read(list(range(1,count+1)), out_shape=(count, min(ds.height,256), min(ds.width,256)), masked=True)
            chans = []
            for b in arr:
                vals = np.asarray(b.astype(np.float32).filled(np.nan), dtype=np.float32); valid = np.isfinite(vals)
                if not valid.any(): chans.append(np.zeros(vals.shape,dtype=np.uint8)); continue
                lo,hi=np.nanpercentile(vals[valid],[2,98]); chans.append(np.zeros(vals.shape,dtype=np.uint8) if hi<=lo else np.clip((np.nan_to_num(vals,nan=lo)-lo)/(hi-lo)*255,0,255).astype(np.uint8))
            while len(chans)<3: chans.append(chans[-1])
            im=Image.fromarray(np.stack(chans[:3],axis=-1))
    else:
        with Image.open(path) as src: im=src.convert("RGB")
    im.thumbnail((256,256)); arr=np.asarray(im)
    features=[]
    for channel in range(3):
        hist,_=np.histogram(arr[:,:,channel],bins=16,range=(0,256),density=False)
        hist=hist.astype(float); hist/=max(hist.sum(),1); features.extend(hist.tolist())
    gray=np.asarray(im.convert("L"),dtype=float)
    features.extend([float(gray.mean()/255),float(gray.std()/255)])
    return features


@app.post("/api/retrieval/exemplars/{dataset_id}", status_code=201)
def index_exemplar(dataset_id: str) -> dict[str, Any]:
    with connect() as con: row=con.execute("SELECT * FROM datasets WHERE id=?",(dataset_id,)).fetchone()
    if not row: raise HTTPException(404,"Dataset not found")
    desc=visual_descriptor(dataset_path(row)); exemplar_id=str(uuid.uuid4()); created=now()
    with connect() as con:
        con.execute("INSERT INTO exemplars VALUES (?,?,?,?,?)",(exemplar_id,dataset_id,json.dumps(desc),"RGB histogram + grayscale mean/std; low-level visual similarity",created))
    return {"id":exemplar_id,"dataset_id":dataset_id,"method":"RGB histogram + grayscale mean/std; low-level visual similarity","created_at":created}


@app.get("/api/retrieval/exemplars")
def list_exemplars() -> list[dict[str, Any]]:
    with connect() as con: rows=con.execute("SELECT e.id,e.dataset_id,e.method,e.created_at,d.filename FROM exemplars e JOIN datasets d ON d.id=e.dataset_id ORDER BY e.created_at DESC").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/retrieval/visual")
def retrieve_visual(req: QueryRequest) -> dict[str, Any]:
    # query must identify an imported dataset ID; this makes the comparison reproducible.
    with connect() as con:
        queryrow=con.execute("SELECT * FROM datasets WHERE id=?",(req.query,)).fetchone()
        rows=con.execute("SELECT e.*,d.filename FROM exemplars e JOIN datasets d ON d.id=e.dataset_id").fetchall()
    if not queryrow: raise HTTPException(422,"For visual retrieval, query must be the ID of an imported dataset")
    q=np.asarray(visual_descriptor(dataset_path(queryrow)),dtype=float)
    results=[]
    for r in rows:
        if r["dataset_id"]==queryrow["id"]: continue
        d=np.asarray(json.loads(r["descriptor"]),dtype=float)
        similarity=float(np.clip(1-np.linalg.norm(q-d)/np.sqrt(len(q)*2),0,1))
        results.append({"exemplar_id":r["id"],"dataset_id":r["dataset_id"],"filename":r["filename"],"similarity":similarity,"method":r["method"],"interpretation":"Low-level color/texture descriptor similarity only; not semantic scene understanding."})
    results.sort(key=lambda x:x["similarity"],reverse=True)
    return {"query_dataset_id":queryrow["id"],"method":"RGB histogram + grayscale mean/std distance","indexed_exemplars":len(rows),"results":results[:req.limit],"message":None if results else "No other indexed exemplars are available."}


class VerifyRequest(BaseModel):
    job_id: str
    evidence_dataset_id: str | None = None
    evidence_description: str | None = None
    independent: bool = False


@app.post("/api/verifications", status_code=201)
def verify(req: VerifyRequest) -> dict[str, Any]:
    with connect() as con:
        job=con.execute("SELECT * FROM jobs WHERE id=?",(req.job_id,)).fetchone()
        evidence=con.execute("SELECT * FROM datasets WHERE id=?",(req.evidence_dataset_id,)).fetchone() if req.evidence_dataset_id else None
    if not job: raise HTTPException(404,"Analysis job not found")
    if job["status"]!="SUCCEEDED": raise HTTPException(409,"Only completed analysis jobs can be reviewed")
    if req.evidence_dataset_id and not evidence: raise HTTPException(404,"Evidence dataset not found")
    if not req.independent or not evidence:
        status="NOT_VERIFIABLE"; explanation="No suitable independent evidence was supplied. A prediction or analysis result alone cannot independently verify itself. Provide a separate, relevant reference observation or labeled dataset and document its provenance."
    elif evidence["id"]==job["dataset_id"]:
        status="NOT_VERIFIABLE"; explanation="The evidence dataset is the same as the analysis input, so it is not independent."
    else:
        status="INCONCLUSIVE"; explanation="A separate dataset was declared, but independence and scientific suitability have not been established automatically. A researcher must assess acquisition, provenance, spatial/temporal compatibility, and relevance to the claim."
    verification_id=str(uuid.uuid4()); created=now()
    with connect() as con:
        con.execute("INSERT INTO verifications VALUES (?,?,?,?,?,?)",(verification_id,req.job_id,status,explanation,req.evidence_dataset_id,created))
    return {"id":verification_id,"job_id":req.job_id,"status":status,"explanation":explanation,"evidence_dataset_id":req.evidence_dataset_id,"evidence_description":req.evidence_description,"created_at":created}


@app.get("/api/verifications")
def list_verifications() -> list[dict[str, Any]]:
    with connect() as con: rows=con.execute("SELECT * FROM verifications ORDER BY created_at DESC").fetchall()
    return [{"id":r["id"],"job_id":r["job_id"],"status":r["status"],"explanation":r["explanation"],"evidence_dataset_id":r["evidence_dataset_id"],"created_at":r["created_at"]} for r in rows]


def make_report(job_id: str) -> dict[str, Any]:
    with connect() as con:
        job=con.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        if not job: raise HTTPException(404,"Analysis job not found")
        dataset=con.execute("SELECT * FROM datasets WHERE id=?",(job["dataset_id"],)).fetchone()
        verifications=con.execute("SELECT * FROM verifications WHERE job_id=? ORDER BY created_at",(job_id,)).fetchall()
    if job["status"]!="SUCCEEDED": raise HTTPException(409,"Cannot report an analysis that has not succeeded")
    return {"report_id":str(uuid.uuid4()),"created_at":now(),"objective":f"{job['kind']} analysis of {dataset['filename']}","input_dataset":row_to_dataset(dataset),"analysis":{"id":job["id"],"kind":job["kind"],"status":job["status"],"parameters":json.loads(job["parameters"]),"result":json.loads(job["result"]),"warnings":json.loads(job["warnings"]),"created_at":job["created_at"],"updated_at":job["updated_at"]},"verifications":[{"id":v["id"],"status":v["status"],"explanation":v["explanation"],"evidence_dataset_id":v["evidence_dataset_id"],"created_at":v["created_at"]} for v in verifications],"limitations":["Computed pixel statistics and differences are not independently verified scientific conclusions.","No sensor, acquisition date, or spectral-band identity is inferred when unavailable.","Visual preview normalization is for display and is not a calibrated measurement."],"reproducibility":{"software":"Bhu-Drishti API "+APP_VERSION,"input_sha256":dataset["sha256"],"storage_mode":"local"}}


@app.post("/api/reports/{job_id}", status_code=201)
def create_report(job_id: str) -> dict[str, Any]:
    report=make_report(job_id); created=report["created_at"]
    with connect() as con: con.execute("INSERT INTO reports VALUES (?,?,?,?)",(report["report_id"],job_id,json.dumps(report),created))
    return report


@app.get("/api/reports/{job_id}/export")
def export_report(job_id: str, format: str = "json") -> Response:
    with connect() as con: row=con.execute("SELECT * FROM reports WHERE job_id=? ORDER BY created_at DESC LIMIT 1",(job_id,)).fetchone()
    report=json.loads(row["content"]) if row else make_report(job_id)
    if format=="json": return Response(json.dumps(report,indent=2),media_type="application/json",headers={"Content-Disposition":f'attachment; filename="bhu-drishti-{job_id}.json"'})
    if format=="csv":
        buf=io.StringIO(); writer=csv.writer(buf); writer.writerow(["section","key","value"])
        writer.writerow(["analysis","job_id",job_id]); writer.writerow(["analysis","status",report["analysis"]["status"]]); writer.writerow(["analysis","kind",report["analysis"]["kind"]]); writer.writerow(["dataset","filename",report["input_dataset"]["filename"]]); writer.writerow(["dataset","sha256",report["input_dataset"]["sha256"]]); writer.writerow(["analysis","result_json",json.dumps(report["analysis"]["result"])]); writer.writerow(["verification","records_json",json.dumps(report["verifications"])]); writer.writerow(["limitations","items",json.dumps(report["limitations"])])
        return Response(buf.getvalue(),media_type="text/csv",headers={"Content-Disposition":f'attachment; filename="bhu-drishti-{job_id}.csv"'})
    raise HTTPException(400,"format must be json or csv")
