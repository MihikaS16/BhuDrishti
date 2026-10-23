<<<<<<< HEAD
# Bhu-Drishti

A local-first geospatial research workspace. This implementation prioritizes actual file ingestion, metadata inspection, reproducible raster statistics, strict pixel-difference screening, persisted job records, explicit verification states, lexical text retrieval, low-level visual exemplar retrieval, and JSON/CSV reports. It does not require an API key.

## Prerequisites

- Python 3.11+ (Python 3.13 is used in the current build environment)
- Node.js 20+ and npm
- Windows 10/11 PowerShell or a Unix-like shell

## Start on Windows PowerShell

From the project root:

```powershell
# Backend
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r backend\requirements.txt
$env:BHU_DRISHTI_DATA_DIR = "$PWD\local-data"
uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

Open a second PowerShell terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173. API docs are at http://127.0.0.1:8000/docs.

## Run tests

From project root, with backend dependencies installed:

```powershell
$env:PYTHONPATH = "$PWD\backend"
pytest backend\tests -q
```

Frontend:

```powershell
cd frontend
npm test
npm run build
```

## Storage and configuration

- `BHU_DRISHTI_DATA_DIR`: persistent database, uploaded inputs and derived artifacts; defaults to `backend/data` when the backend starts from the repository root with the documented command.
- `BHU_DRISHTI_MAX_UPLOAD_BYTES`: maximum upload size; default 100 MiB.
- `BHU_DRISHTI_CORS_ORIGINS`: comma-separated allowed development origins; default localhost Vite origins.
- `VITE_API_URL`: frontend API base URL; defaults to `http://127.0.0.1:8000/api`.

Uploaded files are stored under `uploads/`, generated analysis JSON under `outputs/`, and SQLite metadata in `bhu_drishti.sqlite3` inside the data directory. Do not commit local data.

## Workflow

1. Import `.tif`/`.tiff` or ordinary `.png`, `.jpg`, `.jpeg`, `.bmp`, `.webp` images.
2. Inspect dimensions, band count, data types, CRS, bounds, resolution, nodata and a display-only preview when available.
3. Run per-band statistics. Pixel-difference screening is limited to raster pairs with matching dimensions, band counts, CRS and affine transforms; no silent resampling or registration is performed.
4. Record verification separately. Without appropriate independent evidence the result is `NOT_VERIFIABLE`; a separate dataset without suitability assessment remains `INCONCLUSIVE`.
5. Add actual source text for lexical retrieval. Index imported imagery as visual exemplars to compare low-level RGB histograms and grayscale statistics.
6. Generate JSON or CSV reports from persisted successful analysis records.

## Scientific limitations

- Ordinary RGB images are not assumed to be georeferenced satellite imagery.
- Display contrast stretching is not radiometric calibration.
- Sensor, acquisition date, spectral-band meaning and units are not inferred when absent.
- Pixel difference is screening evidence, not proof of environmental change.
- Text retrieval is lexical term-frequency search, not semantic embedding search.
- Visual retrieval uses low-level color/texture descriptors, not semantic image understanding.
- PDF export, model inference, segmentation, automatic registration, geospatial tiling, and semantic embeddings are not implemented in this initial slice.
- The verification workflow intentionally does not auto-label results `SUPPORTED` or `CONTRADICTED` without a defined and validated evidence assessment.

## Security notes

Uploads are size-limited, stored under generated IDs, and duplicate content is detected by SHA-256. The development server is intended for local use, not direct public exposure. Review allowed CORS origins and add authentication before deployment to a shared network.
=======
# BhuDrishti
Agentic vision-language intelligence for satellite imagery analysis
>>>>>>> 22be96f118e7d2103cea85e3efd71ff3c07b85b0
