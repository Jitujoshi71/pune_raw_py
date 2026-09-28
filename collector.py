#!/usr/bin/env python3

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


# ============================================================
# CONFIG
# ============================================================

VERSION = "2.4"

RELEASE_TAG = "pune-raw-v1"
RELEASE_NAME = "Pune Raw Open Data v1"

PUNE_BBOX = (
    73.70,  # min longitude
    18.40,  # min latitude
    74.05,  # max longitude
    18.70,  # max latitude
)

# Pune is in Geofabrik Western Zone.
WESTERN_ZONE_URL = (
    "https://download.geofabrik.de/asia/india/"
    "western-zone-latest.osm.pbf"
)

SOURCE_OSM_PATH = Path(
    "data/raw/source/western-zone-latest.osm.pbf"
)

# Processor expects this filename.
PUNE_OSM_PATH = Path(
    "data/raw/osm/central-zone.osm.pbf"
)

TMP_DIR = Path("data/tmp")

DIAGNOSTIC_PATH = (
    TMP_DIR / "pune-diagnostic.geojsonseq"
)

DEM_DIR = Path("data/raw/dem")

OPENCITY_DIR = Path(
    "data/raw/opencity"
)

MANIFEST_PATH = Path(
    "data/manifest.json"
)

CHECKSUM_PATH = Path(
    "data/SHA256SUMS.txt"
)

DOWNLOAD_CHUNK_SIZE = 1024 * 1024

REQUEST_TIMEOUT = 120

MAX_DOWNLOAD_RETRIES = 5

MIN_PUNE_OSM_SIZE = 1024 * 1024

MIN_EXPECTED_NODES = 1000

MIN_EXPECTED_WAYS = 100

MIN_EXPECTED_RELATIONS = 1


# ============================================================
# DEM
# ============================================================

DEM_TILES = {
    "N18E073": (
        "https://s3.amazonaws.com/elevation-tiles-prod/"
        "skadi/N18/N18E073.hgt.gz"
    ),
    "N18E074": (
        "https://s3.amazonaws.com/elevation-tiles-prod/"
        "skadi/N18/N18E074.hgt.gz"
    ),
    "N19E073": (
        "https://s3.amazonaws.com/elevation-tiles-prod/"
        "skadi/N19/N19E073.hgt.gz"
    ),
    "N19E074": (
        "https://s3.amazonaws.com/elevation-tiles-prod/"
        "skadi/N19/N19E074.hgt.gz"
    ),
}


# ============================================================
# OPENCITY
# ============================================================

OPENCITY_API = (
    "https://data.opencity.in/api/3/action/package_search"
)

OPENCITY_ORG = "pune-municipal-corporation"

ALLOWED_FORMATS = {
    "CSV",
    "KML",
    "KMZ",
    "GEOJSON",
    "JSON",
    "GPKG",
    "SHP",
    "ZIP",
    "XLSX",
    "XLS",
    "ODS",
    "PDF",
}


# ============================================================
# LOGGING
# ============================================================

def log(message=""):
    print(message, flush=True)


def section(title):
    log("=" * 70)
    log(title)
    log("=" * 70)


# ============================================================
# COMMAND EXECUTION
# ============================================================

def run_command(
    command,
    check=True,
    capture_output=False,
    timeout=900,
):
    """Run a subprocess with useful CI diagnostics."""
    command_text = " ".join(str(x) for x in command)

    log("$ " + command_text)

    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        log(f"Command TIMEOUT after {timeout}s")

        if exc.stdout:
            log("STDOUT:")
            log(
                exc.stdout
                if isinstance(exc.stdout, str)
                else exc.stdout.decode(errors="replace")
            )

        if exc.stderr:
            log("STDERR:")
            log(
                exc.stderr
                if isinstance(exc.stderr, str)
                else exc.stderr.decode(errors="replace")
            )

        raise RuntimeError(
            f"Command timed out after {timeout}s: {command_text}"
        ) from exc

    log(f"Command exit code: {result.returncode}")

    if result.returncode != 0:
        if result.returncode < 0:
            log(
                "Command terminated by signal: "
                f"{-result.returncode}"
            )

        if result.stdout:
            log("STDOUT:")
            log(result.stdout)

        if result.stderr:
            log("STDERR:")
            log(result.stderr)

        if check:
            raise RuntimeError(
                "Command failed with exit code "
                f"{result.returncode}: {command_text}"
            )

    elif capture_output:
        if result.stdout:
            log("STDOUT:")
            log(result.stdout)

        if result.stderr:
            log("STDERR:")
            log(result.stderr)

    return result


# ============================================================
# FILE HELPERS
# ============================================================

def ensure_directory(path):
    Path(path).mkdir(
        parents=True,
        exist_ok=True,
    )


def sha256_file(path):
    digest = hashlib.sha256()

    with open(
        path,
        "rb",
    ) as f:

        while True:
            chunk = f.read(
                8 * 1024 * 1024
            )

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def human_size(size):
    size = float(size)

    units = [
        "B",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
    ]

    for unit in units:

        if size < 1024:
            return (
                f"{size:.2f} {unit}"
            )

        size /= 1024

    return (
        f"{size:.2f} PiB"
    )


# ============================================================
# GENERIC DOWNLOAD
# ============================================================

def download_file(
    url,
    output_path,
    retries=MAX_DOWNLOAD_RETRIES,
):
    output_path = Path(
        output_path
    )

    ensure_directory(
        output_path.parent
    )

    section("DOWNLOAD")

    log(f"URL: {url}")
    log(f"OUT: {output_path}")

    last_error = None

    for attempt in range(
        1,
        retries + 1,
    ):

        log(
            f"Attempt {attempt}/"
            f"{retries}"
        )

        temp_path = output_path.with_suffix(
            output_path.suffix + ".part"
        )

        try:

            if temp_path.exists():
                temp_path.unlink()

            with requests.get(
                url,
                stream=True,
                timeout=REQUEST_TIMEOUT,
                headers={
                    "User-Agent":
                        "PuneRawCollector/2.3"
                },
            ) as response:

                response.raise_for_status()

                total = response.headers.get(
                    "Content-Length"
                )

                total_bytes = (
                    int(total)
                    if (
                        total
                        and total.isdigit()
                    )
                    else None
                )

                if total_bytes:
                    log(
                        "Expected size: "
                        + human_size(
                            total_bytes
                        )
                    )

                downloaded = 0
                last_report = -1

                with open(
                    temp_path,
                    "wb",
                ) as f:

                    for chunk in response.iter_content(
                        chunk_size=DOWNLOAD_CHUNK_SIZE
                    ):

                        if not chunk:
                            continue

                        f.write(chunk)

                        downloaded += len(
                            chunk
                        )

                        current_mb = (
                            downloaded
                            // (
                                100
                                * 1024
                                * 1024
                            )
                        )

                        if (
                            current_mb
                            > last_report
                        ):
                            last_report = (
                                current_mb
                            )

                            if (
                                downloaded
                                >= 100
                                * 1024
                                * 1024
                            ):
                                log(
                                    "Downloaded: "
                                    + human_size(
                                        downloaded
                                    )
                                )

            if not temp_path.exists():
                raise RuntimeError(
                    "Temporary file "
                    "was not created."
                )

            actual_size = (
                temp_path.stat().st_size
            )

            if actual_size <= 0:
                raise RuntimeError(
                    "Downloaded file "
                    "is empty."
                )

            if (
                total_bytes
                and actual_size
                != total_bytes
            ):
                raise RuntimeError(
                    "Downloaded size mismatch: "
                    f"expected "
                    f"{total_bytes}, "
                    f"got "
                    f"{actual_size}"
                )

            temp_path.replace(
                output_path
            )

            log(
                "DOWNLOAD SUCCESS"
            )

            log(
                "Size: "
                + human_size(
                    actual_size
                )
            )

            return output_path

        except Exception as exc:

            last_error = exc

            log(
                "Download failed: "
                f"{exc}"
            )

            if temp_path.exists():
                temp_path.unlink()

            if attempt < retries:

                sleep_seconds = min(
                    attempt * 5,
                    30,
                )

                log(
                    "Retrying in "
                    f"{sleep_seconds}s..."
                )

                time.sleep(
                    sleep_seconds
                )

    raise RuntimeError(
        "Download failed after "
        f"{retries} attempts: "
        f"{last_error}"
    )


# ============================================================
# WESTERN ZONE DOWNLOAD
# ============================================================

def download_western_zone():

    return download_file(
        WESTERN_ZONE_URL,
        SOURCE_OSM_PATH,
    )


# ============================================================
# EXTRACT PUNE OSM
# ============================================================

def extract_pune_osm():

    section(
        "EXTRACTING PUNE OSM"
    )

    ensure_directory(
        PUNE_OSM_PATH.parent
    )

    min_lon, min_lat, max_lon, max_lat = (
        PUNE_BBOX
    )

    bbox = (
        f"{min_lon},"
        f"{min_lat},"
        f"{max_lon},"
        f"{max_lat}"
    )

    command = [
        "osmium",
        "extract",
        "--bbox",
        bbox,
        "--strategy",
        "smart",
        str(SOURCE_OSM_PATH),
        "-o",
        str(PUNE_OSM_PATH),
        "--overwrite",
    ]

    run_command(
        command
    )

    if not PUNE_OSM_PATH.exists():
        raise RuntimeError(
            "Pune PBF was not created."
        )

    size = (
        PUNE_OSM_PATH.stat()
        .st_size
    )

    log(
        "Pune PBF size: "
        + human_size(size)
    )

    if size < MIN_PUNE_OSM_SIZE:
        raise RuntimeError(
            "Pune PBF is "
            "suspiciously small: "
            f"{size} bytes"
        )

    log(
        "Pune PBF validation: "
        "file exists and size is valid."
    )


# ============================================================
# OSM FILEINFO
# ============================================================

def get_osm_fileinfo():
    """
    Lightweight OSM validation.

    Avoid `osmium fileinfo --extended` in the CI validation path.
    """
    section("OSMIUM FILEINFO")

    result = run_command(
        [
            "osmium",
            "fileinfo",
            str(PUNE_OSM_PATH),
        ],
        capture_output=True,
        timeout=180,
    )

    output = result.stdout or ""

    if not output.strip():
        raise RuntimeError(
            "osmium fileinfo returned no output."
        )

    log(output)
    return output


def parse_osm_counts(
    fileinfo
):

    patterns = {
        "nodes":
            r"Number of nodes:\s*([\d,]+)",

        "ways":
            r"Number of ways:\s*([\d,]+)",

        "relations":
            r"Number of relations:\s*([\d,]+)",
    }

    counts = {}

    for key, pattern in patterns.items():

        match = re.search(
            pattern,
            fileinfo,
        )

        if match:
            counts[key] = int(
                match.group(
                    1
                ).replace(
                    ",",
                    "",
                )
            )
        else:
            counts[key] = 0

    return counts


# ============================================================
# GEOJSON COORDINATE READER
# ============================================================

def find_coordinates(
    value
):
    """
    Recursively finds GeoJSON
    coordinate pairs.

    Supports:
      Point
      LineString
      Polygon
      MultiLineString
      MultiPolygon
    """

    if not isinstance(
        value,
        list,
    ):
        return

    if (
        len(value) >= 2
        and isinstance(
            value[0],
            (int, float),
        )
        and isinstance(
            value[1],
            (int, float),
        )
    ):

        yield (
            float(value[0]),
            float(value[1]),
        )

        return

    for item in value:

        yield from find_coordinates(
            item
        )


def coordinate_inside_pune(
    lon,
    lat,
):

    min_lon, min_lat, max_lon, max_lat = (
        PUNE_BBOX
    )

    return (
        min_lon
        <= lon
        <= max_lon
        and
        min_lat
        <= lat
        <= max_lat
    )


# ============================================================
# GEOMETRY VALIDATION
# ============================================================

def validate_osm_geometry():

    section(
        "PUNE GEOMETRY DIAGNOSTIC"
    )

    ensure_directory(
        DIAGNOSTIC_PATH.parent
    )

    if DIAGNOSTIC_PATH.exists():
        DIAGNOSTIC_PATH.unlink()

    # osmium 1.16.0 accepts:
    # linestring, polygon
    #
    # Do NOT use "points".
    command = [
        "osmium",
        "export",
        str(PUNE_OSM_PATH),
        "-o",
        str(DIAGNOSTIC_PATH),
        "--overwrite",
        "--output-format=geojsonseq",
        "--geometry-types",
        "linestring,polygon",
    ]

    run_command(
        command,
        timeout=900,
    )

    if not DIAGNOSTIC_PATH.exists():
        raise RuntimeError(
            "Diagnostic GeoJSONSeq "
            "was not created."
        )

    size = (
        DIAGNOSTIC_PATH.stat()
        .st_size
    )

    log(
        "Diagnostic size: "
        + human_size(size)
    )

    if size == 0:
        raise RuntimeError(
            "Diagnostic GeoJSONSeq "
            "is empty."
        )

    total_features = 0

    total_coordinates = 0

    inside_coordinates = 0

    samples = []

    with open(
        DIAGNOSTIC_PATH,
        "r",
        encoding="utf-8",
    ) as f:

        for line_number, line in enumerate(
            f,
            start=1,
        ):

            line = line.strip()

            if not line:
                continue

            try:

                feature = json.loads(
                    line
                )

            except json.JSONDecodeError as exc:

                raise RuntimeError(
                    "Invalid GeoJSONSeq "
                    f"at line "
                    f"{line_number}: "
                    f"{exc}"
                )

            total_features += 1

            geometry = feature.get(
                "geometry"
            )

            if not geometry:
                continue

            coordinates = geometry.get(
                "coordinates"
            )

            if coordinates is None:
                continue

            for lon, lat in find_coordinates(
                coordinates
            ):

                total_coordinates += 1

                inside = (
                    coordinate_inside_pune(
                        lon,
                        lat,
                    )
                )

                if inside:
                    inside_coordinates += 1

                if len(samples) < 20:

                    samples.append(
                        {
                            "lon": lon,
                            "lat": lat,
                            "inside": inside,
                        }
                    )

                if (
                    inside_coordinates
                    >= 10
                ):
                    break

            if (
                inside_coordinates
                >= 10
            ):
                break

    log(
        "Features inspected: "
        f"{total_features}"
    )

    log(
        "Coordinates inspected: "
        f"{total_coordinates}"
    )

    log(
        "Coordinates inside "
        "Pune bbox: "
        f"{inside_coordinates}"
    )

    log("")
    log(
        "SAMPLE COORDINATES"
    )

    for index, sample in enumerate(
        samples,
        start=1,
    ):

        log(
            f"Sample {index}: "
            f"lon={sample['lon']:.7f}, "
            f"lat={sample['lat']:.7f}, "
            f"inside={sample['inside']}"
        )

    if total_features == 0:
        raise RuntimeError(
            "No line/polygon "
            "geometries were exported."
        )

    if total_coordinates == 0:
        raise RuntimeError(
            "No coordinates were found "
            "in diagnostic geometry."
        )

    if inside_coordinates == 0:
        raise RuntimeError(
            "ZERO geometry coordinates "
            "were inside Pune bbox."
        )

    log("")
    log(
        "PUNE GEOMETRY "
        "VALIDATION PASSED"
    )


# ============================================================
# COMPLETE OSM VALIDATION
# ============================================================

def validate_osm():

    section(
        "VALIDATING PUNE OSM"
    )

    fileinfo = (
        get_osm_fileinfo()
    )

    counts = (
        parse_osm_counts(
            fileinfo
        )
    )

    log(
        "Nodes      : "
        f"{counts['nodes']:,}"
    )

    log(
        "Ways       : "
        f"{counts['ways']:,}"
    )

    log(
        "Relations   : "
        f"{counts['relations']:,}"
    )

    if (
        counts["nodes"]
        < MIN_EXPECTED_NODES
    ):
        raise RuntimeError(
            "Too few OSM nodes."
        )

    if (
        counts["ways"]
        < MIN_EXPECTED_WAYS
    ):
        raise RuntimeError(
            "Too few OSM ways."
        )

    if (
        counts["relations"]
        < MIN_EXPECTED_RELATIONS
    ):
        log(
            "WARNING: No relations found."
        )

    validate_osm_geometry()

    return counts


# ============================================================
# DEM COLLECTION
# ============================================================

def collect_dem():

    section(
        "COLLECTING DEM"
    )

    ensure_directory(
        DEM_DIR
    )

    for tile_name, url in (
        DEM_TILES.items()
    ):

        output = (
            DEM_DIR
            / f"{tile_name}.hgt.gz"
        )

        if (
            output.exists()
            and output.stat().st_size > 0
        ):

            log(
                "Already exists: "
                f"{output}"
            )

            continue

        log(
            "Downloading DEM: "
            f"{tile_name}"
        )

        download_file(
            url,
            output,
        )

    log(
        "DEM collection complete."
    )


# ============================================================
# OPENCITY API
# ============================================================

def opencity_request(
    params,
    retries=3,
):

    last_error = None

    for attempt in range(
        1,
        retries + 1,
    ):

        try:

            response = requests.get(
                OPENCITY_API,
                params=params,
                timeout=REQUEST_TIMEOUT,
                headers={
                    "User-Agent":
                        "PuneRawCollector/2.3"
                },
            )

            response.raise_for_status()

            data = response.json()

            if not data.get(
                "success"
            ):
                raise RuntimeError(
                    "OpenCity API returned "
                    "success=false"
                )

            return data["result"]

        except Exception as exc:

            last_error = exc

            log(
                "OpenCity request failed: "
                f"{exc}"
            )

            if attempt < retries:

                time.sleep(
                    attempt * 3
                )

    raise RuntimeError(
        "OpenCity API request failed: "
        f"{last_error}"
    )


# ============================================================
# OPENCITY UNAVAILABLE MARKER
# ============================================================

def write_opencity_unavailable(
    reason
):

    ensure_directory(
        OPENCITY_DIR
    )

    marker_path = (
        OPENCITY_DIR
        / "unavailable.json"
    )

    data = {
        "available": False,
        "collector_version": VERSION,
        "timestamp_utc":
            time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            ),
        "api": OPENCITY_API,
        "organization": OPENCITY_ORG,
        "reason": str(reason),
    }

    with open(
        marker_path,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )

    log(
        "Created: "
        f"{marker_path}"
    )

    return marker_path


# ============================================================
# OPENCITY CATALOG
# ============================================================

def collect_opencity_catalog():

    section(
        "COLLECTING OPENCITY CATALOG"
    )

    ensure_directory(
        OPENCITY_DIR
    )

    # IMPORTANT:
    # Do not send q="".
    # Some CKAN/OpenCity installations
    # reject an empty q parameter with HTTP 400.
    #
    # First attempt uses the organization
    # filter without q.
    params = {
        "rows": 1000,
        "organization":
            OPENCITY_ORG,
    }

    try:

        result = opencity_request(
            params
        )

    except Exception as first_error:

        log(
            "OpenCity organization "
            "query failed."
        )

        # Second fallback:
        # search specifically for Pune
        # municipal data without the
        # organization parameter.
        log(
            "Trying OpenCity fallback "
            "search..."
        )

        try:

            result = opencity_request(
                {
                    "q": "Pune",
                    "rows": 1000,
                }
            )

        except Exception as second_error:

            reason = (
                "Primary OpenCity query "
                f"failed: {first_error}; "
                "fallback query failed: "
                f"{second_error}"
            )

            log(
                "OpenCity unavailable."
            )

            log(
                "WARNING: "
                + reason
            )

            marker = (
                write_opencity_unavailable(
                    reason
                )
            )

            return {
                "available": False,
                "result": None,
                "marker": marker,
            }

    catalog_path = (
        OPENCITY_DIR
        / "package_search.json"
    )

    with open(
        catalog_path,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            result,
            f,
            indent=2,
            ensure_ascii=False,
        )

    package_count = len(
        result.get(
            "results",
            []
        )
    )

    log(
        "OpenCity packages: "
        f"{package_count}"
    )

    return {
        "available": True,
        "result": result,
        "marker": None,
    }


# ============================================================
# SAFE FILENAMES
# ============================================================

def safe_filename(
    name
):

    name = str(name)

    name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        name,
    )

    name = name.strip(
        "._"
    )

    if not name:
        name = "resource"

    return name[:180]


def resource_extension(
    resource
):

    fmt = str(
        resource.get(
            "format",
            "",
        )
    ).upper().strip()

    if fmt:

        fmt_clean = re.sub(
            r"[^A-Z0-9]+",
            "",
            fmt,
        )

        if fmt_clean == "GEOJSON":
            return ".geojson"

        return (
            "."
            + fmt_clean.lower()
        )

    url = resource.get(
        "url",
        "",
    )

    parsed = urlparse(
        url
    )

    return Path(
        parsed.path
    ).suffix.lower()


# ============================================================
# OPENCITY RESOURCE COLLECTION
# ============================================================

def collect_opencity_resources(
    catalog_result
):

    section(
        "COLLECTING OPENCITY RESOURCES"
    )

    resources_root = (
        OPENCITY_DIR
        / "resources"
    )

    ensure_directory(
        resources_root
    )

    if not catalog_result:
        log(
            "No OpenCity catalog."
        )
        return

    packages = catalog_result.get(
        "results",
        []
    )

    downloaded = 0
    skipped = 0
    failed = 0

    for package_index, package in enumerate(
        packages,
        start=1,
    ):

        package_name = safe_filename(
            package.get(
                "name",
                f"package_{package_index}",
            )
        )

        package_dir = (
            resources_root
            / package_name
        )

        ensure_directory(
            package_dir
        )

        resources = package.get(
            "resources",
            []
        )

        log(
            f"[{package_index}/"
            f"{len(packages)}] "
            f"{package_name} "
            f"resources="
            f"{len(resources)}"
        )

        for resource_index, resource in enumerate(
            resources,
            start=1,
        ):

            url = resource.get(
                "url"
            )

            if not url:
                skipped += 1
                continue

            fmt = str(
                resource.get(
                    "format",
                    "",
                )
            ).upper().strip()

            if (
                fmt
                and fmt
                not in ALLOWED_FORMATS
            ):
                skipped += 1
                continue

            resource_name = (
                resource.get(
                    "name"
                )
                or resource.get(
                    "id"
                )
                or (
                    f"resource_"
                    f"{resource_index}"
                )
            )

            resource_name = safe_filename(
                resource_name
            )

            extension = (
                resource_extension(
                    resource
                )
            )

            if extension:

                if not resource_name.lower().endswith(
                    extension.lower()
                ):
                    resource_name += (
                        extension
                    )

            output = (
                package_dir
                / resource_name
            )

            if (
                output.exists()
                and output.stat().st_size > 0
            ):

                skipped += 1
                continue

            try:

                download_file(
                    url,
                    output,
                    retries=3,
                )

                downloaded += 1

            except Exception as exc:

                failed += 1

                log(
                    "WARNING: OpenCity "
                    "resource failed:"
                )

                log(
                    f"URL: {url}"
                )

                log(
                    f"Reason: {exc}"
                )

    log("")
    log(
        "OpenCity resource summary:"
    )

    log(
        f"Downloaded: {downloaded}"
    )

    log(
        f"Skipped:    {skipped}"
    )

    log(
        f"Failed:     {failed}"
    )

    if failed:

        log(
            "WARNING: Some OpenCity "
            "resources could not "
            "be downloaded."
        )


# ============================================================
# MANIFEST
# ============================================================

def create_manifest(
    osm_counts,
    opencity_available,
):

    section(
        "CREATING MANIFEST"
    )

    files = []

    raw_root = Path(
        "data/raw"
    )

    if raw_root.exists():

        for path in raw_root.rglob(
            "*"
        ):

            if not path.is_file():
                continue

            files.append(
                {
                    "path":
                        path.as_posix(),

                    "size":
                        path.stat().st_size,

                    "sha256":
                        sha256_file(
                            path
                        ),
                }
            )

    manifest = {
        "collector_version":
            VERSION,

        "release_tag":
            RELEASE_TAG,

        "release_name":
            RELEASE_NAME,

        "generated_at_utc":
            time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            ),

        "pune_bbox": {
            "min_lon":
                PUNE_BBOX[0],

            "min_lat":
                PUNE_BBOX[1],

            "max_lon":
                PUNE_BBOX[2],

            "max_lat":
                PUNE_BBOX[3],
        },

        "osm": {
            "source":
                WESTERN_ZONE_URL,

            "source_file":
                str(
                    SOURCE_OSM_PATH
                ),

            "pune_file":
                str(
                    PUNE_OSM_PATH
                ),

            "nodes":
                osm_counts["nodes"],

            "ways":
                osm_counts["ways"],

            "relations":
                osm_counts["relations"],
        },

        "opencity": {
            "available":
                opencity_available,
        },

        "files":
            files,
    }

    ensure_directory(
        MANIFEST_PATH.parent
    )

    with open(
        MANIFEST_PATH,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            manifest,
            f,
            indent=2,
            ensure_ascii=False,
        )

    log(
        "Manifest files: "
        f"{len(files)}"
    )


# ============================================================
# SHA256
# ============================================================

def create_checksums():

    section(
        "CREATING SHA256 CHECKSUMS"
    )

    lines = []

    raw_root = Path(
        "data/raw"
    )

    if raw_root.exists():

        for path in sorted(
            raw_root.rglob("*")
        ):

            if not path.is_file():
                continue

            digest = sha256_file(
                path
            )

            lines.append(
                f"{digest}  "
                f"{path.as_posix()}"
            )

    ensure_directory(
        CHECKSUM_PATH.parent
    )

    with open(
        CHECKSUM_PATH,
        "w",
        encoding="utf-8",
    ) as f:

        if lines:
            f.write(
                "\n".join(
                    lines
                )
            )

            f.write("\n")

    log(
        "Checksum entries: "
        f"{len(lines)}"
    )


# ============================================================
# GITHUB RELEASE
# ============================================================

def ensure_release():

    section(
        "GITHUB RELEASE"
    )

    result = subprocess.run(
        [
            "gh",
            "release",
            "view",
            RELEASE_TAG,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if result.returncode == 0:

        log(
            "Release exists: "
            f"{RELEASE_TAG}"
        )

        return

    log(
        "Creating release: "
        f"{RELEASE_TAG}"
    )

    run_command(
        [
            "gh",
            "release",
            "create",
            RELEASE_TAG,
            "--title",
            RELEASE_NAME,
            "--notes",
            (
                "Pune raw open-data "
                f"collection v{VERSION}."
            ),
        ]
    )


def upload_asset(
    path
):

    path = Path(
        path
    )

    if not path.exists():
        return

    if path.stat().st_size == 0:
        log(
            "Skipping empty asset: "
            f"{path}"
        )
        return

    log(
        "Uploading release asset: "
        f"{path}"
    )

    run_command(
        [
            "gh",
            "release",
            "upload",
            RELEASE_TAG,
            str(path),
            "--clobber",
        ]
    )


# ============================================================
# RELEASE UPLOAD
# ============================================================

def upload_release_assets():

    section(
        "UPLOADING RELEASE ASSETS"
    )

    # --------------------------------------------------------
    # Main Pune OSM
    # --------------------------------------------------------

    upload_asset(
        PUNE_OSM_PATH
    )

    # --------------------------------------------------------
    # DEM
    # --------------------------------------------------------

    if DEM_DIR.exists():

        for path in sorted(
            DEM_DIR.iterdir()
        ):

            if path.is_file():

                upload_asset(
                    path
                )

    # --------------------------------------------------------
    # OpenCity catalog/resources
    # --------------------------------------------------------

    if OPENCITY_DIR.exists():

        for path in sorted(
            OPENCITY_DIR.rglob("*")
        ):

            if not path.is_file():
                continue

            upload_asset(
                path
            )

    # --------------------------------------------------------
    # Manifest
    # --------------------------------------------------------

    upload_asset(
        MANIFEST_PATH
    )

    # --------------------------------------------------------
    # Checksums
    # --------------------------------------------------------

    upload_asset(
        CHECKSUM_PATH
    )

    log(
        "Release upload complete."
    )


# ============================================================
# CLEANUP SOURCE
# ============================================================

def cleanup_source_file():

    if SOURCE_OSM_PATH.exists():

        log(
            "Removing intermediate "
            "Western Zone source PBF..."
        )

        SOURCE_OSM_PATH.unlink()


# ============================================================
# FINAL SUMMARY
# ============================================================

def print_final_summary(
    osm_counts,
    opencity_available,
):

    section(
        "FINAL SUMMARY"
    )

    log(
        "Pune OSM:"
    )

    log(
        f"  Nodes     : "
        f"{osm_counts['nodes']:,}"
    )

    log(
        f"  Ways      : "
        f"{osm_counts['ways']:,}"
    )

    log(
        f"  Relations : "
        f"{osm_counts['relations']:,}"
    )

    log("")

    log(
        "OpenCity:"
    )

    if opencity_available:
        log(
            "  AVAILABLE"
        )
    else:
        log(
            "  UNAVAILABLE "
            "(non-fatal)"
        )

    log("")

    log(
        "Release:"
    )

    log(
        f"  {RELEASE_TAG}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    start_time = time.time()

    section(
        f"PUNE RAW DATA "
        f"COLLECTOR v{VERSION}"
    )

    log(
        f"Pune bbox: {PUNE_BBOX}"
    )

    try:

        # ----------------------------------------------------
        # Prepare directories
        # ----------------------------------------------------

        ensure_directory(
            Path("data/raw")
        )

        ensure_directory(
            Path("data/tmp")
        )

        # ----------------------------------------------------
        # GitHub release
        # ----------------------------------------------------

        ensure_release()

        # ----------------------------------------------------
        # 1. Western Zone
        # ----------------------------------------------------

        download_western_zone()

        # ----------------------------------------------------
        # 2. Extract Pune
        # ----------------------------------------------------

        extract_pune_osm()

        # ----------------------------------------------------
        # 3. Validate OSM
        # ----------------------------------------------------

        osm_counts = (
            validate_osm()
        )

        # ----------------------------------------------------
        # 4. DEM
        # ----------------------------------------------------

        collect_dem()

        # ----------------------------------------------------
        # 5. OpenCity catalog
        # ----------------------------------------------------

        opencity = (
            collect_opencity_catalog()
        )

        opencity_available = (
            opencity["available"]
        )

        # ----------------------------------------------------
        # 6. OpenCity resources
        # ----------------------------------------------------

        if opencity_available:

            collect_opencity_resources(
                opencity["result"]
            )

        else:

            log("")
            log(
                "Skipping OpenCity "
                "resource collection "
                "because API is unavailable."
            )

        # ----------------------------------------------------
        # 7. Manifest
        # ----------------------------------------------------

        create_manifest(
            osm_counts,
            opencity_available,
        )

        # ----------------------------------------------------
        # 8. SHA256
        # ----------------------------------------------------

        create_checksums()

        # ----------------------------------------------------
        # 9. Upload
        # ----------------------------------------------------

        upload_release_assets()

        # ----------------------------------------------------
        # 10. Remove 210MB intermediate PBF
        # ----------------------------------------------------

        cleanup_source_file()

        # ----------------------------------------------------
        # Final
        # ----------------------------------------------------

        elapsed = (
            time.time()
            - start_time
        )

        print_final_summary(
            osm_counts,
            opencity_available,
        )

        log("")

        log(
            "Total time: "
            f"{elapsed / 60:.1f} minutes"
        )

        log("")

        section(
            "COLLECTION COMPLETE"
        )

        log(
            "PUNE RAW DATA "
            "COLLECTION SUCCESSFUL"
        )

    except Exception as exc:

        section(
            "ERROR"
        )

        log(
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        sys.exit(1)


if __name__ == "__main__":
    main()
