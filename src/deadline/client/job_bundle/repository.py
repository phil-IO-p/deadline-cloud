# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Bundle repository abstraction for browsing job bundles from local filesystem or S3.
Supports both directory-based bundles and archive bundles (.zip, .tar.gz, etc.).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from logging import getLogger
from typing import Optional, Protocol

import yaml

logger = getLogger(__name__)

TEMPLATE_FILENAMES = ("template.yaml", "template.json")
S3_JOB_BUNDLES_PREFIX = "job-bundles"
ARCHIVE_EXTENSIONS = (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")
CACHE_META_FILENAME = ".bundle_cache_meta.json"


def _is_archive(name: str) -> bool:
    """Check if a filename looks like a supported archive."""
    return any(name.endswith(ext) for ext in ARCHIVE_EXTENSIONS)


def _strip_archive_ext(name: str) -> str:
    """Remove the archive extension from a filename."""
    for ext in ARCHIVE_EXTENSIONS:
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _extract_archive(archive_path: str, dest_dir: str) -> None:
    """Extract an archive to dest_dir."""
    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest_dir)
    else:
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(dest_dir, filter="data")


def _read_template_from_archive_path(archive_path: str) -> Optional[tuple[str, str]]:
    """Read a template file from a local archive. Returns (contents, filename) or None."""
    if archive_path.endswith(".zip"):
        return _read_template_from_zip_path(archive_path)
    else:
        return _read_template_from_tar_path(archive_path)


def _read_template_from_zip_path(archive_path: str) -> Optional[tuple[str, str]]:
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            return _read_template_from_zip(zf)
    except Exception:
        logger.debug("Failed to read template from zip %s", archive_path, exc_info=True)
        return None


def _read_template_from_tar_path(archive_path: str) -> Optional[tuple[str, str]]:
    try:
        with tarfile.open(archive_path, "r:*") as tf:
            return _read_template_from_tar(tf)
    except Exception:
        logger.debug("Failed to read template from tar %s", archive_path, exc_info=True)
        return None


def _read_template_from_zip(zf: zipfile.ZipFile) -> Optional[tuple[str, str]]:
    """Read a template file from an open ZipFile. Returns (contents, filename) or None."""
    names = zf.namelist()
    for fname in TEMPLATE_FILENAMES:
        # Check both root-level and single-directory-wrapped
        matches = [n for n in names if n == fname or n.endswith("/" + fname)]
        # Prefer the shallowest match
        matches.sort(key=lambda n: n.count("/"))
        if matches:
            return zf.read(matches[0]).decode("utf-8"), fname
    return None


def _read_template_from_tar(tf: tarfile.TarFile) -> Optional[tuple[str, str]]:
    """Read a template file from an open TarFile. Returns (contents, filename) or None."""
    members = tf.getnames()
    for fname in TEMPLATE_FILENAMES:
        matches = [n for n in members if n == fname or n.endswith("/" + fname)]
        matches.sort(key=lambda n: n.count("/"))
        if matches:
            f = tf.extractfile(matches[0])
            if f:
                return f.read().decode("utf-8"), fname
    return None


def _read_template_from_bytes(data: bytes, filename: str) -> Optional[tuple[str, str]]:
    """Read a template from archive bytes in memory. Returns (contents, template_filename) or None."""
    if filename.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                return _read_template_from_zip(zf)
        except Exception:
            return None
    else:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
                return _read_template_from_tar(tf)
        except Exception:
            return None


def _extract_archive_from_bytes(data: bytes, filename: str, dest_dir: str) -> None:
    """Extract an archive from bytes in memory to dest_dir."""
    if filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            zf.extractall(dest_dir)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            tf.extractall(dest_dir, filter="data")


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


def _extract_bundle_info(template: dict, path: str) -> BundleInfo:
    """Extract BundleInfo from a parsed template dict."""
    return BundleInfo(
        path=path,
        name=template.get("name", os.path.basename(path.rstrip("/"))),
        description=template.get("description", ""),
        step_names=[s.get("name", "") for s in template.get("steps", [])],
        parameters=template.get("parameterDefinitions", []),
    )


class LocalBundleRepository:
    """Browse job bundles on the local filesystem. Supports directories and archives."""

    def __init__(self, root: str = ""):
        self._root = root or os.path.expanduser("~")

    def root_path(self) -> str:
        return self._root

    def list_entries(self, path: str) -> list[BrowseEntry]:
        entries: list[BrowseEntry] = []
        try:
            children = sorted(os.listdir(path))
        except OSError:
            return entries
        for name in children:
            full = os.path.join(path, name)
            if os.path.isdir(full):
                is_bundle = self._is_dir_bundle(full)
                entries.append(BrowseEntry(name=name, path=full, is_bundle=is_bundle))
            elif _is_archive(name) and os.path.isfile(full):
                entries.append(
                    BrowseEntry(
                        name=_strip_archive_ext(name),
                        path=full,
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
                    return _extract_bundle_info(template, path)
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
    from ..config.config_file import get_cache_directory

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


def _write_cache_meta(cache_dir: str, etag: str, last_modified: str) -> None:
    meta_path = os.path.join(cache_dir, CACHE_META_FILENAME)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"etag": etag, "last_modified": last_modified}, f)


# ── S3 Repository ────────────────────────────────────────────


class S3BundleRepository:
    """Browse job bundles in an S3 bucket under {rootPrefix}/job-bundles/.
    Supports both folder-based bundles and archive bundles (.zip, .tar.gz, etc.).
    Archive bundles are cached locally with ETag validation."""

    def __init__(self, bucket_name: str, root_prefix: str, session=None):
        import boto3 as _boto3

        self._bucket = bucket_name
        base = root_prefix.rstrip("/")
        self._prefix = f"{base}/{S3_JOB_BUNDLES_PREFIX}/"
        self._session = session or _boto3.Session()
        self._s3 = self._session.client("s3")

    def root_path(self) -> str:
        return f"s3://{self._bucket}/{self._prefix}"

    def list_entries(self, path: str) -> list[BrowseEntry]:
        prefix = self._to_s3_prefix(path)
        entries: list[BrowseEntry] = []
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self._bucket, Prefix=prefix, Delimiter="/"
            ):
                # Folder-based bundles (common prefixes)
                for cp in page.get("CommonPrefixes", []):
                    child_prefix = cp["Prefix"]
                    name = child_prefix.rstrip("/").rsplit("/", 1)[-1]
                    child_path = f"s3://{self._bucket}/{child_prefix}"
                    is_bundle = self._is_folder_bundle(child_prefix)
                    entries.append(BrowseEntry(name=name, path=child_path, is_bundle=is_bundle))
                # Archive bundles (objects with archive extensions)
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
        return entries

    def get_bundle_info(self, path: str) -> Optional[BundleInfo]:
        if self._path_is_archive(path):
            return self._get_archive_bundle_info(path)
        return self._get_folder_bundle_info(path)

    def resolve_bundle(self, path: str, dest_dir: str) -> str:
        """Resolve an S3 bundle to a local directory path.
        For archives: downloads, caches with ETag, and extracts.
        For folders: downloads all objects to dest_dir.
        Returns the local path to the usable bundle directory."""
        if self._path_is_archive(path):
            return self._resolve_archive_bundle(path)
        return self._download_folder_bundle(path, dest_dir)

    # ── Folder bundles ───────────────────────────────────────

    def _get_folder_bundle_info(self, path: str) -> Optional[BundleInfo]:
        prefix = self._to_s3_prefix(path)
        for fname in TEMPLATE_FILENAMES:
            key = prefix + fname
            try:
                resp = self._s3.get_object(Bucket=self._bucket, Key=key)
                raw = resp["Body"].read().decode("utf-8")
                template = _parse_template(raw, fname)
                if template:
                    return _extract_bundle_info(template, path)
            except self._s3.exceptions.NoSuchKey:
                continue
            except Exception:
                logger.debug("Failed to get S3 object %s", key, exc_info=True)
                continue
        return None

    def _download_folder_bundle(self, path: str, dest_dir: str) -> str:
        prefix = self._to_s3_prefix(path)
        bundle_name = prefix.rstrip("/").rsplit("/", 1)[-1]
        local_bundle = os.path.join(dest_dir, bundle_name)
        os.makedirs(local_bundle, exist_ok=True)

        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(prefix) :]
                if not rel:
                    continue
                local_path = os.path.join(local_bundle, rel)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                self._s3.download_file(self._bucket, key, local_path)

        return local_bundle

    def _is_folder_bundle(self, prefix: str) -> bool:
        for fname in TEMPLATE_FILENAMES:
            try:
                self._s3.head_object(Bucket=self._bucket, Key=prefix + fname)
                return True
            except Exception:
                continue
        return False

    # ── Archive bundles ──────────────────────────────────────

    def _get_archive_bundle_info(self, path: str) -> Optional[BundleInfo]:
        key = self._to_s3_key(path)
        cache_dir = os.path.join(_get_bundle_cache_dir(), _cache_key(self._bucket, key))
        meta = _read_cache_meta(cache_dir)

        if meta:
            try:
                head = self._s3.head_object(Bucket=self._bucket, Key=key)
                if head.get("ETag") == meta.get("etag"):
                    # Cache is valid — read template from extracted cache
                    return self._read_info_from_cache(cache_dir, path)
            except Exception:
                pass

        # Cache miss or stale — download, cache, and parse
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=key)
            data = resp["Body"].read()
            etag = resp.get("ETag", "")
            last_modified = str(resp.get("LastModified", ""))
        except Exception:
            logger.debug("Failed to download S3 archive %s", key, exc_info=True)
            return None

        filename = key.rsplit("/", 1)[-1]

        # Extract to cache so resolve_bundle can reuse it
        if os.path.exists(cache_dir):
            import shutil

            shutil.rmtree(cache_dir)
        os.makedirs(cache_dir, exist_ok=True)
        try:
            _extract_archive_from_bytes(data, filename, cache_dir)
            _write_cache_meta(cache_dir, etag, last_modified)
        except Exception:
            logger.debug("Failed to cache S3 archive %s", key, exc_info=True)

        # Parse template from the downloaded bytes
        result = _read_template_from_bytes(data, filename)
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
                if head.get("ETag") == meta.get("etag"):
                    bundle_path = self._find_bundle_in_cache(cache_dir)
                    if bundle_path:
                        logger.info("Using cached bundle: %s", bundle_path)
                        return bundle_path
            except Exception:
                pass

        # Download, extract, and cache
        resp = self._s3.get_object(Bucket=self._bucket, Key=key)
        data = resp["Body"].read()
        etag = resp.get("ETag", "")
        last_modified = str(resp.get("LastModified", ""))

        # Clear old cache and extract
        if os.path.exists(cache_dir):
            import shutil

            shutil.rmtree(cache_dir)
        os.makedirs(cache_dir, exist_ok=True)

        filename = key.rsplit("/", 1)[-1]
        _extract_archive_from_bytes(data, filename, cache_dir)
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
                    return _extract_bundle_info(template, original_path)
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

    @staticmethod
    def _path_is_archive(path: str) -> bool:
        # Strip s3:// URI to get the key, then check extension
        name = path.rstrip("/").rsplit("/", 1)[-1]
        return _is_archive(name)

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
