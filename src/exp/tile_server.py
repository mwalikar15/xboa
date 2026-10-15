"""
TiTiler Production Dynamic COG Tile Server
===========================================
Production-ready ASGI tile server powered by TiTiler (titiler.core + rio-tiler),
FastAPI, and Uvicorn.

Key Capabilities:
-----------------
1. TiTiler Core Integration:
   - Mounts TiTiler's official TilerFactory under /cog (info, stats, tiles, tilejson).
   - Serves any COG via URL parameter (local path or s3:// URI).

2. Seamless Multi-Raster Mosaic Serving:
   - Serves the entire raster collection under /tiles/{z}/{x}/{y}.png.
   - Powered by rio-tiler's multi-source mosaic_reader with dynamic overview selection.
   - Currently serves from local directory; seamlessly switches to Amazon S3 (s3://...)
     by changing the storage path.

3. Performance & Standards:
   - Sub-millisecond palette colormapping with rio-tiler.
   - Async non-blocking execution via asyncio.to_thread.
   - In-memory thread-safe LRU caching with MD5 ETags (HTTP 304 Not Modified).
   - Full CORS and standard Cache-Control headers.

Usage:
------
    # Serve COGs from local directory (default: output/rasters/EPSG-3857)
    python src/exp/tile_server.py

    # Serve on all interfaces
    python src/exp/tile_server.py --host 0.0.0.0 --port 8000

    # Seamlessly point to Amazon S3 (when ready)
    python src/exp/tile_server.py --storage-path s3://my-gee-bucket/rasters/EPSG-3857
"""

import os
# Ensure matplotlib does not write to non-writable directories
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import logging
import argparse
from pathlib import Path
from typing import Optional, Union

import uvicorn
from fastapi import FastAPI, Request, Response, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from softmaxx.config import AppConfig, get_logger_config
from softmaxx.geo.titiler_engine import (
    TiTilerEngine,
    DEFAULT_CMAP,
    DEFAULT_VMIN,
    DEFAULT_VMAX,
    SUPPORTED_CMAPS,
)

logger = logging.getLogger("main." + __name__)


def create_titiler_app(
    storage_path: Union[str, Path],
    dataset_id: str = "COPERNICUS/S2_SR_HARMONIZED",
    cache_size: int = 512,
    default_cmap: str = DEFAULT_CMAP,
    viewer_html_path: Optional[Path] = None,
) -> FastAPI:
    """
    Constructs the production FastAPI application with TiTiler routers.
    """
    engine = TiTilerEngine(
        storage_path=storage_path,
        dataset_id=dataset_id,
        cache_size=cache_size,
        default_cmap=default_cmap,
    )
    meta = engine.metadata

    app = FastAPI(
        title="TiTiler Dynamic COG Tile Server",
        description="Production dynamic tile server using titiler.core and rio-tiler",
        version="2.4.0",
    )

    # Enable CORS for Leaflet, MapLibre, OpenLayers web clients
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    )

    # 1. Mount official TiTiler COG router under /cog
    app.include_router(
        engine.tiler_factory.router,
        prefix="/cog",
        tags=["Cloud Optimized GeoTIFF (TiTiler)"],
    )

    # Store engine on app.state
    app.state.engine = engine

    # Resolve HTML viewer
    if viewer_html_path is None or not viewer_html_path.exists():
        repo_root = Path(__file__).resolve().parents[2]
        viewer_html_path = repo_root / "src" / "exp" / "ndvi_viewer.html"

    cached_html = viewer_html_path.read_text(encoding="utf-8") if viewer_html_path.exists() else None

    # --------------------------------------------------------------------------
    # Viewer Endpoints
    # --------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    @app.get("/viewer", response_class=HTMLResponse)
    @app.get("/ndvi_viewer.html", response_class=HTMLResponse)
    async def serve_viewer():
        if viewer_html_path and viewer_html_path.exists():
            html_content = viewer_html_path.read_text(encoding="utf-8")
        elif cached_html is not None:
            html_content = cached_html
        else:
            raise HTTPException(status_code=404, detail="HTML viewer not found on disk.")
        return HTMLResponse(
            content=html_content,
            status_code=200,
            headers={"Cache-Control": "no-cache"},
        )

    # --------------------------------------------------------------------------
    # Health & Metadata Endpoints
    # --------------------------------------------------------------------------

    @app.get("/health")
    async def health_check():
        return {
            "status": "ok",
            "titiler_version": "2.4.0",
            "storage_type": meta.storage_type,
            "storage_path": meta.storage_path,
            "dataset_id": meta.dataset_id,
            "s_comp_meters": meta.s_comp_meters,
            "native_zoom": meta.native_zoom,
            "display_match_zoom": meta.display_match_zoom,
            "rasters_indexed": meta.total_rasters,
            "cache": engine.cache.stats(),
        }

    @app.get("/info")
    async def tile_info(request: Request):
        base_url = str(request.base_url).rstrip("/")
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
            "endpoints": {
                "viewer": f"{base_url}/",
                "health": f"{base_url}/health",
                "info": f"{base_url}/info",
                "tile_template": f"{base_url}/tiles/{{z}}/{{x}}/{{y}}.png",
                "titiler_cog": f"{base_url}/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png?url=...",
            },
        }

    # --------------------------------------------------------------------------
    # Seamless Multi-Raster Dynamic XYZ Tile Endpoint
    # --------------------------------------------------------------------------

    @app.get("/tiles/{z}/{x}/{y}.png")
    @app.get("/boa/v1/tiles/{z}/{x}/{y}.png")
    async def render_tile(
        z: int,
        x: int,
        y: int,
        request: Request,
        cmap: str = Query(DEFAULT_CMAP, description="rio-tiler colormap name"),
        vmin: float = Query(DEFAULT_VMIN, description="Lower NDVI bound"),
        vmax: float = Query(DEFAULT_VMAX, description="Upper NDVI bound"),
    ):
        # Validate coordinates
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
            result = await engine.render_tile_async(
                x=x, y=y, z=z, cmap_name=cmap_clean, vmin=vmin, vmax=vmax
            )
        except Exception as err:
            logger.exception(f"Unhandled render error at {z}/{x}/{y}: {err}")
            raise HTTPException(status_code=500, detail="Internal tile rendering error")

        if result is None:
            # 204 No Content for empty / non-intersecting tiles
            return Response(
                status_code=204,
                headers={
                    "Cache-Control": "public, max-age=300",
                    "Access-Control-Allow-Origin": "*",
                },
            )

        png_bytes, etag = result

        # Check HTTP ETag for 304 Not Modified
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

    return app


def run_tile_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    storage_path: Optional[Union[str, Path]] = None,
    dataset_id: str = "COPERNICUS/S2_SR_HARMONIZED",
    cache_size: int = 512,
    default_cmap: str = DEFAULT_CMAP,
) -> None:
    """Bootstrap and run the TiTiler Uvicorn ASGI server."""
    AppConfig.load()
    try:
        lc = get_logger_config("local")
        AppConfig.init_logging(log_file=lc.log_file, log_level=lc.log_level)
    except Exception:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        )

    repo_root = Path(__file__).resolve().parents[2]

    # Discover default local raster directory if not provided
    if storage_path is None:
        candidates = [
            repo_root / "output" / "rasters" / "EPSG-3857",
        ]
        for c in candidates:
            if c.is_dir() and (list(c.glob("*.tif")) or list(c.glob("*.tiff"))):
                storage_path = c
                break

    if not storage_path:
        raise FileNotFoundError(
            "No GeoTIFF raster directory found. "
            "Run ndvi_downloader.py first, or pass --storage-path explicitly."
        )

    app = create_titiler_app(
        storage_path=storage_path,
        dataset_id=dataset_id,
        cache_size=cache_size,
        default_cmap=default_cmap,
    )
    meta = app.state.engine.metadata

    base_url = f"http://{host}:{port}"
    logger.info("=" * 75)
    logger.info("  🚀 TITILER DYNAMIC COG TILE SERVER (titiler.core + rio-tiler)")
    logger.info(f"  Base URL              : {base_url}")
    logger.info(f"  Web Map Viewer        : {base_url}/")
    logger.info(f"  Health Check          : {base_url}/health")
    logger.info(f"  Tile Metadata (/info) : {base_url}/info")
    logger.info(f"  Seamless Tile Layer   : {base_url}/tiles/{{z}}/{{x}}/{{y}}.png")
    logger.info(f"  TiTiler COG Endpoint  : {base_url}/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png?url=...")
    logger.info("-" * 75)
    logger.info(f"  Storage Backend       : {meta.storage_type.upper()} ({meta.storage_path})")
    logger.info(f"  GEE Dataset ID        : {meta.dataset_id}")
    logger.info(f"  Native Res (s_comp)   : {meta.s_comp_meters} m/pixel (Dynamic from GEE)")
    logger.info(f"  Container Native Zoom : Z={meta.native_zoom} (from calculate_web_map_tile_zoom)")
    logger.info(f"  Display Zoom (1:1)    : Z={meta.display_match_zoom} (from calculate_display_match_zoom)")
    logger.info(f"  Supported Zoom Range  : Z=[{meta.min_zoom} .. {meta.max_zoom}]")
    logger.info(f"  Indexed Rasters       : {meta.total_rasters} GeoTIFF(s)")
    logger.info(f"  LRU Cache Size        : {cache_size} tiles")
    logger.info("=" * 75)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TiTiler Dynamic COG Tile Server (Local Directory or S3)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Use 0.0.0.0 to expose on all interfaces (e.g. Docker).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="TCP port to listen on.",
    )
    parser.add_argument(
        "--storage-path",
        "--raster-dir",
        dest="storage_path",
        default=None,
        help="Directory containing EPSG:3857 GeoTIFF rasters, or an S3 URI (s3://bucket/prefix).",
    )
    parser.add_argument(
        "--dataset-id",
        type=str,
        default="COPERNICUS/S2_SR_HARMONIZED",
        help="GEE Dataset ID used to dynamically resolve native resolution.",
    )
    parser.add_argument(
        "--cache-size",
        type=int,
        default=512,
        help="Maximum number of rendered PNG tiles held in LRU memory cache.",
    )
    parser.add_argument(
        "--cmap",
        default=DEFAULT_CMAP,
        choices=SUPPORTED_CMAPS,
        help="Default NDVI colormap name.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_tile_server(
        host=args.host,
        port=args.port,
        storage_path=args.storage_path,
        dataset_id=args.dataset_id,
        cache_size=args.cache_size,
        default_cmap=args.cmap,
    )
