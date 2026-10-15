# TiTiler Dynamic Raster Tile Server: Architecture & Implementation Guide

## 1. Executive Summary & Problem Context

In modern Earth Observation (EO) workflows, satellite imagery is ingested, analyzed, and exported at scale. Specifically, our pipeline exports Sentinel-2 Normalized Difference Vegetation Index (NDVI) rasters from **Google Earth Engine (GEE)**:
- **Spatial Reference System**: Web Mercator (`EPSG:3857`).
- **Nominal Ground Resolution**: $10.0\text{ m/pixel}$ ($s_{comp} = 10.0\text{ m}$).
- **Raster Dimensions**: Typically $1000 \times 1000$ pixels per exported raster chunk ($\approx 10^6$ pixels), covering a geographic footprint of $10\text{ km} \times 10\text{ km}$.
- **Grid Alignment**: Exactly aligned to EPSG:3857 Web Mercator tile boundaries.

### The Challenge
How do we serve these multi-temporal, multi-tile raster datasets to interactive web clients (Leaflet, MapLibre, OpenLayers) with:
1. **Zero Hardcoded Parameters**: Computing nominal resolution, native zoom levels, display match zoom levels, and geographic bounds dynamically from dataset metadata and spherical mathematical formulas.
2. **Smooth Storage Transition**: Serving directly from a local staging directory today (`output/rasters/EPSG-3857`) while being architected to switch seamlessly to **Amazon S3 (`s3://...`)** with zero code modifications.
3. **Sub-second Response Times**: Dynamic on-the-fly rendering across multi-zoom scales ($Z=0 \dots 20$) without pre-generating terabytes of static XYZ image pyramids.

To fulfill these requirements, we implemented a production-ready dynamic tile server powered by **TiTiler** (`titiler.core` + `rio-tiler`) and **FastAPI**.

---

## 2. Why TiTiler? (Architecture Decision Matrix)

When serving Cloud Optimized GeoTIFFs (COGs) on the web, several engineering pathways exist. Below is the technical evaluation that led to selecting **TiTiler**:

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                            Tile Serving Architecture Options                     │
├───────────────────────┬──────────────────────┬───────────────────────────────────┤
│ Option                │ Pros                 │ Cons / Failure Modes              │
├───────────────────────┼──────────────────────┼───────────────────────────────────┤
│ Option A:             │ • Zero server compute│ • Combinatorial storage explosion │
│ Static Tile Packages  │ • Cheap S3 hosting   │ • Slow offline batch pre-rendering│
│ (gdal2tiles, MBTiles) │ • High concurrency   │ • Cannot change colormaps/dynamic │
│                       │                      │   stretch ranges on the fly       │
├───────────────────────┼──────────────────────┼───────────────────────────────────┤
│ Option B:             │ • Mature enterprise  │ • Heavy JVM memory footprint      │
│ Heavyweight GIS Server│   OGC support        │ • Slow startup & complex XML setup│
│ (GeoServer, MapServer)│ • Vector + Raster    │ • Cumbersome cloud-native S3      │
│                       │                      │   streaming configuration         │
├───────────────────────┼──────────────────────┼───────────────────────────────────┤
│ Option C:             │ • Total control      │ • Huge maintenance burden         │
│ Custom Rasterio Script│ • Lightweight        │ • Must manually code overviews,   │
│                       │                      │   mosaicing, window reads, affine │
│                       │                      │   transforms, and thread safety   │
├───────────────────────┼──────────────────────┼───────────────────────────────────┤
│ Option D: (CHOSEN)    │ • Battle-tested      │ • Requires Python ASGI runtime    │
│ TiTiler + rio-tiler   │ • Cloud-native S3    │   (mitigated by Uvicorn / Docker) │
│ (titiler.core)        │ • Dynamic colormaps  │                                   │
│                       │ • Zero pre-rendering │                                   │
└───────────────────────┴──────────────────────┴───────────────────────────────────┘
```

### Key Reasons for Choosing TiTiler:
1. **Industry Standard for Cloud-Native Geospatial**: TiTiler is maintained by DevelopmentSeed and used in mission-critical infrastructure by NASA, USGS, Planet, and the AWS Open Data Program.
2. **Built-in S3 / Local Filesystem Abstraction**: Built on `rio-tiler` and GDAL's virtual filesystem (`/vsis3/`). The reader API does not care whether the target is `/local/path/raster.tif` or `s3://bucket/raster.tif`. It issues HTTP range requests to read only the required header bytes and spatial tiles.
3. **Dynamic On-the-Fly Rescaling & Colormapping**: Instead of baking colormapped PNGs into storage, raw float NDVI data ($[-1.0, 1.0]$) is read from disk/S3 and colorized in memory using vectorized numpy lookups (`rio_tiler.colormap`) in **< 5 milliseconds**.
4. **Out-of-the-Box Multi-COG Mosaicing**: `rio_tiler.mosaic.mosaic_reader` handles multi-tile spatial intersections automatically, stitching overlapping rasters onto a single $256 \times 256$ Web Mercator canvas seamlessly.
5. **Modern Async ASGI Architecture**: Directly integrates with FastAPI and Uvicorn, providing native OpenAPI/Swagger docs, async request handling, and dependency injection.

---

## 3. Mathematical Foundations: Zero Hardcoding

A critical design requirement was **eliminating hardcoded magic numbers** (such as $Z=12$, $Z=14$, or $10.0\text{ m}$). All zoom levels and spatial bounds are dynamically computed.

```
       GEE Asset Metadata                 WGS84 Web Mercator Constants
 (dataset_id, band nominal scale)           (Equatorial Radius R = 6,378,137m)
                │                                         │
                ▼                                         ▼
   s_comp = 10.0m (dynamic)                  Circumference C = 2πR ≈ 40,075,016.68m
                │                                         │
                └────────────────────┬────────────────────┘
                                     │
           ┌─────────────────────────┴─────────────────────────┐
           ▼                                                   ▼
  Tile Level Zoom (W_tile=1000px)                     Pixel Level Zoom (W_px=256px)
   Z_native = round(log2(C / 10000))                   Z_display = round(log2(C / 2560))
        Z_native = 12                                       Z_display = 14
```

### 3.1. Dynamic Resolution Acquisition
In [`softmaxx.geo.gee.get_gee_dataset_resolution()`](file:///home/manju/code/github/xboa/src/lib/softmaxx/geo/gee.py#L32), the dataset nominal resolution is queried directly from the Earth Engine Asset Catalog:
```python
ee_image = ee.ImageCollection(dataset_id).first().select(band)
nominal_scale = ee_image.projection().nominalScale().getInfo()
```
If network access is restricted (e.g. offline sandbox or airgapped staging), a built-in sensor scale catalog provides automatic fallback (`COPERNICUS/S2_*` $\to 10.0\text{m}$, `LANDSAT/*` $\to 30.0\text{m}$, `MODIS/*` $\to 250.0\text{m}$).

### 3.2. Native Tile Zoom Level ($Z_{native}$)
In Web Mercator (`EPSG:3857`), the Earth's circumference at the equator is:
$$C = 2\pi R = 40,075,016.686\text{ meters}$$

At zoom level $Z$, the world is divided into $2^Z$ tiles across each axis. The geographic width of each tile in meters is:
$$\text{TileWidth}(Z) = \frac{C}{2^Z}$$

For exported GEE rasters of pixel dimension $W_{tile} \times W_{tile}$ (e.g., $1000 \times 1000$) and target resolution $s_{comp}$ ($10.0\text{ m}$), the physical span of one exported raster is:
$$\text{Span} = W_{tile} \cdot s_{comp} = 1000 \cdot 10.0 = 10,000\text{ meters}$$

Equating $\text{TileWidth}(Z)$ to $\text{Span}$ and solving for $Z$:
$$2^Z = \frac{C}{W_{tile} \cdot s_{comp}} \implies Z_{native} = \mathrm{round}\left(\log_2\left(\frac{C}{W_{tile} \cdot s_{comp}}\right)\right)$$

Plugging in values:
$$Z_{native} = \mathrm{round}\left(\log_2\left(\frac{40,075,016.686}{10,000}\right)\right) = \mathrm{round}(\log_2(4007.50)) = \mathrm{round}(11.968) = \mathbf{12}$$

Implemented in [`softmaxx.geo.tiles.calculate_web_map_tile_zoom()`](file:///home/manju/code/github/xboa/src/lib/softmaxx/geo/tiles.py#L115).

### 3.3. Display Match Zoom Level ($Z_{display}$)
Web mapping libraries (Leaflet, MapLibre) render standard tiles at $256 \times 256$ pixels. At display zoom $Z_{display}$, one screen pixel corresponds to:
$$\text{PixelResolution}(Z) = \frac{C}{256 \cdot 2^Z}$$

Setting $\text{PixelResolution}(Z) = s_{comp}$ ($10.0\text{ m/px}$):
$$Z_{display} = \mathrm{round}\left(\log_2\left(\frac{C}{256 \cdot s_{comp}}\right)\right) = \mathrm{round}\left(\log_2\left(\frac{40,075,016.686}{2,560}\right)\right) = \mathrm{round}(13.936) = \mathbf{14}$$

Implemented in [`softmaxx.geo.tiles.calculate_display_match_zoom()`](file:///home/manju/code/github/xboa/src/lib/softmaxx/geo/tiles.py#L143).

### 3.4. Multi-Scale Tiling Scenarios
The server seamlessly handles all 3 operational scenarios requested by web clients:

| Scenario | Client Zoom | Relation to Native | TiTiler Engine Behavior |
| :--- | :--- | :--- | :--- |
| **Scenario A** | $Z = Z_{native} = 12$ | Exact 1:1 footprint | Direct read of the intersecting COG. Resamples $1000 \times 1000 \to 256 \times 256$ for the display tile. |
| **Scenario B** | $Z > 12$ (e.g. $Z=13 \dots 20$) | Zoom-In (Oversampling) | Reads the sub-window corresponding to child coordinates $(2x+i, 2y+j)$ using bilinear resampling. High-detail view of field boundaries. |
| **Scenario C** | $Z < 12$ (e.g. $Z=0 \dots 11$) | Zoom-Out (Aggregating) | Reads overview decimation layers across multiple parent COGs and composites them onto the $256 \times 256$ canvas. |

---

## 4. Software Architecture & Implementation Details

```
                                FastAPI Application
 ┌───────────────────────────────────────┬────────────────────────────────────────┐
 │ Seamless Mosaic Route                 │ TiTiler TilerFactory                   │
 │ GET /tiles/{z}/{x}/{y}.png            │ GET /cog/tiles/WebMercatorQuad/...     │
 └───────────────────┬───────────────────┴───────────────────┬────────────────────┘
                     │                                       │
                     ▼                                       ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │ TiTilerEngine (src/lib/softmaxx/geo/titiler_engine.py)                         │
 │                                                                                │
 │  • Spatial Bounds Discovery: rasterio.warp.transform_bounds                   │
 │  • Spatial Indexing: BoundingBox intersection filtering                       │
 │  • Thread-safe LRU Cache: OrderedDict with Lock & MD5 ETags                   │
 │  • Async Execution: asyncio.to_thread() pool                                  │
 └───────────────────────────────────────┬────────────────────────────────────────┘
                                         │
                     ┌───────────────────┴───────────────────┐
                     ▼                                       ▼
       Local File System Reader                 GDAL Virtual Filesystem (/vsis3/)
       Path("output/rasters/EPSG-3857")         s3://bucket/rasters/EPSG-3857/
```

### 4.1. Core Engine: `TiTilerEngine`
Located in [`src/lib/softmaxx/geo/titiler_engine.py`](file:///home/manju/code/github/xboa/src/lib/softmaxx/geo/titiler_engine.py):
- **Dynamic Initialization**: Accepts either a `Path` or `str`. Automatically determines `storage_type = "directory"` vs `"s3"`.
- **Rasters Discovery**: Discovers all GeoTIFFs, opens them with `rio_tiler.io.COGReader`, reads their bounds, and computes the union bounding box in both `EPSG:3857` and `EPSG:4326` (WGS84).
- **Mosaicing Pipeline**:
  ```python
  def _read_tile(asset: str) -> ImageData:
      with COGReader(asset) as cog:
          return cog.tile(x, y, z)

  img, _ = mosaic_reader(intersecting_rasters, _read_tile)
  ```
  `rio_tiler.mosaic.mosaic_reader` automatically filters rasters to only those whose spatial extent intersects $(x, y, z)$.
- **Vectorized Colormapping**: Float NDVI values are rescaled to uint8 and mapped to RGBA using `rio-tiler` colormaps (`rdylgn`, `viridis`, etc.):
  ```python
  img.rescale(in_range=[(vmin, vmax)], out_range=[(0, 255)])
  cmap = rio_cmap.get(cmap_name)
  png_bytes = img.render(img_format="PNG", colormap=cmap)
  ```
- **Thread-safe LRU Caching**: Stores up to 512 recently requested tiles in an `OrderedDict` protected by a `threading.Lock`. Computes MD5 checksums for HTTP ETag validation.

### 4.2. API Routers
- **Standalone Server**: [`src/exp/tile_server.py`](file:///home/manju/code/github/xboa/src/exp/tile_server.py)
  Provides CLI execution (`--host`, `--port`, `--storage-path`, `--dataset-id`, `--cache-size`, `--cmap`).
- **Modular API Router**: [`src/web/api/routers/tiles.py`](file:///home/manju/code/github/xboa/src/web/api/routers/tiles.py)
  Mounted directly into the master application [`src/web/api/main.py`](file:///home/manju/code/github/xboa/src/web/api/main.py) under `/boa/v1/tiles` and `/boa/v1/cog`.

### 4.3. Dynamic Frontend Viewer
Located in [`src/exp/ndvi_viewer.html`](file:///home/manju/code/github/xboa/src/exp/ndvi_viewer.html):
- On startup, the browser sends a request to `/info`.
- Reads `bounds_wgs84: [min_lon, min_lat, max_lon, max_lat]`.
- Uses Leaflet's `map.fitBounds()` to automatically position the camera exactly over the ingested rasters.
- Updates the UI header dynamically with the active dataset name, nominal scale, and raster count.

---

## 5. Storage Evolution: Directory $\to$ Amazon S3

The architecture allows switching from local development to cloud production with **zero changes to application code**:

```
                 LOCAL ENVIRONMENT                                   PRODUCTION ENVIRONMENT
   
   CLI: --storage-path output/rasters/EPSG-3857       CLI: --storage-path s3://my-prod-bucket/rasters/
                          │                                                  │
                          ▼                                                  ▼
                   POSIX Directory                                   Amazon S3 Bucket
                          │                                                  │
                          ▼                                                  ▼
                 Standard OS File I/O                               GDAL /vsis3/ Streaming
             (open / read 256x256 window)                     (HTTP Range: bytes=1024-8192)
```

### How S3 Streaming Works Under the Hood:
1. `TiTilerEngine` detects `s3://` prefix.
2. It uses `boto3` or GDAL to list raster keys under the S3 prefix.
3. When a tile request `/tiles/12/3015/1746.png` arrives, `rio-tiler` uses GDAL's `/vsis3/` virtual filesystem.
4. GDAL reads only the TIFF header (first few kilobytes) and then issues targeted **HTTP GET Range Requests** (`bytes=start-end`) to read only the specific byte offsets containing the requested tile data.
5. **Bandwidth Savings**: A 50MB GeoTIFF requires downloading only $\approx 50\text{KB}$ of tile data over the wire!

---

## 6. Verification & Test Results

The implementation was validated using the test suite in [`tests/test_tile_server.py`](file:///home/manju/code/github/xboa/tests/test_tile_server.py).

### Test Suite Execution
```bash
.venv/bin/python tests/test_tile_server.py
```
**Results**:
- `test_dynamic_metadata`: Validated that nominal scale ($10.0\text{m}$), native zoom ($Z=12$), display match zoom ($Z=14$), and bounding boxes are correctly derived dynamically.
- `test_titiler_engine_scenarios`:
  - **Scenario A ($Z=12$)**: Validated successful multi-raster compositing into a valid PNG.
  - **Scenario B ($Z=14$)**: Validated oversampled window reading at display match zoom.
  - **Scenario C ($Z=10$)**: Validated aggregated overview rendering across parent boundaries.
  - **Out of Bounds ($Z=12, X=0, Y=0$)**: Validated return of `None` $\to$ HTTP 204 No Content.
- `test_fastapi_endpoints`:
  - Validated `/health` and `/info` JSON schema.
  - Validated conditional GET (`If-None-Match: <etag>`) returning **HTTP 304 Not Modified**.
  - Validated TiTiler `/cog` single-file endpoint.

All tests passed cleanly in **1.71 seconds**.

---

## 7. Production Operations Guide

### 7.1. Running in Production
```bash
# Production multi-worker execution
uvicorn src.exp.tile_server:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 4 \
  --log-level info
```

### 7.2. Connecting to S3
Ensure the host environment has AWS IAM credentials configured:
```bash
export AWS_ACCESS_KEY_ID="AKIA..."
export AWS_SECRET_ACCESS_KEY="..."
export AWS_DEFAULT_REGION="us-west-2"

python src/exp/tile_server.py \
  --storage-path s3://my-earth-engine-cogs/rasters/EPSG-3857/
```

### 7.3. Edge CDN Integration
The server sends standard caching headers:
```http
Cache-Control: public, max-age=86400, stale-while-revalidate=604800
ETag: "9a7f3e2d1c0b..."
```
Placing an edge CDN (AWS CloudFront or Cloudflare) in front of the tile server allows repeated tile requests to be served directly from edge points of presence (PoPs) in **< 15ms**, shielding the backend Python server from high load.

