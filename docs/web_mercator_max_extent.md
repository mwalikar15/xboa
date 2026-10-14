# Web Mercator (`EPSG:3857`) `MAX_EXTENT` Derivation & Zoom Math

> **Document Purpose**: Detailed step-by-step mathematical explanation of the constant `MAX_EXTENT = 20037508.342789244` used in `calculate_web_map_tile_zoom()` inside [`softmaxx.geo.tiles`](file:///home/manju/code/github/xboa/src/lib/softmaxx/geo/tiles.py).

---

## 1. Overview & WGS84 Reference Sphere

In GIS standards (OpenStreetMap, Google Maps, Mapbox, Leaflet), **Web Mercator (`EPSG:3857`)** projects geographic coordinates (longitude and latitude in degrees) onto a flat 2D Cartesian plane measured in physical meters.

The standard spherical Earth radius ($R$) defined by the WGS84 semi-major axis is:

$$
R = 6,378,137.0 \text{ meters}
$$

---

## 2. Derivation of Earth's Circumference ($C$)

The total circumference of the Earth around the Equator ($360^\circ$ arc) is calculated using the circle circumference formula $C = 2\pi R$:

$$
C = 2 \times \pi \times 6,378,137.0 = 40,075,016.685578488 \text{ meters}
$$

---

## 3. Derivation of `MAX_EXTENT`

In the `EPSG:3857` coordinate system:
- The origin $(X=0, Y=0)$ is situated at the intersection of the **Prime Meridian ($0^\circ$ Longitude)** and the **Equator ($0^\circ$ Latitude)**.
- Longitude extends from $-180^\circ$ (West boundary) to $+180^\circ$ (East boundary).

The maximum metric distance from the origin $(0,0)$ to the $+180^\circ$ East boundary (or $-180^\circ$ West boundary) is exactly **half of the Earth's equatorial circumference**:

$$
\text{MAX\_EXTENT} = \frac{C}{2} = \pi \times R = \pi \times 6,378,137.0 = 20,037,508.342789244 \text{ meters}
$$

---

## 4. Why Web Mercator Forms a Square Bounding Box

Standard Web Mercator map tiling requires a square grid layout where total width equals total height:

- **X Axis (Longitude)**: $[-\text{MAX\_EXTENT}, +\text{MAX\_EXTENT}] \implies \text{Total Width} = 2 \times \text{MAX\_EXTENT} = 40,075,016.68557849 \text{ m}$
- **Y Axis (Latitude)**: $[-\text{MAX\_EXTENT}, +\text{MAX\_EXTENT}] \implies \text{Total Height} = 2 \times \text{MAX\_EXTENT} = 40,075,016.68557849 \text{ m}$

To maintain a 1:1 square aspect ratio, latitude ($\phi$) is cut off where the Mercator projection equation $Y = R \cdot \ln\left(\tan\left(\frac{\pi}{4} + \frac{\phi}{2}\right)\right)$ reaches $\pm\text{MAX\_EXTENT}$:

$$
\phi = 2 \cdot \arctan\left(e^\pi\right) - \frac{\pi}{2} \approx \pm 85.05112878^\circ
$$

---

## 5. Application in `calculate_web_map_tile_zoom()`

In [`softmaxx.geo.tiles.calculate_web_map_tile_zoom()`](file:///home/manju/code/github/xboa/src/lib/softmaxx/geo/tiles.py#L554-L571):

```python
MAX_EXTENT = 20037508.342789244
initial_resolution = MAX_EXTENT * 2.0         # 40,075,016.68557849 meters (World Width/Height)
tile_z0_area = initial_resolution ** 2        # ~1.606e15 m² (Total World Area at Zoom 0)
```

### Step 1: Tile Area Breakdown at Zoom Level $Z$
At zoom level $Z$, the world grid splits into $2^Z \times 2^Z = 2^{2Z}$ square tiles. The physical surface area covered by a single tile is:

$$
\text{tile\_area}(Z) = \frac{\text{tile\_z0\_area}}{2^{2Z}}
$$

### Step 2: Pixel Count Constraint
For a target spatial resolution $s_{\text{comp}}$ (in meters per pixel) and maximum pixel limit per tile $\text{pixels\_limit}$ (e.g., $10^6$ pixels), the total pixel count per tile is constrained by:

$$
\frac{\text{tile\_area}(Z)}{s_{\text{comp}}^2} \le \text{pixels\_limit}
$$

Substituting $\text{tile\_area}(Z)$:

$$
\frac{\text{tile\_z0\_area}}{2^{2Z} \cdot s_{\text{comp}}^2} \le \text{pixels\_limit} \implies 2^{2Z} \ge \frac{\text{tile\_z0\_area}}{s_{\text{comp}}^2 \cdot \text{pixels\_limit}}
$$

### Step 3: Logarithmic Solution
Defining $\text{pixel\_scale}$:

$$
\text{pixel\_scale} = \frac{\text{tile\_z0\_area}}{s_{\text{comp}}^2 \cdot \text{pixels\_limit}}
$$

Taking base-2 logarithms to solve for $Z$:

$$
2Z \ge \log_2(\text{pixel\_scale}) \implies Z \ge \frac{\log_2(\text{pixel\_scale})}{2}
$$

Rounding up ($\lceil Z \rceil$) via `math.ceil(Z)` yields the optimal integer zoom level that satisfies the pixel count limit.
