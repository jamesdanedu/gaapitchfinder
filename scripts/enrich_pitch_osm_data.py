#!/usr/bin/env python3
"""
Enrich GAA pitch centroid data with polygon geometry from OpenStreetMap.

Queries the Overpass API at the county level (one batch query per county)
to minimise API calls. Extracts polygon corners, calculates pitch
dimensions and orientation. Supports checkpoint/resume and endpoint
rotation with conservative rate limiting.

Usage:
    python enrich_pitch_osm_data.py [CSV_PATH] [--county COUNTY]

Examples:
    python enrich_pitch_osm_data.py                          # process all
    python enrich_pitch_osm_data.py --county Leitrim         # one county
    python enrich_pitch_osm_data.py data.csv --county Cork   # custom CSV
"""

import argparse
import json
import math
import os
import random
import sys
import time

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT_CSV = os.path.join(SCRIPT_DIR, "..", "gaapitchfinder_data.csv")
DEFAULT_OUTPUT_CSV = os.path.join(SCRIPT_DIR, "..", "gaapitchfinder_data_with_geometry.csv")
CHECKPOINT_FILE = os.path.join(SCRIPT_DIR, "..", ".osm_enrichment_checkpoint.json")

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OVERPASS_STATUS_URL = "https://overpass-api.de/api/status"

SEARCH_RADIUS_M = 200
REQUEST_TIMEOUT_S = 60
MAX_RETRIES_PER_COUNTY = 3

# Delays
BASE_DELAY_MIN_S = 3.0
BASE_DELAY_MAX_S = 5.0
COUNTY_PAUSE_S = 10.0
RATE_LIMIT_WAIT_S = 30.0
CONNECTION_ERROR_WAIT_S = 15.0
STATUS_POLL_INTERVAL_S = 30.0

# New columns to add
NEW_COLUMNS = [
    "osm_way_id",
    "corner_nw_lat", "corner_nw_lon",
    "corner_ne_lat", "corner_ne_lon",
    "corner_se_lat", "corner_se_lon",
    "corner_sw_lat", "corner_sw_lon",
    "pitch_length_m", "pitch_width_m",
    "orientation_degrees",
    "geometry_source", "geometry_verified",
]

# ---------------------------------------------------------------------------
# County bounding boxes: {name: (min_lat, min_lon, max_lat, max_lon)}
# Irish counties hardcoded; others computed from centroid data at runtime.
# ---------------------------------------------------------------------------
IRELAND_COUNTY_BBOXES = {
    # Republic of Ireland (26 counties)
    "Carlow":     (52.57, -7.06, 52.86, -6.49),
    "Cavan":      (53.73, -7.69, 54.15, -6.80),
    "Clare":      (52.56, -10.00, 53.00, -8.30),
    "Cork":       (51.42, -10.26, 52.18, -7.73),
    "Donegal":    (54.18, -8.68, 55.43, -7.18),
    "Dublin":     (53.22, -6.60, 53.52, -6.01),
    "Galway":     (53.00, -10.70, 53.63, -7.95),
    "Kerry":      (51.57, -10.65, 52.42, -9.33),
    "Kildare":    (52.98, -7.08, 53.42, -6.45),
    "Kilkenny":   (52.30, -7.65, 52.80, -6.90),
    "Laois":      (52.78, -7.92, 53.13, -7.15),
    "Leitrim":    (53.82, -8.35, 54.47, -7.58),
    "Limerick":   (52.33, -9.52, 52.79, -8.18),
    "Longford":   (53.55, -8.06, 53.82, -7.40),
    "Louth":      (53.70, -6.65, 54.10, -6.10),
    "Mayo":       (53.42, -10.55, 54.10, -9.00),
    "Meath":      (53.35, -7.30, 53.80, -6.25),
    "Monaghan":   (53.90, -7.32, 54.35, -6.56),
    "Offaly":     (52.92, -8.10, 53.40, -7.18),
    "Roscommon":  (53.42, -8.80, 54.00, -7.82),
    "Sligo":      (53.85, -8.95, 54.40, -8.10),
    "Tipperary":  (52.22, -8.48, 52.96, -7.38),
    "Waterford":  (51.93, -7.88, 52.35, -6.93),
    "Westmeath":  (53.35, -7.98, 53.72, -7.08),
    "Wexford":    (52.17, -7.00, 52.65, -6.15),
    "Wicklow":    (52.78, -6.65, 53.12, -6.02),
    # Northern Ireland (6 counties)
    "Antrim":     (54.40, -6.58, 55.25, -5.65),
    "Armagh":     (54.07, -6.98, 54.47, -6.28),
    "Derry":      (54.62, -7.55, 55.20, -6.50),
    "Down":       (54.05, -6.35, 54.62, -5.43),
    "Fermanagh":  (54.07, -8.18, 54.52, -7.25),
    "Tyrone":     (54.22, -7.65, 54.82, -6.68),
}

# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2):
    """Return distance in metres between two lat/lon points."""
    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing(lat1, lon1, lat2, lon2):
    """Return initial bearing in degrees from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    x = math.sin(dlam) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def oriented_bounding_box(coords):
    """
    Compute the minimum-area oriented bounding box for a set of 2-D points.

    Uses rotating calipers on the convex hull. Returns the 4 corner coords
    (as lat/lon pairs) of the OBB and the rotation angle.
    """
    pts = np.array(coords)

    # Approximate metres so the OBB works in metric space
    lat_center = pts[:, 0].mean()
    lon_scale = math.cos(math.radians(lat_center))
    scaled = np.column_stack([
        pts[:, 0] * 111_320,            # lat -> m (approx)
        pts[:, 1] * 111_320 * lon_scale  # lon -> m (approx)
    ])

    from scipy.spatial import ConvexHull
    try:
        hull = ConvexHull(scaled)
    except Exception:
        return None
    hull_pts = scaled[hull.vertices]

    best_area = float("inf")
    best_box = None
    best_angle = 0

    edges = np.diff(np.vstack([hull_pts, hull_pts[0:1]]), axis=0)
    for edge in edges:
        angle = math.atan2(edge[1], edge[0])
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        rot = np.array([[cos_a, sin_a], [-sin_a, cos_a]])
        rotated = hull_pts @ rot.T
        min_xy = rotated.min(axis=0)
        max_xy = rotated.max(axis=0)
        area = (max_xy[0] - min_xy[0]) * (max_xy[1] - min_xy[1])
        if area < best_area:
            best_area = area
            best_angle = angle
            corners_rot = np.array([
                [min_xy[0], min_xy[1]],
                [max_xy[0], min_xy[1]],
                [max_xy[0], max_xy[1]],
                [min_xy[0], max_xy[1]],
            ])
            inv_rot = np.array([[cos_a, -sin_a], [sin_a, cos_a]])
            best_box = corners_rot @ inv_rot.T

    if best_box is None:
        return None

    box_latlon = np.column_stack([
        best_box[:, 0] / 111_320,
        best_box[:, 1] / (111_320 * lon_scale),
    ])

    return box_latlon, best_angle


def classify_corners(box_latlon):
    """
    Given 4 lat/lon corners, label them NW / NE / SE / SW.
    Returns dict with keys nw, ne, se, sw, each a (lat, lon) tuple.
    """
    pts = list(map(tuple, box_latlon))
    north = sorted(pts, key=lambda p: -p[0])[:2]
    south = sorted(pts, key=lambda p: p[0])[:2]
    nw = min(north, key=lambda p: p[1])
    ne = max(north, key=lambda p: p[1])
    sw = min(south, key=lambda p: p[1])
    se = max(south, key=lambda p: p[1])
    return {"nw": nw, "ne": ne, "se": se, "sw": sw}


def compute_pitch_metrics(corners):
    """Return (length_m, width_m, orientation_degrees)."""
    nw, ne, se, sw = corners["nw"], corners["ne"], corners["se"], corners["sw"]
    side_north = haversine(*nw, *ne)
    side_east = haversine(*ne, *se)

    length = max(side_north, side_east)
    width = min(side_north, side_east)

    if side_north >= side_east:
        orient = bearing(*nw, *ne)
    else:
        orient = bearing(*ne, *se)

    return round(length, 1), round(width, 1), round(orient, 1)


def bbox_corners(coords):
    """Axis-aligned bounding box from a list of (lat, lon) coords."""
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return [
        (max(lats), min(lons)),  # NW
        (max(lats), max(lons)),  # NE
        (min(lats), max(lons)),  # SE
        (min(lats), min(lons)),  # SW
    ]


def extract_geometry(way, node_lookup):
    """
    Extract corner coordinates from a way. Returns (corners_dict, source)
    where source is 'osm_polygon' (OBB from polygon nodes) or 'osm_bbox'.
    """
    coords = [(node_lookup[nid][0], node_lookup[nid][1])
              for nid in way["nodes"] if nid in node_lookup]

    if len(coords) < 3:
        return None, "not_found"

    try:
        result = oriented_bounding_box(coords)
        if result is not None:
            box_latlon, _ = result
            corners = classify_corners(box_latlon)
            return corners, "osm_polygon"
    except Exception:
        pass

    bbox = bbox_corners(coords)
    box_arr = np.array(bbox)
    corners = classify_corners(box_arr)
    return corners, "osm_bbox"

# ---------------------------------------------------------------------------
# Overpass API helpers
# ---------------------------------------------------------------------------
class OverpassClient:
    """Manages endpoint rotation, status checking, and rate-limit-aware queries."""

    def __init__(self):
        self._endpoint_idx = 0
        self._consecutive_errors = 0

    @property
    def current_endpoint(self):
        return OVERPASS_ENDPOINTS[self._endpoint_idx % len(OVERPASS_ENDPOINTS)]

    def rotate_endpoint(self):
        """Switch to the next endpoint in the list."""
        old = self.current_endpoint
        self._endpoint_idx += 1
        new = self.current_endpoint
        print(f"    Rotating endpoint: {old.split('//')[1].split('/')[0]}"
              f" -> {new.split('//')[1].split('/')[0]}")

    def polite_delay(self):
        """Wait a randomised 3-5 seconds after a successful request."""
        delay = random.uniform(BASE_DELAY_MIN_S, BASE_DELAY_MAX_S)
        time.sleep(delay)

    def check_status(self):
        """
        Check overpass-api.de status. If 0 slots available, poll until
        a slot opens. Silently returns if status endpoint is unreachable.
        """
        print("  Checking Overpass API status …", end=" ", flush=True)
        try:
            resp = requests.get(OVERPASS_STATUS_URL, timeout=10)
            if resp.status_code != 200:
                print("could not reach status endpoint, proceeding.")
                return
            text = resp.text
            # Parse "Rate limit: N" and "N slots available"
            slots_available = None
            for line in text.splitlines():
                if "slots available" in line.lower():
                    parts = line.strip().split()
                    for part in parts:
                        if part.isdigit():
                            slots_available = int(part)
                            break
            if slots_available is not None and slots_available > 0:
                print(f"{slots_available} slot(s) available. OK.")
                return
            elif slots_available == 0:
                print(f"0 slots available. Waiting …")
                while True:
                    time.sleep(STATUS_POLL_INTERVAL_S)
                    try:
                        resp2 = requests.get(OVERPASS_STATUS_URL, timeout=10)
                        for line in resp2.text.splitlines():
                            if "slots available" in line.lower():
                                parts = line.strip().split()
                                for part in parts:
                                    if part.isdigit():
                                        slots_available = int(part)
                                        break
                        if slots_available and slots_available > 0:
                            print(f"  {slots_available} slot(s) now available. Proceeding.")
                            return
                        print(f"  Still 0 slots. Waiting {STATUS_POLL_INTERVAL_S}s …")
                    except Exception:
                        print("  Status check failed, proceeding anyway.")
                        return
            else:
                print("OK (could not parse slot count, proceeding).")
        except Exception:
            print("could not reach status endpoint, proceeding.")

    def query_county(self, bbox):
        """
        Query all pitches within a county bounding box. Returns elements
        list or None after MAX_RETRIES_PER_COUNTY failures.

        bbox: (min_lat, min_lon, max_lat, max_lon)
        """
        min_lat, min_lon, max_lat, max_lon = bbox
        query = f"""
[out:json][timeout:120];
(
  way["sport"="gaelic_football"]({min_lat},{min_lon},{max_lat},{max_lon});
  way["sport"="hurling"]({min_lat},{min_lon},{max_lat},{max_lon});
  way["sport"="gaelic_games"]({min_lat},{min_lon},{max_lat},{max_lon});
  way["leisure"="pitch"]({min_lat},{min_lon},{max_lat},{max_lon});
  relation["sport"="gaelic_football"]({min_lat},{min_lon},{max_lat},{max_lon});
  relation["sport"="hurling"]({min_lat},{min_lon},{max_lat},{max_lon});
  relation["sport"="gaelic_games"]({min_lat},{min_lon},{max_lat},{max_lon});
  relation["leisure"="pitch"]({min_lat},{min_lon},{max_lat},{max_lon});
);
out body;
>;
out skel qt;
"""
        for attempt in range(MAX_RETRIES_PER_COUNTY):
            endpoint = self.current_endpoint
            try:
                print(f"    Querying {endpoint.split('//')[1].split('/')[0]} "
                      f"(attempt {attempt + 1}/{MAX_RETRIES_PER_COUNTY}) …",
                      end=" ", flush=True)
                resp = requests.post(
                    endpoint,
                    data={"data": query},
                    timeout=REQUEST_TIMEOUT_S,
                )

                if resp.status_code == 429:
                    print(f"RATE LIMITED. Waiting {RATE_LIMIT_WAIT_S}s, rotating …")
                    time.sleep(RATE_LIMIT_WAIT_S)
                    self.rotate_endpoint()
                    self._consecutive_errors += 1
                    continue

                if resp.status_code in (504, 502, 503):
                    print(f"HTTP {resp.status_code}. Waiting {RATE_LIMIT_WAIT_S}s, rotating …")
                    time.sleep(RATE_LIMIT_WAIT_S)
                    self.rotate_endpoint()
                    self._consecutive_errors += 1
                    continue

                resp.raise_for_status()
                elements = resp.json().get("elements", [])
                self._consecutive_errors = 0
                print(f"OK ({len(elements)} elements)")
                return elements

            except requests.exceptions.Timeout:
                print(f"TIMEOUT. Waiting {RATE_LIMIT_WAIT_S}s, rotating …")
                time.sleep(RATE_LIMIT_WAIT_S)
                self.rotate_endpoint()
                self._consecutive_errors += 1

            except requests.exceptions.ConnectionError:
                print(f"CONNECTION ERROR. Waiting {CONNECTION_ERROR_WAIT_S}s …")
                time.sleep(CONNECTION_ERROR_WAIT_S)
                self._consecutive_errors += 1

            except requests.exceptions.RequestException as exc:
                print(f"ERROR: {exc}. Waiting {CONNECTION_ERROR_WAIT_S}s …")
                time.sleep(CONNECTION_ERROR_WAIT_S)
                self._consecutive_errors += 1

            # Progressive back-off if many consecutive errors
            if self._consecutive_errors >= 3:
                extra_wait = min(60, self._consecutive_errors * 10)
                print(f"    {self._consecutive_errors} consecutive errors. "
                      f"Extra wait: {extra_wait}s")
                time.sleep(extra_wait)

        return None

# ---------------------------------------------------------------------------
# Local matching: match centroids to OSM polygons within a county batch
# ---------------------------------------------------------------------------
def parse_osm_elements(elements):
    """
    Parse Overpass response into node_lookup and ways list.
    Returns (node_lookup, ways) where node_lookup = {id: (lat, lon)}.
    """
    node_lookup = {}
    ways = []
    for el in elements:
        if el["type"] == "node":
            node_lookup[el["id"]] = (el["lat"], el["lon"])
        elif el["type"] == "way" and "nodes" in el:
            ways.append(el)
    return node_lookup, ways


def compute_way_centroid(way, node_lookup):
    """Return (lat, lon) centroid of a way's nodes, or None."""
    coords = [node_lookup[nid] for nid in way["nodes"] if nid in node_lookup]
    if not coords:
        return None
    avg_lat = sum(c[0] for c in coords) / len(coords)
    avg_lon = sum(c[1] for c in coords) / len(coords)
    return avg_lat, avg_lon


def find_best_match(centroid_lat, centroid_lon, ways, node_lookup):
    """
    Find the best matching OSM way for a given centroid.
    Prefers GAA-specific sport tags, then nearest by distance.
    Only considers ways within SEARCH_RADIUS_M.
    Returns (way, node_lookup) or (None, None).
    """
    gaa_tags = {"gaelic_football", "hurling", "gaelic_games"}
    candidates = []

    for way in ways:
        wc = compute_way_centroid(way, node_lookup)
        if wc is None:
            continue
        dist = haversine(centroid_lat, centroid_lon, wc[0], wc[1])
        if dist > SEARCH_RADIUS_M:
            continue
        tags = way.get("tags", {})
        sport = tags.get("sport", "")
        is_gaa = sport in gaa_tags
        # Score: GAA-specific first (0), then generic (1), then by distance
        candidates.append((0 if is_gaa else 1, dist, way))

    if not candidates:
        return None, None

    candidates.sort()
    return candidates[0][2], node_lookup

# ---------------------------------------------------------------------------
# Checkpoint logic
# ---------------------------------------------------------------------------
def load_checkpoint():
    """Load checkpoint data: processed indices and completed counties."""
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r") as f:
            data = json.load(f)
            return {
                "processed_indices": set(data.get("processed_indices", [])),
                "completed_counties": set(data.get("completed_counties", [])),
            }
    return {"processed_indices": set(), "completed_counties": set()}


def save_checkpoint(processed_indices, completed_counties):
    """Persist checkpoint data."""
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({
            "processed_indices": sorted(processed_indices),
            "completed_counties": sorted(completed_counties),
        }, f)


def get_county_bbox(county_name, df_county):
    """
    Get bounding box for a county. Uses hardcoded Irish county boxes
    when available, otherwise computes from the centroid data with padding.
    Returns (min_lat, min_lon, max_lat, max_lon).
    """
    if county_name in IRELAND_COUNTY_BBOXES:
        return IRELAND_COUNTY_BBOXES[county_name]

    # Compute from data with 0.05 degree (~5km) padding
    valid = df_county.dropna(subset=["Latitude", "Longitude"])
    if valid.empty:
        return None
    min_lat = valid["Latitude"].min() - 0.05
    max_lat = valid["Latitude"].max() + 0.05
    min_lon = valid["Longitude"].min() - 0.05
    max_lon = valid["Longitude"].max() + 0.05
    return (min_lat, min_lon, max_lat, max_lon)

# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------
def process_county(county_name, df_county, df_out, processed, node_lookup, ways, stats):
    """
    Match each pitch in a county to the best OSM polygon from the
    pre-fetched county-level batch. Updates df_out and processed in place.
    """
    for _, row in df_county.iterrows():
        idx = row.name  # original dataframe index
        if idx in processed:
            continue

        club = row.get("Club", "")
        lat = row.get("Latitude")
        lon = row.get("Longitude")

        if pd.isna(lat) or pd.isna(lon):
            df_out.at[idx, "geometry_source"] = "not_found"
            df_out.at[idx, "geometry_verified"] = False
            processed.add(idx)
            stats["not_found"] += 1
            print(f"    {club}: SKIP (no coordinates)")
            continue

        lat, lon = float(lat), float(lon)
        way, nl = find_best_match(lat, lon, ways, node_lookup)

        if way is None:
            df_out.at[idx, "geometry_source"] = "not_found"
            df_out.at[idx, "geometry_verified"] = False
            processed.add(idx)
            stats["not_found"] += 1
            print(f"    {club}: not found")
        else:
            corners, source = extract_geometry(way, nl)
            if corners is None:
                df_out.at[idx, "geometry_source"] = "not_found"
                df_out.at[idx, "geometry_verified"] = False
                processed.add(idx)
                stats["not_found"] += 1
                print(f"    {club}: not found (bad geometry)")
            else:
                length, width, orient = compute_pitch_metrics(corners)
                df_out.at[idx, "osm_way_id"] = way["id"]
                df_out.at[idx, "corner_nw_lat"] = round(corners["nw"][0], 7)
                df_out.at[idx, "corner_nw_lon"] = round(corners["nw"][1], 7)
                df_out.at[idx, "corner_ne_lat"] = round(corners["ne"][0], 7)
                df_out.at[idx, "corner_ne_lon"] = round(corners["ne"][1], 7)
                df_out.at[idx, "corner_se_lat"] = round(corners["se"][0], 7)
                df_out.at[idx, "corner_se_lon"] = round(corners["se"][1], 7)
                df_out.at[idx, "corner_sw_lat"] = round(corners["sw"][0], 7)
                df_out.at[idx, "corner_sw_lon"] = round(corners["sw"][1], 7)
                df_out.at[idx, "pitch_length_m"] = length
                df_out.at[idx, "pitch_width_m"] = width
                df_out.at[idx, "orientation_degrees"] = orient
                df_out.at[idx, "geometry_source"] = source
                df_out.at[idx, "geometry_verified"] = False
                processed.add(idx)
                stats["matched"] += 1
                print(f"    {club}: OK ({source}, {length}x{width}m, {orient}deg)")


def main():
    # Ensure scipy is available
    try:
        from scipy.spatial import ConvexHull  # noqa
    except ImportError:
        print("Installing scipy ...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "scipy", "-q"])

    # Parse arguments
    parser = argparse.ArgumentParser(
        description="Enrich GAA pitch data with OSM polygon geometry."
    )
    parser.add_argument(
        "csv_path", nargs="?", default=DEFAULT_INPUT_CSV,
        help="Path to input CSV (default: gaapitchfinder_data.csv)"
    )
    parser.add_argument(
        "--county", type=str, default=None,
        help="Process only this county (e.g. --county Leitrim)"
    )
    args = parser.parse_args()

    input_csv = args.csv_path
    output_csv = DEFAULT_OUTPUT_CSV

    print("Loading input data ...")
    df = pd.read_csv(input_csv)
    total = len(df)
    print(f"  {total} pitches loaded.")

    # Load or initialise output dataframe
    if os.path.exists(output_csv):
        df_out = pd.read_csv(output_csv)
        for col in NEW_COLUMNS:
            if col not in df_out.columns:
                df_out[col] = ""
        print(f"  Resuming from existing output ({len(df_out)} rows).")
    else:
        df_out = df.copy()
        for col in NEW_COLUMNS:
            df_out[col] = ""

    # Load checkpoint
    checkpoint = load_checkpoint()
    processed = checkpoint["processed_indices"]
    completed_counties = checkpoint["completed_counties"]

    # Determine which counties to process
    all_counties = df["County"].dropna().unique().tolist()
    if args.county:
        if args.county not in all_counties:
            print(f"  ERROR: County '{args.county}' not found in data.")
            print(f"  Available counties: {', '.join(sorted(all_counties))}")
            sys.exit(1)
        counties_to_process = [args.county]
    else:
        counties_to_process = sorted(all_counties)

    # Filter out already-completed counties
    remaining_counties = [c for c in counties_to_process if c not in completed_counties]
    print(f"  {len(completed_counties)} counties already completed, "
          f"{len(remaining_counties)} remaining.")

    # Also check if remaining counties have any unprocessed rows
    final_counties = []
    for county in remaining_counties:
        county_indices = df[df["County"] == county].index.tolist()
        unprocessed = [i for i in county_indices if i not in processed]
        if unprocessed:
            final_counties.append(county)
    remaining_counties = final_counties
    print(f"  {len(remaining_counties)} counties with unprocessed rows.\n")

    stats = {"matched": 0, "not_found": 0, "api_errors": 0}

    # Count previously processed results
    for i in processed:
        if i < len(df_out):
            src = df_out.at[i, "geometry_source"]
            if src in ("osm_polygon", "osm_bbox"):
                stats["matched"] += 1
            elif src == "api_error":
                stats["api_errors"] += 1
            else:
                stats["not_found"] += 1

    # Create Overpass client and check status
    client = OverpassClient()
    if remaining_counties:
        client.check_status()

    # Process county by county
    for county_num, county_name in enumerate(remaining_counties, 1):
        df_county = df[df["County"] == county_name]
        unprocessed_count = sum(1 for i in df_county.index if i not in processed)

        print(f"\n[County {county_num}/{len(remaining_counties)}] "
              f"{county_name} ({len(df_county)} pitches, {unprocessed_count} remaining)")

        # Get bounding box
        bbox = get_county_bbox(county_name, df_county)
        if bbox is None:
            print(f"  SKIP: no valid coordinates for {county_name}")
            for idx in df_county.index:
                if idx not in processed:
                    df_out.at[idx, "geometry_source"] = "not_found"
                    df_out.at[idx, "geometry_verified"] = False
                    processed.add(idx)
                    stats["not_found"] += 1
            completed_counties.add(county_name)
            save_checkpoint(processed, completed_counties)
            df_out.to_csv(output_csv, index=False)
            continue

        print(f"  BBox: ({bbox[0]:.4f}, {bbox[1]:.4f}, {bbox[2]:.4f}, {bbox[3]:.4f})")

        # Batch query for entire county
        elements = client.query_county(bbox)

        if elements is None:
            print(f"  API FAILED for {county_name} after {MAX_RETRIES_PER_COUNTY} retries.")
            print(f"  Marking {unprocessed_count} pitches as api_error.")
            for idx in df_county.index:
                if idx not in processed:
                    df_out.at[idx, "geometry_source"] = "api_error"
                    df_out.at[idx, "geometry_verified"] = False
                    processed.add(idx)
                    stats["api_errors"] += 1
            # Do NOT add to completed_counties so it can be retried
            save_checkpoint(processed, completed_counties)
            df_out.to_csv(output_csv, index=False)
            continue

        # Parse elements
        node_lookup, ways = parse_osm_elements(elements)
        print(f"  Found {len(ways)} OSM ways, {len(node_lookup)} nodes.")

        # Match each pitch locally
        process_county(county_name, df_county, df_out, processed,
                       node_lookup, ways, stats)

        # Mark county as complete and save
        completed_counties.add(county_name)
        save_checkpoint(processed, completed_counties)
        df_out.to_csv(output_csv, index=False)
        print(f"  {county_name} complete. Saved checkpoint.")

        # Polite pause between counties
        if county_num < len(remaining_counties):
            print(f"  Pausing {COUNTY_PAUSE_S}s before next county …")
            time.sleep(COUNTY_PAUSE_S)
            # Randomised extra delay
            client.polite_delay()

    # Final save
    save_checkpoint(processed, completed_counties)
    df_out.to_csv(output_csv, index=False)

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    total_processed = stats["matched"] + stats["not_found"] + stats["api_errors"]
    match_rate = (stats["matched"] / total_processed * 100) if total_processed > 0 else 0

    print("\n" + "=" * 60)
    print("ENRICHMENT SUMMARY")
    print("=" * 60)
    print(f"  Total pitches:          {total}")
    print(f"  Processed:              {total_processed}")
    print(f"  Matched (OSM):          {stats['matched']}")
    print(f"  Not found:              {stats['not_found']}")
    print(f"  API errors:             {stats['api_errors']}")
    print(f"  Match rate:             {match_rate:.1f}%")
    print(f"  Counties completed:     {len(completed_counties)}")
    print(f"\n  Output: {os.path.abspath(output_csv)}")
    if stats["api_errors"] > 0:
        print(f"\n  NOTE: {stats['api_errors']} pitches had API errors (geometry_source='api_error').")
        print("  Re-run the script to retry them (they are NOT in completed counties).")
    print("=" * 60)


if __name__ == "__main__":
    main()
