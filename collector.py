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


VERSION = "2.1"

RELEASE_TAG = "pune-raw-v1"
RELEASE_NAME = "Pune Raw Open Data v1"

PUNE_BBOX = (
    73.70,
    18.40,
    74.05,
    18.70,
)

BASE_DIR = Path("data/raw")

SOURCE_DIR = BASE_DIR / "source"
OSM_DIR = BASE_DIR / "osm"
DEM_DIR = BASE_DIR / "dem"
OPENCITY_DIR = BASE_DIR / "opencity"

RELEASE_DIR = Path("release")
TMP_DIR = Path("data/tmp")

WESTERN_ZONE_URL = (
    "https://download.geofabrik.de/"
    "asia/india/western-zone-latest.osm.pbf"
)

SOURCE_OSM = (
    SOURCE_DIR /
    "western-zone-latest.osm.pbf"
)

FINAL_OSM_NAME = "central-zone.osm.pbf"

FINAL_OSM = (
    OSM_DIR /
    FINAL_OSM_NAME
)

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

MAX_RETRIES = 5

CONNECT_TIMEOUT = 60
READ_TIMEOUT = 1800

CHUNK_SIZE = 1024 * 1024

MAX_DOWNLOAD_SIZE = (
    2 * 1024 * 1024 * 1024
)

session = requests.Session()

session.headers.update(
    {
        "User-Agent":
            "PuneRawCollector/2.1 "
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
    print(message, flush=True)


def fail(message):
    log()
    log("=" * 70)
    log("ERROR")
    log("=" * 70)
    log(message)
    sys.exit(1)


# ============================================================
# HELPERS
# ============================================================

def human_size(size):
    value = float(size)

    for unit in (
        "B",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
    ):
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

    return re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        name,
    )[:180]


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
        MAX_RETRIES + 1,
    ):

        try:

            if part.exists():
                part.unlink()

            log(
                f"Attempt "
                f"{attempt}/{MAX_RETRIES}"
            )

            with session.get(
                url,
                stream=True,
                timeout=(
                    CONNECT_TIMEOUT,
                    READ_TIMEOUT,
                ),
            ) as response:

                response.raise_for_status()

                content_length = (
                    response.headers.get(
                        "Content-Length"
                    )
                )

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
                            % (
                                100
                                * 1024
                                * 1024
                            )
                            < len(chunk)
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

            if size <= 0:
                raise RuntimeError(
                    "Downloaded file is empty."
                )

            if (
                size >
                MAX_DOWNLOAD_SIZE
            ):
                raise RuntimeError(
                    "Downloaded file exceeds "
                    "maximum allowed size."
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
                f"Download failed: {exc}"
            )

            if part.exists():
                part.unlink()

            if attempt < MAX_RETRIES:

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

    fail(
        f"Unable to download: {url}"
    )


# ============================================================
# WESTERN ZONE
# ============================================================

def download_western_zone():

    if (
        SOURCE_OSM.exists()
        and SOURCE_OSM.stat().st_size
        > 100 * 1024 * 1024
    ):
        log(
            "Western Zone source already exists."
        )
        return

    download_file(
        WESTERN_ZONE_URL,
        SOURCE_OSM,
    )


# ============================================================
# PUNE OSM EXTRACTION
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

    run_command(
        [
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
        ],
        "EXTRACTING PUNE OSM",
    )

    if not FINAL_OSM.exists():
        fail(
            "Pune OSM was not created."
        )

    size = FINAL_OSM.stat().st_size

    log(
        f"Pune PBF size: "
        f"{human_size(size)}"
    )

    if size < 100_000:
        fail(
            "Extracted Pune PBF is "
            "suspiciously small."
        )


# ============================================================
# OSM FILEINFO
# ============================================================

def get_osm_fileinfo():

    return run_command(
        [
            "osmium",
            "fileinfo",
            "--extended",
            str(FINAL_OSM),
        ],
        "OSMIUM FILEINFO",
    )


# ============================================================
# COORDINATE EXTRACTION
# ============================================================

def find_coordinate(
    value,
):

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


# ============================================================
# OSM GEOMETRY VALIDATION
# ============================================================

def validate_osm_geometry():

    diagnostic = (
        TMP_DIR /
        "pune-diagnostic.geojsonseq"
    )

    if diagnostic.exists():
        diagnostic.unlink()

    # IMPORTANT:
    # Do NOT use --output-header.
    # osmium 1.16 does not support it.

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
        ],
        "PUNE GEOMETRY DIAGNOSTIC",
    )

    if (
        not diagnostic.exists()
        or diagnostic.stat().st_size == 0
    ):
        fail(
            "Diagnostic GeoJSONSeq "
            "is empty."
        )

    xmin, ymin, xmax, ymax = (
        PUNE_BBOX
    )

    total_samples = 0
    inside_samples = 0

    outside_samples = 0

    first_inside = None

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

            coordinate = (
                find_coordinate(
                    coordinates
                )
            )

            if not coordinate:
                continue

            lon, lat = coordinate

            total_samples += 1

            is_inside = (
                xmin <= lon <= xmax
                and
                ymin <= lat <= ymax
            )

            if is_inside:

                inside_samples += 1

                if first_inside is None:
                    first_inside = (
                        lon,
                        lat,
                    )

            else:
                outside_samples += 1

            if (
                total_samples <= 20
            ):

                log(
                    f"Sample "
                    f"{total_samples}: "
                    f"lon={lon}, "
                    f"lat={lat}, "
                    f"inside={is_inside}"
                )

            # We already have enough
            # evidence after 100 samples.
            if (
                total_samples >= 100
                and inside_samples >= 10
            ):
                break

    log()
    log(
        f"Total samples: "
        f"{total_samples}"
    )

    log(
        f"Inside Pune: "
        f"{inside_samples}"
    )

    log(
        f"Outside Pune: "
        f"{outside_samples}"
    )

    if (
        total_samples == 0
        or inside_samples == 0
    ):
        fail(
            "PUNE GEOMETRY VALIDATION FAILED. "
            "No geometry sample was found "
            "inside the Pune bbox."
        )

    if first_inside:
        log()
        log(
            "First Pune coordinate:"
        )
        log(
            f"longitude = "
            f"{first_inside[0]}"
        )
        log(
            f"latitude  = "
            f"{first_inside[1]}"
        )

    log()
    log(
        "PUNE GEOMETRY VALIDATION PASSED"
    )


# ============================================================
# COMPLETE OSM VALIDATION
# ============================================================

def validate_osm():

    log()
    log("=" * 70)
    log("VALIDATING PUNE OSM")
    log("=" * 70)

    fileinfo = (
        get_osm_fileinfo()
    )

    lower = fileinfo.lower()

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
        int(
            node_match.group(1)
        )
        if node_match
        else 0
    )

    ways = (
        int(
            way_match.group(1)
        )
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
        f"Nodes      : "
        f"{nodes:,}"
    )

    log(
        f"Ways       : "
        f"{ways:,}"
    )

    log(
        f"Relations   : "
        f"{relations:,}"
    )

    if nodes <= 0:
        fail(
            "Pune PBF contains "
            "zero nodes."
        )

    if ways <= 0:
        fail(
            "Pune PBF contains "
            "zero ways."
        )

    validate_osm_geometry()


# ============================================================
# DEM
# ============================================================

def collect_dem(
    manifest,
):

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

        manifest.append(
            {
                "type":
                    "dem",
                "tile":
                    tile,
                "url":
                    url,
                "file":
                    str(output),
                "size":
                    output.stat().st_size,
                "sha256":
                    sha256_file(
                        output
                    ),
            }
        )


# ============================================================
# OPENCITY
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
                    "success=false"
                )

            return data

        except Exception as exc:

            log(
                f"OpenCity attempt "
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

    fail(
        "OpenCity API failed."
    )


def collect_opencity_catalog():

    log()
    log("=" * 70)
    log("COLLECTING OPENCITY CATALOG")
    log("=" * 70)

    datasets = []

    start = 0
    rows = 100

    while True:

        data = opencity_request(
            {
                "fq":
                    f"organization:"
                    f"{OPENCITY_ORG}",
                "rows":
                    rows,
                "start":
                    start,
            }
        )

        result = data.get(
            "result",
            {},
        )

        current = result.get(
            "results",
            [],
        )

        if not current:
            break

        datasets.extend(
            current
        )

        total = int(
            result.get(
                "count",
                len(datasets),
            )
        )

        log(
            f"Datasets: "
            f"{len(datasets)}/"
            f"{total}"
        )

        start += len(
            current
        )

        if start >= total:
            break

    catalog = {
        "success": True,
        "result": {
            "count":
                len(datasets),
            "results":
                datasets,
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

    return catalog


def collect_opencity_resources(
    catalog,
    manifest,
):

    datasets = (
        catalog
        .get("result", {})
        .get("results", [])
    )

    seen = set()

    for dataset in datasets:

        title = dataset.get(
            "title",
            "dataset",
        )

        resources = dataset.get(
            "resources",
            [],
        )

        slug = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            title.lower(),
        )[:100]

        directory = (
            OPENCITY_DIR /
            slug
        )

        directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        for index, resource in enumerate(
            resources,
            start=1,
        ):

            url = resource.get(
                "url"
            )

            if not url:
                continue

            if url in seen:
                continue

            seen.add(
                url
            )

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
                continue

            filename = safe_filename(
                url,
                f"resource-{index}",
            )

            output = (
                directory /
                filename
            )

            try:

                download_file(
                    url,
                    output,
                )

            except Exception as exc:

                log(
                    "WARNING: "
                    f"{url}"
                )

                log(
                    repr(exc)
                )

                continue

            manifest.append(
                {
                    "type":
                        "opencity",
                    "dataset":
                        title,
                    "format":
                        fmt,
                    "url":
                        url,
                    "file":
                        str(output),
                    "size":
                        output.stat().st_size,
                    "sha256":
                        sha256_file(
                            output
                        ),
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
        "bbox": {
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


def create_checksums(
    manifest,
):

    output = (
        RELEASE_DIR /
        "SHA256SUMS.txt"
    )

    lines = []

    for item in manifest:

        checksum = item.get(
            "sha256"
        )

        path = item.get(
            "file"
        )

        if checksum and path:
            lines.append(
                f"{checksum}  "
                f"{Path(path).as_posix()}"
            )

    output.write_text(
        "\n".join(lines)
        + "\n",
        encoding="utf-8",
    )

    return output


# ============================================================
# GITHUB RELEASE
# ============================================================

def ensure_release():

    result = subprocess.run(
        [
            "gh",
            "release",
            "view",
            RELEASE_TAG,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if result.returncode == 0:
        log(
            f"Release exists: "
            f"{RELEASE_TAG}"
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
                "Pune OSM extracted from "
                "Geofabrik Western Zone "
                "and geographically validated."
            ),
        ],
        "CREATING RELEASE",
    )


def upload_asset(
    path,
):

    if (
        not path.exists()
        or path.stat().st_size == 0
    ):
        fail(
            f"Invalid release asset: "
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
        ],
        f"UPLOADING {path.name}",
    )


# ============================================================
# MAIN
# ============================================================

def main():

    started = time.time()

    log()
    log("=" * 70)
    log(
        f"PUNE RAW DATA COLLECTOR "
        f"v{VERSION}"
    )
    log("=" * 70)

    log(
        f"Pune bbox: "
        f"{PUNE_BBOX}"
    )

    for command in (
        "osmium",
        "gh",
    ):

        if shutil.which(
            command
        ) is None:

            fail(
                f"Missing command: "
                f"{command}"
            )

    if not os.environ.get(
        "GH_TOKEN"
    ):
        fail(
            "GH_TOKEN is not set."
        )

    # --------------------------------------------------------
    # Clean temporary directory only.
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
    # OSM
    # --------------------------------------------------------

    download_western_zone()

    extract_pune_osm()

    validate_osm()

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
                sha256_file(
                    FINAL_OSM
                ),
        }
    )

    # --------------------------------------------------------
    # DEM
    # --------------------------------------------------------

    collect_dem(
        manifest
    )

    # --------------------------------------------------------
    # OpenCity
    # --------------------------------------------------------

    catalog = (
        collect_opencity_catalog()
    )

    collect_opencity_resources(
        catalog,
        manifest,
    )

    # --------------------------------------------------------
    # Manifest
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
    # Upload OSM
    # --------------------------------------------------------

    upload_asset(
        FINAL_OSM
    )

    # --------------------------------------------------------
    # Upload DEM
    # --------------------------------------------------------

    for tile in DEM_TILES:

        upload_asset(
            DEM_DIR /
            f"{tile}.hgt.gz"
        )

    # --------------------------------------------------------
    # Upload catalog
    # --------------------------------------------------------

    upload_asset(
        OPENCITY_DIR /
        "package_search.json"
    )

    # --------------------------------------------------------
    # Upload resources
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
    # Upload manifest
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

    log(
        f"OSM size: "
        f"{human_size(FINAL_OSM.stat().st_size)}"
    )

    log(
        f"Manifest files: "
        f"{len(manifest)}"
    )

    log(
        f"Elapsed: "
        f"{elapsed / 60:.1f} minutes"
    )

    log(
        f"Release: "
        f"{RELEASE_TAG}"
    )


if __name__ == "__main__":
    main()
