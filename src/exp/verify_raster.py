"""
GeoTIFF Raster Verification Utility
====================================
CLI tool that delegates raster verification & inspection to the Core SDK
library method `softmaxx.geo.verify_geotiff_raster()`.
"""

import sys
from pathlib import Path
from softmaxx.geo import verify_geotiff_raster


def main():
    repo_root = Path(__file__).resolve().parents[2]
    target_dir = repo_root / "output" / "rasters" / "EPSG-3857"

    if len(sys.argv) > 1:
        target_path = Path(sys.argv[1])
        if target_path.is_file():
            tif_files = [target_path]
        else:
            tif_files = list(target_path.glob("*.tif"))
    else:
        tif_files = list(target_dir.glob("*.tif"))

    if not tif_files:
        print(f"No .tif files found to verify in {target_dir}")
        return

    for tif in tif_files:
        print("=" * 75)
        print(f"VERIFYING RASTER: {tif.name}")
        print("=" * 75)
        try:
            stats = verify_geotiff_raster(tif)
            print(f"File Size            : {stats['file_size_kb']} KB")
            print(f"Dimensions           : {stats['width']} x {stats['height']} (Bands: {stats['bands']})")
            print(f"CRS                  : {stats['crs']}")
            print(f"Bounds (W, S, E, N)  : [{stats['bounds']['left']}, {stats['bounds']['bottom']}, {stats['bounds']['right']}, {stats['bounds']['top']}]")
            print(f"Data Type            : {stats.get('dtype', 'N/A')}")
            print(f"Valid Pixels Coverage: {stats.get('valid_pixel_percentage', 'N/A')}% ({stats.get('valid_pixel_count', 'N/A')} px)")
            print(f"NDVI Value Range     : Min = {stats['min_val']} | Max = {stats['max_val']} | Mean = {stats['mean_val']}")

            if stats["is_valid_ndvi"]:
                print("STATUS               : ✅ VALID NDVI RASTER (Values within [-1.0, +1.0])")
            else:
                print("STATUS               : ⚠️ WARNING: Values out of bounds or no valid pixels found")
        except Exception as exc:
            print(f"❌ Verification failed for {tif.name}: {exc}")
        print()


if __name__ == "__main__":
    main()
