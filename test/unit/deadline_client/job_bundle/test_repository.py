# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the job bundle repository module."""

from __future__ import annotations

import json
import os
import sys
import zipfile

import pytest
import yaml

from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from deadline.client.job_bundle.repository import (
    LocalBundleRepository,
    S3BundleRepository,
    VISIBILITY_MAX_RETRIES,
    _bundle_info_from_s3_metadata,
    _extract_bundle_info,
    _is_archive,
    _parse_template,
    _read_template_from_archive_path,
    _safe_zip_extract,
    _strip_archive_ext,
    sanitize_bundle_name,
)


class TestParseTemplate:
    def test_parse_yaml(self):
        raw = "name: Test\nsteps:\n- name: Step1\n"
        result = _parse_template(raw, "template.yaml")
        assert result == {"name": "Test", "steps": [{"name": "Step1"}]}

    def test_parse_json(self):
        raw = json.dumps({"name": "Test", "steps": [{"name": "Step1"}]})
        result = _parse_template(raw, "template.json")
        assert result == {"name": "Test", "steps": [{"name": "Step1"}]}

    def test_parse_invalid_yaml(self):
        result = _parse_template("{{invalid", "template.yaml")
        assert result is None

    def test_parse_invalid_json(self):
        result = _parse_template("{invalid", "template.json")
        assert result is None


class TestExtractBundleInfo:
    def test_full_template(self):
        template = {
            "name": "My Job",
            "description": "A test job",
            "steps": [{"name": "Step1"}, {"name": "Step2"}],
            "parameterDefinitions": [
                {"name": "Param1", "type": "STRING"},
                {"name": "Param2", "type": "PATH"},
            ],
        }
        info = _extract_bundle_info(template, "/path/to/bundle")
        assert info.name == "My Job"
        assert info.description == "A test job"
        assert info.step_names == ["Step1", "Step2"]
        assert len(info.parameters) == 2

    def test_minimal_template(self):
        template = {"steps": [{"name": "OnlyStep"}]}
        info = _extract_bundle_info(template, "/path/to/bundle")
        assert info.name == "bundle"
        assert info.description == ""
        assert info.step_names == ["OnlyStep"]
        assert info.parameters == []

    def test_parameter_values_from_file(self):
        template = {
            "name": "Job",
            "steps": [],
            "parameterDefinitions": [
                {"name": "Frames", "type": "STRING", "default": "1-10"},
                {"name": "Output", "type": "PATH"},
            ],
        }
        pv = {"parameterValues": [{"name": "Frames", "value": "1-50"}]}
        info = _extract_bundle_info(template, "/path", pv)
        frames = next(p for p in info.parameters if p["name"] == "Frames")
        output = next(p for p in info.parameters if p["name"] == "Output")
        assert frames["_display_value"] == "1-50"  # from parameter_values
        assert "_display_value" not in output  # no value or default

    def test_parameter_default_used_when_no_value(self):
        template = {
            "name": "Job",
            "steps": [],
            "parameterDefinitions": [
                {"name": "Frames", "type": "STRING", "default": "1-10"},
            ],
        }
        info = _extract_bundle_info(template, "/path")
        frames = info.parameters[0]
        assert frames["_display_value"] == "1-10"

    def test_name_with_param_reference(self):
        template = {
            "name": "Render {{Param.SceneName}}",
            "steps": [],
            "parameterDefinitions": [
                {"name": "SceneName", "type": "STRING", "default": "my_scene"},
            ],
        }
        info = _extract_bundle_info(template, "/path")
        assert info.name == "Render {{Param.SceneName}}"

    def test_name_not_resolved_with_parameter_values(self):
        template = {
            "name": "{{Param.JobName}}",
            "steps": [],
            "parameterDefinitions": [
                {"name": "JobName", "type": "STRING", "default": "Default Name"},
            ],
        }
        pv = {"parameterValues": [{"name": "JobName", "value": "Custom Name"}]}
        info = _extract_bundle_info(template, "/path", pv)
        assert info.name == "{{Param.JobName}}"

    def test_name_unresolved_param(self):
        template = {
            "name": "{{Param.Missing}}",
            "steps": [],
            "parameterDefinitions": [],
        }
        info = _extract_bundle_info(template, "/path")
        assert info.name == "{{Param.Missing}}"

    def test_name_not_resolved_from_pv(self):
        """Parameter values don't affect the displayed name."""
        template = {
            "name": "{{Param.JobName}}",
            "steps": [],
            "parameterDefinitions": [],
        }
        pv = {"parameterValues": [{"name": "JobName", "value": "From PV"}]}
        info = _extract_bundle_info(template, "/path", pv)
        assert info.name == "{{Param.JobName}}"


class TestBundleInfoFromS3Metadata:
    def test_full_metadata(self):
        metadata = {
            "ojd-name": "My Bundle",
            "ojd-desc": "A description",
            "ojd-steps": "Step1,Step2",
            "ojd-params": "Frames:STRING,Output:PATH",
        }
        info = _bundle_info_from_s3_metadata(metadata, "s3://bucket/key")
        assert info is not None
        assert info.name == "My Bundle"
        assert info.description == "A description"
        assert info.step_names == ["Step1", "Step2"]
        assert len(info.parameters) == 2
        assert info.parameters[0] == {"name": "Frames", "type": "STRING"}
        assert info.parameters[1] == {"name": "Output", "type": "PATH"}

    def test_missing_name_returns_none(self):
        info = _bundle_info_from_s3_metadata({}, "s3://bucket/key")
        assert info is None

    def test_name_only(self):
        info = _bundle_info_from_s3_metadata({"ojd-name": "Simple"}, "s3://bucket/key")
        assert info is not None
        assert info.name == "Simple"
        assert info.step_names == []
        assert info.parameters == []


class TestArchiveHelpers:
    def test_is_archive(self):
        assert _is_archive("bundle.ojd")
        assert not _is_archive("bundle.zip")
        assert not _is_archive("bundle.tar.gz")
        assert not _is_archive("bundle.tgz")
        assert not _is_archive("bundle.tar.bz2")
        assert not _is_archive("bundle.tar.xz")
        assert not _is_archive("bundle.tar")
        assert not _is_archive("bundle")
        assert not _is_archive("template.yaml")

    def test_strip_archive_ext(self):
        assert _strip_archive_ext("bundle.ojd") == "bundle"
        assert _strip_archive_ext("my-job.ojd") == "my-job"
        assert _strip_archive_ext("noext") == "noext"


class TestReadTemplateFromArchive:
    def _make_ojd(self, tmp_path, contents: dict[str, str]) -> str:
        ojd_path = str(tmp_path / "bundle.ojd")
        with zipfile.ZipFile(ojd_path, "w") as zf:
            for name, data in contents.items():
                zf.writestr(name, data)
        return ojd_path

    def test_ojd_root_template(self, tmp_path):
        path = self._make_ojd(tmp_path, {"template.yaml": "name: OjdBundle\nsteps: []\n"})
        result = _read_template_from_archive_path(path)
        assert result is not None
        raw, fname = result
        assert "OjdBundle" in raw
        assert fname == "template.yaml"

    def test_ojd_wrapped_template(self, tmp_path):
        path = self._make_ojd(tmp_path, {"my-bundle/template.yaml": "name: Wrapped\nsteps: []\n"})
        result = _read_template_from_archive_path(path)
        assert result is not None
        raw, fname = result
        assert "Wrapped" in raw

    def test_ojd_json_template(self, tmp_path):
        path = self._make_ojd(
            tmp_path,
            {"template.json": json.dumps({"name": "JSONBundle", "steps": []})},
        )
        result = _read_template_from_archive_path(path)
        assert result is not None
        raw, fname = result
        assert fname == "template.json"

    def test_ojd_no_template(self, tmp_path):
        path = self._make_ojd(tmp_path, {"readme.txt": "no template here"})
        result = _read_template_from_archive_path(path)
        assert result is None


class TestLocalBundleRepository:
    def test_root_path_default(self):
        repo = LocalBundleRepository()
        assert repo.root_path() == os.path.expanduser("~")

    def test_root_path_custom(self, tmp_path):
        repo = LocalBundleRepository(root=str(tmp_path))
        assert repo.root_path() == str(tmp_path)

    def test_list_entries_empty(self, tmp_path):
        repo = LocalBundleRepository(root=str(tmp_path))
        entries = repo.list_entries(str(tmp_path))
        assert entries == []

    def test_list_entries_with_bundles_and_dirs(self, tmp_path):
        bundle_dir = tmp_path / "my-bundle"
        bundle_dir.mkdir()
        (bundle_dir / "template.yaml").write_text("name: Test Bundle\nsteps:\n- name: Step1\n")

        regular_dir = tmp_path / "regular-dir"
        regular_dir.mkdir()

        (tmp_path / "some-file.txt").write_text("not a dir")

        repo = LocalBundleRepository(root=str(tmp_path))
        entries = repo.list_entries(str(tmp_path))

        assert len(entries) == 2
        names = {e.name for e in entries}
        assert "my-bundle" in names
        assert "regular-dir" in names

        bundle_entry = next(e for e in entries if e.name == "my-bundle")
        assert bundle_entry.is_bundle is True
        assert bundle_entry.is_archive is False

        dir_entry = next(e for e in entries if e.name == "regular-dir")
        assert dir_entry.is_bundle is False

    def test_list_entries_with_valid_archive(self, tmp_path):
        ojd_path = tmp_path / "render-job.ojd"
        with zipfile.ZipFile(str(ojd_path), "w") as zf:
            zf.writestr("template.yaml", "name: Render\nsteps:\n- name: S1\n")

        repo = LocalBundleRepository(root=str(tmp_path))
        entries = repo.list_entries(str(tmp_path))

        archive_entries = [e for e in entries if e.is_archive]
        assert len(archive_entries) == 1
        assert archive_entries[0].name == "render-job"
        assert archive_entries[0].is_bundle is True

    def test_list_entries_invalid_archive_excluded(self, tmp_path):
        """An .ojd without a template should not appear as a bundle."""
        ojd_path = tmp_path / "random.ojd"
        with zipfile.ZipFile(str(ojd_path), "w") as zf:
            zf.writestr("readme.txt", "not a bundle")

        repo = LocalBundleRepository(root=str(tmp_path))
        entries = repo.list_entries(str(tmp_path))
        assert len(entries) == 0

    def test_list_entries_include_archives_false(self, tmp_path):
        """With include_archives=False, archives are skipped entirely."""
        ojd_path = tmp_path / "bundle.ojd"
        with zipfile.ZipFile(str(ojd_path), "w") as zf:
            zf.writestr("template.yaml", "name: Zipped\nsteps: []\n")

        bundle_dir = tmp_path / "dir-bundle"
        bundle_dir.mkdir()
        (bundle_dir / "template.yaml").write_text("name: Dir\nsteps: []\n")

        repo = LocalBundleRepository(root=str(tmp_path), include_archives=False)
        entries = repo.list_entries(str(tmp_path))

        assert len(entries) == 1
        assert entries[0].name == "dir-bundle"

    def test_list_entries_nonexistent_path(self):
        repo = LocalBundleRepository()
        entries = repo.list_entries("/nonexistent/path/that/does/not/exist")
        assert entries == []

    def test_get_bundle_info_yaml(self, tmp_path):
        bundle_dir = tmp_path / "test-bundle"
        bundle_dir.mkdir()
        (bundle_dir / "template.yaml").write_text(
            yaml.dump(
                {
                    "specificationVersion": "jobtemplate-2023-09",
                    "name": "Test Bundle",
                    "description": "A test",
                    "steps": [{"name": "Render"}],
                    "parameterDefinitions": [{"name": "Frames", "type": "STRING"}],
                }
            )
        )

        repo = LocalBundleRepository(root=str(tmp_path))
        info = repo.get_bundle_info(str(bundle_dir))

        assert info is not None
        assert info.name == "Test Bundle"
        assert info.description == "A test"
        assert info.step_names == ["Render"]
        assert len(info.parameters) == 1

    def test_get_bundle_info_with_parameter_values(self, tmp_path):
        bundle_dir = tmp_path / "pv-bundle"
        bundle_dir.mkdir()
        (bundle_dir / "template.yaml").write_text(
            yaml.dump(
                {
                    "name": "{{Param.JobName}}",
                    "steps": [{"name": "Run"}],
                    "parameterDefinitions": [
                        {"name": "JobName", "type": "STRING", "default": "Default"},
                        {"name": "Frames", "type": "STRING"},
                    ],
                }
            )
        )
        (bundle_dir / "parameter_values.yaml").write_text(
            yaml.dump(
                {
                    "parameterValues": [
                        {"name": "JobName", "value": "My Custom Job"},
                        {"name": "Frames", "value": "1-100"},
                    ]
                }
            )
        )

        repo = LocalBundleRepository(root=str(tmp_path))
        info = repo.get_bundle_info(str(bundle_dir))

        assert info is not None
        assert info.name == "{{Param.JobName}}"
        frames = next(p for p in info.parameters if p["name"] == "Frames")
        assert frames["_display_value"] == "1-100"

    def test_get_bundle_info_archive(self, tmp_path):
        ojd_path = tmp_path / "my-job.ojd"
        with zipfile.ZipFile(str(ojd_path), "w") as zf:
            zf.writestr(
                "template.yaml",
                yaml.dump(
                    {
                        "name": "Archive Job",
                        "description": "From an ojd",
                        "steps": [{"name": "Run"}],
                        "parameterDefinitions": [{"name": "Input", "type": "PATH"}],
                    }
                ),
            )

        repo = LocalBundleRepository(root=str(tmp_path))
        info = repo.get_bundle_info(str(ojd_path))

        assert info is not None
        assert info.name == "Archive Job"
        assert info.description == "From an ojd"
        assert info.step_names == ["Run"]
        assert len(info.parameters) == 1

    def test_get_bundle_info_not_a_bundle(self, tmp_path):
        regular_dir = tmp_path / "not-a-bundle"
        regular_dir.mkdir()

        repo = LocalBundleRepository(root=str(tmp_path))
        info = repo.get_bundle_info(str(regular_dir))
        assert info is None

    def test_extract_bundle_flat(self, tmp_path):
        ojd_path = tmp_path / "flat.ojd"
        with zipfile.ZipFile(str(ojd_path), "w") as zf:
            zf.writestr("template.yaml", "name: Flat\nsteps: []\n")
            zf.writestr("scripts/run.sh", "#!/bin/bash\necho hello\n")

        dest = tmp_path / "extracted"
        dest.mkdir()
        repo = LocalBundleRepository()
        result = repo.extract_bundle(str(ojd_path), str(dest))

        assert os.path.isfile(os.path.join(result, "template.yaml"))
        assert os.path.isfile(os.path.join(result, "scripts", "run.sh"))

    def test_extract_bundle_wrapped(self, tmp_path):
        ojd_path = tmp_path / "wrapped.ojd"
        with zipfile.ZipFile(str(ojd_path), "w") as zf:
            zf.writestr("my-bundle/template.yaml", "name: Wrapped\nsteps: []\n")
            zf.writestr("my-bundle/scripts/run.sh", "#!/bin/bash\n")

        dest = tmp_path / "extracted"
        dest.mkdir()
        repo = LocalBundleRepository()
        result = repo.extract_bundle(str(ojd_path), str(dest))

        assert os.path.isfile(os.path.join(result, "template.yaml"))

    def test_nested_bundles(self, tmp_path):
        parent = tmp_path / "projects"
        parent.mkdir()

        nested_bundle = parent / "my-job"
        nested_bundle.mkdir()
        (nested_bundle / "template.yaml").write_text("name: Nested\nsteps:\n- name: S1\n")

        repo = LocalBundleRepository(root=str(tmp_path))
        entries = repo.list_entries(str(parent))
        assert len(entries) == 1
        assert entries[0].is_bundle is True
        assert entries[0].name == "my-job"

    def test_read_parameter_values_yaml(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "parameter_values.yaml").write_text(
            yaml.dump({"parameterValues": [{"name": "X", "value": "1"}]})
        )
        result = LocalBundleRepository._read_parameter_values(str(bundle_dir))
        assert result is not None
        assert result["parameterValues"][0]["value"] == "1"

    def test_read_parameter_values_json(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        (bundle_dir / "parameter_values.json").write_text(
            json.dumps({"parameterValues": [{"name": "Y", "value": "2"}]})
        )
        result = LocalBundleRepository._read_parameter_values(str(bundle_dir))
        assert result is not None
        assert result["parameterValues"][0]["value"] == "2"

    def test_read_parameter_values_none(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        result = LocalBundleRepository._read_parameter_values(str(bundle_dir))
        assert result is None


class TestSafeZipExtract:
    def test_rejects_absolute_path(self, tmp_path):
        archive = tmp_path / "bad.zip"
        with zipfile.ZipFile(str(archive), "w") as zf:
            zf.writestr("/etc/passwd", "malicious")

        dest = tmp_path / "out"
        dest.mkdir()
        with zipfile.ZipFile(str(archive), "r") as zf:
            with pytest.raises(ValueError, match="absolute path"):
                _safe_zip_extract(zf, str(dest))

    def test_rejects_parent_directory_traversal(self, tmp_path):
        archive = tmp_path / "bad.zip"
        with zipfile.ZipFile(str(archive), "w") as zf:
            zf.writestr("../../etc/passwd", "malicious")

        dest = tmp_path / "out"
        dest.mkdir()
        with zipfile.ZipFile(str(archive), "r") as zf:
            with pytest.raises(ValueError, match="outside target directory"):
                _safe_zip_extract(zf, str(dest))

    def test_allows_normal_archive(self, tmp_path):
        archive = tmp_path / "good.zip"
        with zipfile.ZipFile(str(archive), "w") as zf:
            zf.writestr("template.yaml", "name: Test\n")
            zf.writestr("subdir/file.txt", "hello")

        dest = tmp_path / "out"
        dest.mkdir()
        with zipfile.ZipFile(str(archive), "r") as zf:
            _safe_zip_extract(zf, str(dest))

        assert (dest / "template.yaml").exists()
        assert (dest / "subdir" / "file.txt").exists()


class TestSanitizeBundleName:
    def test_slashes_replaced(self):
        assert sanitize_bundle_name("path/to/bundle") == "path_to_bundle"

    def test_backslashes_replaced_on_windows(self):
        if sys.platform == "win32":
            assert sanitize_bundle_name("path\\to\\bundle") == "path_to_bundle"

    def test_backslashes_preserved_on_posix(self):
        if sys.platform != "win32":
            assert sanitize_bundle_name("path\\to\\bundle") == "path\\to\\bundle"

    def test_windows_illegal_chars_replaced_on_windows(self):
        if sys.platform == "win32":
            assert sanitize_bundle_name("file:name*with?bad<chars>") == "file_name_with_bad_chars_"

    def test_colons_preserved_on_posix(self):
        if sys.platform != "win32":
            assert sanitize_bundle_name("my:bundle") == "my:bundle"

    def test_empty_after_sanitization_raises(self):
        with pytest.raises(ValueError, match="empty after sanitization"):
            sanitize_bundle_name("///")

    def test_long_name_preserved(self):
        long_name = "a" * 1000
        assert sanitize_bundle_name(long_name) == long_name

    def test_normal_name_unchanged(self):
        assert sanitize_bundle_name("blender-render_v2.1") == "blender-render_v2.1"


class TestS3BundleVisibility:
    def _make_repo(self):
        """Create an S3BundleRepository with a mocked S3 client."""
        with patch("boto3.Session"):
            repo = S3BundleRepository(
                bucket_name="test-bucket",
                root_prefix="DeadlineCloud",
                session=MagicMock(),
            )
        repo._s3 = MagicMock()
        return repo

    def test_get_hidden_set_empty_when_no_manifest(self):
        repo = self._make_repo()
        repo._s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        assert repo.get_hidden_set() == set()

    def test_get_hidden_set_returns_names(self):
        repo = self._make_repo()
        manifest = json.dumps({"version": 1, "hidden": ["bundle-a", "bundle-b"]})
        repo._s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=manifest.encode()))
        }
        assert repo.get_hidden_set() == {"bundle-a", "bundle-b"}

    def test_set_bundle_visibility_hide(self):
        repo = self._make_repo()
        manifest = json.dumps({"version": 1, "hidden": ["existing"]})
        repo._s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=manifest.encode())),
            "ETag": '"abc123"',
        }

        repo.set_bundle_visibility("new-bundle", hidden=True)

        repo._s3.put_object.assert_called_once()
        call_kwargs = repo._s3.put_object.call_args[1]
        written = json.loads(call_kwargs["Body"])
        assert "new-bundle" in written["hidden"]
        assert "existing" in written["hidden"]
        assert call_kwargs["IfMatch"] == '"abc123"'

    def test_set_bundle_visibility_unhide(self):
        repo = self._make_repo()
        manifest = json.dumps({"version": 1, "hidden": ["bundle-a", "bundle-b"]})
        repo._s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=manifest.encode())),
            "ETag": '"abc123"',
        }

        repo.set_bundle_visibility("bundle-a", hidden=False)

        repo._s3.put_object.assert_called_once()
        call_kwargs = repo._s3.put_object.call_args[1]
        written = json.loads(call_kwargs["Body"])
        assert "bundle-a" not in written["hidden"]
        assert "bundle-b" in written["hidden"]

    def test_set_bundle_visibility_noop_already_hidden(self):
        repo = self._make_repo()
        manifest = json.dumps({"version": 1, "hidden": ["bundle-a"]})
        repo._s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=manifest.encode())),
            "ETag": '"abc123"',
        }

        repo.set_bundle_visibility("bundle-a", hidden=True)
        repo._s3.put_object.assert_not_called()

    def test_set_bundle_visibility_noop_already_visible(self):
        repo = self._make_repo()
        repo._s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        repo.set_bundle_visibility("bundle-a", hidden=False)
        repo._s3.put_object.assert_not_called()

    def test_set_bundle_visibility_creates_manifest_on_first_hide(self):
        repo = self._make_repo()
        repo._s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        repo.set_bundle_visibility("new-bundle", hidden=True)

        repo._s3.put_object.assert_called_once()
        call_kwargs = repo._s3.put_object.call_args[1]
        written = json.loads(call_kwargs["Body"])
        assert written == {"version": 1, "hidden": ["new-bundle"]}
        assert call_kwargs["IfNoneMatch"] == "*"

    def test_set_bundle_visibility_retries_on_conflict(self):
        repo = self._make_repo()
        manifest = json.dumps({"version": 1, "hidden": []})
        repo._s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=manifest.encode())),
            "ETag": '"etag1"',
        }
        # First put fails with PreconditionFailed, second succeeds
        repo._s3.put_object.side_effect = [
            ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject"),
            {},
        ]

        repo.set_bundle_visibility("bundle-a", hidden=True)

        assert repo._s3.put_object.call_count == 2
        assert repo._s3.get_object.call_count == 2

    def test_set_bundle_visibility_raises_after_max_retries(self):
        repo = self._make_repo()
        manifest = json.dumps({"version": 1, "hidden": []})
        repo._s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=manifest.encode())),
            "ETag": '"etag1"',
        }
        repo._s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
        )

        from deadline.client.exceptions import DeadlineOperationError

        with pytest.raises(DeadlineOperationError, match="Failed to update bundle visibility"):
            repo.set_bundle_visibility("bundle-a", hidden=True)

        assert repo._s3.put_object.call_count == VISIBILITY_MAX_RETRIES

    def test_set_bundle_visibility_hidden_list_sorted(self):
        repo = self._make_repo()
        manifest = json.dumps({"version": 1, "hidden": ["z-bundle", "a-bundle"]})
        repo._s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=manifest.encode())),
            "ETag": '"etag1"',
        }

        repo.set_bundle_visibility("m-bundle", hidden=True)

        call_kwargs = repo._s3.put_object.call_args[1]
        written = json.loads(call_kwargs["Body"])
        assert written["hidden"] == ["a-bundle", "m-bundle", "z-bundle"]

    def test_prune_hidden_set_removes_stale_entries(self):
        repo = self._make_repo()
        manifest = json.dumps({"version": 1, "hidden": ["exists", "deleted"]})
        repo._s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=manifest.encode())),
            "ETag": '"etag1"',
        }

        repo.prune_hidden_set(existing_names={"exists", "other"})

        repo._s3.put_object.assert_called_once()
        call_kwargs = repo._s3.put_object.call_args[1]
        written = json.loads(call_kwargs["Body"])
        assert written["hidden"] == ["exists"]

    def test_prune_hidden_set_noop_when_nothing_to_prune(self):
        repo = self._make_repo()
        manifest = json.dumps({"version": 1, "hidden": ["exists"]})
        repo._s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=manifest.encode())),
            "ETag": '"etag1"',
        }

        repo.prune_hidden_set(existing_names={"exists", "other"})
        repo._s3.put_object.assert_not_called()

    def test_prune_hidden_set_noop_when_no_manifest(self):
        repo = self._make_repo()
        repo._s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

        repo.prune_hidden_set(existing_names={"anything"})
        repo._s3.put_object.assert_not_called()
