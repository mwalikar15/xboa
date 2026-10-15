"""
FastAPI TiTiler Router
======================
Production-grade tile endpoints exposing dynamic NDVI GeoTIFF rendering
under the standard XBOA API prefix (/boa/v1/tiles and /boa/v1/cog).
"""

import os
from pathlib import Path
from typing import Optional, Union

from fastapi import APIRouter, Request, Response, HTTPException, Query
from titiler.core.factory import TilerFactory
from softmaxx.geo.titiler_engine import TiTilerEngine, DEFAULT_CMAP, DEFAULT_VMIN, DEFAULT_VMAX, SUPPORTED_CMAPS

# 1. Seamless Mosaic Tile Router
tiles_router = APIRouter(
    prefix="/tiles",
    tags=["TILES (TiTiler)"],
    responses={
        400: {"description": "Invalid tile coordinates"},
        404: {"description": "Tile endpoint not found"},
    },
)

# 2. Official TiTiler COG Router (serves any COG by URL)
cog_factory = TilerFactory()
cog_router = cog_factory.router

# Global singleton engine instance
_engine: Optional[TiTilerEngine] = None


def get_titiler_engine(
    storage_path: Optional[Union[str, Path]] = None,
    dataset_id: str = "COPERNICUS/S2_SR_HARMONIZED",
    cache_size: int = 512,
) -> TiTilerEngine:
    global _engine
    if _engine is None:
        if storage_path is None:
            # Check environment variable first (e.g. s3://my-bucket/rasters)
            env_storage = os.getenv("COG_STORAGE_PATH")
            if env_storage:
                storage_path = env_storage
            else:
                candidates = [
                    Path.cwd() / "output" / "rasters" / "EPSG-3857",
                    Path(__file__).resolve().parents[4] / "output" / "rasters" / "EPSG-3857",
                    Path(__file__).resolve().parents[3] / "output" / "rasters" / "EPSG-3857",
                ]
                for c in candidates:
                    if c.is_dir() and (list(c.glob("*.tif")) or list(c.glob("*.tiff"))):
                        storage_path = c
                        break

        if not storage_path:
            raise RuntimeError(
                "No raster directory available. Run ndvi_downloader.py first, "
                "or set COG_STORAGE_PATH=s3://... environment variable."
            )

        _engine = TiTilerEngine(
            storage_path=storage_path,
            dataset_id=dataset_id,
            cache_size=cache_size,
        )
    return _engine


def set_titiler_engine(engine: TiTilerEngine) -> None:
    global _engine
    _engine = engine


@tiles_router.get("/health")
def tile_health():
    """Health check endpoint providing engine status and cache metrics."""
    try:
        engine = get_titiler_engine()
        meta = engine.metadata
        return {
            "status": "ok",
            "engine": "TiTiler (titiler.core + rio-tiler)",
            "storage_type": meta.storage_type,
            "storage_path": meta.storage_path,
            "dataset_id": meta.dataset_id,
            "s_comp_meters": meta.s_comp_meters,
            "native_zoom": meta.native_zoom,
            "display_match_zoom": meta.display_match_zoom,
            "rasters_indexed": meta.total_rasters,
            "cache": engine.cache.stats(),
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "error": str(exc),
        }


@tiles_router.get("/info")
def tile_info(request: Request):
    """Returns comprehensive tile-set metadata and client map configuration."""
    engine = get_titiler_engine()
    meta = engine.metadata
    base_url = str(request.base_url).rstrip("/")
    tile_template = f"{base_url}/boa/v1/tiles/{{z}}/{{x}}/{{y}}.png"

    return {
        "engine": "TiTiler (titiler.core + rio-tiler)",
        "storage_type": meta.storage_type,
        "storage_path": meta.storage_path,
        "dataset_id": meta.dataset_id,
        "s_comp_meters": meta.s_comp_meters,
        "native_zoom": meta.native_zoom,
        "display_match_zoom": meta.display_match_zoom,
        "min_zoom": meta.min_zoom,
        "max_zoom": meta.max_zoom,
        "tile_size_px": meta.tile_size_px,
        "total_rasters": meta.total_rasters,
        "bounds_3857": meta.bounds_3857,
        "bounds_wgs84": meta.bounds_wgs84,
        "ndvi_range": meta.ndvi_range,
        "supported_cmaps": meta.supported_cmaps,
        "cache": engine.cache.stats(),
        "tile_url_template": tile_template,
    }


@tiles_router.get("/{z}/{x}/{y}.png")
async def get_tile_png(
    z: int,
    x: int,
    y: int,
    request: Request,
    cmap: str = Query(DEFAULT_CMAP, description="rio-tiler colormap name"),
    vmin: float = Query(DEFAULT_VMIN, description="Lower NDVI bound"),
    vmax: float = Query(DEFAULT_VMAX, description="Upper NDVI bound"),
):
    """
    Renders an EPSG:3857 Web Mercator PNG tile on-the-fly using rio-tiler.
    Supports HTTP conditional requests (ETag / If-None-Match -> 304 Not Modified).
    """
    engine = get_titiler_engine()

    max_idx = 1 << z
    if z < 0 or z > 24 or x < 0 or x >= max_idx or y < 0 or y >= max_idx:
        raise HTTPException(
            status_code=400,
            detail=f"Tile coordinate ({z}/{x}/{y}) out of range [0..{max_idx - 1}].",
        )

    cmap_clean = cmap.lower()
    if cmap_clean not in SUPPORTED_CMAPS:
        cmap_clean = DEFAULT_CMAP

    try:
        result = await engine.render_tile_async(x=x, y=y, z=z, cmap_name=cmap_clean, vmin=vmin, vmax=vmax)
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"Tile rendering error: {err}")

    if result is None:
        return Response(
            status_code=204,
            headers={
                "Cache-Control": "public, max-age=300",
                "Access-Control-Allow-Origin": "*",
            },
        )

    png_bytes, etag = result

    # Check ETag for 304 Not Modified
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match.strip('"') == etag:
        return Response(status_code=304)

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "ETag": f'"{etag}"',
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )

