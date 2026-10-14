"""
Visual Verification Script for GeoTIFF Raster Tiles
=====================================================
CLI tool that delegates raster visualization to the Core SDK library method
`softmaxx.geo.plot_geotiff_ndvi()`.
"""

import sys
from pathlib import Path
from softmaxx.geo import plot_geotiff_ndvi


def main():
    repo_root = Path(__file__).resolve().parents[2]
    target_dir = repo_root / "output" / "rasters" / "EPSG-3857"

    # Allow custom path argument or inspect default directory
    if len(sys.argv) > 1:
        target_path = Path(sys.argv[1])
        if target_path.is_file():
            tif_files = [target_path]
        else:
            tif_files = list(target_path.glob("*.tif"))
    else:
        tif_files = list(target_dir.glob("*.tif"))

    if not tif_files:
        print(f"No .tif files found to plot in {target_dir}")
        return

    for tif in tif_files:
        plot_geotiff_ndvi(tif, save_png=True, show_plot=True)


if __name__ == "__main__":
    main()
