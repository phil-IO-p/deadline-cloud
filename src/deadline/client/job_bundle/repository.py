# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Bundle repository abstraction for browsing job bundles from local filesystem or S3.
Supports both directory-based bundles and .ojd archive bundles (zip format).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from logging import getLogger
from typing import Optional, Protocol

import yaml

from ..api import get_boto3_session
from ..config import config_file
from ..config.config_file import get_cache_directory
from ..exceptions import DeadlineOperationError
from ...job_attachments._aws.deadline import get_queue

logger = getLogger(__name__)

TEMPLATE_FILENAMES = ("template.yaml", "template.json")
S3_JOB_BUNDLES_PREFIX = "job-bundles"
ARCHIVE_EXTENSION = ".ojd"
CACHE_META_FILENAME = ".bundle_cache_meta.json"

# S3 user-defined metadata is limited to 2 KB total (keys + values, UTF-8 encoded).
# Keys include the "x-amz-meta-" prefix (12 bytes) added by S3.
# Budget: 4 keys × (12 + ~9 avg key len) = ~83 bytes for keys, leaving ~1,965 for values.
# See: https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingMetadata.html#UserMetadata
METADATA_KEY_NAME = "ojd-name"
METADATA_KEY_DESC = "ojd-desc"
METADATA_KEY_STEPS = "ojd-steps"
METADATA_KEY_PARAMS = "ojd-params"
METADATA_LIMIT_NAME = 256
METADATA_LIMIT_DESC = 480
METADATA_LIMIT_STEPS = 480
METADATA_LIMIT_PARAMS = 700

# POSIX only forbids / and null; Windows also forbids \ : * ? " < > |
# Control characters (0x00-0x1F, 0x7F) are problematic on all platforms
_WINDOWS_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')
_POSIX_UNSAFE_CHARS = re.compile(r"[/\x00-\x1f\x7f]+")


def sanitize_bundle_name(name: str) -> str:
    """Sanitize a bundle name for use as a local directory name.

    Only replaces characters illegal on the current OS, preserving the
    original name as closely as possible.
    """
    pattern = _WINDOWS_UNSAFE_CHARS if sys.platform == "win32" else _POSIX_UNSAFE_CHARS
    name = pattern.sub("_", name).strip("_")
    if not name:
        raise ValueError("Bundle name is empty after sanitization")
    return name


def _is_archive(name: str) -> bool:
    """Check if a filename is an .ojd archive."""
    return name.endswith(ARCHIVE_EXTENSION)


def _strip_archive_ext(name: str) -> str:
    """Remove the .ojd extension from a filename."""
    if name.endswith(ARCHIVE_EXTENSION):
        return name[: -len(ARCHIVE_EXTENSION)]
    return name


def _safe_zip_extract(zf: zipfile.ZipFile, dest_dir: str) -> None:
    """Extract a zip file, rejecting archives with entries that would escape dest_dir."""
    dest = os.path.realpath(dest_dir)

    for member in zf.namelist():
        if os.path.isabs(member):
            raise ValueError(f"Archive contains absolute path: {member}")
        target = os.path.realpath(os.path.join(dest, member))
        try:
            common = os.path.commonpath([dest, target])
        except ValueError:
            # On Windows, different drives have no common path
            raise ValueError(f"Archive entry would extract outside target directory: {member}")
        if common != dest:
            raise ValueError(f"Archive entry would extract outside target directory: {member}")
    zf.extractall(dest_dir)


def _extract_archive(archive_path: str, dest_dir: str) -> None:
    """Extract an .ojd archive to dest_dir."""
    with zipfile.ZipFile(archive_path, "r") as zf:
        _safe_zip_extract(zf, dest_dir)


def _read_template_from_archive_path(archive_path: str) -> Optional[tuple[str, str]]:
    """Read a template file from a local .ojd archive. Returns (contents, filename) or None."""
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            return _read_template_from_zip(zf)
    except Exception:
        logger.debug("Failed to read template from archive %s", archive_path, exc_info=True)
        return None


def _read_template_from_zip(zf: zipfile.ZipFile) -> Optional[tuple[str, str]]:
    """Read a template file from an open ZipFile. Returns (contents, filename) or None."""
    names = zf.namelist()
    for fname in TEMPLATE_FILENAMES:
        matches = [n for n in names if n == fname or n.endswith("/" + fname)]
        matches.sort(key=lambda n: n.count("/"))
        if matches:
            return zf.read(matches[0]).decode("utf-8"), fname
    return None


def _read_template_from_bytes(data: bytes) -> Optional[tuple[str, str]]:
    """Read a template from .ojd archive bytes in memory. Returns (contents, template_filename) or None."""
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            return _read_template_from_zip(zf)
    except Exception:
        return None


def _extract_archive_from_bytes(data: bytes, dest_dir: str) -> None:
    """Extract an .ojd archive from bytes in memory to dest_dir."""
    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        _safe_zip_extract(zf, dest_dir)


@dataclass
class BundleInfo:
    """Metadata extracted from a job bundle's template."""

    path: str
    name: str
    description: str = ""
    step_names: list[str] = field(default_factory=list)
    parameters: list[dict] = field(default_factory=list)


@dataclass
class BrowseEntry:
    """A single item in the browser listing."""

    name: str
    path: str
    is_bundle: bool
    is_archive: bool = False


class BundleRepository(Protocol):
    def list_entries(self, path: str) -> list[BrowseEntry]:
        """List immediate children of `path`. Returns folders and bundles."""
        ...

    def get_bundle_info(self, path: str) -> Optional[BundleInfo]:
        """Load and return metadata for the bundle at `path`, or None if invalid."""
        ...

    def root_path(self) -> str:
        """The starting path for browsing."""
        ...


def _parse_template(raw: str, filename: str) -> Optional[dict]:
    """Parse a template file's contents, returning the dict or None on failure."""
    try:
        if filename.endswith(".json"):
            return json.loads(raw)
        else:
            return yaml.safe_load(raw)
    except Exception:
        logger.debug("Failed to parse template %s", filename, exc_info=True)
        return None


def _extract_bundle_info(
    template: dict, path: str, parameter_values: Optional[dict] = None
) -> BundleInfo:
    """Extract BundleInfo from a parsed template dict.
    If parameter_values is provided, merges values into the parameter definitions."""
    params = template.get("parameterDefinitions", [])

    # Build a lookup from parameter_values file
    pv_map: dict[str, str] = {}
    if parameter_values:
        for pv in parameter_values.get("parameterValues", []):
            if "name" in pv and "value" in pv:
                pv_map[pv["name"]] = pv["value"]

    # Attach resolved value to each parameter: parameter_values > default > empty
    for p in params:
        name = p.get("name", "")
        if name in pv_map:
            p["_display_value"] = pv_map[name]
        elif "default" in p:
            p["_display_value"] = str(p["default"])

    raw_name = template.get("name", os.path.basename(path.rstrip("/")))

    return BundleInfo(
        path=path,
        name=raw_name,
        description=template.get("description", ""),
        step_names=[s.get("name", "") for s in template.get("steps", [])],
        parameters=params,
    )


class LocalBundleRepository:
    """Browse job bundles on the local filesystem. Supports directories and archives."""

    def __init__(self, root: str = "", include_archives: bool = True):
        self._root = root or os.path.expanduser("~")
        self._include_archives = include_archives

    def root_path(self) -> str:
        return self._root

    def list_entries(self, path: str) -> list[BrowseEntry]:
        entries: list[BrowseEntry] = []
        try:
            with os.scandir(path) as it:
                children = sorted(it, key=lambda e: e.name)
        except OSError:
            return entries
        for entry in children:
            if entry.is_dir(follow_symlinks=False):
                is_bundle = self._is_dir_bundle(entry.path)
                entries.append(BrowseEntry(name=entry.name, path=entry.path, is_bundle=is_bundle))
            elif (
                self._include_archives
                and entry.is_file(follow_symlinks=False)
                and _is_archive(entry.name)
            ):
                # Only show archives that actually contain a template
                if _read_template_from_archive_path(entry.path) is not None:
                    entries.append(
                        BrowseEntry(
                            name=_strip_archive_ext(entry.name),
                            path=entry.path,
                            is_bundle=True,
                            is_archive=True,
                        )
                    )
        return entries

    def get_bundle_info(self, path: str) -> Optional[BundleInfo]:
        if os.path.isfile(path) and _is_archive(path):
            return self._get_archive_bundle_info(path)
        return self._get_dir_bundle_info(path)

    def extract_bundle(self, path: str, dest_dir: str) -> str:
        """Extract an archive bundle to dest_dir. Returns path to the extracted bundle."""
        bundle_name = _strip_archive_ext(os.path.basename(path))
        extract_dir = os.path.join(dest_dir, bundle_name)
        os.makedirs(extract_dir, exist_ok=True)
        _extract_archive(path, extract_dir)
        # If the archive contains a single top-level directory, use that
        contents = os.listdir(extract_dir)
        if len(contents) == 1 and os.path.isdir(os.path.join(extract_dir, contents[0])):
            return os.path.join(extract_dir, contents[0])
        return extract_dir

    def _get_dir_bundle_info(self, path: str) -> Optional[BundleInfo]:
        for fname in TEMPLATE_FILENAMES:
            fpath = os.path.join(path, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, encoding="utf-8") as f:
                        raw = f.read()
                except OSError:
                    return None
                template = _parse_template(raw, fname)
                if template:
                    pv = self._read_parameter_values(path)
                    return _extract_bundle_info(template, path, pv)
        return None

    @staticmethod
    def _read_parameter_values(path: str) -> Optional[dict]:
        """Read parameter_values.yaml or .json from a bundle directory."""
        for pvname in ("parameter_values.yaml", "parameter_values.json"):
            pvpath = os.path.join(path, pvname)
            if os.path.isfile(pvpath):
                try:
                    with open(pvpath, encoding="utf-8") as f:
                        return _parse_template(f.read(), pvname)
                except OSError:
                    pass
        return None

    def _get_archive_bundle_info(self, path: str) -> Optional[BundleInfo]:
        result = _read_template_from_archive_path(path)
        if result:
            raw, fname = result
            template = _parse_template(raw, fname)
            if template:
                return _extract_bundle_info(template, path)
        return None

    @staticmethod
    def _is_dir_bundle(path: str) -> bool:
        for fname in TEMPLATE_FILENAMES:
            if os.path.isfile(os.path.join(path, fname)):
                return True
        return False


# ── S3 Cache ─────────────────────────────────────────────────


def _get_bundle_cache_dir() -> str:
    """Get the root cache directory for S3 bundle archives."""
    return os.path.join(get_cache_directory(), "job-bundles")


def _cache_key(bucket: str, s3_key: str) -> str:
    """Deterministic cache subdirectory from bucket + key."""
    h = hashlib.sha256(f"{bucket}/{s3_key}".encode()).hexdigest()[:16]
    name = _strip_archive_ext(s3_key.rstrip("/").rsplit("/", 1)[-1])
    return os.path.join(h, name)


def _read_cache_meta(cache_dir: str) -> Optional[dict]:
    meta_path = os.path.join(cache_dir, CACHE_META_FILENAME)
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _normalize_etag(etag: Optional[str]) -> str:
    """Strip surrounding quotes from an ETag for consistent comparison."""
    if not etag:
        return ""
    return etag.strip('"')


def _write_cache_meta(cache_dir: str, etag: str, last_modified: str) -> None:
    meta_path = os.path.join(cache_dir, CACHE_META_FILENAME)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"etag": etag, "last_modified": last_modified}, f)


# ── S3 Repository ────────────────────────────────────────────


def _bundle_info_from_s3_metadata(metadata: dict, path: str) -> Optional[BundleInfo]:
    """Try to construct BundleInfo from S3 user metadata set during upload.
    Returns None if the required 'ojd-name' key is missing."""
    name = metadata.get(METADATA_KEY_NAME)
    if not name:
        return None
    params = []
    params_str = metadata.get(METADATA_KEY_PARAMS, "")
    if params_str:
        for p in params_str.split(","):
            parts = p.split(":", 1)
            if len(parts) == 2:
                params.append({"name": parts[0], "type": parts[1]})
    return BundleInfo(
        path=path,
        name=name,
        description=metadata.get(METADATA_KEY_DESC, ""),
        step_names=[s for s in metadata.get(METADATA_KEY_STEPS, "").split(",") if s],
        parameters=params,
    )


class S3BundleRepository:
    """Browse .ojd job bundles in an S3 bucket under {rootPrefix}/job-bundles/.
    Only .ojd archives are supported. Subfolders are shown for navigation only.
    Archive bundles are cached locally with ETag validation."""

    def __init__(self, bucket_name: str, root_prefix: str, session=None):
        import boto3 as _boto3

        self._bucket = bucket_name
        base = root_prefix.rstrip("/")
        self._prefix = f"{base}/{S3_JOB_BUNDLES_PREFIX}/"
        self._session = session or _boto3.Session()
        self._s3 = self._session.client("s3")

    @classmethod
    def from_config(cls, config=None) -> "S3BundleRepository":
        """Create an S3BundleRepository from the user's Deadline Cloud configuration.

        Handles session creation, queue lookup, and attachment settings extraction.
        Raises DeadlineOperationError if farm/queue is not configured or has no attachments.
        """
        farm_id = config_file.get_setting("defaults.farm_id", config=config)
        queue_id = config_file.get_setting("defaults.queue_id", config=config)
        if not farm_id or not queue_id:
            raise DeadlineOperationError("A default farm and queue must be configured.")
        session = get_boto3_session(config=config)
        queue = get_queue(farm_id=farm_id, queue_id=queue_id, session=session)
        if not queue.jobAttachmentSettings:
            raise DeadlineOperationError(
                f"Queue {queue_id} does not have job attachment settings configured."
            )
        return cls(
            bucket_name=queue.jobAttachmentSettings.s3BucketName,
            root_prefix=queue.jobAttachmentSettings.rootPrefix,
            session=session,
        )

    def root_path(self) -> str:
        return f"s3://{self._bucket}/{self._prefix}"

    def list_entries(self, path: str) -> list[BrowseEntry]:
        prefix = self._to_s3_prefix(path)
        entries: list[BrowseEntry] = []
        child_prefixes: list[tuple[str, str, str]] = []  # (name, child_prefix, child_path)
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix, Delimiter="/"):
                # Subfolders (for navigation only, not bundles)
                for cp in page.get("CommonPrefixes", []):
                    child_prefix = cp["Prefix"]
                    name = child_prefix.rstrip("/").rsplit("/", 1)[-1]
                    child_path = f"s3://{self._bucket}/{child_prefix}"
                    child_prefixes.append((name, child_prefix, child_path))
                # .ojd archive bundles
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    name = key.rsplit("/", 1)[-1] if "/" in key else key
                    if _is_archive(name):
                        s3_path = f"s3://{self._bucket}/{key}"
                        entries.append(
                            BrowseEntry(
                                name=_strip_archive_ext(name),
                                path=s3_path,
                                is_bundle=True,
                                is_archive=True,
                            )
                        )
        except Exception:
            logger.warning("Failed to list S3 prefix %s", prefix, exc_info=True)
            raise

        # Subfolders are shown for navigation but never as bundles
        for name, child_prefix, child_path in child_prefixes:
            entries.append(BrowseEntry(name=name, path=child_path, is_bundle=False))

        entries.sort(key=lambda e: e.name.lower())
        return entries

    def get_bundle_info(self, path: str) -> Optional[BundleInfo]:
        return self._get_archive_bundle_info(path)

    def resolve_bundle(self, path: str, dest_dir: str) -> str:
        """Resolve an S3 .ojd bundle to a local directory path.
        Downloads, caches with ETag, and extracts.
        Returns the local path to the usable bundle directory."""
        return self._resolve_archive_bundle(path)

    def download_full_bundle(self, path: str, dest_dir: str) -> str:
        """Download a complete S3 .ojd bundle to a local directory.
        Uses the ETag cache for repeated access."""
        return self._resolve_archive_bundle(path)

    # ── Archive bundles ──────────────────────────────────────

    def _get_archive_bundle_info(self, path: str) -> Optional[BundleInfo]:
        key = self._to_s3_key(path)
        cache_dir = os.path.join(_get_bundle_cache_dir(), _cache_key(self._bucket, key))
        meta = _read_cache_meta(cache_dir)

        # Always do a head_object first — it's cheap and gives us both
        # ETag (for cache validation) and user metadata (for preview without download)
        head = None
        try:
            head = self._s3.head_object(Bucket=self._bucket, Key=key)
        except Exception:
            pass  # head_object failure is non-fatal; we fall through to download

        if head:
            # Check local cache validity
            cache_valid = meta and _normalize_etag(head.get("ETag")) == _normalize_etag(
                meta.get("etag")
            )

            # Try S3 user metadata for preview (set by 'deadline bundle upload')
            s3_metadata = head.get("Metadata", {})
            info = _bundle_info_from_s3_metadata(s3_metadata, path)
            if info:
                # If cache is valid, enrich with parameter values from the cached bundle
                if cache_valid:
                    cached_info = self._read_info_from_cache(cache_dir, path)
                    if cached_info:
                        info.parameters = cached_info.parameters
                return info

            # No S3 metadata — fall back to cache
            if cache_valid:
                return self._read_info_from_cache(cache_dir, path)

        # Cache miss or stale — download, cache, and parse
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=key)
            data = resp["Body"].read()
            etag = resp.get("ETag", "")
            last_modified = str(resp.get("LastModified", ""))
        except Exception:
            logger.debug("Failed to download S3 archive %s", key, exc_info=True)
            return None

        # Extract to cache so resolve_bundle can reuse it
        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir)
        os.makedirs(cache_dir, exist_ok=True)
        try:
            _extract_archive_from_bytes(data, cache_dir)
            _write_cache_meta(cache_dir, etag, last_modified)
        except Exception:
            logger.debug("Failed to cache S3 archive %s", key, exc_info=True)

        # Parse template from the downloaded bytes
        result = _read_template_from_bytes(data)
        if result:
            raw, fname = result
            template = _parse_template(raw, fname)
            if template:
                return _extract_bundle_info(template, path)
        return None

    def _resolve_archive_bundle(self, path: str) -> str:
        key = self._to_s3_key(path)
        cache_dir = os.path.join(_get_bundle_cache_dir(), _cache_key(self._bucket, key))

        # Check if cache is valid
        meta = _read_cache_meta(cache_dir)
        if meta:
            try:
                head = self._s3.head_object(Bucket=self._bucket, Key=key)
                if _normalize_etag(head.get("ETag")) == _normalize_etag(meta.get("etag")):
                    bundle_path = self._find_bundle_in_cache(cache_dir)
                    if bundle_path:
                        logger.info("Using cached bundle: %s", bundle_path)
                        return bundle_path
            except Exception:
                pass  # Cache validation failed; re-download below

        # Download, extract, and cache
        resp = self._s3.get_object(Bucket=self._bucket, Key=key)
        data = resp["Body"].read()
        etag = resp.get("ETag", "")
        last_modified = str(resp.get("LastModified", ""))

        # Clear old cache and extract
        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir)
        os.makedirs(cache_dir, exist_ok=True)

        _extract_archive_from_bytes(data, cache_dir)
        _write_cache_meta(cache_dir, etag, last_modified)

        bundle_path = self._find_bundle_in_cache(cache_dir)
        if bundle_path:
            return bundle_path
        return cache_dir

    def _read_info_from_cache(self, cache_dir: str, original_path: str) -> Optional[BundleInfo]:
        """Read bundle info from an already-extracted cache directory."""
        bundle_dir = self._find_bundle_in_cache(cache_dir)
        if not bundle_dir:
            return None
        for fname in TEMPLATE_FILENAMES:
            fpath = os.path.join(bundle_dir, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, encoding="utf-8") as f:
                        raw = f.read()
                except OSError:
                    return None
                template = _parse_template(raw, fname)
                if template:
                    pv = LocalBundleRepository._read_parameter_values(bundle_dir)
                    return _extract_bundle_info(template, original_path, pv)
        return None

    @staticmethod
    def _find_bundle_in_cache(cache_dir: str) -> Optional[str]:
        """Find the actual bundle directory within a cache dir.
        Handles both flat extraction and single-directory-wrapped archives."""
        # Check if template is directly in cache_dir
        for fname in TEMPLATE_FILENAMES:
            if os.path.isfile(os.path.join(cache_dir, fname)):
                return cache_dir
        # Check one level deep (single wrapper directory)
        try:
            contents = [
                d
                for d in os.listdir(cache_dir)
                if os.path.isdir(os.path.join(cache_dir, d)) and d != CACHE_META_FILENAME
            ]
        except OSError:
            return None
        for d in contents:
            subdir = os.path.join(cache_dir, d)
            for fname in TEMPLATE_FILENAMES:
                if os.path.isfile(os.path.join(subdir, fname)):
                    return subdir
        return None

    # ── Helpers ──────────────────────────────────────────────

    def _to_s3_key(self, path: str) -> str:
        """Convert an s3:// URI to a raw S3 key."""
        if path.startswith("s3://"):
            _, _, key = path.partition(f"s3://{self._bucket}/")
            return key
        return path

    def _to_s3_prefix(self, path: str) -> str:
        """Convert an s3:// URI or prefix to a raw S3 prefix ending with /."""
        key = self._to_s3_key(path)
        return key if key.endswith("/") else key + "/"
