"""
Google Earth Engine (GEE) Integration Module
==============================================
Encapsulates all Google Earth Engine (GEE) provider operations including:
- Authentication & initialization
- Geometry conversions (CRS84Tile / WebMapTile -> ee.Geometry)
- Sentinel-2 image collection filtering & SCL cloud masking
- Median compositing & NDVI computation
- GeoTIFF REST download URL generation & streaming downloads

This module isolates GEE-specific API calls so that spatial grid math
(softmaxx.geo.tiles) remains provider-agnostic.
"""

import io
import json
import logging
import zipfile
import requests
from pathlib import Path
from typing import Union, Optional, Any

import ee
from google.oauth2.service_account import Credentials

from softmaxx.config import AppConfig, get_gee_config
from softmaxx.geo.tiles import CRS84Tile, WebMapTile

logger = logging.getLogger("main." + __name__)


# ==============================================================================
# 1. AUTHENTICATION & INITIALIZATION
# ==============================================================================

def init_gee(project: Optional[str] = None) -> bool:
    """
    Initializes Google Earth Engine using service account credentials from
    AppConfig / get_gee_config(). Falls back to standard ee.Initialize() if
    credentials are managed via environment or gcloud.
    """
    try:
        # Check if already initialized
        if getattr(ee.data, "_credentials", None) or getattr(ee.data, "_auth_app", None):
            logger.debug("GEE already initialized.")
            return True
    except Exception:
        pass

    try:
        # Ensure AppConfig is loaded
        try:
            gee_config = get_gee_config()
            target_project = project or gee_config.project
            scopes = ["https://www.googleapis.com/auth/earthengine"]
            credentials = Credentials.from_service_account_info(
                gee_config.get_credentials_dict(),
                scopes=scopes,
            )
            ee.Initialize(credentials=credentials, project=target_project)
            logger.info(f"Earth Engine initialized successfully for project '{target_project}'.")
            return True
        except Exception as config_err:
            logger.warning(f"Config-based GEE initialization failed ({config_err}). Falling back to standard ee.Initialize()...")
            ee.Initialize(project=project)
            logger.info("Earth Engine initialized via standard credentials.")
            return True
    except Exception as err:
        logger.error(f"Failed to initialize Earth Engine: {err}")
        raise RuntimeError(f"GEE Initialization Error: {err}") from err


# ==============================================================================
# 2. GEOMETRY CONVERSIONS
# ==============================================================================

def crs84_to_gee_region(tile: Union[CRS84Tile, WebMapTile]) -> ee.Geometry.Rectangle:
    """
    Converts a deserialized CRS84Tile or WebMapTile object into a Google Earth Engine
    computational region (ee.Geometry.Rectangle).
    """
    return ee.Geometry.Rectangle(
        coords=[tile.min_lon, tile.min_lat, tile.max_lon, tile.max_lat],
        proj="EPSG:4326",
        geodesic=False,
    )


# ==============================================================================
# 3. SENTINEL-2 CLOUD MASKING & NDVI COMPOSITING
# ==============================================================================

def mask_scl_clouds(img: ee.Image) -> ee.Image:
    """
    Cloud-masking filter using Sentinel-2 Scene Classification Layer (SCL).
    Keeps valid pixels: 4=Vegetation, 5=Bare Soil, 6=Water, 7=Unclassified.
    """
    scl = img.select("SCL")
    valid_pixels = (
        scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6)).Or(scl.eq(7))
    )
    return img.updateMask(valid_pixels)


def get_s2_ndvi_image(
    region: ee.Geometry.Rectangle,
    start_date: str,
    end_date: str,
    cloudy_pixel_percentage: int = 20,
    dataset_id: str = "COPERNICUS/S2_SR_HARMONIZED",
) -> ee.Image:
    """
    Queries Sentinel-2 collection, applies SCL cloud mask, computes median composite,
    calculates Float32 NDVI = (B8 - B4) / (B8 + B4), and clips to region.
    """
    s2_collection = (
        ee.ImageCollection(dataset_id)
        .filterBounds(region)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", cloudy_pixel_percentage))
    )

    composite = s2_collection.map(mask_scl_clouds).median()

    nir = composite.select("B8")
    red = composite.select("B4")
    ndvi = nir.subtract(red).divide(nir.add(red)).rename("NDVI").toFloat()

    return ndvi.clip(region)


# ==============================================================================
# 4. DOWNLOAD URL GENERATION & FILE DOWNLOADING
# ==============================================================================

def get_image_download_url(
    img: ee.Image,
    region: ee.Geometry.Rectangle,
    name: str = "NDVI_Tile",
    scale: float = 10.0,
    crs: str = "EPSG:4326",
    file_format: str = "GEO_TIFF",
) -> str:
    """
    Generates a synchronous GEE REST download URL for an ee.Image clipped to region.
    """
    download_params = {
        "name": name,
        "region": region,
        "scale": scale,
        "format": file_format,
    }
    if crs:
        download_params["crs"] = crs

    return img.getDownloadURL(download_params)


def download_raster_geotiff(
    download_url: str,
    output_filepath: Union[str, Path],
    max_retries: int = 3,
) -> Path:
    """
    Streams binary raster bytes from GEE download URL and writes locally as a GeoTIFF.
    Handles ZIP archive extraction transparently if GEE packages the file inside a ZIP.
    Includes retry logic for transient GEE HTTP errors (e.g. 502, 503).
    """
    import time
    target_path = Path(output_filepath)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    response = None
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(download_url, stream=True, timeout=60)
            if response.status_code == 200:
                break
            logger.warning(f"Attempt {attempt}/{max_retries} failed with HTTP Status: {response.status_code}")
        except Exception as req_err:
            logger.warning(f"Attempt {attempt}/{max_retries} failed with exception: {req_err}")
        if attempt < max_retries:
            time.sleep(2 * attempt)

    if response is None or response.status_code != 200:
        status = response.status_code if response else "No response"
        text = response.text if response else ""
        raise RuntimeError(
            f"Failed to fetch raster from GEE URL. HTTP Status: {status}, Response: {text}"
        )

    content = response.content

    # If GEE packages payload inside a ZIP archive (PK\x03\x04), extract the .tif
    if content[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            tif_names = [n for n in zf.namelist() if n.lower().endswith((".tif", ".tiff"))]
            if tif_names:
                content = zf.read(tif_names[0])

    target_path.write_bytes(content)
    logger.info(f"Saved raster GeoTIFF → {target_path} ({len(content):,} bytes)")
    return target_path



def download_ndvi_tile(
    tile: Union[CRS84Tile, WebMapTile],
    output_dir: Union[str, Path],
    start_date: str,
    end_date: str,
    scale: float = 10.0,
    cloudy_pixel_percentage: int = 20,
    crs: str = "EPSG:4326",
) -> Path:
    """
    High-level convenience method: fetches and saves a single NDVI GeoTIFF tile locally.
    """
    if isinstance(tile, CRS84Tile):
        tile_name = f"ndvi_z{tile.zoom}_lat{tile.lat_index}_lon{tile.lon_index}"
    else:
        tile_name = f"ndvi_z{tile.zoom}_x{tile.x_index}_y{tile.y_index}"

    output_path = Path(output_dir) / f"{tile_name}.tif"

    region = crs84_to_gee_region(tile)
    ndvi_img = get_s2_ndvi_image(region, start_date, end_date, cloudy_pixel_percentage=cloudy_pixel_percentage)
    url = get_image_download_url(ndvi_img, region, name=tile_name, scale=scale, crs=crs)
    return download_raster_geotiff(url, output_path)


# ==============================================================================
# 5. DATASET METADATA & RESOLUTION QUERIES
# ==============================================================================

KNOWN_GEE_DATASET_RESOLUTIONS = {
    "COPERNICUS/S2_SR_HARMONIZED": {
        "B2": 10.0, "B3": 10.0, "B4": 10.0, "B8": 10.0,
        "B5": 20.0, "B6": 20.0, "B7": 20.0, "B8A": 20.0, "B11": 20.0, "B12": 20.0,
        "B1": 60.0, "B9": 60.0,
        "default": 10.0,
    },
    "COPERNICUS/S2_HARMONIZED": {
        "B2": 10.0, "B3": 10.0, "B4": 10.0, "B8": 10.0, "default": 10.0
    },
    "COPERNICUS/S2_SR": {
        "B2": 10.0, "B3": 10.0, "B4": 10.0, "B8": 10.0, "default": 10.0
    },
    "LANDSAT/LC08/C02/T1_L2": {
        "SR_B2": 30.0, "SR_B3": 30.0, "SR_B4": 30.0, "SR_B5": 30.0, "default": 30.0
    },
    "LANDSAT/LC09/C02/T1_L2": {
        "SR_B2": 30.0, "SR_B3": 30.0, "SR_B4": 30.0, "SR_B5": 30.0, "default": 30.0
    },
    "MODIS/006/MOD09GA": {
        "sur_refl_b01": 250.0, "sur_refl_b02": 250.0, "default": 250.0
    },
}


def get_gee_dataset_resolution(
    dataset_id: str = "COPERNICUS/S2_SR_HARMONIZED",
    band: str = "B4",
) -> float:
    """
    Retrieves the native spatial resolution (meters per pixel) for a given GEE dataset and band.
    Attempts GEE API nominalScale() query, with fallback to known remote sensing catalog.
    """
    try:
        if not (getattr(ee.data, "_credentials", None) or getattr(ee.data, "_auth_app", None)):
            init_gee()

        image = ee.Image(dataset_id)
        scale = image.select(band).projection().nominalScale().getInfo()
        if scale and isinstance(scale, (int, float)) and scale > 0:
            logger.info(f"Retrieved native resolution from GEE for {dataset_id} band {band}: {scale}m")
            return float(scale)
    except Exception as gee_err:
        logger.debug(f"Could not query GEE API for nominalScale ({gee_err}); falling back to catalog.")

    ds_upper = dataset_id.strip()
    for known_id, band_map in KNOWN_GEE_DATASET_RESOLUTIONS.items():
        if known_id in ds_upper or ds_upper in known_id:
            res = band_map.get(band, band_map.get("default", 10.0))
            logger.info(f"Resolved native resolution from catalog for {dataset_id} ({band}): {res}m")
            return float(res)

    default_res = 10.0
    logger.warning(
        f"Unknown GEE dataset '{dataset_id}'. Defaulting native computation resolution to {default_res}m."
    )
    return default_res

