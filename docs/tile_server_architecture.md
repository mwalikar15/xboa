# The Frontend Solution: Serving EPSG:3857 COG Rasters via a Tile Server

> Extracted from a Google AI Mode (Gemini) multi-turn conversation.  
> Source: `src/exp/Google_GEE_Tile_Server.html`

---

## Context & Problem Statement

We have rasters in **EPSG:3857 CRS** with **1 pixel = 10m × 10m** resolution.
One raster typically contains **10⁶ pixels** (≈ 1000 × 1000 grid).
The rasters are:
- Aligned to the **EPSG:3857 Web Mercator Grid** — edges match grid tile boundaries exactly. That is by design.
- Exported as **Cloud Optimized GeoTIFFs (COGs)** to **Amazon S3**.
- Indexed in a database: each raster is mapped to its EPSG:3857 tile coordinates **(X, Y, Z)**.

**Goal**: Serve these rasters to a frontend web map application.

---

## Part 1 — The Three Server Architecture Options

Because web tile levels double in resolution at every step, a **10m resolution** raster maps closest to **Zoom Level 14** (which has a resolution of roughly 9.55 meters/pixel at the equator).

### Option 1: Pre-rendered Tile Packages (Fastest & Easiest)

Best for **static datasets** that don't change frequently.

- Slice rasters into an **MBTiles** database or a directory of static folders (`{z}/{x}/{y}.png`).
- Host on simple, cheap storage like AWS S3 or DigitalOcean Spaces.

**Generate using GDAL:**
```bash
gdal2tiles.py -z 8-15 -r bilinear input_raster.tif output_folder/
```

### Option 2: Cloud Optimized GeoTIFF (COG) + Dynamic Tiler ⭐ Most Scalable

Best for **massive or frequently updated data**.

Store the file in the cloud and a lightweight server reads only the specific pixels needed for a requested tile on the fly.

| Dynamic Tiler   | Language        | Best For                                            |
|-----------------|-----------------|-----------------------------------------------------|
| **Titiler**     | Python/FastAPI  | Cloud-native microservices, AWS Lambda deployments  |
| **Martin**      | Rust            | Blazing fast, low memory footprint, bare-metal      |
| **Terracotta**  | Python          | Simplest plug-and-play setup                        |

**Workflow:**
1. Convert raster to COG: `gdal_translate input.tif cog.tif -co TILED=YES -co COMPRESS=DEFLATE`
2. Upload `cog.tif` to cloud storage.
3. Point **Titiler** or **Martin** at your cloud URL — they automatically serve standard XYZ/WMTS tile URLs.

### Option 3: Full-Featured GIS Servers (Enterprise / On-Premise)

If you need robust access control, styled layers, or need to serve data via official OGC standards like WMS or WMTS alongside vector data:

- **GeoServer**: Enterprise standard (Java-based). Built-in extension GeoWebCache automatically caches and serves 3857 layers.
- **MapServer**: C-based, exceptionally fast and memory-efficient.
- **QGIS Server**: Style rasters directly in QGIS Desktop and publish to the web.

---

## Part 2 — How to Pull Tiles into the Frontend

Once your server is running or your static tiles are hosted, pull them into a web map (like **Leaflet** or **MapLibre GL JS**) using a standard URL template:

```javascript
// Example using Leaflet
L.tileLayer('https://your-server-or-s3.com/{z}/{x}/{y}.png', {
    maxZoom: 15,
    minZoom: 0,
    attribution: 'My 10m Raster Data'
}).addTo(map);
```

---

## Part 3 — The Dynamic Tiling Engine: Core Logic (Grid-Aligned COGs)

> **Key insight**: Because the COG files are explicitly matched to the edges of the EPSG:3857 Grid by design, the serving architecture becomes remarkably clean, deterministic, and efficient.

In GEE terms, rasters are **pre-tiled** at a specific "native" zoom level. Given 10m × 10m resolution and a tile spanning 10⁶ pixels (1000 × 1000), this matches a standard **Zoom Level 11** tile (which stretches 10,000m × 10,000m at the equator).

The entire logic boils down to **direct grid arithmetic** based on the relationship between the **Requested Zoom (Z_req)** and the **Native Zoom (Z_native)**.

### The Full Request Pipeline

#### Step 1 — The Frontend Request
The frontend map application (Leaflet, MapLibre, or OpenLayers) moves, zooms, and figures out which EPSG:3857 map tiles (X, Y, Z) are currently visible on the user's screen. It sends standard tile requests using a structured URL pattern:
```
/tiles/{z}/{x}/{y}
```

#### Step 2 — Middleware Routing & Database Lookup
- Your web server or API gateway intercepts the incoming `(X, Y, Z)` request.
- Your server queries the database using those exact `(X, Y, Z)` coordinates to find the matching raster metadata.
- The database returns the unique **Amazon S3 URL** of the specific COG file that covers or overlaps the requested tile area.

#### Step 3 — The Dynamic Tiling Engine (Core Logic)

Once the middleware knows the target Zoom, X, Y, and the corresponding S3 COG path, it passes this information to a dynamic tiling engine. The engine performs three rapid sub-steps:

1. **Virtual Cropping (Internal Spatial Query)**: Translates the requested web tile's geographical boundaries into the raw pixel coordinate boundaries of your COG file.
2. **Smart Byte-Range Requests**: Because the file is a COG stored on Amazon S3, the engine does **not** download the whole file. Instead, it sends an HTTP Range Request to S3, pulling only the specific blocks of bytes required to construct that single requested web tile.
3. **On-the-Fly Downsampling/Upsampling**: Since your pixels are natively 10m × 10m, they line up closest to Zoom 14. If the frontend requests Zoom 14, the engine copies the pixels almost 1-to-1. If the frontend requests Zoom 10 (zoomed far out), the engine automatically reads the pre-calculated internal overviews (pyramids) inside the COG to serve a lower-resolution tile instantly.

#### Step 4 — The Response Delivery
- The dynamic tiler compresses the sliced pixel data into a standard web-friendly format (like a **PNG**, **JPEG**, or **WebP** image tile).
- The server responds to the frontend map application with this image.
- The frontend renders the tile seamlessly in the browser exactly where it belongs on the map grid.

---

## Part 4 — Scenario-Based Zoom Handling

### Scenario A: Requested Zoom = Native Zoom (Z_req = Z_native) ✅ Most Efficient

The most efficient scenario — the user is browsing at the exact scale of your source data.

- **Lookup Logic**: Frontend asks for tile `X/Y/Z`. Server performs a **direct key-value lookup** in the database: `X == requested_X AND Y == requested_Y AND Z == native_Z`.
- **Tiling Logic**: The database returns the exact S3 URL for that specific COG. Because the raster fills the grid perfectly, the tile server simply reads the imagery data from S3, converts the pixel grid to an image format (PNG or WebP), and serves it directly. **No pixel resampling or edge cropping happens.**

### Scenario B: Requested Zoom is Closer/Higher (Z_req > Z_native) — Zoom In

The user has zoomed in past source data resolution to see a higher detailed view of a specific area.

- **Lookup Logic**: A single source raster now covers multiple requested frontend tiles. To find the source file, the server performs a standard **Tile Parent Calculation**:

```
Parent_X = floor(X_req / 2^(Z_req - Z_native))
Parent_Y = floor(Y_req / 2^(Z_req - Z_native))
```

- **Tiling Logic**: The server uses the calculated `Parent X/Y` coordinates to fetch the correct COG from S3. Because the grids align perfectly, the server can precisely calculate what sub-quadrant of the COG to read using a byte-range request. It extracts that exact pixel patch, stretches (upsamples) it to fill the standard tile size, and delivers it.

### Scenario C: Requested Zoom is Further/Lower (Z_req < Z_native) — Zoom Out

The user has zoomed out to see a broad, bird's-eye view of the map.

- **Lookup Logic**: A single frontend tile now encompasses multiple adjacent source rasters. The server calculates the range of "children" rasters that fall perfectly inside the requested tile boundaries:

```
Min_Child_X = X_req × 2^(Z_native - Z_req)
Max_Child_X = (Min_Child_X) + 2^(Z_native - Z_req)
(The same logic applies to find the Y range.)
```

- **Tiling Logic**: The server queries the database for all COG files matching that specific bounding block of coordinates. Because the files are COGs, the server uses HTTP range requests to read only the **low-resolution internal overviews (pyramids)** of each file. It stitches these small, downsampled pieces side-by-side without any overlap gaps to create one unified, zoomed-out tile image.

---

## Part 5 — The 256×256 Rule: Display Pixels vs Data Pixels

> **Key insight**: The 256×256 rule refers strictly to **display pixels**, while the pixels you get from GEE are **data pixels**.

### The 256×256 Display Pixel Rule

The 256×256 dimension is a rigid, legacy standard hardcoded into frontend mapping libraries (Leaflet, OpenLayers, MapLibre).

- It means the browser allocates an HTML `<img>` canvas of exactly **256×256 layout/screen pixels** to render that tile, regardless of what zoom level you are on.
- If you send a PNG or JPEG tile to the browser that is **not** 256×256 pixels, the browser will forcibly squash or stretch it to 256×256 display pixels to fit the map grid layout, which can distort your map.

### Your GEE Example: 9784 × 9784 Data Pixels

At **Zoom 12**, an EPSG:3857 grid tile spans exactly **9783.94 meters** on each side at the equator.

Because native data resolution is **10m per pixel**, GEE gives a raster containing roughly **978×978 data pixels** (9784 meters / 10 meters per pixel). *Note: It is 978×978 data pixels, not 9784×9784, because each data pixel represents a 10m block, not a 1m block.*

This means the raw storage contains a **978×978 data grid** filling up that Zoom 12 tile boundary.

---

## Part 6 — How to Bridge the Data-to-Display Gap

Because the data grid (978×978) does not equal the browser's display grid (256×256), you cannot serve this file directly to the frontend at Zoom 12 without the tile server adjusting it.

### Approach A: The Pure Imagery Path (Visual Tiles) — Recommended for Variable Resolution

This decouples variable source resolution (10m, 30m, etc.) from the rigid 256×256 display grid.

**Server Actions**:
1. The server reads the 978×978 COG data grid from S3.
2. The server **downsamples (resamples)** that 978×978 data grid into a standard **256×256 compressed image matrix**.
3. It sends that 256×256 image to the browser — the browser displays it perfectly.

### Approach B: The Zoom-Shift Path (Smart Way for 10m Data)

Because your data pixels are 10m, they naturally carry the density of a deeper zoom level. In the Web Mercator grid, a display pixel matches a 10m data pixel at **Zoom 14**.

Instead of treating the GEE export as a Zoom 12 visual tile, treat it strictly as a **spatial boundary container for Zoom 14 data**. Your logic changes to:

- **The Request**: The user zooms into **Zoom 14**. The browser requests a specific sub-tile.
- **The Math**: Your server determines that this Zoom 14 sub-tile falls inside your Zoom 12 COG area.
- **The Pull**: A Zoom 14 tile needs to cover a 2446m × 2446m area. At 10m resolution, that area contains exactly **244×244 data pixels** inside your file.
- **The Response**: The server requests just those 244×244 data pixels from your S3 file, minimally pads/stretches them to 256×256 display pixels, and hands it to the browser.

By shifting the data to Zoom 14, you display your 10m pixels at their **true, uncompressed native crispness** on the user's screen.

---

## Part 7 — The Reverse Situation: User Zooms In Beyond Native Resolution

When you encounter the **reverse situation** — a user zooms in deep (e.g., to Zoom 16 or 17), but the underlying data only provides a sparse number of pixels (e.g., native data boundary was Zoom 12) — you face a deficit of data pixels relative to the required display pixels.

The tile server and frontend map use **Interpolation and Overzooming** logic to handle the pixel shortage:

### 1. The Server-Side Solution: Up-sampling (Interpolation)

When the frontend requests a high-zoom tile, the server realizes the requested area maps to a tiny fraction of your source data. For instance, a Zoom 16 tile might only overlap a **15 × 15 pixel grid** in your raw COG data.

To satisfy the browser's strict requirement for a 256×256 image, the tile server extracts that tiny 15 × 15 data grid and mathematically stretches it into a 256×256 matrix before sending it out. You configure the server to use one of two logic modes:

- **Nearest Neighbour**: Duplicates existing data pixels without mixing them. Results in a "blocky" or "pixelated" look. **Correct logic for discrete/categorical data** (e.g., land cover classification or crop types).
- **Bilinear or Bicubic Interpolation**: Mathematically blends the sparse pixels together, creating smooth gradients between them. Results in a "blurry" or "fuzzy" look. **Correct logic for continuous data** (e.g., elevation, temperature, or satellite imagery).

### 2. The Frontend Solution: Client-Side Overzooming ⭐ Highly Recommended

Constantly generating up-sampled images on the server for deep zoom levels wastes CPU cycles and S3 bandwidth. Instead, the modern industry standard is to let the **frontend map engine** handle the pixel shortage using a configuration called `maxNativeZoom`.

- **The Logic**: You tell the map library (Leaflet, MapLibre, etc.) the exact zoom level where your data stops giving new details (e.g., Zoom 12).
- **The Execution**: When the user zooms past Zoom 12 to Zoom 13, 14, or 15, the frontend application **stops sending new network requests to your server**. Instead, it takes the Zoom 12 visual image tiles it already downloaded, physically stretches the HTML image elements on the screen, and uses the browser's built-in graphics engine to smooth or pixelate the stretched tiles in real-time.

```javascript
// Leaflet example with maxNativeZoom
L.tileLayer('/tiles/{z}/{x}/{y}.png', {
    maxNativeZoom: 12,  // Stop fetching new tiles from server past zoom 12
    maxZoom: 18         // But allow the user to zoom up to 18 visually
}).addTo(map);
```

---

## Part 8 — Summary of the Complete Pipeline

By combining **Approach A** with **Client-Side Overzooming**, the complete serving logic becomes universally resilient:

| Zoom Direction | What Happens |
|---|---|
| **Zoom Out** | Server reads large COG data grid, shrinks it down to a 256×256 image tile using COG overviews (pyramids). |
| **Native Zoom** | Server reads COG data grid and maps it perfectly to a 256×256 image tile. Direct 1:1 lookup. |
| **Zoom In (Beyond Native)** | Frontend map locks onto the highest available native tile and visually stretches it in the browser, completely saving backend server from unnecessary computation. |

---

## Architectural Impact of Grid-Aligned Rasters

Because rasters are structurally locked to the EPSG:3857 grid system, a **fully deterministic tile router** is achieved. The server can run on ultra-lightweight infrastructure (like AWS Lambda) because it **replaces** resource-heavy GIS spatial projections with simple, instantaneous integer math.

> **Summary**: The boundary grid matching tells the server **which file to open**, but the internal pixel size tells the server **how many rows and columns of data to extract and scale** to fit the frontend's standard 256×256 canvas.

---

## Key Terms Reference

| Term | Meaning |
|------|---------|
| **COG** | Cloud Optimized GeoTIFF — a GeoTIFF structured for efficient HTTP byte-range reads |
| **EPSG:3857** | Web Mercator projection — the standard CRS for web map tile grids |
| **Z_native** | The zoom level at which your raster was generated / its edges align with the grid |
| **Z_req** | The zoom level the frontend is currently requesting |
| **maxNativeZoom** | A Leaflet/MapLibre config that stops new tile fetches past a certain zoom |
| **COG Overviews / Pyramids** | Pre-computed low-res versions embedded inside the COG for fast zoom-out reads |
| **Byte-Range Request** | HTTP `Range:` header request that fetches only a portion of a remote file from S3 |
| **Titiler / Martin / Terracotta** | Open-source dynamic COG tile servers |
