"""
Dynamic On-The-Fly Web Map Tile Server (Approach B)
====================================================
Demonstrates streaming Web Mercator (EPSG:3857) XYZ PNG tiles on-the-fly from local GeoTIFF rasters
using the Core SDK (`softmaxx.config`, `softmaxx.geo.tiles`) and Python's built-in `http.server`.

- 0 disk overhead for pre-rendered PNG tiles
- On-the-fly windowed raster extraction & colormap rendering
- Full CORS support for Leaflet / Mapbox / OpenLayers web clients
"""

import io
import math
import logging
from pathlib import Path
from typing import Optional, Tuple, List
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import urllib.parse

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from rasterio.transform import from_bounds
from PIL import Image
import matplotlib.pyplot as plt
import matplotlib.cm as cm

from softmaxx.config import AppConfig, get_logger_config
import softmaxx.geo.tiles as geo_tiles

logger = logging.getLogger("main." + __name__)

MAX_EXTENT = 20037508.342789244
INITIAL_RESOLUTION = MAX_EXTENT * 2.0


def get_tile_bounds_epsg3857(x: int, y: int, z: int) -> Tuple[float, float, float, float]:
    """Returns (min_x, min_y, max_x, max_y) in EPSG:3857 meters for tile (x, y, z)."""
    tiles_per_axis = 2 ** z
    tile_size_meters = INITIAL_RESOLUTION / tiles_per_axis

    min_x = -MAX_EXTENT + (x * tile_size_meters)
    max_x = min_x + tile_size_meters

    max_y = MAX_EXTENT - (y * tile_size_meters)
    min_y = max_y - tile_size_meters

    return min_x, min_y, max_x, max_y


class DynamicTileRenderer:
    """
    On-the-fly raster renderer that extracts requested Web Mercator (EPSG:3857) tile windows
    from local GeoTIFF files and converts them to RGBA PNG image bytes.
    """
    def __init__(self, raster_files: List[Path], cmap_name: str = "RdYlGn", vmin: float = -0.2, vmax: float = 0.8):
        self.raster_files = raster_files
        self.cmap_name = cmap_name
        self.vmin = vmin
        self.vmax = vmax

        try:
            self.colormap = plt.colormaps[cmap_name]
        except (AttributeError, KeyError):
            self.colormap = cm.get_cmap(cmap_name)

    def render_tile(self, x: int, y: int, z: int, tile_size: int = 256) -> Optional[bytes]:
        t_min_x, t_min_y, t_max_x, t_max_y = get_tile_bounds_epsg3857(x, y, z)
        tile_transform = from_bounds(t_min_x, t_min_y, t_max_x, t_max_y, tile_size, tile_size)

        combined_rgba = np.zeros((tile_size, tile_size, 4), dtype=np.uint8)
        has_pixels = False

        for tif_path in self.raster_files:
            try:
                with rasterio.open(tif_path) as src:
                    src_crs = src.crs or "EPSG:3857"
                    dst_data = np.full((tile_size, tile_size), np.nan, dtype=np.float32)

                    reproject(
                        source=rasterio.band(src, 1),
                        destination=dst_data,
                        src_transform=src.transform,
                        src_crs=src_crs,
                        dst_transform=tile_transform,
                        dst_crs="EPSG:3857",
                        resampling=Resampling.bilinear,
                        src_nodata=src.nodata,
                        dst_nodata=np.nan
                    )

                    if np.all(np.isnan(dst_data)):
                        continue

                    valid_mask = ~np.isnan(dst_data)
                    if src.nodata is not None:
                        valid_mask = valid_mask & (dst_data != src.nodata)

                    if not np.any(valid_mask):
                        continue

                    # Apply NDVI colormap
                    norm_data = np.clip((dst_data - self.vmin) / (self.vmax - self.vmin), 0.0, 1.0)
                    rgba_float = self.colormap(norm_data)
                    rgba_uint8 = (rgba_float * 255).astype(np.uint8)
                    rgba_uint8[~valid_mask, 3] = 0

                    # Blend non-transparent pixels onto accumulator
                    new_pixels = rgba_uint8[:, :, 3] > 0
                    combined_rgba[new_pixels] = rgba_uint8[new_pixels]
                    has_pixels = True

            except Exception as err:
                logger.error(f"Error reading raster {tif_path}: {err}")

        if not has_pixels:
            return None

        # Encode to PNG stream
        img = Image.fromarray(combined_rgba, mode="RGBA")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()


class TileRequestHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler supporting dynamic tile requests (/tiles/{z}/{x}/{y}.png)
    and serving the Leaflet Web Map Viewer.
    """
    renderer: Optional[DynamicTileRenderer] = None
    html_viewer_path: Optional[Path] = None

    def log_message(self, format, *args):
        logger.info("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format % args))

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Serve Web Map Viewer HTML at root or /viewer
        if path in ("/", "/viewer", "/ndvi_viewer.html"):
            if self.html_viewer_path and self.html_viewer_path.exists():
                content = self.html_viewer_path.read_bytes()
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            else:
                self.send_error(404, "HTML viewer template missing")
                return

        # Handle Tile Requests: /tiles/{z}/{x}/{y}.png
        if path.startswith("/tiles/") and (path.endswith(".png") or path.endswith(".webp")):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "tiles":
                try:
                    z = int(parts[1])
                    x = int(parts[2])
                    y_str = parts[3].split(".")[0]
                    y = int(y_str)

                    png_bytes = self.renderer.render_tile(x, y, z) if self.renderer else None

                    if png_bytes:
                        self.send_response(200)
                        self._send_cors_headers()
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Cache-Control", "public, max-age=3600")
                        self.send_header("Content-Length", str(len(png_bytes)))
                        self.end_headers()
                        self.wfile.write(png_bytes)
                        return
                    else:
                        # Return transparent 204 No Content for empty tile
                        self.send_response(204)
                        self._send_cors_headers()
                        self.end_headers()
                        return
                except ValueError:
                    pass

        self.send_error(404, "Resource Not Found")


def run_tile_server(host: str = "127.0.0.1", port: int = 8000, raster_dir: Optional[Path] = None):
    # 1. Load application config
    AppConfig.load()
    try:
        log_config = get_logger_config("local")
        AppConfig.init_logging(log_file=log_config.log_file, log_level=log_config.log_level)
    except Exception:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    repo_root = Path(__file__).resolve().parents[2]

    # Resolve target raster directory
    if raster_dir is None:
        possible_dirs = [
            repo_root / "output" / "rasters" / "EPSG-3857"
        ]
        for p in possible_dirs:
            if p.exists() and list(p.glob("*.tif")):
                raster_dir = p
                break

    if not raster_dir or not raster_dir.exists():
        raise FileNotFoundError("No GeoTIFF raster output directory found. Run ndvi_downloader.py first.")

    tif_files = sorted(list(raster_dir.glob("*.tif")) + list(raster_dir.glob("*.tiff")))
    logger.info(f"Loaded {len(tif_files)} GeoTIFF file(s) from {raster_dir}")
    for tif in tif_files:
        logger.info(f"  - {tif.name}")

    # Instantiate tile renderer and handler
    TileRequestHandler.renderer = DynamicTileRenderer(tif_files)
    TileRequestHandler.html_viewer_path = repo_root / "src" / "exp" / "ndvi_viewer.html"

    class ReusableThreadingServer(ThreadingHTTPServer):
        allow_reuse_address = True

    httpd = None
    target_port = port
    for p in range(port, port + 10):
        try:
            httpd = ReusableThreadingServer((host, p), TileRequestHandler)
            target_port = p
            break
        except OSError:
            continue

    if httpd is None:
        raise OSError(f"Could not bind tile server to any port in range {port}-{port+9}")

    logger.info(f"=== Dynamic Tile Server running at http://{host}:{target_port} ===")
    logger.info(f"Tile Template endpoint: http://{host}:{target_port}/tiles/{{z}}/{{x}}/{{y}}.png")
    logger.info(f"Web Map Viewer page   : http://{host}:{target_port}/")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down tile server.")
        httpd.server_close()


if __name__ == "__main__":
    run_tile_server()

