#!/usr/bin/env python3
"""
Pune Raw Open Data Collector v2.6

Purpose:
- Download Western Zone OSM data.
- Extract Pune bbox into the processor-compatible:
    data/raw/osm/central-zone.osm.pbf
- Fully validate OSM geometry instead of stopping after the first few
  coordinates.
- Download Pune-relevant OpenCity resources while rejecting obvious
  non-Pune city-specific resources.
- Keep genuinely multi-city / all-India resources only when they are
  part of a Pune-relevant package and explicitly look multi-city.
- Download Pune DEM tiles.
- Create manifest + SHA256 checksums.
- Upload release assets without allowing one duplicate basename to abort
  the complete release.

Designed for GitHub Actions / Ubuntu.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests


# ============================================================================
# CONFIG
# ============================================================================

VERSION = "2.6"
RELEASE_TAG = "pune-raw-v1"

PUNE_BBOX = (73.70, 18.40, 74.05, 18.70)
MIN_LON, MIN_LAT, MAX_LON, MAX_LAT = PUNE_BBOX

# Margin used only for diagnostics. Geometry beyond this margin is reported
# as "far outside". This does NOT alter the actual Pune bbox.
DIAGNOSTIC_MARGIN_DEG = 0.50

OSM_SOURCE_URL = (
    "https://download.geofabrik.de/asia/india/"
    "western-zone-latest.osm.pbf"
)

OSM_SOURCE_PATH = Path("data/raw/source/western-zone-latest.osm.pbf")
PUNE_PBF_PATH = Path("data/raw/osm/central-zone.osm.pbf")

DEM_TILES = [
    "N18E073",
    "N18E074",
    "N19E073",
    "N19E074",
]

OPENCITY_BASE = "https://data.opencity.in"
OPENCITY_API = f"{OPENCITY_BASE}/api/3/action"

RAW_ROOT = Path("data/raw")
DEM_ROOT = RAW_ROOT / "dem"
OPENCITY_ROOT = RAW_ROOT / "opencity"
OPENCITY_RESOURCES_ROOT = OPENCITY_ROOT / "resources"
TMP_ROOT = Path("data/tmp")

MANIFEST_PATH = Path("manifest.json")
CHECKSUM_PATH = Path("SHA256SUMS.txt")
OPENCITY_PACKAGE_SEARCH_PATH = OPENCITY_ROOT / "package_search.json"

# Cities that must not enter the Pune-only collection merely because the
# package search happened to return them.
OTHER_CITY_TERMS = {
    "ahmedabad",
    "bengaluru",
    "bangalore",
    "bhubaneswar",
    "bhopal",
    "chandigarh",
    "chennai",
    "coimbatore",
    "delhi",
    "faridabad",
    "ghaziabad",
    "gurugram",
    "gurgaon",
    "hyderabad",
    "jaipur",
    "kanpur",
    "kochi",
    "kolkata",
    "lucknow",
    "mumbai",
    "nagpur",
    "nashik",
    "patna",
    "surat",
    "thane",
    "vadodara",
    "varanasi",
    "visakhapatnam",
}

# These indicate a resource is intentionally multi-city / national.
MULTI_CITY_TERMS = {
    "all_india",
    "all-india",
    "all india",
    "india",
    "national",
    "multi_city",
    "multi-city",
    "multi city",
    "multi_state",
    "multi-state",
    "multi state",
    "urban_india",
    "urban-india",
    "statewise",
    "state-wise",
}

PUNE_TERMS = {
    "pune",
    "pimpri",
    "pcmc",
    "pune municipal corporation",
    "pune district",
    "pune city",
    "pune metropolitan",
    "pmc",
    "pmpml",
}

USER_AGENT = (
    "pune-raw-py/2.6 "
    "(https://github.com/Jitujoshi71/pune_raw_py)"
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

REQUEST_TIMEOUT = 120
DOWNLOAD_RETRIES = 3


# ============================================================================
# LOGGING
# ============================================================================

def banner(title: str) -> None:
    print("=" * 70)
    print(title)
    print("=" * 70, flush=True)


def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"WARNING: {message}", flush=True)


def fail(message: str) -> None:
    print("=" * 70)
    print("ERROR")
    print("=" * 70)
    print(message, flush=True)
    raise RuntimeError(message)


# ============================================================================
# COMMAND EXECUTION
# ============================================================================

def run_cmd(
    cmd: list[str],
    *,
    timeout: int | None = None,
    cwd: str | Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    log("$ " + " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        fail(f"Command timed out after {timeout}s: {' '.join(cmd)}")
        raise exc

    log(f"Command exit code: {result.returncode}")

    if result.stdout.strip():
        print("STDOUT:")
        print(result.stdout.rstrip())

    if result.stderr.strip():
        print("STDERR:")
        print(result.stderr.rstrip())

    if check and result.returncode != 0:
        fail(
            f"Command failed with exit code {result.returncode}: "
            f"{' '.join(cmd)}"
        )

    return result


# ============================================================================
# DOWNLOADS
# ============================================================================

def format_bytes(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(size)

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024

    return f"{size} B"


def download_file(
    url: str,
    output: Path,
    *,
    expected_size: int | None = None,
    retries: int = DOWNLOAD_RETRIES,
) -> bool:
    output.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, retries + 1):
        log(f"Attempt {attempt}/{retries}")
        log(f"URL: {url}")
        log(f"OUT: {output}")

        temp = output.with_suffix(output.suffix + ".part")

        try:
            with SESSION.get(
                url,
                stream=True,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            ) as response:
                response.raise_for_status()

                total = int(response.headers.get("content-length", "0") or 0)
                if expected_size:
                    total = expected_size

                downloaded = 0
                started = time.time()

                with temp.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        downloaded += len(chunk)

                        if downloaded % (25 * 1024 * 1024) < len(chunk):
                            elapsed = max(time.time() - started, 0.001)
                            rate = downloaded / elapsed
                            if total:
                                pct = downloaded * 100 / total
                                log(
                                    f"  {pct:6.2f}% "
                                    f"{format_bytes(downloaded)} / "
                                    f"{format_bytes(total)} "
                                    f"@ {format_bytes(int(rate))}/s"
                                )
                            else:
                                log(
                                    f"  {format_bytes(downloaded)} "
                                    f"@ {format_bytes(int(rate))}/s"
                                )

            if downloaded <= 0:
                raise RuntimeError("Downloaded file is empty")

            if expected_size and downloaded != expected_size:
                raise RuntimeError(
                    f"Size mismatch: expected {expected_size}, "
                    f"got {downloaded}"
                )

            temp.replace(output)

            log("DOWNLOAD SUCCESS")
            log(f"Size: {format_bytes(output.stat().st_size)}")
            return True

        except Exception as exc:
            warn(f"Download attempt failed: {exc}")
            try:
                temp.unlink(missing_ok=True)
            except Exception:
                pass

            if attempt < retries:
                time.sleep(3 * attempt)

    return False


def http_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    response = SESSION.get(
        url,
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


# ============================================================================
# OSM
# ============================================================================

def download_western_zone() -> None:
    banner("DOWNLOADING WESTERN ZONE OSM")

    if OSM_SOURCE_PATH.exists() and OSM_SOURCE_PATH.stat().st_size > 0:
        log(
            f"Existing source found: "
            f"{format_bytes(OSM_SOURCE_PATH.stat().st_size)}"
        )
        return

    if not download_file(OSM_SOURCE_URL, OSM_SOURCE_PATH):
        fail("Western Zone OSM download failed")


def extract_pune_osm() -> None:
    banner("EXTRACTING PUNE OSM")

    PUNE_PBF_PATH.parent.mkdir(parents=True, exist_ok=True)

    bbox = f"{MIN_LON},{MIN_LAT},{MAX_LON},{MAX_LAT}"

    # smart keeps referenced objects complete. The later GeoJSONSeq scan is
    # deliberately strict and checks actual geometry coordinates.
    cmd = [
        "osmium",
        "extract",
        "--bbox",
        bbox,
        "--strategy",
        "smart",
        str(OSM_SOURCE_PATH),
        "-o",
        str(PUNE_PBF_PATH),
        "--overwrite",
    ]

    run_cmd(cmd, timeout=900)

    if not PUNE_PBF_PATH.exists():
        fail("Pune OSM PBF was not created")

    size = PUNE_PBF_PATH.stat().st_size
    if size < 1024:
        fail(f"Pune OSM PBF is suspiciously small: {size} bytes")

    log(f"Pune PBF size: {format_bytes(size)}")


def iter_coords(coords: Any) -> Iterable[tuple[float, float]]:
    if not isinstance(coords, list):
        return

    if (
        len(coords) >= 2
        and isinstance(coords[0], (int, float))
        and isinstance(coords[1], (int, float))
    ):
        yield float(coords[0]), float(coords[1])
        return

    for child in coords:
        yield from iter_coords(child)


def geometry_coords(feature: dict[str, Any]) -> Iterable[tuple[float, float]]:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return

    coords = geometry.get("coordinates")
    if coords is None:
        return

    yield from iter_coords(coords)


def scan_geojsonseq(path: Path) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "features": 0,
        "coordinates": 0,
        "inside_coordinates": 0,
        "outside_coordinates": 0,
        "far_outside_coordinates": 0,
        "min_lon": None,
        "max_lon": None,
        "min_lat": None,
        "max_lat": None,
        "inside_feature_count": 0,
        "outside_only_feature_count": 0,
        "invalid_json_lines": 0,
        "empty_lines": 0,
        "far_outside_samples": [],
    }

    far_min_lon = MIN_LON - DIAGNOSTIC_MARGIN_DEG
    far_max_lon = MAX_LON + DIAGNOSTIC_MARGIN_DEG
    far_min_lat = MIN_LAT - DIAGNOSTIC_MARGIN_DEG
    far_max_lat = MAX_LAT + DIAGNOSTIC_MARGIN_DEG

    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()

            if not line:
                stats["empty_lines"] += 1
                continue

            try:
                feature = json.loads(line)
            except json.JSONDecodeError:
                stats["invalid_json_lines"] += 1
                continue

            if not isinstance(feature, dict):
                continue

            stats["features"] += 1
            feature_inside = False
            feature_coords = 0

            for lon, lat in geometry_coords(feature):
                feature_coords += 1
                stats["coordinates"] += 1

                if stats["min_lon"] is None:
                    stats["min_lon"] = lon
                    stats["max_lon"] = lon
                    stats["min_lat"] = lat
                    stats["max_lat"] = lat
                else:
                    stats["min_lon"] = min(stats["min_lon"], lon)
                    stats["max_lon"] = max(stats["max_lon"], lon)
                    stats["min_lat"] = min(stats["min_lat"], lat)
                    stats["max_lat"] = max(stats["max_lat"], lat)

                inside = (
                    MIN_LON <= lon <= MAX_LON
                    and MIN_LAT <= lat <= MAX_LAT
                )

                if inside:
                    stats["inside_coordinates"] += 1
                    feature_inside = True
                else:
                    stats["outside_coordinates"] += 1

                    far_outside = (
                        lon < far_min_lon
                        or lon > far_max_lon
                        or lat < far_min_lat
                        or lat > far_max_lat
                    )

                    if far_outside:
                        stats["far_outside_coordinates"] += 1

                        if len(stats["far_outside_samples"]) < 20:
                            stats["far_outside_samples"].append(
                                {
                                    "line": line_no,
                                    "lon": round(lon, 7),
                                    "lat": round(lat, 7),
                                }
                            )

            if feature_coords:
                if feature_inside:
                    stats["inside_feature_count"] += 1
                else:
                    stats["outside_only_feature_count"] += 1

    return stats


def validate_pune_osm_geometry() -> dict[str, Any]:
    banner("FULL PUNE OSM GEOMETRY VALIDATION")

    diagnostic = TMP_ROOT / "pune-diagnostic.geojsonseq"
    diagnostic.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "osmium",
        "export",
        str(PUNE_PBF_PATH),
        "-o",
        str(diagnostic),
        "--overwrite",
        "--output-format=geojsonseq",
        "--geometry-types",
        "linestring,polygon",
    ]

    run_cmd(cmd, timeout=1200)

    if not diagnostic.exists():
        fail("OSM diagnostic GeoJSONSeq was not created")

    log(f"Diagnostic size: {format_bytes(diagnostic.stat().st_size)}")
    log("Scanning EVERY GeoJSONSeq feature and coordinate...")

    stats = scan_geojsonseq(diagnostic)

    log(f"Features inspected: {stats['features']:,}")
    log(f"Coordinates inspected: {stats['coordinates']:,}")
    log(f"Inside Pune bbox: {stats['inside_coordinates']:,}")
    log(f"Outside Pune bbox: {stats['outside_coordinates']:,}")
    log(
        "Far outside diagnostic margin: "
        f"{stats['far_outside_coordinates']:,}"
    )
    log(f"Inside features: {stats['inside_feature_count']:,}")
    log(f"Outside-only features: {stats['outside_only_feature_count']:,}")
    log(
        "Bounds: "
        f"lon={stats['min_lon']}..{stats['max_lon']} "
        f"lat={stats['min_lat']}..{stats['max_lat']}"
    )

    if stats["invalid_json_lines"]:
        warn(
            f"Invalid GeoJSONSeq lines: "
            f"{stats['invalid_json_lines']:,}"
        )

    if stats["far_outside_samples"]:
        warn("Far-outside coordinate samples:")
        for sample in stats["far_outside_samples"]:
            log(
                f"  line {sample['line']}: "
                f"{sample['lon']}, {sample['lat']}"
            )

    if stats["inside_coordinates"] == 0:
        fail(
            "PUNE GEOMETRY VALIDATION FAILED: "
            "no geometry coordinate falls inside the Pune bbox."
        )

    log("PUNE GEOMETRY VALIDATION PASSED")

    return stats


# ============================================================================
# DEM
# ============================================================================

def download_dem() -> list[dict[str, Any]]:
    banner("DOWNLOADING DEM")

    results = []

    for tile in DEM_TILES:
        url = (
            "https://s3.amazonaws.com/elevation-tiles-prod/skadi/"
            f"{tile[0:3]}/{tile}.hgt.gz"
        )
        output = DEM_ROOT / f"{tile}.hgt.gz"

        ok = download_file(url, output)
        if not ok:
            fail(f"DEM download failed: {tile}")

        results.append(
            {
                "tile": tile,
                "url": url,
                "path": str(output),
                "size": output.stat().st_size,
            }
        )

    return results


# ============================================================================
# OPENCITY FILTERING
# ============================================================================

def norm(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").lower(),
    ).strip()


def compact(value: Any) -> str:
    return norm(value).replace(" ", "_")


def contains_any(text: str, terms: set[str]) -> bool:
    normalized = norm(text)
    return any(norm(term) in normalized for term in terms)


def has_other_city(text: str) -> bool:
    normalized = norm(text)

    for city in OTHER_CITY_TERMS:
        if norm(city) in normalized:
            return True

    return False


def is_multi_city(text: str) -> bool:
    normalized = norm(text)

    for term in MULTI_CITY_TERMS:
        if norm(term) in normalized:
            return True

    return False


def resource_text(resource: dict[str, Any]) -> str:
    values = [
        resource.get("name"),
        resource.get("description"),
        resource.get("format"),
        resource.get("url"),
        resource.get("path"),
    ]
    return " ".join(str(v or "") for v in values)


def package_text(package: dict[str, Any]) -> str:
    values = [
        package.get("name"),
        package.get("title"),
        package.get("notes"),
        package.get("url"),
        package.get("organization", {}).get("name")
        if isinstance(package.get("organization"), dict)
        else "",
        " ".join(str(x) for x in package.get("groups", []) or []),
        " ".join(str(x) for x in package.get("tags", []) or []),
    ]
    return " ".join(str(v or "") for v in values)


def is_pune_package(package: dict[str, Any]) -> bool:
    text = package_text(package)

    # Explicit Pune context is the preferred signal.
    if contains_any(text, PUNE_TERMS):
        return True

    # A package with an organization/group literally called Pune is accepted.
    for key in ("organization", "groups"):
        value = package.get(key)

        if isinstance(value, dict):
            if contains_any(
                f"{value.get('name', '')} {value.get('title', '')}",
                PUNE_TERMS,
            ):
                return True

        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    item_text = (
                        f"{item.get('name', '')} "
                        f"{item.get('title', '')}"
                    )
                    if contains_any(item_text, PUNE_TERMS):
                        return True

    return False


def is_resource_allowed(
    package: dict[str, Any],
    resource: dict[str, Any],
) -> tuple[bool, str]:
    ptext = package_text(package)
    rtext = resource_text(resource)

    # Reject a resource explicitly tied to another city unless it is also
    # clearly a Pune resource. This catches the current Bengaluru problem.
    if has_other_city(rtext) and not contains_any(rtext, PUNE_TERMS):
        return False, "resource explicitly names another city"

    # If the package itself is Pune-specific, a resource with no city name
    # is generally a legitimate resource belonging to that Pune dataset.
    if contains_any(rtext, PUNE_TERMS):
        return True, "resource explicitly references Pune"

    if is_multi_city(rtext):
        return True, "explicit multi-city / national resource in Pune package"

    # Resource has another city hidden in the URL/path but not in filename.
    if has_other_city(rtext):
        return False, "resource path/name points to another city"

    # Package-level Pune context allows generic resources from that package.
    if contains_any(ptext, PUNE_TERMS):
        return True, "package is Pune-specific and resource is generic"

    return False, "no Pune relevance detected"


def choose_resource_filename(
    package: dict[str, Any],
    resource: dict[str, Any],
    index: int,
) -> str:
    raw_url = str(resource.get("url") or "")
    url_name = Path(urlparse(raw_url).path).name
    original = url_name or str(resource.get("name") or f"resource_{index}")

    original = re.sub(r"[^\w.\- ]+", "_", original).strip()
    if not original:
        original = f"resource_{index}"

    # Keep filenames reasonably portable.
    return original[:220]


def opencity_package_search() -> dict[str, Any]:
    banner("OPENCITY PACKAGE SEARCH")

    # Search broadly, then apply our own Pune filter. This avoids relying on
    # the site's organization parameter, which previously returned HTTP 400.
    params = {
        "q": "Pune",
        "rows": 100,
    }

    url = f"{OPENCITY_API}/package_search"

    try:
        data = http_json(url, params=params)
    except Exception as exc:
        warn(f"OpenCity API package_search failed: {exc}")
        return {
            "success": False,
            "result": {"count": 0, "results": []},
            "error": str(exc),
        }

    if not isinstance(data, dict):
        fail("OpenCity package_search returned a non-JSON object")

    OPENCITY_PACKAGE_SEARCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPENCITY_PACKAGE_SEARCH_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return data


def download_opencity_resources() -> dict[str, Any]:
    banner("DOWNLOADING PUNE-RELEVANT OPENCITY RESOURCES")

    search_data = opencity_package_search()

    result = search_data.get("result", {}) if isinstance(search_data, dict) else {}
    packages = result.get("results", []) if isinstance(result, dict) else []

    log(f"OpenCity search results: {len(packages)}")

    selected_packages: list[dict[str, Any]] = []
    skipped_packages: list[dict[str, Any]] = []
    selected_resources: list[dict[str, Any]] = []
    skipped_resources: list[dict[str, Any]] = []

    for package in packages:
        if not isinstance(package, dict):
            continue

        if is_pune_package(package):
            selected_packages.append(package)
        else:
            skipped_packages.append(
                {
                    "name": package.get("name"),
                    "title": package.get("title"),
                    "reason": "package has no Pune relevance",
                }
            )

    log(f"Pune packages selected: {len(selected_packages)}")
    log(f"Packages skipped: {len(skipped_packages)}")

    used_output_names: set[str] = set()

    for package_index, package in enumerate(selected_packages, start=1):
        package_title = package.get("title") or package.get("name") or "unknown"
        log(f"[PACKAGE {package_index}] {package_title}")

        resources = package.get("resources") or []

        for resource_index, resource in enumerate(resources, start=1):
            if not isinstance(resource, dict):
                continue

            allowed, reason = is_resource_allowed(package, resource)

            resource_name = (
                resource.get("name")
                or resource.get("url")
                or f"resource_{resource_index}"
            )

            if not allowed:
                skipped_resources.append(
                    {
                        "package": package_title,
                        "resource": resource_name,
                        "reason": reason,
                    }
                )
                log(f"  SKIP: {resource_name} -> {reason}")
                continue

            url = resource.get("url")
            if not url:
                skipped_resources.append(
                    {
                        "package": package_title,
                        "resource": resource_name,
                        "reason": "resource has no URL",
                    }
                )
                log(f"  SKIP: {resource_name} -> no URL")
                continue

            filename = choose_resource_filename(
                package,
                resource,
                resource_index,
            )

            # Resource-level duplicate handling.
            # If two different datasets expose the same basename, preserve
            # both files by prefixing the later one with a stable counter.
            candidate = filename
            if candidate.lower() in used_output_names:
                stem = Path(filename).stem
                suffix = Path(filename).suffix
                n = 2

                while f"{stem}__{n}{suffix}".lower() in used_output_names:
                    n += 1

                candidate = f"{stem}__{n}{suffix}"

            used_output_names.add(candidate.lower())

            package_slug = (
                package.get("name")
                or compact(package_title)
                or f"package_{package_index}"
            )
            package_slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", package_slug)

            output = (
                OPENCITY_RESOURCES_ROOT
                / package_slug[:120]
                / candidate
            )

            expected_size = None
            try:
                if resource.get("size"):
                    expected_size = int(resource["size"])
            except (TypeError, ValueError):
                expected_size = None

            log(f"  DOWNLOAD: {resource_name}")
            log(f"  REASON: {reason}")

            ok = download_file(
                str(url),
                output,
                expected_size=expected_size,
            )

            if not ok:
                # Do not silently lose the failure.
                skipped_resources.append(
                    {
                        "package": package_title,
                        "resource": resource_name,
                        "reason": "download failed",
                        "url": url,
                    }
                )
                warn(f"  FAILED: {url}")
                continue

            selected_resources.append(
                {
                    "package_name": package.get("name"),
                    "package_title": package_title,
                    "resource_name": resource_name,
                    "resource_url": url,
                    "output": str(output),
                    "size": output.stat().st_size,
                    "selection_reason": reason,
                }
            )

    log("OpenCity resource summary:")
    log(f"  Downloaded: {len(selected_resources)}")
    log(f"  Skipped:    {len(skipped_resources)}")
    log(f"  Packages:   {len(selected_packages)}")

    return {
        "search_count": len(packages),
        "selected_packages": len(selected_packages),
        "skipped_packages": skipped_packages,
        "downloaded": selected_resources,
        "skipped_resources": skipped_resources,
    }


# ============================================================================
# MANIFEST / CHECKSUMS
# ============================================================================

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as fh:
        while True:
            block = fh.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)

    return digest.hexdigest()


def collect_files() -> list[Path]:
    files: list[Path] = []

    for root in (
        Path("data/raw/osm"),
        DEM_ROOT,
        OPENCITY_ROOT,
    ):
        if not root.exists():
            continue

        for path in root.rglob("*"):
            if path.is_file():
                files.append(path)

    return sorted(files)


def create_manifest(
    *,
    osm_stats: dict[str, Any],
    dem_results: list[dict[str, Any]],
    opencity_results: dict[str, Any],
) -> None:
    banner("CREATING MANIFEST")

    files = collect_files()

    entries = []

    for path in files:
        entries.append(
            {
                "path": str(path),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )

    manifest = {
        "collector_version": VERSION,
        "release_tag": RELEASE_TAG,
        "project": "Pune Raw Open Data",
        "bbox": {
            "min_lon": MIN_LON,
            "min_lat": MIN_LAT,
            "max_lon": MAX_LON,
            "max_lat": MAX_LAT,
        },
        "osm": {
            "source_url": OSM_SOURCE_URL,
            "output": str(PUNE_PBF_PATH),
            "geometry_validation": osm_stats,
        },
        "dem": dem_results,
        "opencity": {
            "package_search_file": str(OPENCITY_PACKAGE_SEARCH_PATH),
            "selected_packages": opencity_results.get("selected_packages", 0),
            "downloaded_count": len(
                opencity_results.get("downloaded", [])
            ),
            "skipped_count": len(
                opencity_results.get("skipped_resources", [])
            ),
            "downloaded": opencity_results.get("downloaded", []),
            "skipped_resources": opencity_results.get(
                "skipped_resources", []
            ),
        },
        "files": entries,
        "file_count": len(entries),
        "created_at_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        ),
    }

    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log(f"Manifest files: {len(entries)}")


def create_checksums() -> None:
    banner("CREATING SHA256 CHECKSUMS")

    files = collect_files()

    lines = []

    for path in files:
        lines.append(f"{sha256_file(path)}  {path}")

    CHECKSUM_PATH.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )

    log(f"Checksum entries: {len(lines)}")


# ============================================================================
# RELEASE UPLOAD
# ============================================================================

def prepare_unique_release_assets() -> Path:
    banner("PREPARING UNIQUE RELEASE ASSETS")

    staging = TMP_ROOT / "release-assets"

    if staging.exists():
        shutil.rmtree(staging)

    staging.mkdir(parents=True, exist_ok=True)

    files = collect_files()

    # Release assets cannot safely share the same basename. We therefore
    # create unique upload names while leaving the local dataset untouched.
    used: set[str] = set()
    mapping: list[dict[str, str]] = []

    for index, path in enumerate(files, start=1):
        basename = path.name
        candidate = basename

        if candidate.lower() in used:
            stem = path.stem
            suffix = path.suffix
            candidate = f"{index:04d}_{stem}{suffix}"

            while candidate.lower() in used:
                candidate = f"{index:04d}_{index}_{stem}{suffix}"

        used.add(candidate.lower())

        destination = staging / candidate
        shutil.copy2(path, destination)

        mapping.append(
            {
                "source": str(path),
                "asset_name": candidate,
            }
        )

    mapping_path = staging / "asset-name-map.json"
    mapping_path.write_text(
        json.dumps(mapping, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    log(f"Release assets prepared: {len(mapping)}")
    log(f"Staging: {staging}")

    return staging


def upload_release_assets() -> None:
    banner("UPLOADING RELEASE ASSETS")

    if shutil.which("gh") is None:
        fail("GitHub CLI 'gh' is not installed")

    staging = prepare_unique_release_assets()

    # Keep the map local; it is useful for reproducing which local file became
    # which GitHub release asset, but don't upload it automatically.
    assets = [
        path
        for path in sorted(staging.iterdir())
        if path.is_file()
        and path.name != "asset-name-map.json"
    ]

    if not assets:
        fail("No release assets were prepared")

    failed: list[dict[str, str]] = []

    for asset in assets:
        log(f"Uploading release asset: {asset}")

        result = run_cmd(
            [
                "gh",
                "release",
                "upload",
                RELEASE_TAG,
                str(asset),
                "--clobber",
            ],
            timeout=900,
            check=False,
        )

        if result.returncode != 0:
            failed.append(
                {
                    "asset": asset.name,
                    "reason": (
                        result.stderr.strip()
                        or f"exit code {result.returncode}"
                    ),
                }
            )

            # Continue uploading the rest. A single duplicate/corrupt asset
            # must not terminate the complete release.
            warn(
                f"Release upload failed for {asset.name}; "
                "continuing."
            )

    if failed:
        banner("RELEASE UPLOAD WARNINGS")
        for item in failed:
            log(f"- {item['asset']}: {item['reason']}")

        # Fail at the END so the workflow is not falsely reported successful.
        fail(
            f"{len(failed)} release asset(s) failed to upload. "
            "All other assets were attempted."
        )

    log("ALL RELEASE ASSETS UPLOADED SUCCESSFULLY")


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    banner(f"PUNE RAW OPEN DATA COLLECTOR v{VERSION}")

    log(f"BBOX: {PUNE_BBOX}")
    log(f"OSM SOURCE: {OSM_SOURCE_URL}")
    log(f"OSM OUTPUT: {PUNE_PBF_PATH}")

    for directory in (
        RAW_ROOT,
        DEM_ROOT,
        OPENCITY_ROOT,
        OPENCITY_RESOURCES_ROOT,
        TMP_ROOT,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    download_western_zone()
    extract_pune_osm()
    osm_stats = validate_pune_osm_geometry()

    dem_results = download_dem()
    opencity_results = download_opencity_resources()

    create_manifest(
        osm_stats=osm_stats,
        dem_results=dem_results,
        opencity_results=opencity_results,
    )
    create_checksums()

    upload_release_assets()

    banner("COLLECTION COMPLETE")
    log(f"OSM: {PUNE_PBF_PATH}")
    log(f"Manifest: {MANIFEST_PATH}")
    log(f"Checksums: {CHECKSUM_PATH}")
    log(f"Release: {RELEASE_TAG}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nFATAL: {exc}", file=sys.stderr)
        raise SystemExit(1)
