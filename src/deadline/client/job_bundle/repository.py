# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Bundle repository abstraction for browsing job bundles from local filesystem or S3.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from logging import getLogger
from typing import Optional, Protocol

import yaml
import json

logger = getLogger(__name__)

TEMPLATE_FILENAMES = ("template.yaml", "template.json")
S3_JOB_BUNDLES_PREFIX = "job-bundles"


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
    """Browse job bundles on the local filesystem."""

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
            if not os.path.isdir(full):
                continue
            is_bundle = self._is_bundle(full)
            entries.append(BrowseEntry(name=name, path=full, is_bundle=is_bundle))
        return entries

    def get_bundle_info(self, path: str) -> Optional[BundleInfo]:
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

    @staticmethod
    def _is_bundle(path: str) -> bool:
        for fname in TEMPLATE_FILENAMES:
            if os.path.isfile(os.path.join(path, fname)):
                return True
        return False


class S3BundleRepository:
    """Browse job bundles in an S3 bucket under {rootPrefix}/job-bundles/."""

    def __init__(self, bucket_name: str, root_prefix: str, session=None):
        import boto3 as _boto3

        self._bucket = bucket_name
        # Ensure the prefix ends with /job-bundles/
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
                for cp in page.get("CommonPrefixes", []):
                    child_prefix = cp["Prefix"]
                    name = child_prefix.rstrip("/").rsplit("/", 1)[-1]
                    child_path = f"s3://{self._bucket}/{child_prefix}"
                    is_bundle = self._is_bundle(child_prefix)
                    entries.append(BrowseEntry(name=name, path=child_path, is_bundle=is_bundle))
        except Exception:
            logger.warning("Failed to list S3 prefix %s", prefix, exc_info=True)
        return entries

    def get_bundle_info(self, path: str) -> Optional[BundleInfo]:
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

    def download_bundle(self, path: str, dest_dir: str) -> str:
        """Download all objects under the bundle prefix to a local directory.
        Returns the local path to the downloaded bundle."""
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

    def _is_bundle(self, prefix: str) -> bool:
        """Check if a prefix contains a template file."""
        for fname in TEMPLATE_FILENAMES:
            try:
                self._s3.head_object(Bucket=self._bucket, Key=prefix + fname)
                return True
            except Exception:
                continue
        return False

    def _to_s3_prefix(self, path: str) -> str:
        """Convert an s3:// URI or prefix back to a raw S3 prefix."""
        if path.startswith("s3://"):
            # s3://bucket/prefix/ -> prefix/
            _, _, prefix = path.partition(f"s3://{self._bucket}/")
            return prefix if prefix.endswith("/") else prefix + "/"
        return path if path.endswith("/") else path + "/"
