import math
import logging
import json 
import ee
from pathlib import Path
from typing import Any, Optional, Union
from enum import Enum
from dataclasses import dataclass
from shapely.geometry import Polygon, box
from shapely.geometry.polygon import orient


@dataclass(frozen=True)
class WebMapTile:
    zoom: int
    x_index: int
    y_index: int
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    centroid_lon: float
    centroid_lat: float
    intersection_fraction: float

    @property
    def bbox(self) -> list[float]:
        """Returns the bounding box as a flat list: [west, south, east, north]"""
        return [self.min_lon, self.min_lat, self.max_lon, self.max_lat]


    def __str__(self) -> str:
        return (
            f"MapTile(Z={self.zoom}, X={self.x_index}, Y={self.y_index}, "
            f"f={self.intersection_fraction * 100:.1f}%, "
            f"C=[{self.centroid_lon:.5f}, {self.centroid_lat:.5f}])"
        )
    
@dataclass
class CRS84Tile:
    zoom: int
    lat_index: int
    lon_index: int
    # Corner Coordinates
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    # Centroid
    centroid_lon: float
    centroid_lat: float
    # Intersection metrics
    intersection_fraction: float

    @property
    def bbox(self) -> list[float]:
        """Returns the bounding box as a flat list: [west, south, east, north]"""
        return [self.min_lon, self.min_lat, self.max_lon, self.max_lat]
    
    def __str__(self) -> str:
        """Returns a scannable, human-readable summary of the tile."""
        return (
            f"CRS84Tile [Z={self.zoom}, LatIdx={self.lat_index}, LonIdx={self.lon_index}]\n"
            f"  Bounds  : Lon({self.min_lon} to {self.max_lon}), Lat({self.min_lat} to {self.max_lat})\n"
            f"  Centroid: ({self.centroid_lon}, {self.centroid_lat})\n"
            f"  Overlap : {self.intersection_fraction * 100:.2f}%"
        )
    

class TileMatrix(Enum):
    WEB_MAP = "WEB_MAP"
    LOCAL = "LOCAL"
    CRS84 = "CRS84"

# Constants for Web Mercator (EPSG:3857) projection math
# EARTH_RADIUS = 6378137.0
# ORIGIN_SHIFT = math.pi * EARTH_RADIUS  # ~20037508.34 meters


def _pack_map_tile(x: int, y: int, z: int) -> int:
    """Packs X, Y, and Z tile coordinates into a single 64-bit integer ID.

    Bit-Packing layout allocation:
    - Z: bits 0-5    (allows zoom levels 0 to 63)
    - X: bits 6-34   (allows X grid indices up to 536,870,911)
    - Y: bits 35-63  (allows Y grid indices up to 536,870,911)

    Returns:
        int: Unique 64-bit packed integer ID
    """
    # 1. Cast inputs to integers to guarantee correct bitwise execution
    x_val = int(x)
    y_val = int(y)
    z_val = int(z)

    # 2. Shift components into their designated bit boundaries and combine them
    packed_id = (y_val << 35) | (x_val << 6) | z_val
    return packed_id


def _unpack_map_tile(packed_id: int) -> tuple[int, int, int]:
    """Decodes a 64-bit packed integer back into its original X, Y, and Z tile coordinates.
    
    Bit-Packing layout mapping:
    - Z: bits 0-5    (mask 63)
    - X: bits 6-34   (mask 536870911)
    - Y: bits 35-63  (mask 536870911)
    
    Returns:
        tuple: (x, y, z) as pure integers
    """
    z = packed_id & 63
    x = (packed_id >> 6) & 536870911
    y = (packed_id >> 35) & 536870911
    return x, y, z


def _wgs84_meters_per_degree(lat_degree:float)-> tuple[float, float]:
    """
    Calculates the meters per degree scale at one exact coordinate using 
    differential calculus. The key point is that the calculation is fine for 
    a particular coordinate but will introduce errors for large tiles (lower zooms). 

    The idea that meters per degree is same for all points of a tile works for small 
    tiles (higher zooms)
    """
    lat_radians = math.radians(lat_degree)

    a = 6378137.0         # semi-major axis (equatorial radius)
    b = 6356752.314245    # semi-minor axis (polar radius)
    e_sq = 1 - (b**2 / a**2) # Eccentricity squared
    
    # Radius of curvature along the meridian (North-South)
    M = (a * (1 - e_sq)) / ((1 - e_sq * math.sin(lat_radians)**2)**1.5)
    
    # Radius of curvature along the prime vertical (East-West)
    N = a / math.sqrt(1 - e_sq * math.sin(lat_radians)**2)
    
    # Convert radian arc length to 1 degree
    meters_per_deg_lat = M * math.radians(1)
    meters_per_deg_lon = N * math.cos(lat_radians) * math.radians(1)
    return meters_per_deg_lat, meters_per_deg_lon



def _project_polygon_to_meter(coords: list[list[float]], 
                         c_lng: float, 
                         c_lat: float, 
                         meters_per_deg_lat: float, meters_per_deg_lon: float) -> list[tuple[float, float]]:
    """
    This function takes a polygon, a set of global geographic coordinates (LAT/LON) 
    and projects them into a local 2D plane measured in physical meters. 
    """
    return [
        ((lon - c_lng) * meters_per_deg_lat, (lat - c_lat) * meters_per_deg_lon)
        for lon, lat in coords
    ]



def _get_local_tiles(polygon_coords: list[list[float]], zoom: int) -> list[WebMapTile]:
    """Finds intersecting tiles and calculates area fractions using raw array mathematics."""
    # 1. Instantiate the raw Shapely polygon
    # Note: Shapely expects coordinate pairs as (longitude, latitude) [X, Y]
    raw_polygon = Polygon(polygon_coords)
    
    # 2. Enforce Counter-Clockwise (CCW) orientation using native orient
    target_polygon = orient(raw_polygon, sign=1.0)
    # Fail fast if the polygon geometry is structurally invalid
    if not target_polygon.is_valid:
        raise ValueError(
            "The provided polygon is topologically invalid (e.g., contains "
            "self-intersections, spikes, or open rings)."
        )
    
    # 3. Extract degree bounding box directly from the corrected shape bounds
    min_lng, min_lat, max_lng, max_lat = target_polygon.bounds
    
    # Extract the freshly oriented coordinate array to pass to your local projector
    # .exterior.coords outputs tuples, convert back to list[list[float]] to match your signature
    corrected_coords = [list(pt) for pt in target_polygon.exterior.coords]

    # Standard tile conversion equations
    def lon2tile(lon, z): return math.floor((lon + 180) / 360 * (2 ** z))
    def lat2tile(lat, z):
        lat = max(min(lat, 85.0511), -85.0511)
        lat_rad = math.radians(lat)
        return math.floor((1 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * (2 ** z))
    
    def tile2lon(x, z): return x / (2 ** z) * 360.0 - 180.0
    def tile2lat(y, z):
        n = math.pi - 2.0 * math.pi * y / (2 ** z)
        return 180.0 / math.pi * math.atan(0.5 * (math.exp(n) - math.exp(-n)))

    # Compute raw grid bounds
    x_min, x_max = sorted([lon2tile(min_lng, zoom), lon2tile(max_lng, zoom)])
    y_min, y_max = sorted([lat2tile(max_lat, zoom), lat2tile(min_lat, zoom)])
    
    tiles = []

    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            w, e = tile2lon(x, zoom), tile2lon(x + 1, zoom)
            n, s = tile2lat(y, zoom), tile2lat(y + 1, zoom)

            # Build structural bounding tile geometry
            tile_box_wgs84 = box(min(w, e), min(s, n), max(w, e), max(s, n))

            # Fast bounding intersection check
            if target_polygon.intersects(tile_box_wgs84):
                c_lng = (w + e) / 2.0
                c_lat = (s + n) / 2.0

                meters_per_deg_lat, meters_per_deg_lon = _wgs84_meters_per_degree(c_lat)

                # Project the tile corner geometry array using your math
                tile_array_wgs84 = [[w, n], [e, n], [e, s], [w, s], [w, n]]
                
                # Transform arrays to native Shapely meters shapes using corrected_coords
                local_tile_shape = Polygon(_project_polygon_to_meter(tile_array_wgs84, c_lng, c_lat, meters_per_deg_lat, meters_per_deg_lon))
                local_poly_shape = Polygon(_project_polygon_to_meter(corrected_coords, c_lng, c_lat, meters_per_deg_lat, meters_per_deg_lon))

                # Compute the area intersection subset
                intersection_geom = local_poly_shape.intersection(local_tile_shape)
                
                if not intersection_geom.is_empty:
                    area_fraction = intersection_geom.area / local_tile_shape.area
                    area_fraction = round(area_fraction, 4)

                    if area_fraction > 0.0001:
                        packed_id = _pack_map_tile(int(x), int(y), int(zoom))
                        tiles.append(WebMapTile(
                            x_index=int(x), 
                            y_index=int(y), 
                            zoom=int(zoom), 
                            min_lon=round(w, 7),
                            min_lat=round(s, 7),
                            max_lon=round(e, 7),
                            max_lat=round(n, 7),
                            centroid_lon=round(c_lng, 7),
                            centroid_lat=round(c_lat, 7),
                            intersection_fraction=area_fraction
                        ))
    return tiles



def _get_crs84_tiles(polygon_coords: list[list[float]], zoom: int) -> list[CRS84Tile]:
    """
    method to find CRS84 tiles that intersect with the supplied polygon at a zoom.
    The polygon coordinates are expected in [longitude, latitude] format
    We use the CRS84 tile mtarix set (TMS). We draw the spatial grid first and then 
    find the start and end tiles that enclose the polygon in both longitude and
    latitude.
    """
    # Fix Grid steps at this zoom
    total_lat_steps = 2**zoom
    total_lon_steps = 2**(zoom + 1)

    # Since the fraction has powers of 2 in the Denominator, 
    # we should get a terminating decimal (how to check that?)
    tile_height_deg = 180.0 / total_lat_steps
    tile_width_deg = 360.0 / total_lon_steps
    
    # Shapely expects coordinate pairs as (longitude, latitude)
    # Unpack our polygon into shapely[X,Y]
    shapely_poly_coords = [(lon, lat) for lon, lat in polygon_coords]
    raw_polygon =  Polygon(shapely_poly_coords)
    target_polygon = orient(raw_polygon, sign=1.0)

    # Fail fast if the polygon geometry is structurally invalid
    if not target_polygon.is_valid:
        raise ValueError(
            "The provided polygon is topologically invalid (e.g., contains "
            "self-intersections, spikes, or open rings)."
        )
    
    p_min_lon, p_min_lat, p_max_lon, p_max_lat = target_polygon.bounds
    # @todo raise errors if polygon is out of bounds 

    # Direct index calculation, zero of longitude is at -180 
    # The zero of latitude is at 90N (+90)
    start_lon_idx = int((p_min_lon + 180.0) // tile_width_deg)
    end_lon_idx = int((p_max_lon + 180.0) // tile_width_deg)
    
    start_lat_idx = int((90.0 - p_max_lat) // tile_height_deg)
    end_lat_idx = int((90.0 - p_min_lat) // tile_height_deg)
    
    # @todo handle edge cases
    start_lon_idx, end_lon_idx = max(0, start_lon_idx), min(total_lon_steps - 1, end_lon_idx)
    start_lat_idx, end_lat_idx = max(0, start_lat_idx), min(total_lat_steps - 1, end_lat_idx)

    tiles = []

    # Iterate strictly within the resolved grid spatial window
    for lat_idx in range(start_lat_idx, end_lat_idx + 1):
        tile_max_lat = 90.0 - (lat_idx * tile_height_deg)
        tile_min_lat = tile_max_lat - tile_height_deg

        for lon_idx in range(start_lon_idx, end_lon_idx + 1):
            tile_min_lon = -180.0 + (lon_idx * tile_width_deg)
            tile_max_lon = tile_min_lon + tile_width_deg
            
            tile_box = box(tile_min_lon, tile_min_lat, tile_max_lon, tile_max_lat)
            if target_polygon.intersects(tile_box):
                intersection_area = target_polygon.intersection(tile_box).area
                tile_area = tile_box.area
                fraction = intersection_area / tile_area if tile_area > 0 else 0.0
                
                c_lon = tile_min_lon + (tile_width_deg / 2.0)
                c_lat = tile_min_lat + (tile_height_deg / 2.0)

                tiles.append(CRS84Tile(
                    zoom=zoom,
                    lat_index=lat_idx,
                    lon_index=lon_idx,
                    min_lon=round(tile_min_lon, 7),
                    min_lat=round(tile_min_lat, 7),
                    max_lon=round(tile_max_lon, 7),
                    max_lat=round(tile_max_lat, 7),
                    centroid_lon=round(c_lon, 7),
                    centroid_lat=round(c_lat, 7),
                    intersection_fraction=round(fraction, 4)
                ))
                
    return tiles



def _wgs84_to_epsg3857(lon: float, lat: float) -> tuple[float, float]:
    """
    Transforms WGS84 coordinates (degrees) to native EPSG:3857 Web Mercator meters.
    Fails explicitly if coordinates escape physical global limits.
    """
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(f"Longitude out of bounds [-180, 180]: {lon}")
    # Web Mercator cuts off mathematically at roughly 85.051129° N/S
    if not (-85.051129 <= lat <= 85.051129):
        raise ValueError(f"Latitude out of bounds for EPSG:3857 [-85.051129, 85.051129]: {lat}")

    R = 6378137.0  # WGS84 equatorial radius in meters
    x = R * math.radians(lon)
    y = R * math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))
    return x, y

def _epsg3857_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """
    Transforms native EPSG:3857 Web Mercator meters back to WGS84 degrees.
    Used to resolve precise geographic bounds for tile objects.
    """
    R = 6378137.0
    lon = math.degrees(x / R)
    lat = math.degrees(2.0 * math.atan(math.exp(y / R)) - math.pi / 2.0)
    return lon, lat


def _get_web_map_tiles(polygon_coords: list[list[float]], zoom: int) -> list[WebMapTile]:
    """
    Calculates exact intersection fractions of a WGS84 geo-polygon with EPSG:3857 tiles.
    Performs all operations natively within the flat EPSG:3857 projection space
    to completely eliminate distortion errors across all latitudes.
    """
    # 1. Establish the fixed global EPSG:3857 constraints
    # Maximum extent of Web Mercator projection axis in meters
    MAX_EXTENT = 20037508.342789244 
    INITIAL_RESOLUTION = MAX_EXTENT * 2.0
    
    tiles_per_axis = 2**zoom
    tile_size_meters = INITIAL_RESOLUTION / tiles_per_axis

    projected_coords = []
    # Unpack explicitly as [longitude, latitude] -> [X, Y]
    for lon, lat in polygon_coords:
        if lat < -85.00 or lat > 85.00:
            raise ValueError("out of valid latitude range [-85.0, 85,0]")

        # we are going from LAT/LON to EPSG:3857 CRS
        projected_coords.append(_wgs84_to_epsg3857(lon, lat))


    # @todo check bounds and raise error
    raw_polygon =  Polygon(projected_coords)
    target_polygon = orient(raw_polygon, sign=1.0)
    # Fail fast if the polygon geometry is structurally invalid
    if not target_polygon.is_valid:
        raise ValueError(
            "The provided polygon is topologically invalid (e.g., contains "
            "self-intersections, spikes, or open rings)."
        )
    
    p_min_x, p_min_y, p_max_x, p_max_y = target_polygon.bounds
    
    # 3. Direct grid index calculation from projected bounds
    # Shift origin from bottom-left (meters) to top-left (standard XYZ tiling)
    start_x_idx = int((p_min_x + MAX_EXTENT) // tile_size_meters)
    end_x_idx = int((p_max_x + MAX_EXTENT) // tile_size_meters)
    
    # Y index is inverted: index 0 starts at +MAX_EXTENT (North) and goes down
    start_y_idx = int((MAX_EXTENT - p_max_y) // tile_size_meters)
    end_y_idx = int((MAX_EXTENT - p_min_y) // tile_size_meters)
    
    # @todo Handle the edge boundary cases 
    tiles = []
    
    # 4. Iterate strictly within the resolved grid spatial window
    for y_idx in range(start_y_idx, end_y_idx + 1):
        tile_max_y = MAX_EXTENT - (y_idx * tile_size_meters)
        tile_min_y = tile_max_y - tile_size_meters
        
        for x_idx in range(start_x_idx, end_x_idx + 1):
            tile_min_x = -MAX_EXTENT + (x_idx * tile_size_meters)
            tile_max_x = tile_min_x + tile_size_meters
            
            # Construct tile directly as a flat meter-based Cartesian box
            tile_box = box(tile_min_x, tile_min_y, tile_max_x, tile_max_y)
            
            # 5. Evaluate spatial intersection inside EPSG:3857 space
            if target_polygon.intersects(tile_box):
                intersection_geom = target_polygon.intersection(tile_box)
                # Fraction represents how much of the square tile is covered
                tile_area = tile_box.area
                fraction = intersection_geom.area / tile_area if tile_area > 0 else 0.0
                
                # Inverse project corners and centroids back to WGS84
                min_lon, min_lat = _epsg3857_to_wgs84(tile_min_x, tile_min_y)
                max_lon, max_lat = _epsg3857_to_wgs84(tile_max_x, tile_max_y)
                
                c_x = tile_min_x + (tile_size_meters / 2.0)
                c_y = tile_min_y + (tile_size_meters / 2.0)
                c_lon, c_lat = _epsg3857_to_wgs84(c_x, c_y)
                
                tiles.append(WebMapTile(
                    zoom=zoom,
                    x_index=x_idx,
                    y_index=y_idx,
                    min_lon=round(min_lon, 7),
                    min_lat=round(min_lat, 7),
                    max_lon=round(max_lon, 7),
                    max_lat=round(max_lat, 7),
                    centroid_lon=round(c_lon, 7),
                    centroid_lat=round(c_lat, 7),
                    intersection_fraction=round(fraction, 4)
                ))
                
    return tiles


# ###########################
# 
# Public methods 
# 
# ############################# 


def get_polygon_intersection_tiles(
        polygon_file: str, 
        zoom_level: int, 
        tile_matrix: TileMatrix = TileMatrix.WEB_MAP
        ) -> Any :
    """
    This method returns the tiles for a tile matrix set at given zoom level.  
    The polygon is read from a file.
    """

    logger = logging.getLogger("main." + __name__)
    file_path = Path(polygon_file)
    if not file_path.exists() or not file_path.is_file():
        logger.error(f"Target GeoJSON source path does not exist: {file_path}")
        raise FileNotFoundError(f"Missing file: {file_path}")

    with file_path.open("r", encoding="utf-8") as file:
        geometry = json.load(file)
    
    # 3. Validate geometry type and extract coordinates
    geom_type = geometry.get("type")
    if geom_type not in ["Polygon", "MultiPolygon"]:
        raise ValueError(f"Unsupported geometry type: {geom_type}. Expected Polygon or MultiPolygon.")

    raw_coordinates = geometry["coordinates"]
    coordinates = raw_coordinates[0]
    if tile_matrix == TileMatrix.WEB_MAP:
        return _get_web_map_tiles(coordinates, zoom=zoom_level)
    elif tile_matrix == TileMatrix.CRS84:
        return _get_crs84_tiles(coordinates, zoom=zoom_level)
    elif tile_matrix == TileMatrix.LOCAL:
        return _get_local_tiles(coordinates, zoom=zoom_level)
    else:
        raise ValueError(f"Unsupported tile matrix set: {tile_matrix}")


def crs84_to_gee_region(tile: CRS84Tile) -> ee.Geometry.Rectangle:
    """
    Converts a deserialized CRS84Tile object into a Google Earth Engine
    computational region (ee.Geometry.Rectangle).
    """
    # GEE Rectangle expects bounds in [xmin, ymin, xmax, ymax] order
    # which maps exactly to [min_lon, min_lat, max_lon, max_lat]
    return ee.Geometry.Rectangle(
        coords=[tile.min_lon, tile.min_lat, tile.max_lon, tile.max_lat],
        proj='EPSG:4326',
        geodesic=False
    )



def calculate_crs84_tile_zoom(s_comp: int, latitude: float,  pixels_limit:int = 10**6) -> int:
    """
    Get the CRS84 GRID tile zoom that will match the constraints of computation 
    resolution and pixels_limit. if s_comp value is low (higher resolution) then a 
    smaller area will contain more pixels.

    At z=0, the angular spans for both longitude and latitude is 180 degrees. 
    The Δ(latitude) is 111694 meters/degree at poles and 110574 at equator. 
    The Δ(longitude) is 111320 x cos(|φ(min)|)
    We use values of 111850 for latitude meters/degree and 111350 for longitude. 

    pixels in a tile = (tile_width x tile_height) / (s_comp x s_comp)
    since, at every zoom, the tile span is reduced by 2^z , the area is reduced by 
    2^z x 2^z = 2 ^2z. [ pixel_z x 2^2z = pixel_z0 ] 
    Hence 2^2z >= pixel_z0 / pixel_limit 
    Taking logarithms, 
    z >= [log(pixel_z0 / pixel_limit)] / [2 * log(2)]

    """
    logger = logging.getLogger("main." + __name__)
    # Convert latitude to radians and get cosine
    cos_lat = math.cos(math.radians(latitude))
    
    # INPUT check for the poles (width is 0, so 0 pixels)
    if cos_lat <= 0:
        raise  ValueError(f"Bad input, latitude: {latitude}")
    
    # Calculate physical tile dimensions at zoom_0
    tile_z0_width = 180 * 111350 * cos_lat
    tile_z0_height = 180 * 111800
    
    # Find the pixel_z0 / pixel_z, write pixel_limits for pixel_z
    pixel_scale = (tile_z0_width * tile_z0_height) / (s_comp * s_comp * pixels_limit)
    logger.info(f"CRS84 pixel scale: {pixel_scale}")
    
    # Solve for z: 2**2z >= pixel_scale => z >= log(pixel_scale) / (2 * log(2))
    # If the scale is <= 1, zoom level 0 scale works.
    z_float = 0.0 
    if pixel_scale >= 1:
        z_float = math.log(pixel_scale) / (2 * math.log(2))
    
    # Find the ceiling of float z
    logger.info(f"CRS84 tile matching zoom: {z_float}")
    return math.ceil(z_float)


def get_crs84_tile_pixels(s_comp: int, latitude: float,  tile_zoom:int = 0) -> int:
    logger = logging.getLogger("main." + __name__)
    # Convert latitude to radians and get cosine
    cos_lat = math.cos(math.radians(latitude))
    
    # INPUT check for the poles (width is 0, so 0 pixels)
    if cos_lat <= 0:
        raise  ValueError(f"Bad input, latitude: {latitude}")

    # calculate CRS84 tile details 
    tile_span = 180 / (2**tile_zoom)
    tile_width = 111350 * cos_lat * tile_span
    tile_height = 111800 * tile_span
    tile_pixels = (tile_width * tile_height) / (s_comp * s_comp)
    logger.info(f"Tile width: {tile_width}, height: {tile_height}")
    return int(tile_pixels)
    

def calculate_web_map_tile_zoom(s_comp: int, latitude: float,  pixels_limit:int = 10**6) -> int:
    """
    Get the EPSG:3857 GRID tile zoom that will match the constraints of computation 
    resolution and pixels_limit. if s_comp value is low (higher resolution) then a 
    smaller area will contain more pixels.

    zoom = 0, area = c_equator x c_equator 
    zoom = z, area = c_equator x c_equator x (1/2^z) x (1/2^z)
    zoom = z, GEE pixels = c_equator x c_equator x (1/2^z) x (1/2^z) x 1/s_comp x 1/s_comp
    pixels_limit = pixels_on_side x pixels_on_side
    =>  c_equator / [2^z x s_comp] * c_equator / [2^z x s_comp] <= pixels_on_side^2
    => c_equator / [2^z x s_comp] <= pixels_on_side 
    => c_equator / [ s_comp x pixels_on_side ] <= 2^z 
    OR 2^z >= c_equator / [ s_comp x pixels_on_side ]
    OR z >= ln(c_equator / [ s_comp x pixels_on_side]) / ln(2)

    """
    logger = logging.getLogger("main." + __name__)
    # Convert latitude to radians and get cosine
    cos_lat = math.cos(math.radians(latitude))
    
    # INPUT check for the poles (width is 0, so 0 pixels)
    if cos_lat <= 0:
        raise  ValueError(f"Bad input, latitude: {latitude}")

    c_equator = 40075016.68  
    num_pixel_on_side =  int(math.sqrt(pixels_limit))
    logger.info(f"number of pixels to cover tile side, {num_pixel_on_side}")

    z_float = math.log((c_equator * cos_lat) / (s_comp * num_pixel_on_side))
    z_float = z_float / math.log(2)
    logger.info(f"Matching zoom float: {z_float}")
    return math.ceil(z_float)



def get_web_map_tile_pixels(s_comp: int,  tile_zoom:int = 0) -> int:
    logger = logging.getLogger("main." + __name__)
    c_equator = 40075016.68
    tile_side_pixels = int(c_equator / (s_comp * (2** tile_zoom)))
    logger.info(f"pixels on one tile side: {tile_side_pixels}")
    return tile_side_pixels * tile_side_pixels



def plot_geotiff_ndvi(
    filepath: Union[str, Path],
    save_png: bool = True,
    show_plot: bool = True
) -> Optional[Path]:
    """
    Public utility method to visualize and plot a GeoTIFF raster file.
    Plots spatial NDVI heatmap with Lat/Lon extent axes and pixel value distribution histogram.
    """
    logger = logging.getLogger("main." + __name__)
    target_path = Path(filepath)
    if not target_path.exists() or not target_path.is_file():
        raise FileNotFoundError(f"Target GeoTIFF raster missing: {target_path}")

    try:
        import rasterio
        import matplotlib
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as err:
        logger.error(f"Plotting dependencies missing: {err}. Install via `pip install rasterio matplotlib numpy`.")
        raise ImportError("Missing dependencies for plot_geotiff_ndvi: rasterio, matplotlib, numpy") from err

    logger.info(f"Plotting raster: {target_path.name}")
    with rasterio.open(target_path) as src:
        ndvi_data = src.read(1)
        bounds = src.bounds
        extent = [bounds.left, bounds.right, bounds.bottom, bounds.top]

        mask = src.dataset_mask()
        ndvi_valid = np.where((mask > 0) & ~np.isnan(ndvi_data), ndvi_data, np.nan)

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6), gridspec_kw={"width_ratios": [1.3, 1]})

        try:
            cmap = matplotlib.colormaps["RdYlGn"].copy()
        except (AttributeError, KeyError):
            cmap = plt.cm.get_cmap("RdYlGn")
        cmap.set_bad(color="lightgray", alpha=0.5)

        im = ax1.imshow(
            ndvi_valid,
            cmap=cmap,
            extent=extent,
            vmin=-0.2,
            vmax=0.8,
            origin="upper",
        )

        ax1.set_title(f"Sentinel-2 NDVI Heatmap\n{target_path.name}", fontsize=12, fontweight="bold")
        ax1.set_xlabel("Longitude (°E)", fontsize=10)
        ax1.set_ylabel("Latitude (°N)", fontsize=10)
        ax1.grid(True, linestyle="--", alpha=0.5)

        cbar = fig.colorbar(im, ax=ax1, fraction=0.046, pad=0.04)
        cbar.set_label("NDVI Index (Normalized Difference Vegetation Index)", fontsize=10)

        valid_flat = ndvi_valid[~np.isnan(ndvi_valid)]
        if valid_flat.size > 0:
            counts, bins, patches = ax2.hist(
                valid_flat, bins=50, range=(-0.2, 0.9), color="forestgreen", edgecolor="black", alpha=0.7
            )

            bin_centers = 0.5 * (bins[:-1] + bins[1:])
            norm = plt.Normalize(-0.2, 0.8)
            for c_patch, c_val in zip(patches, bin_centers):
                c_patch.set_facecolor(cmap(norm(c_val)))

            ax2.axvline(0.0, color="blue", linestyle=":", linewidth=1.5, label="Water threshold (0.0)")
            ax2.axvline(0.2, color="orange", linestyle="--", linewidth=1.5, label="Bare soil (0.2)")
            ax2.axvline(0.5, color="green", linestyle="-.", linewidth=1.5, label="Dense vegetation (0.5+)")

            ax2.set_title("NDVI Pixel Value Distribution", fontsize=12, fontweight="bold")
            ax2.set_xlabel("NDVI Value", fontsize=10)
            ax2.set_ylabel("Pixel Count", fontsize=10)
            ax2.grid(True, linestyle="--", alpha=0.5)
            ax2.legend(loc="upper left", fontsize=9)
        else:
            ax2.text(0.5, 0.5, "No Valid Pixels", ha="center", va="center")

        plt.tight_layout()

        out_png = None
        if save_png:
            out_png = target_path.with_suffix(".png")
            plt.savefig(out_png, dpi=200, bbox_inches="tight")
            logger.info(f"Saved visualization PNG → {out_png}")

        if show_plot:
            plt.show()

        return out_png



def verify_geotiff_raster(
    filepath: Union[str, Path]
) -> dict[str, Any]:
    """
    Public utility method to inspect and verify a GeoTIFF raster file.
    Validates file integrity, spatial dimensions, CRS, GeoTransform bounds,
    valid pixel coverage percentage, and NDVI range [-1.0, +1.0].
    """
    logger = logging.getLogger("main." + __name__)
    target_path = Path(filepath)
    if not target_path.exists() or not target_path.is_file():
        raise FileNotFoundError(f"Target GeoTIFF raster missing: {target_path}")

    try:
        import rasterio
        import numpy as np
        HAS_RASTERIO = True
    except ImportError:
        HAS_RASTERIO = False

    try:
        from osgeo import gdal
        HAS_GDAL = True
    except ImportError:
        HAS_GDAL = False

    if not HAS_RASTERIO and not HAS_GDAL:
        raise ImportError("Neither 'rasterio' nor 'gdal' is installed. Install via `pip install rasterio`.")

    logger.info(f"Verifying raster: {target_path.name}")

    if HAS_RASTERIO:
        with rasterio.open(target_path) as src:
            band1 = src.read(1)
            valid_mask = ~src.dataset_mask() == 0
            valid_pixels = band1[valid_mask & ~np.isnan(band1)]

            min_val = round(float(np.nanmin(valid_pixels)), 4) if valid_pixels.size > 0 else None
            max_val = round(float(np.nanmax(valid_pixels)), 4) if valid_pixels.size > 0 else None
            mean_val = round(float(np.nanmean(valid_pixels)), 4) if valid_pixels.size > 0 else None

            is_valid_ndvi = (
                min_val is not None
                and max_val is not None
                and -1.0 <= min_val <= 1.0
                and -1.0 <= max_val <= 1.0
                and valid_pixels.size > 0
            )

            stats = {
                "file": target_path.name,
                "file_size_kb": round(target_path.stat().st_size / 1024, 2),
                "driver": src.driver,
                "width": src.width,
                "height": src.height,
                "bands": src.count,
                "crs": str(src.crs),
                "dtype": str(src.dtypes[0]),
                "bounds": {
                    "left": round(src.bounds.left, 6),
                    "bottom": round(src.bounds.bottom, 6),
                    "right": round(src.bounds.right, 6),
                    "top": round(src.bounds.top, 6),
                },
                "valid_pixel_count": int(valid_pixels.size),
                "total_pixel_count": int(band1.size),
                "valid_pixel_percentage": round(float(valid_pixels.size / band1.size * 100), 2) if band1.size > 0 else 0.0,
                "min_val": min_val,
                "max_val": max_val,
                "mean_val": mean_val,
                "is_valid_ndvi": is_valid_ndvi,
            }
            logger.info(f"Raster verification for {target_path.name}: NDVI valid={is_valid_ndvi}, bounds={stats['bounds']}, min={min_val}, max={max_val}")
            return stats
    else:
        ds = gdal.Open(str(target_path))
        band = ds.GetRasterBand(1)
        g_stats = band.ComputeStatistics(False)
        width, height = ds.RasterXSize, ds.RasterYSize
        gt = ds.GetGeoTransform()
        is_valid_ndvi = (-1.0 <= g_stats[0] <= 1.0) and (-1.0 <= g_stats[1] <= 1.0)

        return {
            "file": target_path.name,
            "file_size_kb": round(target_path.stat().st_size / 1024, 2),
            "width": width,
            "height": height,
            "bands": ds.RasterCount,
            "crs": ds.GetProjectionRef()[:50] + "...",
            "min_val": round(g_stats[0], 4),
            "max_val": round(g_stats[1], 4),
            "mean_val": round(g_stats[2], 4),
            "bounds": {
                "left": round(gt[0], 6),
                "top": round(gt[3], 6),
                "right": round(gt[0] + width * gt[1], 6),
                "bottom": round(gt[3] + height * gt[5], 6),
            },
            "is_valid_ndvi": is_valid_ndvi,
        }


def compute_multiscale_raster_stats(
    filepath: Union[str, Path],
    target_scales_meters: Optional[list[int]] = None,
    base_scale_meters: float = 10.0,
    ndvi_threshold: float = 0.4,
) -> dict[str, Any]:
    """
    Public SDK method: Computes multi-scale raster statistics 100% locally with 0 GEECU cost
    and 0 GEE reprojection overhead.

    Performs local spatial downsampling (e.g., 10m -> 30m -> 100m -> 250m -> 1000m) using rasterio block
    averaging and calculates latitude-adjusted physical pixel areas (m² & hectares).
    """
    logger = logging.getLogger("main." + __name__)
    target_path = Path(filepath)
    if not target_path.exists() or not target_path.is_file():
        raise FileNotFoundError(f"Target GeoTIFF raster missing: {target_path}")

    if target_scales_meters is None:
        target_scales_meters = [10, 30, 100, 250, 1000]

    try:
        import rasterio
        from rasterio.enums import Resampling
        import numpy as np
    except ImportError as err:
        logger.error(f"Missing rasterio/numpy dependencies: {err}")
        raise ImportError("Missing dependencies for compute_multiscale_raster_stats: rasterio, numpy") from err

    logger.info(f"Computing local multi-scale stats for {target_path.name} at scales: {target_scales_meters}")

    scale_results = {}
    with rasterio.open(target_path) as src:
        bounds = src.bounds
        center_lat = (bounds.bottom + bounds.top) / 2.0

        for target_scale in target_scales_meters:
            scale_factor = base_scale_meters / target_scale
            out_width = max(1, int(src.width * scale_factor))
            out_height = max(1, int(src.height * scale_factor))

            # Resample band 1 using area-average for continuous NDVI data
            data = src.read(
                1,
                out_shape=(out_height, out_width),
                resampling=Resampling.average if scale_factor < 1.0 else Resampling.bilinear,
            )

            # Mask invalid / NoData pixels
            mask = src.dataset_mask()
            resampled_mask = src.read_masks(1, out_shape=(out_height, out_width))
            valid_pixels = data[(resampled_mask > 0) & ~np.isnan(data)]

            # Physical pixel area calculation adjusted for centroid latitude in EPSG:4326
            pixel_deg_x = (bounds.right - bounds.left) / out_width
            pixel_deg_y = (bounds.top - bounds.bottom) / out_height
            meters_x = pixel_deg_x * 111350 * math.cos(math.radians(center_lat))
            meters_y = pixel_deg_y * 111800
            pixel_area_m2 = abs(meters_x * meters_y)

            # Calculate healthy vegetation area above threshold
            healthy_veg_pixels = valid_pixels[valid_pixels >= ndvi_threshold]
            total_veg_area_m2 = healthy_veg_pixels.size * pixel_area_m2
            total_veg_hectares = total_veg_area_m2 / 10000.0

            scale_results[f"{target_scale}m"] = {
                "target_scale_meters": target_scale,
                "grid_dimensions": f"{out_width}x{out_height}",
                "effective_pixel_area_m2": round(pixel_area_m2, 2),
                "total_valid_pixels": int(valid_pixels.size),
                "mean_ndvi": round(float(np.nanmean(valid_pixels)), 4) if valid_pixels.size > 0 else None,
                "min_ndvi": round(float(np.nanmin(valid_pixels)), 4) if valid_pixels.size > 0 else None,
                "max_ndvi": round(float(np.nanmax(valid_pixels)), 4) if valid_pixels.size > 0 else None,
                "healthy_veg_pixel_count": int(healthy_veg_pixels.size),
                "healthy_veg_area_hectares": round(total_veg_hectares, 2),
            }

    return {
        "file": target_path.name,
        "centroid_latitude": round(center_lat, 4),
        "base_scale_meters": base_scale_meters,
        "ndvi_threshold": ndvi_threshold,
        "multiscale_statistics": scale_results,
    }