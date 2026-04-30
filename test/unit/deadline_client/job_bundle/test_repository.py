# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the job bundle repository module."""

import json
import os
import tarfile
import zipfile

import yaml

from deadline.client.job_bundle.repository import (
    LocalBundleRepository,
    _extract_bundle_info,
    _is_archive,
    _parse_template,
    _read_template_from_archive_path,
    _strip_archive_ext,
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


class TestArchiveHelpers:
    def test_is_archive(self):
        assert _is_archive("bundle.zip")
        assert _is_archive("bundle.tar.gz")
        assert _is_archive("bundle.tgz")
        assert _is_archive("bundle.tar.bz2")
        assert _is_archive("bundle.tar.xz")
        assert _is_archive("bundle.tar")
        assert not _is_archive("bundle")
        assert not _is_archive("template.yaml")

    def test_strip_archive_ext(self):
        assert _strip_archive_ext("bundle.zip") == "bundle"
        assert _strip_archive_ext("bundle.tar.gz") == "bundle"
        assert _strip_archive_ext("bundle.tgz") == "bundle"
        assert _strip_archive_ext("my-job.tar.bz2") == "my-job"
        assert _strip_archive_ext("noext") == "noext"


class TestReadTemplateFromArchive:
    def _make_zip(self, tmp_path, contents: dict[str, str]) -> str:
        """Create a zip with the given {filename: content} entries."""
        zip_path = str(tmp_path / "bundle.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, data in contents.items():
                zf.writestr(name, data)
        return zip_path

    def _make_tar_gz(self, tmp_path, contents: dict[str, str]) -> str:
        """Create a tar.gz with the given {filename: content} entries."""
        tar_path = str(tmp_path / "bundle.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tf:
            for name, data in contents.items():
                import io

                info = tarfile.TarInfo(name=name)
                encoded = data.encode("utf-8")
                info.size = len(encoded)
                tf.addfile(info, io.BytesIO(encoded))
        return tar_path

    def test_zip_root_template(self, tmp_path):
        path = self._make_zip(tmp_path, {"template.yaml": "name: ZipBundle\nsteps: []\n"})
        result = _read_template_from_archive_path(path)
        assert result is not None
        raw, fname = result
        assert "ZipBundle" in raw
        assert fname == "template.yaml"

    def test_zip_wrapped_template(self, tmp_path):
        path = self._make_zip(tmp_path, {"my-bundle/template.yaml": "name: Wrapped\nsteps: []\n"})
        result = _read_template_from_archive_path(path)
        assert result is not None
        raw, fname = result
        assert "Wrapped" in raw

    def test_zip_json_template(self, tmp_path):
        path = self._make_zip(
            tmp_path,
            {"template.json": json.dumps({"name": "JSONBundle", "steps": []})},
        )
        result = _read_template_from_archive_path(path)
        assert result is not None
        raw, fname = result
        assert fname == "template.json"

    def test_zip_no_template(self, tmp_path):
        path = self._make_zip(tmp_path, {"readme.txt": "no template here"})
        result = _read_template_from_archive_path(path)
        assert result is None

    def test_tar_gz_root_template(self, tmp_path):
        path = self._make_tar_gz(tmp_path, {"template.yaml": "name: TarBundle\nsteps: []\n"})
        result = _read_template_from_archive_path(path)
        assert result is not None
        raw, fname = result
        assert "TarBundle" in raw

    def test_tar_gz_wrapped_template(self, tmp_path):
        path = self._make_tar_gz(
            tmp_path, {"my-bundle/template.yaml": "name: TarWrapped\nsteps: []\n"}
        )
        result = _read_template_from_archive_path(path)
        assert result is not None
        assert "TarWrapped" in result[0]


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

    def test_list_entries_with_archives(self, tmp_path):
        # Create a zip archive bundle
        zip_path = tmp_path / "render-job.zip"
        with zipfile.ZipFile(str(zip_path), "w") as zf:
            zf.writestr("template.yaml", "name: Render\nsteps:\n- name: S1\n")

        # Create a tar.gz archive bundle
        tar_path = tmp_path / "process-job.tar.gz"
        with tarfile.open(str(tar_path), "w:gz") as tf:
            import io

            data = b"name: Process\nsteps:\n- name: S1\n"
            info = tarfile.TarInfo(name="template.yaml")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        # Create a regular directory bundle
        dir_bundle = tmp_path / "dir-bundle"
        dir_bundle.mkdir()
        (dir_bundle / "template.yaml").write_text("name: Dir\nsteps: []\n")

        repo = LocalBundleRepository(root=str(tmp_path))
        entries = repo.list_entries(str(tmp_path))

        assert len(entries) == 3
        archive_entries = [e for e in entries if e.is_archive]
        assert len(archive_entries) == 2
        archive_names = {e.name for e in archive_entries}
        assert "render-job" in archive_names
        assert "process-job" in archive_names
        for e in archive_entries:
            assert e.is_bundle is True

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

    def test_get_bundle_info_archive(self, tmp_path):
        zip_path = tmp_path / "my-job.zip"
        with zipfile.ZipFile(str(zip_path), "w") as zf:
            zf.writestr(
                "template.yaml",
                yaml.dump(
                    {
                        "name": "Archive Job",
                        "description": "From a zip",
                        "steps": [{"name": "Run"}],
                        "parameterDefinitions": [{"name": "Input", "type": "PATH"}],
                    }
                ),
            )

        repo = LocalBundleRepository(root=str(tmp_path))
        info = repo.get_bundle_info(str(zip_path))

        assert info is not None
        assert info.name == "Archive Job"
        assert info.description == "From a zip"
        assert info.step_names == ["Run"]
        assert len(info.parameters) == 1

    def test_get_bundle_info_not_a_bundle(self, tmp_path):
        regular_dir = tmp_path / "not-a-bundle"
        regular_dir.mkdir()

        repo = LocalBundleRepository(root=str(tmp_path))
        info = repo.get_bundle_info(str(regular_dir))
        assert info is None

    def test_extract_bundle_flat(self, tmp_path):
        """Test extracting a zip where template is at the root."""
        zip_path = tmp_path / "flat.zip"
        with zipfile.ZipFile(str(zip_path), "w") as zf:
            zf.writestr("template.yaml", "name: Flat\nsteps: []\n")
            zf.writestr("scripts/run.sh", "#!/bin/bash\necho hello\n")

        dest = tmp_path / "extracted"
        dest.mkdir()
        repo = LocalBundleRepository()
        result = repo.extract_bundle(str(zip_path), str(dest))

        assert os.path.isfile(os.path.join(result, "template.yaml"))
        assert os.path.isfile(os.path.join(result, "scripts", "run.sh"))

    def test_extract_bundle_wrapped(self, tmp_path):
        """Test extracting a zip where contents are in a single subdirectory."""
        zip_path = tmp_path / "wrapped.zip"
        with zipfile.ZipFile(str(zip_path), "w") as zf:
            zf.writestr("my-bundle/template.yaml", "name: Wrapped\nsteps: []\n")
            zf.writestr("my-bundle/scripts/run.sh", "#!/bin/bash\n")

        dest = tmp_path / "extracted"
        dest.mkdir()
        repo = LocalBundleRepository()
        result = repo.extract_bundle(str(zip_path), str(dest))

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
