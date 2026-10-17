"""
TiTiler Dynamic COG Tile Engine
================================
Industry-standard dynamic tile engine built on top of `titiler.core` and `rio-tiler`.
Provides seamless multi-scale tile rendering for Cloud Optimized GeoTIFFs (COGs).

Storage Agnostic:
- Currently serves COGs from a local directory (e.g. output/rasters/EPSG-3857).
- Supports seamless migration to Amazon S3 (s3://bucket/prefix/...) using GDAL's /vsis3/ driver.
"""

import os
# Ensure matplotlib does not write to non-writable directories
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import io
import hashlib
import logging
import asyncio
import warnings
import threading
from pathlib import Path
from dataclasses import dataclass
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple, Any, Union

import numpy as np
import rasterio
from rasterio.warp import transform_bounds

from rio_tiler.io import COGReader
from rio_tiler.mosaic import mosaic_reader
from rio_tiler.colormap import cmap as rio_cmap
from rio_tiler.errors import EmptyMosaicError, TileOutsideBounds, NoOverviewWarning
from titiler.core.factory import TilerFactory

import softmaxx.geo.tiles as geo_tiles
import softmaxx.geo.gee as gee_provider

# Suppress harmless missing overview warnings on un-pyramided small test rasters
warnings.filterwarnings("ignore", module="rio_tiler.*")
warnings.filterwarnings("ignore", message=".*The dataset has no Overviews.*")

logger = logging.getLogger("main." + __name__)

DEFAULT_CMAP: str = "rdylgn"
DEFAULT_VMIN: float = -0.2
DEFAULT_VMAX: float = 0.8
SUPPORTED_CMAPS: List[str] = [
    "rdylgn", "viridis", "rdylbu", "greens", "brbg", "piyg", "magma", "plasma", "inferno"
]


@dataclass(frozen=True)
class TiTilerMetadata:
    """Metadata describing the TiTiler engine configuration and spatial dataset."""
    storage_path: str
    storage_type: str  # "directory" or "s3"
    dataset_id: str
    s_comp_meters: float
    native_zoom: int
    display_match_zoom: int
    min_zoom: int
    max_zoom: int
    tile_size_px: int
    total_rasters: int
    bounds_3857: Tuple[float, float, float, float]
    bounds_wgs84: Tuple[float, float, float, float]
    ndvi_range: Tuple[float, float]
    supported_cmaps: List[str]
    comp_storage_path: Optional[str] = None


class ThreadSafeTileCache:
    """Thread-safe LRU memory cache for rendered PNG tiles with ETag tracking."""

    def __init__(self, maxsize: int = 512):
        self._maxsize = maxsize
        self._store: OrderedDict[Tuple, Tuple[bytes, str]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: Tuple) -> Optional[Tuple[bytes, str]]:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._hits += 1
                return self._store[key]
            self._misses += 1
            return None

    def put(self, key: Tuple, png_bytes: bytes, etag: str) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (png_bytes, etag)
            if len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = round(self._hits / total, 3) if total > 0 else 0.0
            return {
                "size": len(self._store),
                "capacity": self._maxsize,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0


class TiTilerEngine:
    """
    High-level dynamic tile engine powered by TiTiler (titiler.core + rio-tiler).
    Seamlessly handles:
    1. Direct single-COG queries (via TiTiler's TilerFactory).
    2. Multi-raster directory / S3 mosaic queries (via rio-tiler mosaic_reader).
    """

    def __init__(
        self,
        storage_path: Union[str, Path],
        comp_storage_path: Optional[Union[str, Path]] = None,
        dataset_id: str = "COPERNICUS/S2_SR_HARMONIZED",
        cache_size: int = 512,
        default_cmap: str = DEFAULT_CMAP,
    ):
        self.raw_storage_path = str(storage_path)
        self.dataset_id = dataset_id
        self.default_cmap = default_cmap.lower()
        self.cache = ThreadSafeTileCache(maxsize=cache_size)
        self.tiler_factory = TilerFactory()

        # 1. Determine storage type: Local Directory vs Amazon S3
        if self.raw_storage_path.startswith("s3://"):
            self.storage_type = "s3"
            self.storage_path = self.raw_storage_path.rstrip("/")
            self.raster_paths = self._discover_s3_rasters(self.storage_path)
        else:
            self.storage_type = "directory"
            local_dir = Path(self.raw_storage_path).resolve()
            if not local_dir.is_dir():
                raise FileNotFoundError(f"Raster directory does not exist: {local_dir}")
            self.storage_path = str(local_dir)
            tifs = sorted(list(local_dir.glob("*.tif")) + list(local_dir.glob("*.tiff")))
            if not tifs:
                raise FileNotFoundError(f"No .tif/.tiff files found in {local_dir}")
            self.raster_paths = [str(p) for p in tifs]

        # Resolve EPSG:4326 computation raster directory
        if comp_storage_path is not None:
            comp_dir = Path(comp_storage_path).resolve()
            self.comp_storage_path = comp_dir if comp_dir.is_dir() else None
        else:
            sibling_4326 = Path(self.storage_path).parent / "EPSG-4326"
            self.comp_storage_path = sibling_4326 if sibling_4326.is_dir() else None

        logger.info(
            f"TiTilerEngine initialized: storage_type={self.storage_type}, "
            f"indexed {len(self.raster_paths)} display raster(s) at {self.storage_path}, "
            f"comp_storage_path={self.comp_storage_path}"
        )

        # 2. Derive geographic bounds and zoom parameters
        self._calculate_dataset_extents()

    def _discover_s3_rasters(self, s3_prefix: str) -> List[str]:
        """Placeholder for S3 asset listing; can be configured or discovered via boto3 / STAC."""
        # When switching to S3, rasters can be passed as a list or queried via S3 prefix
        return [s3_prefix]

    def _calculate_dataset_extents(self) -> None:
        """Inspects rasters using COGReader to compute collective bounding box and zoom levels."""
        min_xs, min_ys, max_xs, max_ys = [], [], [], []
        min_zooms, max_zooms = [], []

        for p in self.raster_paths:
            try:
                with COGReader(p) as cog:
                    b = cog.bounds  # in dataset CRS (EPSG:3857)
                    min_xs.append(b[0])
                    min_ys.append(b[1])
                    max_xs.append(b[2])
                    max_ys.append(b[3])
                    min_zooms.append(cog.minzoom)
                    max_zooms.append(cog.maxzoom)
            except Exception as err:
                logger.warning(f"Could not inspect COG bounds for {p}: {err}")

        if min_xs:
            self.bounds_3857 = (min(min_xs), min(min_ys), max(max_xs), max(max_ys))
            try:
                self.bounds_wgs84 = transform_bounds("EPSG:3857", "EPSG:4326", *self.bounds_3857)
            except Exception:
                self.bounds_wgs84 = (-180.0, -85.0, 180.0, 85.0)
            center_lat = (self.bounds_wgs84[1] + self.bounds_wgs84[3]) / 2.0
        else:
            self.bounds_3857 = (0.0, 0.0, 0.0, 0.0)
            self.bounds_wgs84 = (-180.0, -85.0, 180.0, 85.0)
            center_lat = 0.0

        # Dynamically query resolution from GEE metadata
        self.s_comp = gee_provider.get_gee_dataset_resolution(dataset_id=self.dataset_id)

        # Container native zoom and 1:1 display zoom
        self.native_zoom = geo_tiles.calculate_web_map_tile_zoom(
            s_comp=int(self.s_comp),
            latitude=center_lat,
            pixels_limit=10**6,
        )
        self.display_match_zoom = geo_tiles.calculate_display_match_zoom(
            s_comp=self.s_comp,
            latitude=center_lat,
            tile_size=256,
        )

        self.min_zoom = min(min_zooms) if min_zooms else max(0, self.native_zoom - 5)
        self.max_zoom = max(max_zooms) if max_zooms else min(22, self.display_match_zoom + 6)

    @property
    def metadata(self) -> TiTilerMetadata:
        return TiTilerMetadata(
            storage_path=self.storage_path,
            storage_type=self.storage_type,
            dataset_id=self.dataset_id,
            s_comp_meters=self.s_comp,
            native_zoom=self.native_zoom,
            display_match_zoom=self.display_match_zoom,
            min_zoom=self.min_zoom,
            max_zoom=self.max_zoom,
            tile_size_px=256,
            total_rasters=len(self.raster_paths),
            bounds_3857=self.bounds_3857,
            bounds_wgs84=self.bounds_wgs84,
            ndvi_range=(DEFAULT_VMIN, DEFAULT_VMAX),
            supported_cmaps=SUPPORTED_CMAPS,
            comp_storage_path=str(self.comp_storage_path) if self.comp_storage_path else None,
        )

    def render_tile_sync(
        self,
        x: int,
        y: int,
        z: int,
        cmap_name: str = DEFAULT_CMAP,
        vmin: float = DEFAULT_VMIN,
        vmax: float = DEFAULT_VMAX,
    ) -> Optional[Tuple[bytes, str]]:
        """
        Renders a seamless XYZ tile across the collection using rio-tiler's mosaic_reader.
        Returns (png_bytes, etag) or None if no data intersects the tile.
        """
        cmap_key = cmap_name.lower()
        cache_key = (z, x, y, cmap_key, vmin, vmax)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        def _tiler(src_path: str, *args, **kwargs):
            with COGReader(src_path) as cog:
                return cog.tile(*args, **kwargs)

        try:
            # Read and composite tile from intersecting COGs using rio-tiler
            img, assets = mosaic_reader(self.raster_paths, _tiler, x, y, z)
        except (EmptyMosaicError, TileOutsideBounds):
            return None
        except Exception as err:
            logger.error(f"rio-tiler mosaic render error at {z}/{x}/{y}: {err}")
            return None

        if not assets or img is None or img.data.size == 0:
            return None

        # Rescale float NDVI values to uint8 [0..255]
        rescaled_img = img.rescale(in_range=[(vmin, vmax)])

        # Apply TiTiler/rio-tiler colormap
        cmap_dict = rio_cmap.get(cmap_key) if cmap_key in rio_cmap.list() else rio_cmap.get("rdylgn")
        png_bytes = rescaled_img.render(img_format="PNG", colormap=cmap_dict)
        etag = hashlib.md5(png_bytes).hexdigest()

        self.cache.put(cache_key, png_bytes, etag)
        return png_bytes, etag

    async def render_tile_async(
        self,
        x: int,
        y: int,
        z: int,
        cmap_name: str = DEFAULT_CMAP,
        vmin: float = DEFAULT_VMIN,
        vmax: float = DEFAULT_VMAX,
    ) -> Optional[Tuple[bytes, str]]:
        """Non-blocking async wrapper using asyncio.to_thread."""
        return await asyncio.to_thread(
            self.render_tile_sync, x, y, z, cmap_name, vmin, vmax
        )

    def sample_point(self, lat: float, lon: float) -> dict[str, Any]:
        """
        Samples the raw NDVI value at (lat, lon) directly from EPSG:4326 computation rasters.
        """
        if self.comp_storage_path is None or not self.comp_storage_path.is_dir():
            return {
                "lat": lat,
                "lon": lon,
                "ndvi": None,
                "found": False,
                "tile": None,
                "crs": "EPSG:4326",
                "message": "EPSG:4326 computation raster directory is not configured or available.",
            }
        return geo_tiles.sample_crs84_ndvi(
            raster_dir=self.comp_storage_path,
            lat=lat,
            lon=lon,
        )

    async def sample_point_async(self, lat: float, lon: float) -> dict[str, Any]:
        """Non-blocking async wrapper around sample_point."""
        return await asyncio.to_thread(self.sample_point, lat, lon)


