import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


# ============================================================
# CONFIG
# ============================================================

RELEASE_TAG = "pune-raw-v1"
RELEASE_NAME = "Pune Raw Open Data v1"

BASE_DIR = Path("data/raw")

OSM_DIR = BASE_DIR / "osm"
DEM_DIR = BASE_DIR / "dem"
OPENCITY_DIR = BASE_DIR / "opencity"
RELEASE_DIR = Path("release")

OSM_URL = (
    "https://download.geofabrik.de/asia/india/"
    "central-zone-latest.osm.pbf"
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
}

MAX_RETRIES = 5
CONNECT_TIMEOUT = 60
READ_TIMEOUT = 900
CHUNK_SIZE = 1024 * 1024

# GitHub Release asset safety limit.
# GitHub Releases supports large files, but keeping a safety
# check prevents accidentally uploading broken/unexpected files.
MAX_ASSET_SIZE = 1_900 * 1024 * 1024


# ============================================================
# DIRECTORIES
# ============================================================

for directory in [
    OSM_DIR,
    DEM_DIR,
    OPENCITY_DIR,
    RELEASE_DIR,
]:
    directory.mkdir(parents=True, exist_ok=True)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": (
            "PuneRawCollector/1.0 "
            "(open-data collection project)"
        )
    }
)


# ============================================================
# HELPERS
# ============================================================

def log(message=""):
    print(message, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        while True:
            chunk = file.read(CHUNK_SIZE)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def human_size(size: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]

    value = float(size)

    for unit in units:
        if value < 1024:
            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{value:.2f} PiB"


def safe_filename(url: str, fallback: str) -> str:
    parsed = urlparse(url)

    name = Path(parsed.path).name.strip()

    if not name:
        return fallback

    return name


def download_file(
    url: str,
    output: Path,
    retries: int = MAX_RETRIES,
) -> bool:

    log()
    log("=" * 70)
    log("DOWNLOAD")
    log("=" * 70)
    log(f"URL: {url}")
    log(f"OUT: {output}")

    part = output.with_suffix(output.suffix + ".part")

    for attempt in range(1, retries + 1):

        log()
        log(f"Attempt {attempt}/{retries}")

        try:

            # Remove incomplete previous download.
            if part.exists():
                part.unlink()

            with session.get(
                url,
                stream=True,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=True,
            ) as response:

                response.raise_for_status()

                content_length = response.headers.get(
                    "Content-Length"
                )

                if content_length:
                    log(
                        "Expected size: "
                        + human_size(int(content_length))
                    )

                downloaded = 0

                with part.open("wb") as file:

                    for chunk in response.iter_content(
                        chunk_size=CHUNK_SIZE
                    ):

                        if not chunk:
                            continue

                        file.write(chunk)
                        downloaded += len(chunk)

                        if downloaded % (
                            100 * 1024 * 1024
                        ) < CHUNK_SIZE:

                            log(
                                "Downloaded: "
                                + human_size(downloaded)
                            )

            if not part.exists():
                raise RuntimeError(
                    "Temporary file was not created."
                )

            size = part.stat().st_size

            if size == 0:
                raise RuntimeError(
                    "Downloaded file is empty."
                )

            if size > MAX_ASSET_SIZE:
                raise RuntimeError(
                    "Downloaded file exceeds safety limit: "
                    + human_size(size)
                )

            part.replace(output)

            log()
            log("DOWNLOAD SUCCESS")
            log(f"Size: {human_size(size)}")

            return True

        except requests.exceptions.RequestException as exc:

            log()
            log("REQUEST ERROR:")
            log(repr(exc))

        except Exception as exc:

            log()
            log("ERROR:")
            log(repr(exc))

        if attempt < retries:
            wait = min(30 * attempt, 120)

            log(
                f"Retrying in {wait} seconds..."
            )

            time.sleep(wait)

    if part.exists():
        part.unlink()

    log()
    log("DOWNLOAD FAILED")
    log(url)

    return False


def upload_release_asset(path: Path) -> bool:

    log()
    log("=" * 70)
    log("GITHUB RELEASE UPLOAD")
    log("=" * 70)
    log(f"File: {path}")

    if not path.exists():
        log("ERROR: File does not exist.")
        return False

    size = path.stat().st_size

    if size == 0:
        log("ERROR: File is empty.")
        return False

    if size > MAX_ASSET_SIZE:
        log(
            "ERROR: File exceeds safety limit: "
            + human_size(size)
        )
        return False

    command = [
        "gh",
        "release",
        "upload",
        RELEASE_TAG,
        str(path),
        "--clobber",
    ]

    try:

        result = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
        )

        if result.stdout:
            print(result.stdout)

        if result.stderr:
            print(result.stderr)

        if result.returncode != 0:
            log(
                f"GitHub upload failed: "
                f"exit code {result.returncode}"
            )
            return False

        log("UPLOAD SUCCESS")

        return True

    except Exception as exc:

        log("GitHub upload exception:")
        log(repr(exc))

        return False


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
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if check.returncode == 0:

        log(
            f"Release already exists: "
            f"{RELEASE_TAG}"
        )

        return

    log("Creating release...")

    result = subprocess.run(
        [
            "gh",
            "release",
            "create",
            RELEASE_TAG,
            "--title",
            RELEASE_NAME,
            "--notes",
            (
                "Pune Raw Open Data collected "
                "from open data sources."
            ),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:

        print(result.stdout)
        print(result.stderr)

        raise RuntimeError(
            "Could not create GitHub Release."
        )

    log("Release created.")


# ============================================================
# OSM
# ============================================================

def collect_osm(manifest):

    output = OSM_DIR / "central-zone.osm.pbf"

    if output.exists() and output.stat().st_size > 100_000_000:

        log()
        log("OSM already exists:")
        log(str(output))

    else:

        success = download_file(
            OSM_URL,
            output,
        )

        if not success:
            raise RuntimeError(
                "OSM download failed."
            )

    checksum = sha256_file(output)

    manifest.append(
        {
            "source": "OSM / Geofabrik",
            "url": OSM_URL,
            "file": str(output),
            "size": output.stat().st_size,
            "sha256": checksum,
        }
    )

    log(f"OSM SHA256: {checksum}")

    upload_release_asset(output)


# ============================================================
# DEM
# ============================================================

def collect_dem(manifest):

    for tile in DEM_TILES:

        latitude = tile[:3]

        url = (
            f"{DEM_BASE_URL}/"
            f"{latitude}/"
            f"{tile}.hgt.gz"
        )

        output = DEM_DIR / f"{tile}.hgt.gz"

        if output.exists() and output.stat().st_size > 0:

            log()
            log(f"DEM already exists: {tile}")

        else:

            success = download_file(
                url,
                output,
            )

            if not success:

                log(
                    f"WARNING: DEM failed: {tile}"
                )

                continue

        checksum = sha256_file(output)

        manifest.append(
            {
                "source": "AWS Terrain Tiles",
                "tile": tile,
                "url": url,
                "file": str(output),
                "size": output.stat().st_size,
                "sha256": checksum,
            }
        )

        log(
            f"{tile} SHA256: {checksum}"
        )

        upload_release_asset(output)


# ============================================================
# OPENCITY API
# ============================================================

def collect_opencity_catalog():

    output = OPENCITY_DIR / "package_search.json"

    params = {
        "fq": f"organization:{OPENCITY_ORG}",
        "rows": 100,
        "start": 0,
    }

    url = OPENCITY_API

    log()
    log("=" * 70)
    log("OPENCITY CATALOG")
    log("=" * 70)

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            log(
                f"API attempt "
                f"{attempt}/{MAX_RETRIES}"
            )

            response = session.get(
                url,
                params=params,
                timeout=(CONNECT_TIMEOUT, 300),
            )

            response.raise_for_status()

            data = response.json()

            if not data.get("success"):
                raise RuntimeError(
                    "OpenCity API returned success=false."
                )

            output.write_text(
                json.dumps(
                    data,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            log(
                f"Catalog saved: {output}"
            )

            upload_release_asset(output)

            return data

        except Exception as exc:

            log(
                "OpenCity API error:"
            )
            log(repr(exc))

            if attempt < MAX_RETRIES:
                time.sleep(
                    min(10 * attempt, 60)
                )

    raise RuntimeError(
        "OpenCity catalog download failed."
    )


# ============================================================
# OPENCITY RESOURCES
# ============================================================

def collect_opencity_resources(
    catalog,
    manifest,
):

    result = catalog.get("result", {})
    datasets = result.get("results", [])

    log()
    log("=" * 70)
    log("OPENCITY DATASETS")
    log("=" * 70)
    log(
        f"Datasets found: {len(datasets)}"
    )

    for dataset_index, dataset in enumerate(
        datasets,
        start=1,
    ):

        title = dataset.get(
            "title",
            f"dataset-{dataset_index}",
        )

        resources = dataset.get(
            "resources",
            [],
        )

        log()
        log(
            f"[{dataset_index}/{len(datasets)}] "
            f"{title}"
        )

        log(
            f"Resources: {len(resources)}"
        )

        for resource_index, resource in enumerate(
            resources,
            start=1,
        ):

            url = resource.get("url")

            if not url:
                continue

            resource_format = str(
                resource.get(
                    "format",
                    "",
                )
            ).upper().strip()

            if (
                resource_format
                and resource_format not in ALLOWED_FORMATS
            ):
                log(
                    f"Skipping unsupported format: "
                    f"{resource_format}"
                )
                continue

            original_name = resource.get(
                "name",
                "",
            ).strip()

            filename = safe_filename(
                url,
                f"resource-{resource_index}",
            )

            if original_name:

                original_suffix = Path(
                    original_name
                ).suffix

                if original_suffix:
                    filename = (
                        Path(filename).stem
                        + original_suffix
                    )

            dataset_slug = (
                title.lower()
                .replace("/", "_")
                .replace("\\", "_")
                .replace(" ", "_")
            )

            dataset_slug = "".join(
                character
                for character in dataset_slug
                if character.isalnum()
                or character in "_-"
            )

            dataset_dir = (
                OPENCITY_DIR
                / dataset_slug[:100]
            )

            dataset_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            output = dataset_dir / filename

            log()
            log(
                f"Resource "
                f"{resource_index}/{len(resources)}"
            )

            log(f"Format: {resource_format}")
            log(f"URL: {url}")

            if output.exists() and output.stat().st_size > 0:

                log(
                    "Already downloaded."
                )

                success = True

            else:

                success = download_file(
                    url,
                    output,
                )

            if not success:

                log()
                log(
                    "WARNING: Resource failed."
                )

                continue

            checksum = sha256_file(
                output
            )

            entry = {
                "source": "OpenCity",
                "dataset": title,
                "format": resource_format,
                "url": url,
                "file": str(output),
                "size": output.stat().st_size,
                "sha256": checksum,
            }

            manifest.append(entry)

            log(
                f"SHA256: {checksum}"
            )

            upload_release_asset(output)


# ============================================================
# MANIFEST
# ============================================================

def create_manifest(manifest):

    output = RELEASE_DIR / "manifest.json"

    data = {
        "project": "Pune 3D Raw Data",
        "release_tag": RELEASE_TAG,
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        ),
        "files": manifest,
    }

    output.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    log()
    log(
        f"Manifest created: {output}"
    )

    upload_release_asset(output)


# ============================================================
# CHECKSUMS
# ============================================================

def create_checksums(manifest):

    output = RELEASE_DIR / "SHA256SUMS.txt"

    lines = []

    for entry in manifest:

        checksum = entry.get(
            "sha256"
        )

        file_path = entry.get(
            "file"
        )

        if checksum and file_path:

            relative = Path(
                file_path
            ).as_posix()

            lines.append(
                f"{checksum}  {relative}"
            )

    output.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    log()
    log(
        f"Checksums created: {output}"
    )

    upload_release_asset(output)


# ============================================================
# MAIN
# ============================================================

def main():

    start_time = time.time()

    log()
    log("=" * 70)
    log("PUNE RAW DATA COLLECTOR")
    log("=" * 70)
    log()
    log(f"Release: {RELEASE_TAG}")
    log("Language: Python")
    log()

    if not os.environ.get("GH_TOKEN"):
        log(
            "WARNING: GH_TOKEN is not set."
        )

    ensure_release()

    manifest = []

    # --------------------------------------------------------
    # OSM
    # --------------------------------------------------------

    collect_osm(manifest)

    # --------------------------------------------------------
    # DEM
    # --------------------------------------------------------

    collect_dem(manifest)

    # --------------------------------------------------------
    # OpenCity
    # --------------------------------------------------------

    catalog = collect_opencity_catalog()

    collect_opencity_resources(
        catalog,
        manifest,
    )

    # --------------------------------------------------------
    # Manifest + Checksums
    # --------------------------------------------------------

    create_manifest(manifest)

    create_checksums(manifest)

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    elapsed = time.time() - start_time

    log()
    log("=" * 70)
    log("COLLECTION FINISHED")
    log("=" * 70)
    log()
    log(
        f"Successful files: {len(manifest)}"
    )
    log(
        f"Elapsed time: {elapsed / 60:.1f} minutes"
    )
    log()
    log(
        f"Release: {RELEASE_TAG}"
    )


if __name__ == "__main__":
    main()
