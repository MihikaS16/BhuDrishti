import io
import os
from pathlib import Path

import numpy as np
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin


def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("BHU_DRISHTI_DATA_DIR", str(tmp_path / "data"))
    from app import main
    main.DATA_DIR = Path(os.environ["BHU_DRISHTI_DATA_DIR"]).resolve()
    main.UPLOAD_DIR = main.DATA_DIR / "uploads"; main.OUTPUT_DIR = main.DATA_DIR / "outputs"; main.DB_PATH = main.DATA_DIR / "bhu_drishti.sqlite3"
    main.UPLOAD_DIR.mkdir(parents=True, exist_ok=True); main.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    main.init_db()
    return TestClient(main.app)


def tif_bytes(arr, crs="EPSG:4326"):
    buf=io.BytesIO()
    with rasterio.open(buf,"w",driver="GTiff",height=arr.shape[1],width=arr.shape[2],count=arr.shape[0],dtype=arr.dtype,crs=crs,transform=from_origin(10,20,0.1,0.1),nodata=0) as ds: ds.write(arr)
    return buf.getvalue()


def test_health_and_upload_metadata(tmp_path, monkeypatch):
    client=make_client(tmp_path,monkeypatch)
    assert client.get('/api/health').json()['status']=='ok'
    raw=tif_bytes(np.array([[[0,1],[2,3]]],dtype=np.uint8))
    r=client.post('/api/datasets',files={'file':('test.tif',raw,'image/tiff')})
    assert r.status_code==201, r.text
    data=r.json(); assert data['width']==2 and data['height']==2 and data['band_count']==1
    assert data['crs']=='EPSG:4326' and data['nodata']==[0.0]
    assert client.get('/api/datasets/'+data['id']).status_code==200


def test_invalid_and_duplicate_upload(tmp_path, monkeypatch):
    client=make_client(tmp_path,monkeypatch)
    assert client.post('/api/datasets',files={'file':('bad.exe',b'x','application/octet-stream')}).status_code==415
    raw=tif_bytes(np.ones((1,2,2),dtype=np.uint8))
    assert client.post('/api/datasets',files={'file':('one.tif',raw,'image/tiff')}).status_code==201
    assert client.post('/api/datasets',files={'file':('two.tif',raw,'image/tiff')}).status_code==409


def test_statistics_job_and_report(tmp_path, monkeypatch):
    client=make_client(tmp_path,monkeypatch)
    raw=tif_bytes(np.array([[[1,2],[3,4]]],dtype=np.uint8))
    ds=client.post('/api/datasets',files={'file':('fixture.tif',raw,'image/tiff')}).json()
    # Run worker directly to make the test deterministic; endpoint also queues it.
    job=client.post('/api/analyses',json={'dataset_id':ds['id'],'kind':'statistics'}).json()
    from app.main import compute_job
    compute_job(job['id'])
    result=client.get('/api/analyses/'+job['id']).json()
    assert result['status']=='SUCCEEDED'
    assert result['result']['bands'][0]['mean']==2.5
    assert result['result']['bands'][0]['valid_pixels']==4
    report=client.post('/api/reports/'+job['id'])
    assert report.status_code==201 and report.json()['analysis']['result']['bands'][0]['mean']==2.5
    assert 'input_sha256' in report.json()['reproducibility']


def test_verification_without_independent_evidence(tmp_path, monkeypatch):
    client=make_client(tmp_path,monkeypatch)
    raw=tif_bytes(np.ones((1,2,2),dtype=np.uint8))
    ds=client.post('/api/datasets',files={'file':('fixture.tif',raw,'image/tiff')}).json()
    job=client.post('/api/analyses',json={'dataset_id':ds['id']}).json()
    from app.main import compute_job
    compute_job(job['id'])
    result=client.post('/api/verifications',json={'job_id':job['id']})
    assert result.status_code==201
    assert result.json()['status']=='NOT_VERIFIABLE'


def test_preview_and_text_retrieval(tmp_path, monkeypatch):
    client=make_client(tmp_path,monkeypatch)
    raw=tif_bytes(np.array([[[0,10],[20,30]]],dtype=np.uint8))
    ds=client.post('/api/datasets',files={'file':('preview.tif',raw,'image/tiff')}).json()
    preview=client.get(f"/api/datasets/{ds['id']}/preview")
    assert preview.status_code==200 and preview.headers['content-type']=='image/png'
    added=client.post('/api/retrieval/documents',json={'filename':'methods.txt','text':'Flood mapping requires careful validation and independent reference evidence.'})
    assert added.status_code==201
    found=client.post('/api/retrieval/text',json={'query':'independent reference evidence'})
    assert len(found.json()['results'])==1
    assert found.json()['results'][0]['filename']=='methods.txt'


def test_difference_screening_requires_same_grid(tmp_path, monkeypatch):
    client=make_client(tmp_path,monkeypatch)
    raw1=tif_bytes(np.array([[[1,2],[3,4]]],dtype=np.uint8))
    raw2=tif_bytes(np.array([[[2,2],[3,5]]],dtype=np.uint8))
    one=client.post('/api/datasets',files={'file':('one.tif',raw1,'image/tiff')}).json()
    two=client.post('/api/datasets',files={'file':('two.tif',raw2,'image/tiff')}).json()
    job=client.post('/api/analyses',json={'dataset_id':one['id'],'kind':'difference_screening','reference_dataset_id':two['id']}).json()
    from app.main import compute_job
    compute_job(job['id'])
    result=client.get('/api/analyses/'+job['id']).json()
    assert result['status']=='SUCCEEDED'
    assert result['result']['absolute_difference'][0]['mean_absolute_difference']==0.5
    assert any('not proof' in w for w in result['warnings'])
