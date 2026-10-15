import sys
import unittest
from pathlib import Path

# Ensure `src` is in sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import warnings
warnings.filterwarnings("ignore", message=".*The dataset has no Overviews.*")
warnings.filterwarnings("ignore", category=UserWarning)

import softmaxx.geo as geo
from softmaxx.geo.titiler_engine import TiTilerEngine
from web.api.routers.tiles import tiles_router, cog_router


class TestTiTilerServer(unittest.TestCase):

    def setUp(self):
        import re
        self.raster_dir = REPO_ROOT / "output" / "rasters" / "EPSG-3857"
        self.engine = TiTilerEngine(storage_path=self.raster_dir)

        # Dynamically discover an existing tile coordinate from indexed rasters
        tifs = sorted(self.raster_dir.glob("*.tif"))
        self.assertTrue(len(tifs) > 0, "No rasters found in output/rasters/EPSG-3857")
        self.sample_file = str(tifs[0].resolve())
        match = re.search(r"ndvi_z(\d+)_x(\d+)_y(\d+)", tifs[0].name)
        if match:
            self.test_z = int(match.group(1))
            self.test_x = int(match.group(2))
            self.test_y = int(match.group(3))
        else:
            self.test_z, self.test_x, self.test_y = 12, 2914, 1932

    def test_dynamic_metadata(self):
        meta = self.engine.metadata
        self.assertEqual(meta.storage_type, "directory")
        self.assertEqual(meta.s_comp_meters, 10.0)
        self.assertEqual(meta.native_zoom, 12)
        self.assertEqual(meta.display_match_zoom, 14)
        self.assertTrue(meta.total_rasters > 0)
        # Bounds must be valid
        self.assertTrue(meta.bounds_3857[0] < meta.bounds_3857[2])
        self.assertTrue(meta.bounds_3857[1] < meta.bounds_3857[3])

    def test_titiler_engine_scenarios(self):
        # 1. Native Z=12 (Scenario A)
        res_a = self.engine.render_tile_sync(self.test_x, self.test_y, self.test_z)
        self.assertIsNotNone(res_a)
        png_a, etag_a = res_a
        self.assertTrue(png_a.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(len(etag_a), 32)

        # 2. Zoom in Z=14 (Scenario B)
        res_b = self.engine.render_tile_sync(self.test_x * 4, self.test_y * 4, self.test_z + 2)
        self.assertIsNotNone(res_b)
        png_b, _ = res_b
        self.assertTrue(png_b.startswith(b"\x89PNG\r\n\x1a\n"))

        # 3. Zoom out Z=10 (Scenario C)
        res_c = self.engine.render_tile_sync(self.test_x // 4, self.test_y // 4, self.test_z - 2)
        self.assertIsNotNone(res_c)
        png_c, _ = res_c
        self.assertTrue(png_c.startswith(b"\x89PNG\r\n\x1a\n"))

        # 4. Out of bounds (204)
        res_empty = self.engine.render_tile_sync(0, 0, self.test_z)
        self.assertIsNone(res_empty)

    def test_fastapi_endpoints(self):
        app = FastAPI()
        app.include_router(tiles_router, prefix="/boa/v1")
        app.include_router(cog_router, prefix="/boa/v1/cog")

        with TestClient(app) as client:
            # 1. Health
            h_res = client.get("/boa/v1/tiles/health")
            self.assertEqual(h_res.status_code, 200)
            self.assertEqual(h_res.json()["status"], "ok")
            self.assertEqual(h_res.json()["storage_type"], "directory")

            # 2. Info
            i_res = client.get("/boa/v1/tiles/info")
            self.assertEqual(i_res.status_code, 200)
            info = i_res.json()
            self.assertEqual(info["native_zoom"], 12)
            self.assertEqual(info["display_match_zoom"], 14)

            # 3. Seamless XYZ Tile
            t_res = client.get(f"/boa/v1/tiles/{self.test_z}/{self.test_x}/{self.test_y}.png")
            self.assertEqual(t_res.status_code, 200)
            self.assertEqual(t_res.headers["content-type"], "image/png")
            self.assertIn("etag", t_res.headers)

            # 4. ETag 304 Not Modified
            etag = t_res.headers["etag"]
            c_res = client.get(f"/boa/v1/tiles/{self.test_z}/{self.test_x}/{self.test_y}.png", headers={"if-none-match": etag})
            self.assertEqual(c_res.status_code, 304)

            # 5. Out of bounds (204 No Content)
            empty_res = client.get(f"/boa/v1/tiles/{self.test_z}/0/0.png")
            self.assertEqual(empty_res.status_code, 204)

            # 6. Official TiTiler COG endpoint (with local file path)
            titiler_res = client.get(
                f"/boa/v1/cog/tiles/WebMercatorQuad/{self.test_z}/{self.test_x}/{self.test_y}.png?url={self.sample_file}&colormap_name=rdylgn&rescale=-0.2,0.8"
            )
            self.assertEqual(titiler_res.status_code, 200)
            self.assertTrue(titiler_res.content.startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
