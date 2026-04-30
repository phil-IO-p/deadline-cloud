# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the job bundle repository module."""

import json
import os
import pytest
import yaml

from deadline.client.job_bundle.repository import (
    BrowseEntry,
    BundleInfo,
    LocalBundleRepository,
    S3BundleRepository,
    _extract_bundle_info,
    _parse_template,
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
        assert info.name == "bundle"  # Falls back to basename
        assert info.description == ""
        assert info.step_names == ["OnlyStep"]
        assert info.parameters == []


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
        # Create a bundle directory
        bundle_dir = tmp_path / "my-bundle"
        bundle_dir.mkdir()
        (bundle_dir / "template.yaml").write_text(
            "specificationVersion: 'jobtemplate-2023-09'\nname: Test Bundle\nsteps:\n- name: Step1\n"
        )

        # Create a regular directory
        regular_dir = tmp_path / "regular-dir"
        regular_dir.mkdir()

        # Create a file (should be ignored)
        (tmp_path / "some-file.txt").write_text("not a dir")

        repo = LocalBundleRepository(root=str(tmp_path))
        entries = repo.list_entries(str(tmp_path))

        assert len(entries) == 2
        names = {e.name for e in entries}
        assert "my-bundle" in names
        assert "regular-dir" in names

        bundle_entry = next(e for e in entries if e.name == "my-bundle")
        assert bundle_entry.is_bundle is True

        dir_entry = next(e for e in entries if e.name == "regular-dir")
        assert dir_entry.is_bundle is False

    def test_list_entries_json_template(self, tmp_path):
        bundle_dir = tmp_path / "json-bundle"
        bundle_dir.mkdir()
        (bundle_dir / "template.json").write_text(
            json.dumps({"name": "JSON Bundle", "steps": [{"name": "S1"}]})
        )

        repo = LocalBundleRepository(root=str(tmp_path))
        entries = repo.list_entries(str(tmp_path))
        assert len(entries) == 1
        assert entries[0].is_bundle is True

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
        assert info.parameters[0]["name"] == "Frames"

    def test_get_bundle_info_not_a_bundle(self, tmp_path):
        regular_dir = tmp_path / "not-a-bundle"
        regular_dir.mkdir()

        repo = LocalBundleRepository(root=str(tmp_path))
        info = repo.get_bundle_info(str(regular_dir))
        assert info is None

    def test_nested_bundles(self, tmp_path):
        """Test that bundles nested inside directories are found when listing the parent."""
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
