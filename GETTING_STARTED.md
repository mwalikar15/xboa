# Getting Started with XBoa (Boa Python SDK)

This guide provides step-by-step instructions for getting started with development on the `xboa` codebase.

---

## 1. Repository Architecture Overview

The repository is structured around a central SDK library (`softmaxx`), a web API, queue workers, and geo-spatial utility scripts.

```
xboa/
├── src/
│   ├── lib/softmaxx/          # Core SDK library
│   │   ├── config.py          # Centralized configuration & logging engine
│   │   ├── geo/tiles.py       # Spatial indexing (WebMercator / CRS84 / GEE integration)
│   │   ├── game/              # Game domain logic
│   │   └── api/               # Shared API utilities and exception handling
│   ├── web/api/               # FastAPI REST Service (Entry point: main.py)
│   │   └── routers/           # Endpoint handlers (/hello, /llm, /game)
│   ├── worker/                # Asynchronous queue workers (PgQue & Kafka)
│   └── exp/                   # Experimental / testing tools
├── scripts/
│   └── geo/test.py            # Geo tile grid populator and indexing script
├── misc/
│   └── postgres/tile-schema.sql  # Database schema for spatial computations
└── docs/                      # Environment and setup reference documentation
```

---

## 2. Environment Setup

### Step 2.1: Python Virtual Environment & Path Setup (`.pth` File)

1. **Create and activate a Python 3.10+ virtual environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies**:

   **Core web & database**:
   ```bash
   pip install fastapi uvicorn httpx requests pydantic psycopg[binary]
   ```

   **Google Earth Engine & geospatial**:
   ```bash
   pip install earthengine-api google-auth shapely rasterio
   ```

   **AI / LLM**:
   ```bash
   pip install openai
   ```

   **Data & visualisation** (experimental scripts only):
   ```bash
   pip install numpy matplotlib Pillow
   ```

   **Queue workers**:
   ```bash
   pip install pgque confluent-kafka
   ```

   Or install everything at once:
   ```bash
   pip install fastapi uvicorn httpx requests pydantic psycopg[binary] \
               earthengine-api google-auth shapely rasterio \
               openai \
               numpy matplotlib Pillow \
               pgque confluent-kafka
   ```

3. **Configure Python import path via `.pth` file**:
   Add `src/lib` to the virtual environment's site-packages so `softmaxx` can be imported across all sessions without needing `export PYTHONPATH`.

   > **Important**: this command must run with the **venv Python**, not the system Python. If you run it with the system `python3` the `.pth` file will be written to the wrong site-packages directory and `softmaxx` will still be missing at runtime.

   **Option A — venv already activated** (`source .venv/bin/activate` was run first):
   ```bash
   python -c "import sysconfig; from pathlib import Path; Path(sysconfig.get_path('purelib'), 'xboa.pth').write_text(str(Path('src/lib').resolve()) + '\n')"
   ```

   **Option B — call the venv Python directly** (works regardless of shell activation state):
   ```bash
   .venv/bin/python -c "import sysconfig; from pathlib import Path; Path(sysconfig.get_path('purelib'), 'xboa.pth').write_text(str(Path('src/lib').resolve()) + '\n')"
   ```

   Verify it worked:
   ```bash
   .venv/bin/python -c "from softmaxx.config import AppConfig; print('softmaxx import OK')"
   ```

---

## 3. Configuration Setup

The SDK configuration parser ([src/lib/softmaxx/config.py](file:///home/manju/code/github/xboa/src/lib/softmaxx/config.py)) searches for configuration in `$XBOA_CONFIG_PATH` or at `~/.config/softmaxx/xboa/config.json`.

1. **Create the configuration directory**:
   ```bash
   mkdir -p ~/.config/softmaxx/xboa
   ```

2. **Create `~/.config/softmaxx/xboa/config.json`**:
   ```json
   {
     "database": {
       "default": {
         "user": "xapi_user",
         "password": "your_secure_password",
         "host": "localhost",
         "port": 5432,
         "database": "xapi_db"
       }
     },
     "logging": {
       "fastapi": {
         "log_file": "/tmp/xboa-fastapi.log",
         "log_level": 20
       },
       "local": {
         "log_file": "/tmp/xboa-local.log",
         "log_level": 20
       },
       "pgque": {
         "log_file": "/tmp/xboa-pgque.log",
         "log_level": 20
       }
     },
     "api_keys": {
       "QWEN3_32B_KEY": "your_key_here"
     },
     "gee": {
       "credentials_file": "/path/to/your-service-account-key.json"
     }
   }
   ```

   > **Note**: The `gee.credentials_file` path must point to a Google Cloud service account JSON key file.
   > The GCP project ID and service-account e-mail are read directly from that file — no need to duplicate them here.

---

## 4. Database Setup (PostgreSQL)

The spatial indexing features require a PostgreSQL database loaded with [misc/postgres/tile-schema.sql](file:///home/manju/code/github/xboa/misc/postgres/tile-schema.sql).

1. **Create Database User & Database**:
   ```bash
   sudo -u postgres psql
   ```
   Execute the SQL statements:
   ```sql
   CREATE USER xapi_user WITH PASSWORD 'your_secure_password';
   CREATE DATABASE xapi_db WITH OWNER = xapi_user;
   \q
   ```

2. **Apply the Schema**:
   Run the schema script as `xapi_user`:
   ```bash
   psql -h localhost -U xapi_user -d xapi_db -f misc/postgres/tile-schema.sql
   ```

---

## 5. Running Application Components

### A. Web API Server (FastAPI)
Run the REST API service using `uvicorn`:
```bash
uvicorn src.web.api.main:app --reload --port 8000
```
- API Documentation (Swagger): `http://localhost:8000/docs`
- Main API Router Prefix: `/boa/v1`

### B. Geo Spatial Test & Polygon Linking
Run the sample script in [scripts/geo/test.py](file:///home/manju/code/github/xboa/scripts/geo/test.py):
```bash
python scripts/geo/test.py
```

### C. Background Queue Workers
To process asynchronous tasks with PostgreSQL queues:
```bash
python src/worker/pgque/consumer.py
```

---

## 6. Development Workflow & Recommended Next Steps

### A. Adding REST APIs
To create new API endpoints, create a router module in `src/web/api/routers/` and mount it inside [src/web/api/main.py](file:///home/manju/code/github/xboa/src/web/api/main.py).

#### Example:
1. Create `src/web/api/routers/analytics.py`:
   ```python
   from fastapi import APIRouter

   analytics_router = APIRouter(prefix="/analytics", tags=["analytics"])

   @analytics_router.get("/summary")
   async def get_summary():
       return {"status": "success", "metrics": {"active_computations": 5}}
   ```

2. Include the router in [src/web/api/main.py](file:///home/manju/code/github/xboa/src/web/api/main.py):
   ```python
   from .routers.analytics import analytics_router

   # Inside main_router configuration
   main_router.include_router(analytics_router)
   ```

---

### B. Geo Computation & Tile Math
Extend spatial grid tile calculations or Google Earth Engine (GEE) region exports in [src/lib/softmaxx/geo/tiles.py](file:///home/manju/code/github/xboa/src/lib/softmaxx/geo/tiles.py).

#### Example:
```python
import ee
from softmaxx.geo.tiles import get_polygon_intersection_tiles, crs84_to_gee_region, TileMatrix

# 1. Fetch tiles intersecting an AOI GeoJSON polygon at target zoom level
tiles = get_polygon_intersection_tiles(
    polygon_file="scripts/geo/polygon.json", 
    zoom_level=14, 
    tile_matrix=TileMatrix.CRS84
)

# 2. Iterate over tiles and convert to Earth Engine computational regions
for tile in tiles:
    gee_region: ee.Geometry.Rectangle = crs84_to_gee_region(tile)
    # Perform GEE image reduction or calculation inside gee_region
    print(f"Processing Tile LatIdx={tile.lat_index}, LonIdx={tile.lon_index} with fraction {tile.intersection_fraction}")
```

---

### C. VS Code Environment & IntelliSense Setup
To enable complete code completion and resolve import paths in VS Code without errors, configure `.vscode/settings.json` (as described in [docs/vscode-setup.txt](file:///home/manju/code/github/xboa/docs/vscode-setup.txt)):

1. Create directory `.vscode/` at the root of the repository.
2. Create `.vscode/settings.json`:
   ```json
   {
     "python.analysis.extraPaths": [
       "${workspaceFolder}/src/lib"
     ],
     "python.autoComplete.extraPaths": [
       "${workspaceFolder}/src/lib"
     ]
   }
   ```

---

## 7. Key References

- Configuration Module: [src/lib/softmaxx/config.py](file:///home/manju/code/github/xboa/src/lib/softmaxx/config.py)
- Geo Tile Math: [src/lib/softmaxx/geo/tiles.py](file:///home/manju/code/github/xboa/src/lib/softmaxx/geo/tiles.py)
- Web API Entrypoint: [src/web/api/main.py](file:///home/manju/code/github/xboa/src/web/api/main.py)
- Database Setup Guide: [docs/postgres.txt](file:///home/manju/code/github/xboa/docs/postgres.txt)
- VS Code Workspace Setup: [docs/vscode-setup.txt](file:///home/manju/code/github/xboa/docs/vscode-setup.txt)

