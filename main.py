from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
import pandas as pd
from shapely.geometry import Point
import geopandas as gpd
import tempfile
import os
import shutil
import fiona
import io
import re
import math
from pyproj import Transformer
import json, base64

# Aktifin support KML di Geopandas
fiona.supported_drivers['KML'] = 'rw'
fiona.supported_drivers['LIBKML'] = 'rw'

app = FastAPI(title="TerraPivot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

def baca_vektor(file_path: str, filename: str):
    ext = os.path.splitext(filename)[1].lower()
    if ext == '.zip':
        return gpd.read_file(f"zip://{file_path}")
    elif ext in ['.geojson', '.json']:
        return gpd.read_file(file_path)
    elif ext == '.kml':
        return gpd.read_file(file_path, driver='KML')
    elif ext == '.gpkg':
        return gpd.read_file(file_path, driver='GPKG')
    else:
        try:
            return gpd.read_file(file_path)
        except:
            raise Exception("Format input tidak dikenali atau tidak didukung.")

# Fungsi konversi Lon/Lat ke UTM X, Y, dan Zona
def hitung_utm(lon, lat):
    if pd.isna(lon) or pd.isna(lat): return pd.Series([None, None, None])
    zone = math.floor((lon + 180) / 6) + 1
    hemisphere = 'N' if lat >= 0 else 'S'
    epsg_code = 32600 + zone if hemisphere == 'N' else 32700 + zone
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg_code}", always_xy=True)
    utm_x, utm_y = transformer.transform(lon, lat)
    return pd.Series([round(utm_x, 3), round(utm_y, 3), f"{zone}{hemisphere}"])

# ==========================================
# 1. VECTOR CONVERTER
# ==========================================
@app.post("/api/preview-vector")
async def preview_vector(file: UploadFile = File(...)):
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, file.filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        try:
            gdf = baca_vektor(file_path, file.filename)
            if gdf.crs is not None and not gdf.crs.equals("EPSG:4326"):
                gdf = gdf.to_crs("EPSG:4326")
            return Response(content=gdf.to_json(), media_type="application/json")
        except Exception as e:
            raise HTTPException(500, detail=f"Gagal baca data: {str(e)}")

@app.post("/api/convert-vector")
async def convert_vector(file: UploadFile = File(...), format_output: str = Form(...)):
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, file.filename)
        with open(file_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)
        try:
            gdf = baca_vektor(file_path, file.filename)
            base_name = os.path.splitext(file.filename)[0]

            if format_output == "GeoJSON": return Response(content=gdf.to_json(), media_type="application/geo+json", headers={"Content-Disposition": f'attachment; filename="{base_name}.geojson"'})
            elif format_output == "KML":
                if gdf.crs is not None and not gdf.crs.equals("EPSG:4326"): gdf = gdf.to_crs("EPSG:4326")
                out_path = os.path.join(tmpdir, f"{base_name}.kml")
                gdf.to_file(out_path, driver="KML")
                with open(out_path, "rb") as f: return Response(content=f.read(), media_type="application/vnd.google-earth.kml+xml", headers={"Content-Disposition": f'attachment; filename="{base_name}.kml"'})
            elif format_output == "GPKG":
                out_path = os.path.join(tmpdir, f"{base_name}.gpkg")
                gdf.to_file(out_path, driver="GPKG")
                with open(out_path, "rb") as f: return Response(content=f.read(), media_type="application/geopackage+sqlite3", headers={"Content-Disposition": f'attachment; filename="{base_name}.gpkg"'})
            elif format_output == "SHP":
                shp_dir = os.path.join(tmpdir, "shp_output"); os.makedirs(shp_dir)
                gdf.to_file(os.path.join(shp_dir, f"{base_name}.shp"), driver="ESRI Shapefile")
                shutil.make_archive(os.path.join(tmpdir, base_name), 'zip', shp_dir)
                with open(os.path.join(tmpdir, f"{base_name}.zip"), "rb") as f: return Response(content=f.read(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{base_name}.zip"'})
            else: raise HTTPException(400, detail="Format output tidak didukung!")
        except Exception as e: raise HTTPException(500, detail=f"Gagal convert: {str(e)}")
        
# ==========================================
# 2. CSV TO SPATIAL
# ==========================================
@app.post("/api/get-columns")
async def get_columns(file: UploadFile = File(...)):
    try:
        ext = os.path.splitext(file.filename)[1].lower()
        contents = await file.read()
        if ext == '.csv':
            # Auto-detect separator koma atau titik koma (sep=None, engine='python')
            df = pd.read_csv(io.StringIO(contents.decode('utf-8')), sep=None, engine='python')
        elif ext in ['.xls', '.xlsx']:
            df = pd.read_excel(io.BytesIO(contents))
        else: raise HTTPException(400, detail="Hanya terima CSV/Excel")
        return {"columns": list(df.columns)}
    except Exception as e: raise HTTPException(500, detail=f"Gagal membaca kolom: {str(e)}")
    
@app.post("/api/table-to-spatial")
async def table_to_spatial(
    file: UploadFile = File(...), x_col: str = Form(...), y_col: str = Form(...), 
    z_col: str = Form("NONE"), name_col: str = Form("NONE"), format_output: str = Form(...)
):
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, file.filename)
        with open(file_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)
        ext = os.path.splitext(file.filename)[1].lower()
        try:
            if ext == '.csv': df = pd.read_csv(file_path, sep=None, engine='python')
            else: df = pd.read_excel(file_path)
            
            df[x_col] = pd.to_numeric(df[x_col], errors='coerce')
            df[y_col] = pd.to_numeric(df[y_col], errors='coerce')
            df = df.dropna(subset=[x_col, y_col]) 
            if z_col and z_col != "NONE" and z_col in df.columns:
                df[z_col] = pd.to_numeric(df[z_col], errors='coerce')
                geometry = [Point(x, y, z) if pd.notnull(z) else Point(x, y) for x, y, z in zip(df[x_col], df[y_col], df[z_col])]
            else: geometry = [Point(xy) for xy in zip(df[x_col], df[y_col])]
            
            gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
            base_name = os.path.splitext(file.filename)[0]

            if format_output == "PREVIEW": return Response(content=gdf.to_json(), media_type="application/json")
            elif format_output == "GeoJSON": return Response(content=gdf.to_json(), media_type="application/geo+json", headers={"Content-Disposition": f'attachment; filename="{base_name}.geojson"'})
            elif format_output == "KML":
                kml_gdf = gdf.copy()
                if name_col != "NONE" and name_col in kml_gdf.columns: kml_gdf['Name'] = kml_gdf[name_col].astype(str)
                else: kml_gdf['Name'] = "Titik_" + kml_gdf.index.astype(str)
                def build_html_table(row):
                    html = "<table border='1' style='border-collapse:collapse;' cellpadding='5'>"
                    for col in df.columns: html += f"<tr><td><b>{col}</b></td><td>{row[col]}</td></tr>"
                    html += "</table>"
                    return html
                kml_gdf['Description'] = kml_gdf.apply(build_html_table, axis=1)
                out_path = os.path.join(tmpdir, f"{base_name}.kml")
                kml_gdf.to_file(out_path, driver="KML")
                with open(out_path, "rb") as f: return Response(content=f.read(), media_type="application/vnd.google-earth.kml+xml", headers={"Content-Disposition": f'attachment; filename="{base_name}.kml"'})
            elif format_output == "GPKG":
                out_path = os.path.join(tmpdir, f"{base_name}.gpkg")
                gdf.to_file(out_path, driver="GPKG")
                with open(out_path, "rb") as f: return Response(content=f.read(), media_type="application/geopackage+sqlite3", headers={"Content-Disposition": f'attachment; filename="{base_name}.gpkg"'})
            elif format_output == "SHP":
                shp_dir = os.path.join(tmpdir, "shp_output"); os.makedirs(shp_dir)
                gdf.to_file(os.path.join(shp_dir, f"{base_name}.shp"), driver="ESRI Shapefile")
                shutil.make_archive(os.path.join(tmpdir, base_name), 'zip', shp_dir)
                with open(os.path.join(tmpdir, f"{base_name}.zip"), "rb") as f: return Response(content=f.read(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{base_name}.zip"'})
        except Exception as e: raise HTTPException(500, detail=f"Gagal convert spasial: {str(e)}")

# ==========================================
# 3. DMS TO DD
# ==========================================
def konversi_dms_ke_dd(dms_str):
    if pd.isna(dms_str) or str(dms_str).strip() == '': return None
    teks = str(dms_str).strip().upper()
    angka = re.findall(r"[-+]?\d*\.\d+|\d+", teks)
    semua_arah = re.findall(r'[NSEW]', teks)
    arah_final = semua_arah[-1] if semua_arah else None
    try:
        if len(angka) >= 3:
            derajat = abs(float(angka[0])); menit = float(angka[1]); detik = float(angka[2])
            dd = derajat + (menit / 60.0) + (detik / 3600.0)
            if float(angka[0]) < 0 or arah_final in ['S', 'W']: dd = -dd
            return dd
        elif len(angka) == 1: return float(angka[0])
        else: return None
    except: return None

@app.post("/api/dms-to-spatial")
async def dms_to_spatial(
    file: UploadFile = File(...), x_col: str = Form(...), y_col: str = Form(...), 
    name_col: str = Form("NONE"), format_output: str = Form(...)
):
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, file.filename)
        with open(file_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)
        ext = os.path.splitext(file.filename)[1].lower()
        try:
            if ext == '.csv': df = pd.read_csv(file_path, sep=None, engine='python')
            else: df = pd.read_excel(file_path)
            
            df[x_col] = df[x_col].apply(konversi_dms_ke_dd)
            df[y_col] = df[y_col].apply(konversi_dms_ke_dd)
            df = df.dropna(subset=[x_col, y_col]) 
            
            geometry = [Point(xy) for xy in zip(df[x_col], df[y_col])]
            gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
            base_name = os.path.splitext(file.filename)[0]

            if format_output == "PREVIEW": return Response(content=gdf.to_json(), media_type="application/json")
            elif format_output == "GeoJSON": return Response(content=gdf.to_json(), media_type="application/geo+json", headers={"Content-Disposition": f'attachment; filename="{base_name}.geojson"'})
            elif format_output == "KML":
                kml_gdf = gdf.copy()
                if name_col != "NONE" and name_col in kml_gdf.columns: kml_gdf['Name'] = kml_gdf[name_col].astype(str)
                else: kml_gdf['Name'] = "Titik_" + kml_gdf.index.astype(str)
                def build_html_table(row):
                    html = "<table border='1' style='border-collapse:collapse;' cellpadding='5'>"
                    for col in df.columns: html += f"<tr><td><b>{col}</b></td><td>{row[col]}</td></tr>"
                    html += "</table>"
                    return html
                kml_gdf['Description'] = kml_gdf.apply(build_html_table, axis=1)
                out_path = os.path.join(tmpdir, f"{base_name}.kml")
                kml_gdf.to_file(out_path, driver="KML")
                with open(out_path, "rb") as f: return Response(content=f.read(), media_type="application/vnd.google-earth.kml+xml", headers={"Content-Disposition": f'attachment; filename="{base_name}.kml"'})
            elif format_output == "GPKG":
                out_path = os.path.join(tmpdir, f"{base_name}.gpkg")
                gdf.to_file(out_path, driver="GPKG")
                with open(out_path, "rb") as f: return Response(content=f.read(), media_type="application/geopackage+sqlite3", headers={"Content-Disposition": f'attachment; filename="{base_name}.gpkg"'})
            elif format_output == "SHP":
                shp_dir = os.path.join(tmpdir, "shp_output"); os.makedirs(shp_dir)
                gdf.to_file(os.path.join(shp_dir, f"{base_name}.shp"), driver="ESRI Shapefile")
                shutil.make_archive(os.path.join(tmpdir, base_name), 'zip', shp_dir)
                with open(os.path.join(tmpdir, f"{base_name}.zip"), "rb") as f: return Response(content=f.read(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{base_name}.zip"'})

        except Exception as e: raise HTTPException(500, detail=f"Gagal convert spasial: {str(e)}")

# ==========================================
# 4. SPATIAL TO TABLE (UPDATE: UTM & EXCEL)
# ==========================================
@app.post("/api/spatial-to-table")
async def spatial_to_table(file: UploadFile = File(...), format_output: str = Form(...)):
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, file.filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        try:
            gdf = baca_vektor(file_path, file.filename)
            base_name = os.path.splitext(file.filename)[0]

            # Set CRS ke WGS84 biar bisa tarik lintang bujur
            if gdf.crs is not None and not gdf.crs.equals("EPSG:4326"):
                gdf = gdf.to_crs("EPSG:4326")
            
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                gdf['Lon_X'] = round(gdf.geometry.centroid.x, 7)
                gdf['Lat_Y'] = round(gdf.geometry.centroid.y, 7)
            
            # Buang kolom geometry biar jadi DataFrame biasa
            df = pd.DataFrame(gdf.drop(columns='geometry'))
            
            # Hitung UTM X, UTM Y, dan Zona secara sakti
            df[['UTM_X', 'UTM_Y', 'UTM_Zona']] = df.apply(lambda row: hitung_utm(row['Lon_X'], row['Lat_Y']), axis=1)
            
            if format_output == "PREVIEW":
                preview_df = df.head(100).fillna("") 
                return Response(content=preview_df.to_json(orient="records"), media_type="application/json")
                
            elif format_output == "EXCEL":
                excel_buffer = io.BytesIO()
                # Export ke native Excel biar kebal dari masalah separator!
                df.to_excel(excel_buffer, index=False, engine='openpyxl')
                excel_buffer.seek(0)
                return Response(
                    content=excel_buffer.read(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="Tabel_{base_name}.xlsx"'}
                )
                
            elif format_output == "CSV_COMMA":
                csv_data = df.to_csv(index=False, sep=',')
                return Response(content=csv_data, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="Tabel_{base_name}.csv"'})
                
            elif format_output == "CSV_SEMI":
                csv_data = df.to_csv(index=False, sep=';', decimal=',')
                return Response(content=csv_data, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="Tabel_{base_name}.csv"'})
            else:
                raise HTTPException(400, detail="Format tidak didukung!")
                
        except Exception as e:
            raise HTTPException(500, detail=f"Gagal ekstrak tabel: {str(e)}")
        
# ==========================================
# 5. KONVERSI PROYEKSI (WGS84 ↔ UTM / TM3)
# ==========================================

# Kamus zona TM3 Indonesia (format: "TM3_zona" -> EPSG)
TM3_ZONES = {
    "TM3_46.2": 23840,
    "TM3_48.1": 23841,
    "TM3_48.2": 23842,
    "TM3_49.1": 23843,
    "TM3_49.2": 23844,
    "TM3_50.1": 23845,
    "TM3_50.2": 23846,
    "TM3_51.1": 23847,
    "TM3_51.2": 23848,
    "TM3_52.1": 23849,
    "TM3_52.2": 23850,
    "TM3_53.1": 23851,
    "TM3_53.2": 23852,
    "TM3_54.1": 23853,
    "TM3_54.2": 23854,
    "TM3_55.1": 23855,
}

def parse_crs(crs_string: str) -> int:
    """
    Mengubah string CRS dari frontend menjadi kode EPSG integer.
    Contoh:
        "EPSG:4326" -> 4326
        "UTM_48S"   -> 32748 (UTM zona 48 South)
        "UTM_48N"   -> 32648
        "TM3_48.2"  -> 23842
    """
    if crs_string.startswith("EPSG:"):
        return int(crs_string.split(":")[1])
    elif crs_string.startswith("UTM_"):
        # Format: UTM_{zona}{N/S}
        zone_str = crs_string[4:-1]  # ambil angka zona
        hemisfer = crs_string[-1]    # N atau S
        zone = int(zone_str)
        if hemisfer.upper() == 'N':
            return 32600 + zone
        else:
            return 32700 + zone
    elif crs_string.startswith("TM3_"):
        # Format: TM3_zona (misal TM3_48.2)
        key = crs_string  # sudah persis dengan key di TM3_ZONES
        if key in TM3_ZONES:
            return TM3_ZONES[key]
        else:
            raise HTTPException(400, detail=f"Zona TM3 tidak dikenal: {crs_string}")
    else:
        raise HTTPException(400, detail=f"Format CRS tidak didukung: {crs_string}")

@app.post("/api/convert-projection")
async def convert_projection(
    file: UploadFile = File(...),
    source_crs: str = Form("EPSG:4326"),
    target_crs: str = Form(...),
    output_format: str = Form("GeoJSON")   # <-- TAMBAHKAN
):
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, file.filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        try:
            gdf = baca_vektor(file_path, file.filename)

            # Jika file sudah memiliki CRS, abaikan source_crs dari user
            if gdf.crs is None:
                # Gunakan source_crs yang diberikan user
                src_epsg = parse_crs(source_crs)
                gdf.set_crs(epsg=src_epsg, inplace=True)
            else:
                # Gunakan CRS bawaan file
                pass  # gdf.crs sudah terdefinisi

            # Parse target CRS
            tgt_epsg = parse_crs(target_crs)

            # Preview GeoJSON (WGS84)
            gdf_preview = gdf.to_crs("EPSG:4326")
            preview_geojson = gdf_preview.to_json()

            # Konversi ke target CRS
            gdf_target = gdf.to_crs(tgt_epsg)
            base_name = os.path.splitext(file.filename)[0]

            # Siapkan respons berdasarkan output_format
            if output_format == "GeoJSON":
                geojson_str = gdf_target.to_json()
                file_content = geojson_str.encode('utf-8')
                media_type = "application/geo+json"
                filename = f"{base_name}_reprojected.geojson"
                resp = Response(content=file_content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

            elif output_format == "KML":
                if gdf_target.crs is not None and not gdf_target.crs.equals("EPSG:4326"):
                    gdf_target = gdf_target.to_crs("EPSG:4326")
                out_path = os.path.join(tmpdir, f"{base_name}.kml")
                gdf_target.to_file(out_path, driver="KML")
                with open(out_path, "rb") as f:
                    file_content = f.read()
                resp = Response(content=file_content, media_type="application/vnd.google-earth.kml+xml", headers={"Content-Disposition": f'attachment; filename="{base_name}_reprojected.kml"'})

            elif output_format == "GPKG":
                out_path = os.path.join(tmpdir, f"{base_name}.gpkg")
                gdf_target.to_file(out_path, driver="GPKG")
                with open(out_path, "rb") as f:
                    file_content = f.read()
                resp = Response(content=file_content, media_type="application/geopackage+sqlite3", headers={"Content-Disposition": f'attachment; filename="{base_name}_reprojected.gpkg"'})

            elif output_format == "SHP":
                shp_dir = os.path.join(tmpdir, "shp_output")
                os.makedirs(shp_dir)
                gdf_target.to_file(os.path.join(shp_dir, f"{base_name}.shp"), driver="ESRI Shapefile")
                zip_path = os.path.join(tmpdir, f"{base_name}_reprojected.zip")
                shutil.make_archive(zip_path.replace('.zip', ''), 'zip', shp_dir)
                with open(zip_path, "rb") as f:
                    file_content = f.read()
                resp = Response(content=file_content, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{base_name}_reprojected.zip"'})
            else:
                raise HTTPException(400, detail="Format output tidak didukung!")

            # Gabungkan dengan preview (karena frontend tetap perlu JSON)
            return {
                "preview": json.loads(preview_geojson),
                "file_data": base64.b64encode(file_content).decode('utf-8'),
                "filename": resp.headers["Content-Disposition"].split("filename=")[1].strip('"')
            }

        except Exception as e:
            raise HTTPException(500, detail=f"Gagal konversi proyeksi: {str(e)}")
        
# Fungsi kebalikan: dari EPSG integer ke label string untuk frontend
def crs_to_label(epsg_code: int) -> str:
    """Mengonversi kode EPSG ke label yang dikenali frontend."""
    # Cek TM3
    for label, code in TM3_ZONES.items():
        if code == epsg_code:
            return label
    
    # Cek UTM South (32701-32760)
    if 32701 <= epsg_code <= 32760:
        zone = epsg_code - 32700
        return f"UTM_{zone}S"
    
    # Cek UTM North (32601-32660)
    if 32601 <= epsg_code <= 32660:
        zone = epsg_code - 32600
        return f"UTM_{zone}N"
    
    # Default
    return f"EPSG:{epsg_code}"

@app.post("/api/detect-crs")
async def detect_crs(file: UploadFile = File(...)):
    """Membaca CRS bawaan file spasial dan mengembalikan labelnya."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, file.filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        try:
            gdf = baca_vektor(file_path, file.filename)
            if gdf.crs is None:
                return {"crs_label": None, "message": "File tidak memiliki CRS."}
            
            epsg = gdf.crs.to_epsg()
            if epsg is None:
                return {"crs_label": None, "message": "CRS tidak terbaca."}
            
            label = crs_to_label(epsg)
            return {"crs_label": label}
        except Exception as e:
            raise HTTPException(500, detail=f"Gagal mendeteksi CRS: {str(e)}")
        
# ==========================================
# 6. BATCH REPROJECTOR
# ==========================================
import zipfile

@app.post("/api/batch-reproject")
async def batch_reproject(
    file: UploadFile = File(...),
    target_crs: str = Form(...)
):
    """
    Menerima file ZIP berisi banyak shapefile.
    Memproyeksikan ulang setiap shapefile ke target_crs,
    lalu mengembalikan ZIP berisi semua hasil.
    """
    if not file.filename.lower().endswith('.zip'):
        raise HTTPException(400, detail="Hanya file ZIP yang diterima.")

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, file.filename)
        with open(zip_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        try:
            tgt_epsg = parse_crs(target_crs)
            output_dir = os.path.join(tmpdir, "output")
            os.makedirs(output_dir, exist_ok=True)

            # Ekstrak ZIP
            with zipfile.ZipFile(zip_path, 'r') as z:
                z.extractall(tmpdir)

            # Cari semua .shp
            shp_files = []
            for root, dirs, files in os.walk(tmpdir):
                for f in files:
                    if f.lower().endswith('.shp') and "output" not in root:
                        shp_files.append(os.path.join(root, f))

            if not shp_files:
                raise HTTPException(400, detail="Tidak ada shapefile ditemukan dalam ZIP.")

            processed = 0
            for shp_path in shp_files:
                try:
                    gdf = gpd.read_file(shp_path)
                    if gdf.crs is None:
                        continue
                    gdf_proj = gdf.to_crs(tgt_epsg)
                    base = os.path.splitext(os.path.basename(shp_path))[0]
                    out_shp = os.path.join(output_dir, f"{base}_reprojected.shp")
                    gdf_proj.to_file(out_shp, driver="ESRI Shapefile")
                    processed += 1
                except Exception:
                    continue

            if processed == 0:
                raise HTTPException(400, detail="Gagal memproses semua shapefile. Pastikan setiap shapefile memiliki CRS yang valid.")

            # Buat ZIP hasil
            result_zip = os.path.join(tmpdir, "hasil_reproject")
            shutil.make_archive(result_zip, 'zip', output_dir)
            result_zip += ".zip"

            with open(result_zip, "rb") as f:
                zip_bytes = f.read()

            return Response(
                content=zip_bytes,
                media_type="application/zip",
                headers={"Content-Disposition": "attachment; filename=hasil_reproject.zip"}
            )

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, detail=f"Gagal batch reproject: {str(e)}")