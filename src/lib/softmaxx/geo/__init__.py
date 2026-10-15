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
    tile_bounds_3857,
    tile_bounds_wgs84,
    bounds_to_tile,
    calculate_display_match_zoom,
    web_map_pixel_resolution,
    get_parent_tile,
    get_child_tile_range,
    tiles_intersect,
)

from softmaxx.geo import gee
from softmaxx.geo.titiler_engine import (
    TiTilerEngine,
    TiTilerMetadata,
    ThreadSafeTileCache,
)

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
    "tile_bounds_3857",
    "tile_bounds_wgs84",
    "bounds_to_tile",
    "calculate_display_match_zoom",
    "web_map_pixel_resolution",
    "get_parent_tile",
    "get_child_tile_range",
    "tiles_intersect",
    "TiTilerEngine",
    "TiTilerMetadata",
    "ThreadSafeTileCache",
    "gee",
]


