"""
Softmaxx Geo Package
====================
Provides spatial grid matrix indexing (tiles) and GIS data provider integrations (gee).
"""

from softmaxx.geo.tiles import (
    WebMapTile,
    CRS84Tile,
    TileMatrix,
    get_polygon_intersection_tiles,
    calculate_crs84_tile_zoom,
    calculate_web_map_tile_zoom,
    get_crs84_tile_pixels,
    plot_geotiff_ndvi,
    verify_geotiff_raster,
    compute_multiscale_raster_stats,
)

from softmaxx.geo import gee

__all__ = [
    "WebMapTile",
    "CRS84Tile",
    "TileMatrix",
    "get_polygon_intersection_tiles",
    "calculate_crs84_tile_zoom",
    "calculate_web_map_tile_zoom",
    "get_crs84_tile_pixels",
    "plot_geotiff_ndvi",
    "verify_geotiff_raster",
    "compute_multiscale_raster_stats",
    "gee",
]

