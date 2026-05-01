# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
UI widgets for the scene settings tab.
"""

from __future__ import annotations

import os
from logging import getLogger
from typing import Any, Optional

from qtpy.QtCore import Signal  # type: ignore
from qtpy.QtWidgets import (  # type: ignore
    QVBoxLayout,
    QWidget,
    QMessageBox,
)

from ..dataclasses import JobBundleSettings
from .openjd_parameters_widget import OpenJDParametersWidget
from ...job_bundle.submission import AssetReferences
from ...job_bundle.loader import read_yaml_or_json_object, validate_directory_symlink_containment
from ...job_bundle.parameters import read_job_bundle_parameters

logger = getLogger(__name__)


class JobBundleSettingsWidget(QWidget):
    """
    Widget containing job setup specific to CLI jobs.

    Signals:
        parameter_changed: This is sent whenever a parameter value in the widget changes. The message
            is a copy of the parameter definition with the "value" key containing the new value.

    Args:
        initial_settings (CliJobSettings): dataclass containing the job-specific settings.
        parent: The parent Qt Widget.
    """

    parameter_changed = Signal(dict)

    def __init__(self, initial_settings: JobBundleSettings, parent: Optional[QWidget] = None):
        super().__init__(parent=parent)

        self.param_layout = QVBoxLayout()

        self._build_ui(initial_settings)

    def _build_ui(self, initial_settings: JobBundleSettings):
        self.input_job_bundle_dir = initial_settings.input_job_bundle_dir

        layout = QVBoxLayout(self)

        layout.addLayout(self.param_layout)
        self.refresh_ui(initial_settings)

    def refresh_ui(self, settings: JobBundleSettings):
        # Clear the layout
        for i in reversed(range(self.param_layout.count())):
            item = self.param_layout.takeAt(i)
            if item is None:
                continue
            widget = item.widget()
            if widget:
                widget.deleteLater()

        self.parameters_widget = OpenJDParametersWidget(
            parameter_definitions=settings.parameters, parent=self
        )
        self.param_layout.addWidget(self.parameters_widget)
        self.parameters_widget.parameter_changed.connect(
            lambda message: self.parameter_changed.emit(message)
        )

    def on_load_bundle(self):
        """
        Browse and load the selected submission bundle
        """
        from ..dialogs.job_bundle_browser_dialog import JobBundleBrowserDialog
        from ...config import get_setting

        # Determine the default local browse directory
        default_dir = os.environ.get("DEADLINE_JOB_BUNDLE_DEFAULT_DIRECTORY", "")
        if not default_dir:
            default_dir = get_setting("settings.job_bundle_default_directory")

        # Try to get the queue's S3 bucket for S3 browsing
        s3_bucket = ""
        s3_prefix = ""
        try:
            farm_id = get_setting("defaults.farm_id")
            queue_id = get_setting("defaults.queue_id")
            if farm_id and queue_id:
                from ....job_attachments._aws.deadline import get_queue

                queue = get_queue(farm_id=farm_id, queue_id=queue_id)
                if queue.jobAttachmentSettings:
                    s3_bucket = queue.jobAttachmentSettings.s3BucketName
                    s3_prefix = queue.jobAttachmentSettings.rootPrefix
        except Exception:
            pass

        # Get the job history directory for the current profile
        job_history_dir = os.path.expanduser(get_setting("settings.job_history_dir"))

        browser = JobBundleBrowserDialog(
            local_root=default_dir,
            s3_bucket_name=s3_bucket,
            s3_root_prefix=s3_prefix,
            job_history_dir=job_history_dir,
            parent=self,
        )
        if browser.exec_() != JobBundleBrowserDialog.Accepted or not browser.selected_path:
            return

        if browser.selected_is_s3 and browser.s3_repo:
            if browser.selected_is_archive:
                input_job_bundle_dir = browser.s3_repo.resolve_bundle(browser.selected_path, "")
            else:
                import tempfile
                import atexit
                import shutil

                temp_dir = tempfile.mkdtemp(prefix="deadline-bundle-")
                atexit.register(shutil.rmtree, temp_dir, True)
                input_job_bundle_dir = browser.s3_repo.resolve_bundle(
                    browser.selected_path, temp_dir
                )
        elif browser.selected_is_archive:
            import tempfile
            import atexit
            import shutil

            temp_dir = tempfile.mkdtemp(prefix="deadline-bundle-")
            atexit.register(shutil.rmtree, temp_dir, True)
            input_job_bundle_dir = browser._local_repo.extract_bundle(
                browser.selected_path, temp_dir
            )
        else:
            input_job_bundle_dir = browser.selected_path

        # Update job bundle directory path
        self.input_job_bundle_dir = input_job_bundle_dir

        # Warn the user if the Job Bundle could not be loaded
        try:
            validate_directory_symlink_containment(input_job_bundle_dir)

            asset_references_obj = (
                read_yaml_or_json_object(input_job_bundle_dir, "asset_references", False) or {}
            )
            asset_references = AssetReferences.from_dict(asset_references_obj)

            # Load the template to get the bundle name
            template = read_yaml_or_json_object(input_job_bundle_dir, "template", True)
            name = template.get("name", "Job bundle submission")  # type: ignore[union-attr]
            job_settings = JobBundleSettings(input_job_bundle_dir=input_job_bundle_dir, name=name)
            job_settings.parameters = read_job_bundle_parameters(input_job_bundle_dir)

        except Exception as e:
            msg = str(e)
            QMessageBox.warning(self, "Could not load job bundle", msg)  # type: ignore[call-arg]
            logger.warning(msg)
            return

        dialog = self.window()
        if dialog is not None and hasattr(dialog, "refresh"):
            dialog.refresh(  # type: ignore[union-attr]
                job_settings=job_settings,
                auto_detected_attachments=asset_references,
                attachments=None,
                load_new_bundle=True,
            )

    def update_settings(self, settings: JobBundleSettings):
        """
        Update a settings object with the latest values.
        """
        settings.input_job_bundle_dir = self.input_job_bundle_dir
        settings.parameters = self.parameters_widget.get_parameters()

    def get_parameters(self):
        """
        Returns a list of OpenJD parameter definition dicts with
        a "value" key filled from the widget.
        """
        return self.parameters_widget.get_parameters()

    def set_parameter_value(self, parameter: dict[str, Any]):
        """
        Given an OpenJD parameter definition with a "value" key,
        set the parameter value in the widget.

        If the parameter value cannot be set, raises a KeyError.
        """
        self.parameters_widget.set_parameter_value(parameter)
