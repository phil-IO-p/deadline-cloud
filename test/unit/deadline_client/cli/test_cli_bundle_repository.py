# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""Tests for the bundle CLI commands (list, upload, download, cache)."""

import json
import zipfile

import yaml
from botocore.exceptions import ClientError
from click.testing import CliRunner
from unittest.mock import MagicMock, patch

from deadline.client.cli import main
from deadline.client.job_bundle.repository import (
    METADATA_LIMIT_DESC,
    METADATA_LIMIT_NAME,
    METADATA_LIMIT_PARAMS,
    METADATA_LIMIT_STEPS,
)

BUNDLE_GROUP = "deadline.client.cli._groups.bundle_group"


class TestBundleList:
    def test_list_local_path(self, tmp_path):
        bundle = tmp_path / "my-bundle"
        bundle.mkdir()
        (bundle / "template.yaml").write_text("name: Test\nsteps:\n- name: S1\n")
        (tmp_path / "not-a-bundle").mkdir()

        runner = CliRunner()
        result = runner.invoke(main, ["bundle", "list", str(tmp_path)])
        assert result.exit_code == 0
        assert "my-bundle" in result.output
        assert "not-a-bundle" not in result.output

    def test_list_local_json(self, tmp_path):
        bundle = tmp_path / "render-job"
        bundle.mkdir()
        (bundle / "template.yaml").write_text("name: Render\nsteps: []\n")

        runner = CliRunner()
        result = runner.invoke(main, ["bundle", "list", str(tmp_path), "--output", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "render-job"
        assert data[0]["format"] == "folder"

    def test_list_local_no_archives(self, tmp_path):
        bundle = tmp_path / "dir-bundle"
        bundle.mkdir()
        (bundle / "template.yaml").write_text("name: Dir\nsteps: []\n")

        ojd_path = tmp_path / "archive-bundle.ojd"
        with zipfile.ZipFile(str(ojd_path), "w") as zf:
            zf.writestr("template.yaml", "name: Zipped\nsteps: []\n")

        runner = CliRunner()

        result = runner.invoke(main, ["bundle", "list", str(tmp_path)])
        assert result.exit_code == 0
        assert "dir-bundle" in result.output
        assert "archive-bundle" in result.output

        result = runner.invoke(main, ["bundle", "list", str(tmp_path), "--no-archives"])
        assert result.exit_code == 0
        assert "dir-bundle" in result.output
        assert "archive-bundle" not in result.output

    def test_list_empty_dir(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(main, ["bundle", "list", str(tmp_path)])
        assert result.exit_code == 0
        assert result.output.strip() == ""


class TestBundleUpload:
    @patch(f"{BUNDLE_GROUP}._apply_cli_options_to_config")
    @patch(f"{BUNDLE_GROUP}._get_queue_s3_settings")
    @patch("boto3.client")
    def test_upload_creates_zip(self, mock_boto3_client, mock_s3_settings, mock_config, tmp_path):
        bundle = tmp_path / "my-bundle"
        bundle.mkdir()
        (bundle / "template.yaml").write_text(
            yaml.dump(
                {
                    "specificationVersion": "jobtemplate-2023-09",
                    "name": "Test Bundle",
                    "steps": [{"name": "Run"}],
                }
            )
        )

        mock_session = MagicMock()
        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        mock_session.client.return_value = mock_s3
        mock_s3_settings.return_value = (
            MagicMock(s3BucketName="test-bucket", rootPrefix="DeadlineCloud"),
            mock_session,
        )

        runner = CliRunner()
        result = runner.invoke(main, ["bundle", "upload", str(bundle)])

        assert result.exit_code == 0, result.output
        assert "Uploaded bundle to" in result.output
        mock_s3.upload_fileobj.assert_called_once()

    @patch(f"{BUNDLE_GROUP}._apply_cli_options_to_config")
    @patch(f"{BUNDLE_GROUP}._get_queue_s3_settings")
    def test_upload_not_a_bundle(self, mock_s3_settings, mock_config, tmp_path):
        not_bundle = tmp_path / "empty"
        not_bundle.mkdir()

        mock_s3_settings.return_value = (
            MagicMock(s3BucketName="test-bucket", rootPrefix="DeadlineCloud"),
            MagicMock(),
        )

        runner = CliRunner()
        result = runner.invoke(main, ["bundle", "upload", str(not_bundle)])
        assert result.exit_code == 1
        assert "not appear to be a job bundle" in result.output


REPO_MODULE = "deadline.client.job_bundle.repository"


class TestBundleCacheClean:
    def test_clean_no_cache(self, tmp_path):
        with patch(
            f"{BUNDLE_GROUP}._get_bundle_cache_dir",
            return_value=str(tmp_path / "nonexistent"),
        ):
            runner = CliRunner()
            result = runner.invoke(main, ["bundle", "cache", "clean"])
        assert result.exit_code == 0
        assert "No bundle cache found" in result.output

    def test_clean_dry_run(self, tmp_path):
        cache_dir = tmp_path / "cache"
        hash_dir = cache_dir / "abc123"
        bundle_dir = hash_dir / "test-bundle"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "template.yaml").write_text("name: Test\n")

        with patch(
            f"{BUNDLE_GROUP}._get_bundle_cache_dir",
            return_value=str(cache_dir),
        ):
            runner = CliRunner()
            result = runner.invoke(main, ["bundle", "cache", "clean", "--dry-run"])

        assert result.exit_code == 0
        assert "Would remove" in result.output
        assert (bundle_dir / "template.yaml").exists()

    def test_clean_specific_bundle(self, tmp_path):
        cache_dir = tmp_path / "cache"
        hash_dir = cache_dir / "abc123"
        bundle_a = hash_dir / "bundle-a"
        bundle_b = hash_dir / "bundle-b"
        bundle_a.mkdir(parents=True)
        bundle_b.mkdir(parents=True)
        (bundle_a / "template.yaml").write_text("a")
        (bundle_b / "template.yaml").write_text("b")

        with patch(
            f"{BUNDLE_GROUP}._get_bundle_cache_dir",
            return_value=str(cache_dir),
        ):
            runner = CliRunner()
            result = runner.invoke(main, ["bundle", "cache", "clean", "bundle-a"])

        assert result.exit_code == 0
        assert "Removed cached bundle: bundle-a" in result.output
        assert not bundle_a.exists()
        assert bundle_b.exists()


class TestMetadataTruncation:
    @patch(f"{BUNDLE_GROUP}._apply_cli_options_to_config")
    @patch(f"{BUNDLE_GROUP}._get_queue_s3_settings")
    def test_upload_truncates_metadata_with_warning(
        self, mock_s3_settings, mock_config, tmp_path, capsys
    ):
        bundle = tmp_path / "big-bundle"
        bundle.mkdir()
        (bundle / "template.yaml").write_text(
            yaml.dump(
                {
                    "specificationVersion": "jobtemplate-2023-09",
                    "name": "A" * 300,
                    "description": "D" * 600,
                    "steps": [{"name": f"Step_{i:03d}_Long"} for i in range(40)],
                    "parameterDefinitions": [
                        {"name": f"Param_{i:03d}_Long", "type": "STRING"} for i in range(50)
                    ],
                }
            )
        )

        mock_session = MagicMock()
        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        mock_session.client.return_value = mock_s3
        mock_s3_settings.return_value = (
            MagicMock(s3BucketName="test-bucket", rootPrefix="DC"),
            mock_session,
        )

        runner = CliRunner()
        result = runner.invoke(main, ["bundle", "upload", str(bundle)])
        assert result.exit_code == 0, result.output

        # Verify warnings were emitted
        assert "ojd-name" in result.output
        assert "ojd-desc" in result.output
        assert "ojd-steps" in result.output
        assert "ojd-params" in result.output

        # Verify metadata values respect limits
        call_args = mock_s3.upload_fileobj.call_args
        metadata = call_args[1]["ExtraArgs"]["Metadata"]
        assert len(metadata["ojd-name"]) <= METADATA_LIMIT_NAME
        assert len(metadata["ojd-desc"]) <= METADATA_LIMIT_DESC
        assert len(metadata["ojd-steps"]) <= METADATA_LIMIT_STEPS
        assert len(metadata["ojd-params"]) <= METADATA_LIMIT_PARAMS

        # Verify truncated values end with "..."
        assert metadata["ojd-name"].endswith("...")
        assert metadata["ojd-desc"].endswith("...")
        assert metadata["ojd-steps"].endswith("...")
        assert metadata["ojd-params"].endswith("...")

    @patch(f"{BUNDLE_GROUP}._apply_cli_options_to_config")
    @patch(f"{BUNDLE_GROUP}._get_queue_s3_settings")
    def test_upload_no_truncation_when_within_limits(self, mock_s3_settings, mock_config, tmp_path):
        bundle = tmp_path / "small-bundle"
        bundle.mkdir()
        (bundle / "template.yaml").write_text(
            yaml.dump(
                {
                    "specificationVersion": "jobtemplate-2023-09",
                    "name": "Short Name",
                    "description": "Brief",
                    "steps": [{"name": "Render"}],
                    "parameterDefinitions": [{"name": "Frames", "type": "STRING"}],
                }
            )
        )

        mock_session = MagicMock()
        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        mock_session.client.return_value = mock_s3
        mock_s3_settings.return_value = (
            MagicMock(s3BucketName="test-bucket", rootPrefix="DC"),
            mock_session,
        )

        runner = CliRunner()
        result = runner.invoke(main, ["bundle", "upload", str(bundle)])
        assert result.exit_code == 0, result.output

        # No warnings
        assert "truncated" not in result.output.lower()

        # Values stored as-is without "..."
        call_args = mock_s3.upload_fileobj.call_args
        metadata = call_args[1]["ExtraArgs"]["Metadata"]
        assert metadata["ojd-name"] == "Short Name"
        assert not metadata["ojd-name"].endswith("...")


class TestBundleUploadOverwrite:
    @patch(f"{BUNDLE_GROUP}._apply_cli_options_to_config")
    @patch(f"{BUNDLE_GROUP}._get_queue_s3_settings")
    def test_upload_prompts_when_bundle_exists_and_user_confirms(
        self, mock_s3_settings, mock_config, tmp_path
    ):
        bundle = tmp_path / "my-bundle"
        bundle.mkdir()
        (bundle / "template.yaml").write_text("name: Test\nsteps:\n- name: S1\n")

        mock_session = MagicMock()
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {}  # exists
        mock_session.client.return_value = mock_s3
        mock_s3_settings.return_value = (
            MagicMock(s3BucketName="test-bucket", rootPrefix="DC"),
            mock_session,
        )

        runner = CliRunner()
        result = runner.invoke(main, ["bundle", "upload", str(bundle)], input="y\n")
        assert result.exit_code == 0, result.output
        assert "already exists" in result.output
        mock_s3.upload_fileobj.assert_called_once()

    @patch(f"{BUNDLE_GROUP}._apply_cli_options_to_config")
    @patch(f"{BUNDLE_GROUP}._get_queue_s3_settings")
    def test_upload_aborts_when_bundle_exists_and_user_declines(
        self, mock_s3_settings, mock_config, tmp_path
    ):
        bundle = tmp_path / "my-bundle"
        bundle.mkdir()
        (bundle / "template.yaml").write_text("name: Test\nsteps:\n- name: S1\n")

        mock_session = MagicMock()
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {}  # exists
        mock_session.client.return_value = mock_s3
        mock_s3_settings.return_value = (
            MagicMock(s3BucketName="test-bucket", rootPrefix="DC"),
            mock_session,
        )

        runner = CliRunner()
        result = runner.invoke(main, ["bundle", "upload", str(bundle)], input="n\n")
        assert result.exit_code == 0, result.output
        assert "canceled" in result.output.lower()
        mock_s3.upload_fileobj.assert_not_called()
