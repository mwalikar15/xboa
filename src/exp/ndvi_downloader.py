"""
Simple Standalone Sentinel-2 NDVI Tile Downloader
==================================================
Demonstrates downloading Sentinel-2 NDVI GeoTIFF tiles in both EPSG:4326 and EPSG:3857
projections by consuming the Core SDK library (`softmaxx.config`, `softmaxx.geo.gee`,
and `softmaxx.geo.tiles`).
"""

import logging
from pathlib import Path

from softmaxx.config import AppConfig, get_logger_config
import softmaxx.geo.tiles as geo_tiles
import softmaxx.geo.gee as gee_provider

logger = logging.getLogger("main." + __name__)


def download_epsg4326_tiles(polygon_file: Path, output_dir: Path, date_start: str, date_end: str, scale_meters: float):
    """
    Downloads Sentinel-2 NDVI GeoTIFF tiles in EPSG:4326 projection.
    """
    logger.info("=== Starting EPSG:4326 Raster Downloads ===")
    output_dir.mkdir(parents=True, exist_ok=True)

    zoom_level = geo_tiles.calculate_crs84_tile_zoom(
        s_comp=int(scale_meters),
        latitude=0.0,
        pixels_limit=10**6,
    )
    logger.info(f"Optimal CRS84 tile zoom level: {zoom_level}")

    tiles = geo_tiles.get_polygon_intersection_tiles(
        polygon_file=str(polygon_file),
        zoom_level=zoom_level,
        tile_matrix=geo_tiles.TileMatrix.CRS84,
    )
    logger.info(f"Found {len(tiles)} CRS84 tile(s) intersecting AOI polygon.")

    for index, tile in enumerate(tiles, start=1):
        tile_name = f"ndvi_z{tile.zoom}_lat{tile.lat_index}_lon{tile.lon_index}"
        logger.info(f"[{index}/{len(tiles)}] Processing EPSG:4326 tile: {tile_name} | Lon({tile.min_lon} to {tile.max_lon}), Lat({tile.min_lat} to {tile.max_lat})")

        saved_path = gee_provider.download_ndvi_tile(
            tile=tile,
            output_dir=output_dir,
            start_date=date_start,
            end_date=date_end,
            scale=scale_meters,
            crs="EPSG:4326",
        )
        logger.info(f"Saved EPSG:4326 raster → {saved_path}")

    logger.info(f"EPSG:4326 downloads complete. Files saved in: {output_dir}")


def download_epsg3857_tiles(polygon_file: Path, output_dir: Path, date_start: str, date_end: str, scale_meters: float):
    """
    Downloads Sentinel-2 NDVI GeoTIFF tiles in EPSG:3857 (Web Mercator) projection.
    """
    logger.info("=== Starting EPSG:3857 Raster Downloads ===")
    output_dir.mkdir(parents=True, exist_ok=True)

    zoom_level = geo_tiles.calculate_web_map_tile_zoom(
        s_comp=int(scale_meters),
        latitude=0.0,
        pixels_limit=10**6,
    )
    logger.info(f"Optimal Web Map tile zoom level: {zoom_level}")

    tiles = geo_tiles.get_polygon_intersection_tiles(
        polygon_file=str(polygon_file),
        zoom_level=zoom_level,
        tile_matrix=geo_tiles.TileMatrix.WEB_MAP,
    )
    logger.info(f"Found {len(tiles)} Web Map tile(s) intersecting AOI polygon.")

    for index, tile in enumerate(tiles, start=1):
        tile_name = f"ndvi_z{tile.zoom}_x{tile.x_index}_y{tile.y_index}"
        logger.info(f"[{index}/{len(tiles)}] Processing EPSG:3857 tile: {tile_name} | Lon({tile.min_lon} to {tile.max_lon}), Lat({tile.min_lat} to {tile.max_lat})")

        saved_path = gee_provider.download_ndvi_tile(
            tile=tile,
            output_dir=output_dir,
            start_date=date_start,
            end_date=date_end,
            scale=scale_meters,
            crs="EPSG:3857",
        )
        logger.info(f"Saved EPSG:3857 raster → {saved_path}")

    logger.info(f"EPSG:3857 downloads complete. Files saved in: {output_dir}")


def main():
    # 1. Load application configuration & initialize logging via Core SDK
    AppConfig.load()
    try:
        log_config = get_logger_config("local")
        AppConfig.init_logging(log_file=log_config.log_file, log_level=log_config.log_level)
    except Exception:
        # Fallback logging if "local" logger section is omitted in config
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    logger.info("Initializing Google Earth Engine via Core SDK...")
    gee_provider.init_gee()

    # 2. Setup Paths & Parameters
    repo_root = Path(__file__).resolve().parents[2]
    polygon_file = repo_root / "scripts" / "geo" / "polygon.json"

    # Target output directories per projection
    output_dir_4326 = repo_root / "output" / "rasters" / "EPSG-4326"
    output_dir_3857 = repo_root / "output" / "rasters" / "EPSG-3857"

    date_start = "2024-01-01"
    date_end = "2024-03-31"
    scale_meters = 10.0

    # 3. Download rasters in EPSG:4326
    download_epsg4326_tiles(
        polygon_file=polygon_file,
        output_dir=output_dir_4326,
        date_start=date_start,
        date_end=date_end,
        scale_meters=scale_meters,
    )

    # 4. Download rasters in EPSG:3857
    download_epsg3857_tiles(
        polygon_file=polygon_file,
        output_dir=output_dir_3857,
        date_start=date_start,
        date_end=date_end,
        scale_meters=scale_meters,
    )

    logger.info(f"All tile downloads complete.\nEPSG:4326 -> {output_dir_4326}\nEPSG:3857 -> {output_dir_3857}")


if __name__ == "__main__":
    main()

