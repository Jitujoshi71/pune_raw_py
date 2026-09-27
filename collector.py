#!/usr/bin/env python3

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


# ============================================================
# PUNE RAW DATA COLLECTOR - FRESH
# ============================================================

VERSION = "2.0"

RELEASE_TAG = "pune-raw-v1"
RELEASE_NAME = "Pune Raw Open Data v1"

# ------------------------------------------------------------
# Pune bbox
#
# xmin, ymin, xmax, ymax
# ------------------------------------------------------------

PUNE_BBOX = (
    73.70,
    18.40,
    74.05,
    18.70,
)

PUNE_CENTER = (
    73.8567,
    18.5204,
)

# ------------------------------------------------------------
# Directories
# ------------------------------------------------------------

BASE_DIR = Path("data/raw")

SOURCE_DIR = BASE_DIR / "source"
OSM_DIR = BASE_DIR / "osm"
DEM_DIR = BASE_DIR / "dem"
OPENCITY_DIR = BASE_DIR / "opencity"

RELEASE_DIR = Path("release")

TMP_DIR = Path("data/tmp")

# ------------------------------------------------------------
# Geofabrik
# ------------------------------------------------------------

WESTERN_ZONE_URL = (
    "https://download.geofabrik.de/"
    "asia/india/western-zone-latest.osm.pbf"
)

FINAL_OSM_NAME = "central-zone.osm.pbf"

FINAL_OSM = OSM_DIR / FINAL_OSM_NAME

SOURCE_OSM = (
    SOURCE_DIR /
    "western-zone-latest.osm.pbf"
)

# ------------------------------------------------------------
# DEM
# ------------------------------------------------------------

DEM_BASE_URL = (
    "https://s3.amazonaws.com/"
    "elevation-tiles-prod/skadi"
)

DEM_TILES = [
    "N18E073",
    "N18E074",
    "N19E073",
    "N19E074",
]

# ------------------------------------------------------------
# OpenCity
# ------------------------------------------------------------

OPENCITY_API = (
    "https://data.opencity.in/api/3/action/package_search"
)

OPENCITY_ORG = (
    "pune-municipal-corporation"
)

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

# ------------------------------------------------------------
# Download settings
# ------------------------------------------------------------

MAX_RETRIES = 5

CONNECT_TIMEOUT = 60

READ_TIMEOUT = 1800

CHUNK_SIZE = 1024 * 1024

MAX_DOWNLOAD_SIZE = (
    2 * 1024 * 1024 * 1024
)

# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------

session = requests.Session()

session.headers.update(
    {
        "User-Agent":
            "PuneRawCollector/2.0 "
            "(open-data project)",
    }
)


# ============================================================
# DIRECTORIES
# ============================================================

for directory in (
    SOURCE_DIR,
    OSM_DIR,
    DEM_DIR,
    OPENCITY_DIR,
    RELEASE_DIR,
    TMP_DIR,
):
    directory.mkdir(
        parents=True,
        exist_ok=True,
    )


# ============================================================
# LOGGING
# ============================================================

def log(message=""):
    print(
        message,
        flush=True,
    )


def fail(message):
    log()
    log("=" * 70)
    log("ERROR")
    log("=" * 70)
    log(message)
    sys.exit(1)


# ============================================================
# UTILITIES
# ============================================================

def human_size(size):
    value = float(size)

    units = (
        "B",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
    )

    for unit in units:
        if value < 1024:
            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{value:.2f} PiB"


def sha256_file(path):
    digest = hashlib.sha256()

    with path.open(
        "rb"
    ) as file:

        while True:

            chunk = file.read(
                CHUNK_SIZE
            )

            if not chunk:
                break

            digest.update(
                chunk
            )

    return digest.hexdigest()


def safe_filename(
    url,
    fallback,
):
    parsed = urlparse(url)

    name = Path(
        parsed.path
    ).name.strip()

    if not name:
        return fallback

    # Remove characters that are unsafe
    # in release/archive filenames.
    name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        name,
    )

    return name[:180]


def run_command(
    command,
    description=None,
):
    if description:
        log()
        log("=" * 70)
        log(description)
        log("=" * 70)

    log(
        "$ " +
        " ".join(
            str(x)
            for x in command
        )
    )

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if result.stdout:
        print(
            result.stdout,
            end="",
            flush=True,
        )

    if result.returncode != 0:
        fail(
            "Command failed with "
            f"exit code {result.returncode}"
        )

    return result.stdout


# ============================================================
# DOWNLOAD
# ============================================================

def download_file(
    url,
    output,
    retries=MAX_RETRIES,
):
    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    part = Path(
        str(output) + ".part"
    )

    log()
    log("=" * 70)
    log("DOWNLOAD")
    log("=" * 70)
    log(
        f"URL: {url}"
    )
    log(
        f"OUT: {output}"
    )

    for attempt in range(
        1,
        retries + 1,
    ):

        log()
        log(
            f"Attempt "
            f"{attempt}/{retries}"
        )

        try:

            if part.exists():
                part.unlink()

            with session.get(
                url,
                stream=True,
                timeout=(
                    CONNECT_TIMEOUT,
                    READ_TIMEOUT,
                ),
                allow_redirects=True,
            ) as response:

                response.raise_for_status()

                content_length = (
                    response.headers.get(
                        "Content-Length"
                    )
                )

                expected = None

                if content_length:
                    expected = int(
                        content_length
                    )

                    log(
                        "Expected size: "
                        + human_size(
                            expected
                        )
                    )

                    if (
                        expected
                        > MAX_DOWNLOAD_SIZE
                    ):
                        raise RuntimeError(
                            "Download exceeds "
                            "safety limit."
                        )

                downloaded = 0

                with part.open(
                    "wb"
                ) as file:

                    for chunk in response.iter_content(
                        chunk_size=CHUNK_SIZE
                    ):

                        if not chunk:
                            continue

                        file.write(
                            chunk
                        )

                        downloaded += (
                            len(chunk)
                        )

                        if (
                            downloaded
                            // (
                                100
                                * 1024
                                * 1024
                            )
                            != (
                                downloaded
                                - len(chunk)
                            )
                            // (
                                100
                                * 1024
                                * 1024
                            )
                        ):
                            log(
                                "Downloaded: "
                                + human_size(
                                    downloaded
                                )
                            )

            if not part.exists():
                raise RuntimeError(
                    "Partial file missing."
                )

            size = part.stat().st_size

            if size == 0:
                raise RuntimeError(
                    "Downloaded file is empty."
                )

            if (
                size
                > MAX_DOWNLOAD_SIZE
            ):
                raise RuntimeError(
                    "Downloaded file exceeds "
                    "safety limit."
                )

            part.replace(
                output
            )

            log(
                "DOWNLOAD SUCCESS"
            )

            log(
                f"Size: "
                f"{human_size(size)}"
            )

            return

        except Exception as exc:

            log(
                "DOWNLOAD ERROR:"
            )

            log(
                repr(exc)
            )

            if part.exists():
                part.unlink()

            if attempt < retries:

                wait = min(
                    30 * attempt,
                    120,
                )

                log(
                    f"Retrying in "
                    f"{wait} seconds..."
                )

                time.sleep(
                    wait
                )

    raise RuntimeError(
        f"Download failed after "
        f"{retries} attempts: {url}"
    )


# ============================================================
# OSM SOURCE
# ============================================================

def download_western_zone():
    if (
        SOURCE_OSM.exists()
        and SOURCE_OSM.stat().st_size
        > 100 * 1024 * 1024
    ):
        log()
        log(
            "Western Zone source already exists."
        )

        return

    download_file(
        WESTERN_ZONE_URL,
        SOURCE_OSM,
    )


# ============================================================
# OSM EXTRACT
# ============================================================

def extract_pune_osm():
    if FINAL_OSM.exists():
        FINAL_OSM.unlink()

    xmin, ymin, xmax, ymax = (
        PUNE_BBOX
    )

    bbox = (
        f"{xmin},"
        f"{ymin},"
        f"{xmax},"
        f"{ymax}"
    )

    command = [
        "osmium",
        "extract",
        "--bbox",
        bbox,
        "--strategy",
        "smart",
        str(SOURCE_OSM),
        "-o",
        str(FINAL_OSM),
        "--overwrite",
    ]

    run_command(
        command,
        "EXTRACTING PUNE OSM",
    )

    if not FINAL_OSM.exists():
        fail(
            "Pune OSM was not created."
        )

    size = FINAL_OSM.stat().st_size

    log()
    log(
        "Pune PBF size: "
        + human_size(size)
    )

    # 92-byte empty PBF / similarly tiny output
    # is always rejected.
    if size < 100_000:
        fail(
            "Extracted Pune PBF is "
            "suspiciously small."
        )


# ============================================================
# OSM VALIDATION
# ============================================================

def validate_osm():
    log()
    log("=" * 70)
    log("VALIDATING PUNE OSM")
    log("=" * 70)

    output = run_command(
        [
            "osmium",
            "fileinfo",
            "--extended",
            str(FINAL_OSM),
        ],
        "OSMIUM FILEINFO",
    )

    lower = output.lower()

    # --------------------------------------------------------
    # Basic object count validation
    # --------------------------------------------------------

    node_match = re.search(
        r"number of nodes:\s*([0-9]+)",
        lower,
    )

    way_match = re.search(
        r"number of ways:\s*([0-9]+)",
        lower,
    )

    relation_match = re.search(
        r"number of relations:\s*([0-9]+)",
        lower,
    )

    nodes = (
        int(node_match.group(1))
        if node_match
        else 0
    )

    ways = (
        int(way_match.group(1))
        if way_match
        else 0
    )

    relations = (
        int(
            relation_match.group(1)
        )
        if relation_match
        else 0
    )

    log()
    log(
        f"Nodes     : {nodes:,}"
    )

    log(
        f"Ways      : {ways:,}"
    )

    log(
        f"Relations  : {relations:,}"
    )

    if (
        nodes <= 0
        or ways <= 0
    ):
        fail(
            "Pune PBF has no usable "
            "OSM nodes/ways."
        )

    # --------------------------------------------------------
    # Export a tiny diagnostic sample
    # --------------------------------------------------------

    diagnostic = (
        TMP_DIR /
        "pune-diagnostic.geojsonseq"
    )

    if diagnostic.exists():
        diagnostic.unlink()

    run_command(
        [
            "osmium",
            "export",
            str(FINAL_OSM),
            "-o",
            str(diagnostic),
            "--overwrite",
            "--output-format=geojsonseq",
            "--geometry-types",
            "points,linestring,polygon",
            "--output-header",
            "false",
        ],
        "PUNE GEOMETRY DIAGNOSTIC",
    )

    # --------------------------------------------------------
    # Inspect first valid coordinates.
    # --------------------------------------------------------

    found = 0
    inside = 0

    xmin, ymin, xmax, ymax = (
        PUNE_BBOX
    )

    with diagnostic.open(
        "r",
        encoding="utf-8",
    ) as file:

        for raw_line in file:

            line = raw_line.strip()

            if not line:
                continue

            line = line.lstrip(
                "\x1e"
            )

            try:
                feature = json.loads(
                    line
                )
            except Exception:
                continue

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

            # Find a coordinate recursively.
            def find_coordinate(value):
                if (
                    isinstance(value, list)
                    and len(value) >= 2
                    and isinstance(
                        value[0],
                        (int, float),
                    )
                    and isinstance(
                        value[1],
                        (int, float),
                    )
                ):
                    return (
                        float(value[0]),
                        float(value[1]),
                    )

                if isinstance(
                    value,
                    list,
                ):
                    for item in value:
                        result = (
                            find_coordinate(
                                item
                            )
                        )

                        if result:
                            return result

                return None

            coordinate = (
                find_coordinate(
                    coordinates
                )
            )

            if not coordinate:
                continue

            lon, lat = coordinate

            found += 1

            log()
            log(
                f"Sample #{found}: "
                f"lon={lon}, "
                f"lat={lat}"
            )

            if (
                xmin <= lon <= xmax
                and ymin <= lat <= ymax
            ):
                inside += 1

            if found >= 20:
                break

    log()
    log(
        f"Diagnostic samples: "
        f"{found}"
    )

    log(
        f"Samples inside bbox: "
        f"{inside}"
    )

    # At least some sample coordinates must
    # actually be Pune coordinates.
    if (
        found == 0
        or inside == 0
    ):
        fail(
            "Pune PBF validation FAILED. "
            "No diagnostic coordinates "
            "were found inside Pune bbox."
        )

    log()
    log(
        "PUNE OSM VALIDATION PASSED"
    )


# ============================================================
# DEM
# ============================================================

def collect_dem(manifest):
    log()
    log("=" * 70)
    log("COLLECTING DEM")
    log("=" * 70)

    for tile in DEM_TILES:

        latitude = tile[:3]

        url = (
            f"{DEM_BASE_URL}/"
            f"{latitude}/"
            f"{tile}.hgt.gz"
        )

        output = (
            DEM_DIR /
            f"{tile}.hgt.gz"
        )

        download_file(
            url,
            output,
        )

        checksum = (
            sha256_file(output)
        )

        manifest.append(
            {
                "type": "dem",
                "source": "AWS Terrain Tiles",
                "tile": tile,
                "url": url,
                "file": str(output),
                "size": output.stat().st_size,
                "sha256": checksum,
            }
        )

        log(
            f"{tile}: "
            f"{human_size(output.stat().st_size)}"
        )


# ============================================================
# OPENCITY API
# ============================================================

def opencity_request(
    params,
):
    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):

        try:

            response = session.get(
                OPENCITY_API,
                params=params,
                timeout=(
                    CONNECT_TIMEOUT,
                    300,
                ),
            )

            response.raise_for_status()

            data = response.json()

            if not data.get(
                "success"
            ):
                raise RuntimeError(
                    "OpenCity API "
                    "returned success=false."
                )

            return data

        except Exception as exc:

            log(
                f"OpenCity API attempt "
                f"{attempt} failed: "
                f"{exc}"
            )

            if attempt < MAX_RETRIES:
                time.sleep(
                    min(
                        10 * attempt,
                        60,
                    )
                )

    raise RuntimeError(
        "OpenCity API failed."
    )


# ============================================================
# OPENCITY CATALOG
# ============================================================

def collect_opencity_catalog():
    log()
    log("=" * 70)
    log("COLLECTING OPENCITY CATALOG")
    log("=" * 70)

    all_datasets = []

    start = 0
    rows = 100

    while True:

        params = {
            "fq":
                f"organization:"
                f"{OPENCITY_ORG}",
            "rows": rows,
            "start": start,
        }

        data = opencity_request(
            params
        )

        result = data.get(
            "result",
            {},
        )

        datasets = result.get(
            "results",
            [],
        )

        if not datasets:
            break

        all_datasets.extend(
            datasets
        )

        total = int(
            result.get(
                "count",
                len(all_datasets),
            )
        )

        log(
            f"Catalog: "
            f"{len(all_datasets)}/"
            f"{total}"
        )

        start += len(
            datasets
        )

        if (
            start >= total
        ):
            break

    catalog = {
        "success": True,
        "result": {
            "count":
                len(all_datasets),
            "results":
                all_datasets,
        },
    }

    output = (
        OPENCITY_DIR /
        "package_search.json"
    )

    output.write_text(
        json.dumps(
            catalog,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    log(
        f"Datasets collected: "
        f"{len(all_datasets)}"
    )

    return catalog


# ============================================================
# OPENCITY RESOURCES
# ============================================================

def collect_opencity_resources(
    catalog,
    manifest,
):
    datasets = (
        catalog
        .get("result", {})
        .get("results", [])
    )

    seen_urls = set()

    for index, dataset in enumerate(
        datasets,
        start=1,
    ):

        title = dataset.get(
            "title",
            f"dataset-{index}",
        )

        resources = dataset.get(
            "resources",
            [],
        )

        log()
        log(
            "=" * 70
        )
        log(
            f"DATASET "
            f"{index}/{len(datasets)}"
        )
        log(
            title
        )

        slug = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            title.lower(),
        )

        slug = slug[:100]

        dataset_dir = (
            OPENCITY_DIR /
            slug
        )

        dataset_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        for resource_index, resource in enumerate(
            resources,
            start=1,
        ):

            url = resource.get(
                "url"
            )

            if not url:
                continue

            if url in seen_urls:
                continue

            seen_urls.add(
                url
            )

            resource_format = str(
                resource.get(
                    "format",
                    "",
                )
            ).upper().strip()

            if (
                resource_format
                and resource_format
                not in ALLOWED_FORMATS
            ):
                log(
                    "Skipping format: "
                    f"{resource_format}"
                )
                continue

            original_name = (
                resource.get(
                    "name",
                    "",
                )
                or ""
            ).strip()

            filename = safe_filename(
                url,
                f"resource-{resource_index}",
            )

            if original_name:
                suffix = Path(
                    original_name
                ).suffix

                if suffix:
                    filename = (
                        Path(filename).stem
                        + suffix
                    )

            output = (
                dataset_dir /
                filename
            )

            log()
            log(
                f"Resource "
                f"{resource_index}/"
                f"{len(resources)}"
            )

            log(
                f"Format: "
                f"{resource_format}"
            )

            log(
                f"URL: {url}"
            )

            try:

                download_file(
                    url,
                    output,
                )

            except Exception as exc:

                log(
                    "WARNING: "
                    "Resource download failed:"
                )

                log(
                    repr(exc)
                )

                continue

            checksum = (
                sha256_file(
                    output
                )
            )

            manifest.append(
                {
                    "type":
                        "opencity",
                    "dataset":
                        title,
                    "format":
                        resource_format,
                    "url":
                        url,
                    "file":
                        str(output),
                    "size":
                        output.stat().st_size,
                    "sha256":
                        checksum,
                }
            )


# ============================================================
# MANIFEST
# ============================================================

def create_manifest(
    manifest,
):
    output = (
        RELEASE_DIR /
        "manifest.json"
    )

    data = {
        "project":
            "Pune Raw Open Data",
        "version":
            VERSION,
        "release_tag":
            RELEASE_TAG,
        "generated_at":
            time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            ),
        "pune_bbox": {
            "xmin":
                PUNE_BBOX[0],
            "ymin":
                PUNE_BBOX[1],
            "xmax":
                PUNE_BBOX[2],
            "ymax":
                PUNE_BBOX[3],
        },
        "osm_source":
            WESTERN_ZONE_URL,
        "files":
            manifest,
    }

    output.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return output


# ============================================================
# SHA256
# ============================================================

def create_checksums(
    manifest,
):
    output = (
        RELEASE_DIR /
        "SHA256SUMS.txt"
    )

    lines = []

    for entry in manifest:

        checksum = entry.get(
            "sha256"
        )

        file_path = entry.get(
            "file"
        )

        if checksum and file_path:
            lines.append(
                f"{checksum}  "
                f"{Path(file_path).as_posix()}"
            )

    output.write_text(
        "\n".join(lines)
        + "\n",
        encoding="utf-8",
    )

    return output


# ============================================================
# RELEASE
# ============================================================

def ensure_release():
    log()
    log("=" * 70)
    log("GITHUB RELEASE")
    log("=" * 70)

    check = subprocess.run(
        [
            "gh",
            "release",
            "view",
            RELEASE_TAG,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if check.returncode == 0:
        log(
            "Release exists:"
        )
        log(
            RELEASE_TAG
        )
        return

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
                "Fresh Pune raw open data. "
                "OSM source is Western Zone "
                "and the released PBF is "
                "validated against the Pune bbox."
            ),
        ],
        "CREATING RELEASE",
    )


# ============================================================
# RELEASE UPLOAD
# ============================================================

def upload_asset(
    path,
):
    if not path.exists():
        fail(
            f"Release asset missing: "
            f"{path}"
        )

    size = path.stat().st_size

    if size == 0:
        fail(
            f"Release asset is empty: "
            f"{path}"
        )

    log()
    log(
        f"Uploading: "
        f"{path.name} "
        f"({human_size(size)})"
    )

    result = subprocess.run(
        [
            "gh",
            "release",
            "upload",
            RELEASE_TAG,
            str(path),
            "--clobber",
        ],
        text=True,
    )

    if result.returncode != 0:
        fail(
            f"GitHub Release upload failed: "
            f"{path}"
        )


# ============================================================
# MAIN
# ============================================================

def main():

    started = time.time()

    log()
    log("=" * 70)
    log(
        "PUNE RAW DATA COLLECTOR "
        f"v{VERSION}"
    )
    log("=" * 70)

    log()
    log(
        f"Pune bbox: "
        f"{PUNE_BBOX}"
    )

    # --------------------------------------------------------
    # Required commands
    # --------------------------------------------------------

    for command in (
        "osmium",
        "gh",
    ):
        if shutil.which(
            command
        ) is None:
            fail(
                f"Required command missing: "
                f"{command}"
            )

    # --------------------------------------------------------
    # GitHub authentication
    # --------------------------------------------------------

    if not os.environ.get(
        "GH_TOKEN"
    ):
        fail(
            "GH_TOKEN is not set."
        )

    # --------------------------------------------------------
    # Clean temporary data
    # --------------------------------------------------------

    if TMP_DIR.exists():
        shutil.rmtree(
            TMP_DIR
        )

    TMP_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # Release
    # --------------------------------------------------------

    ensure_release()

    manifest = []

    # --------------------------------------------------------
    # 1. Western Zone
    # --------------------------------------------------------

    download_western_zone()

    # --------------------------------------------------------
    # 2. Pune extraction
    # --------------------------------------------------------

    extract_pune_osm()

    # --------------------------------------------------------
    # 3. Pune validation
    # --------------------------------------------------------

    validate_osm()

    # --------------------------------------------------------
    # 4. OSM manifest
    # --------------------------------------------------------

    osm_checksum = (
        sha256_file(
            FINAL_OSM
        )
    )

    manifest.append(
        {
            "type":
                "osm",
            "source":
                "Geofabrik Western Zone",
            "source_url":
                WESTERN_ZONE_URL,
            "bbox":
                list(PUNE_BBOX),
            "file":
                str(FINAL_OSM),
            "release_filename":
                FINAL_OSM_NAME,
            "size":
                FINAL_OSM.stat().st_size,
            "sha256":
                osm_checksum,
        }
    )

    # --------------------------------------------------------
    # 5. DEM
    # --------------------------------------------------------

    collect_dem(
        manifest
    )

    # --------------------------------------------------------
    # 6. OpenCity
    # --------------------------------------------------------

    catalog = (
        collect_opencity_catalog()
    )

    collect_opencity_resources(
        catalog,
        manifest,
    )

    # --------------------------------------------------------
    # 7. Manifest
    # --------------------------------------------------------

    manifest_path = (
        create_manifest(
            manifest
        )
    )

    checksum_path = (
        create_checksums(
            manifest
        )
    )

    # --------------------------------------------------------
    # 8. Upload OSM
    # --------------------------------------------------------

    upload_asset(
        FINAL_OSM
    )

    # --------------------------------------------------------
    # 9. Upload DEM
    # --------------------------------------------------------

    for tile in DEM_TILES:

        upload_asset(
            DEM_DIR /
            f"{tile}.hgt.gz"
        )

    # --------------------------------------------------------
    # 10. Upload OpenCity catalog
    # --------------------------------------------------------

    upload_asset(
        OPENCITY_DIR /
        "package_search.json"
    )

    # --------------------------------------------------------
    # 11. Upload OpenCity resources
    #
    # Upload every successfully downloaded resource.
    # --------------------------------------------------------

    for path in OPENCITY_DIR.rglob(
        "*"
    ):

        if (
            path.is_file()
            and path.name
            != "package_search.json"
        ):
            upload_asset(
                path
            )

    # --------------------------------------------------------
    # 12. Manifest/checksum upload
    # --------------------------------------------------------

    upload_asset(
        manifest_path
    )

    upload_asset(
        checksum_path
    )

    # --------------------------------------------------------
    # Final
    # --------------------------------------------------------

    elapsed = (
        time.time()
        - started
    )

    log()
    log("=" * 70)
    log(
        "PUNE RAW COLLECTION COMPLETE"
    )
    log("=" * 70)

    log()
    log(
        f"Files in manifest: "
        f"{len(manifest)}"
    )

    log(
        f"Pune OSM: "
        f"{human_size(FINAL_OSM.stat().st_size)}"
    )

    log(
        f"Elapsed: "
        f"{elapsed / 60:.1f} minutes"
    )

    log()
    log(
        f"Release: "
        f"{RELEASE_TAG}"
    )

    log()
    log(
        "FINAL OSM:"
    )

    log(
        str(FINAL_OSM)
    )


if __name__ == "__main__":
    main()
