# coding: utf-8
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""UI Components for the Render Submitter"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import zipfile
from typing import Any, Dict, Optional, Protocol
import yaml

from qtpy.QtCore import QSize, Qt, Signal as _Signal  # pylint: disable=import-error
from qtpy.QtGui import QKeyEvent  # pylint: disable=import-error
from qtpy.QtWidgets import (  # pylint: disable=import-error; type: ignore
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .submit_job_progress_dialog import SubmitJobProgressDialog

from ..dataclasses import HostRequirements
from ...dataclasses import SubmitterInfo
from ... import api
from ...api._session import session_context as _session_context
from ..controllers import AsyncTaskRunner as _AsyncTaskRunner
from ..deadline_authentication_status import DeadlineAuthenticationStatus
from .._utils import block_signals, tr
from ...config import get_setting, set_setting, config_file
from ...config.config_file import _SETTING_FARM_ID, _SETTING_QUEUE_ID
from ...exceptions import UserInitiatedCancel, NonValidInputError
from ...job_bundle import create_job_history_bundle_dir
from ...job_bundle.parameters import JobParameter
from ...job_bundle.submission import AssetReferences
from ...job_bundle.repository import (
    LocalBundleRepository,
    METADATA_KEY_DESC,
    METADATA_KEY_NAME,
    METADATA_KEY_PARAMS,
    METADATA_KEY_STEPS,
    METADATA_LIMIT_DESC,
    METADATA_LIMIT_NAME,
    METADATA_LIMIT_PARAMS,
    METADATA_LIMIT_STEPS,
    S3BundleRepository,
    _extract_bundle_info,
    _parse_template,
)
from ..widgets.deadline_authentication_status_widget import DeadlineAuthenticationStatusWidget
from ..widgets.job_attachments_tab import JobAttachmentsWidget
from ..widgets.shared_job_settings_tab import SharedJobSettingsWidget
from ..widgets.host_requirements_tab import HostRequirementsWidget
from . import DeadlineConfigDialog, DeadlineLoginDialog
from ._types import JobBundlePurpose
from ._help_dialog import _HelpDialog
from .export_bundle_dialog import ExportBundleDialog

logger = logging.getLogger(__name__)


def _truncate_metadata(value: str, limit: int, field: str) -> str:
    """Truncate a metadata value, warning if truncation occurs.

    S3 user-defined metadata is limited to 2 KB total (sum of all UTF-8 encoded keys and values).
    We apply conservative per-field limits to stay well within that budget.
    See: https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingMetadata.html#UserMetadata
    """
    if len(value) > limit:
        logger.warning(
            "Bundle metadata '%s' truncated from %d to %d characters", field, len(value), limit
        )
        return value[: limit - 3] + "..."
    return value


# initialize early so once the UI opens, things are already initialized
DeadlineAuthenticationStatus.getInstance()


class OnCreateJobBundleCallback(Protocol):
    """This protocol defines the callback for creating a job bundle in the SubmitJobToDeadlineDialog."""

    def __call__(
        self,
        widget: SubmitJobToDeadlineDialog,
        job_bundle_dir: str,
        settings: Any,
        queue_parameters: list[JobParameter],
        asset_references: AssetReferences,
        host_requirements: Optional[Dict[str, Any]] = None,
        *,
        purpose: JobBundlePurpose,
    ) -> Optional[dict[str, Any]]: ...


class SubmitJobToDeadlineDialog(QDialog):
    """
    A widget containing all the standard tabs for submitting an AWS Deadline Cloud job.

    If you're using this dialog within an application and want it to stay in front,
    pass f=Qt.Tool, a flag that tells it to do that.

    Args:
        job_setup_widget_type (QWidget): The type of the widget for the job-specific settings.
        initial_job_settings (dataclass): A dataclass containing the initial job settings
        initial_shared_parameter_values (dict[str, Any]): A dict of parameter values {<name>, <value>, ...}
            to override default queue parameter values from the queue. For example,
            a Rez queue environment may have a default "" for the RezPackages parameter, but a Maya
            submitter would override that default with "maya-2023" or similar.
        auto_detected_attachments (AssetReferences): The job attachments that were automatically detected
            from the input document/scene file or starting job bundle.
        attachments (AssetReferences): The job attachments that have been added to the job by the user.
        on_create_job_bundle_callback (OnCreateJobBundleCallback): A function to call when the dialog
            needs to create a Job Bundle. It is called with arguments:
            (widget, job_bundle_dir, settings, queue_parameters, asset_references, host_requirements, purpose).
            It can return either None or a dict with parameters about the submission. Currently,
            the additional parameters supported are:
            {
                # See documentation for deadline.client.api.create_job_from_job_bundle about these parameters
                "job_parameters": [{"name": "ParameterName", "value": "Parameter Value", ...}],
                "known_asset_paths": ["/path/1", ...],
            }
        parent: parent of the widget
        f: Qt Window Flags
        show_host_requirements_tab: Display the host requirements tab in dialog if set to True. Default
            to False.
        submitter_info (SubmitterInfo): Information related to the submitter window and application it's running in
    """

    _auto_select_complete = _Signal()

    def __init__(
        self,
        *,
        job_setup_widget_type: type[QWidget],
        initial_job_settings: Any,
        initial_shared_parameter_values: dict[str, Any],
        auto_detected_attachments: AssetReferences,
        attachments: AssetReferences,
        on_create_job_bundle_callback: OnCreateJobBundleCallback,
        parent: Optional[QWidget] = None,
        f: Any = Qt.WindowFlags(),
        show_host_requirements_tab: bool = False,
        host_requirements: Optional[HostRequirements] = None,
        submitter_info: Optional[SubmitterInfo] = None,
        known_asset_paths: Optional[list[str]] = None,
    ):
        # The Qt.Tool flag makes sure our widget stays in front of the main application window
        super().__init__(parent=parent, f=f)

        # Set window title with submitter package info if available
        window_title = tr("Submit to AWS Deadline Cloud")
        if submitter_info:
            # e.g. Deadline Cloud Blender Submitter x.y.z
            formatted_name = f"Deadline Cloud {submitter_info.submitter_name} {tr('Submitter')}"
            if submitter_info.submitter_package_version:
                window_title = f"{formatted_name} {submitter_info.submitter_package_version}"
            else:
                window_title = f"{formatted_name}"
        self.setWindowTitle(window_title)

        self.setMinimumSize(400, 400)

        self.job_settings_type = type(initial_job_settings)
        self.submitter_info = submitter_info or SubmitterInfo(
            submitter_name=self.job_settings_type().submitter_name
        )
        _session_context["submitter-name"] = self.submitter_info.submitter_name
        _session_context["submitter-version"] = self.submitter_info.submitter_package_version

        self.on_create_job_bundle_callback = on_create_job_bundle_callback
        self.job_id = None
        self.job_history_bundle_dir: Optional[str] = None
        self.deadline_authentication_status = DeadlineAuthenticationStatus.getInstance()
        self.show_host_requirements_tab = show_host_requirements_tab
        self.known_asset_paths = known_asset_paths or []
        self.should_close = False

        # Runs the auto-select farm/queue API calls off the Qt event loop. Using a
        # single operation_key means a newer auto-select supersedes (and cancels) an
        # in-flight older one, so a stale result can never clobber newer settings.
        self._auto_select_runner = _AsyncTaskRunner(self)

        self._build_ui(
            job_setup_widget_type,
            initial_job_settings,
            initial_shared_parameter_values,
            auto_detected_attachments,
            attachments,
            host_requirements,
        )

        self.gui_update_counter: Any = None
        self.refresh_deadline_settings()

    def _submission_succeeded_signal_receiver(self, job_id: str):
        self.job_id = job_id

        set_setting("defaults.job_id", job_id)

    def _close_event_receiver(self):
        if self.submitter_info.submitter_name != "JobBundle" and self.job_id:
            self.close()

    def sizeHint(self):
        return QSize(540, 700)

    def refresh(
        self,
        *,
        job_settings: Optional[Any] = None,
        auto_detected_attachments: Optional[AssetReferences] = None,
        attachments: Optional[AssetReferences] = None,
        load_new_bundle: bool = False,
    ):
        # Refresh the UI components
        self.refresh_deadline_settings()
        if (auto_detected_attachments is not None) or (attachments is not None):
            self.job_attachments.refresh_ui(auto_detected_attachments, attachments)

        if job_settings is not None:
            self.job_settings_type = type(job_settings)
            # Refresh shared job settings
            self.shared_job_settings.refresh_ui(job_settings, load_new_bundle)
            # Refresh job specific settings
            if hasattr(self.job_settings, "refresh_ui"):
                self.job_settings.refresh_ui(job_settings)

    def _build_ui(
        self,
        job_setup_widget_type,
        initial_job_settings,
        initial_shared_parameter_values,
        auto_detected_attachments: AssetReferences,
        attachments: AssetReferences,
        host_requirements: Optional[HostRequirements],
    ):
        self.lyt = QVBoxLayout(self)
        self.lyt.setContentsMargins(5, 5, 5, 5)

        man_layout = QFormLayout()
        self.lyt.addLayout(man_layout)
        self.tabs = QTabWidget()
        self.lyt.addWidget(self.tabs)

        self._build_shared_job_settings_tab(initial_job_settings, initial_shared_parameter_values)
        self._build_job_settings_tab(job_setup_widget_type, initial_job_settings)
        self._build_job_attachments_tab(auto_detected_attachments, attachments)

        # Show host requirements only if requested by the constructor
        if self.show_host_requirements_tab:
            self._build_host_requirements_tab(host_requirements)

        self.auth_status_box = DeadlineAuthenticationStatusWidget(self)
        self.auth_status_box.switch_profile_clicked.connect(self.on_switch_profile_clicked)
        self.auth_status_box.logout_clicked.connect(self.on_logout)
        self.auth_status_box.login_clicked.connect(self.on_login)
        self.lyt.addWidget(self.auth_status_box)
        self.deadline_authentication_status.api_availability_changed.connect(
            self.refresh_deadline_settings
        )
        self._auto_select_complete.connect(self.refresh_deadline_settings)

        # Refresh the submit button enable state once queue parameter status changes
        self.shared_job_settings.valid_parameters.connect(self._set_submit_button_state)

        self.button_box = QDialogButtonBox(Qt.Horizontal)
        self.settings_button = QPushButton(tr("Settings..."))
        self.settings_button.clicked.connect(self.on_settings_button_clicked)
        self.button_box.addButton(self.settings_button, QDialogButtonBox.ResetRole)
        self.help_button = QPushButton(tr("Help"))
        self.help_button.clicked.connect(self._on_help_button_clicked)
        self.button_box.addButton(self.help_button, QDialogButtonBox.HelpRole)
        self.submit_button = QPushButton(tr("Submit"))
        self.submit_button.clicked.connect(self.on_submit)
        self.button_box.addButton(self.submit_button, QDialogButtonBox.AcceptRole)
        if hasattr(initial_job_settings, "browse_enabled") and initial_job_settings.browse_enabled:
            self.load_bundle_button = QPushButton(tr("Load Bundle"))
            self.load_bundle_button.clicked.connect(self._on_load_bundle)
            self.button_box.addButton(self.load_bundle_button, QDialogButtonBox.AcceptRole)
        self.export_bundle_button = QPushButton(tr("Export bundle"))
        self.export_bundle_button.clicked.connect(self.on_export_bundle)
        self.button_box.addButton(self.export_bundle_button, QDialogButtonBox.AcceptRole)

        self.lyt.addWidget(self.button_box)

    def _set_submit_button_state(self):
        # Enable/disable the Submit button based on whether the
        # AWS Deadline Cloud API is accessible and the farm+queue are configured.
        api_available = self.deadline_authentication_status.api_availability is True
        farm_configured = get_setting(_SETTING_FARM_ID) != ""
        queue_configured = get_setting(_SETTING_QUEUE_ID) != ""
        queue_valid = self.shared_job_settings.is_queue_valid()

        enable = api_available and farm_configured and queue_configured and queue_valid

        self.submit_button.setEnabled(enable)

        if not enable:
            issues = []
            if not api_available:
                issues.append(
                    tr(
                        "AWS Deadline Cloud API is not accessible. Check your authentication status."
                    )
                )
            if not farm_configured:
                issues.append(
                    tr("No farm is configured. Click Settings to select a farm for job submission.")
                )
            if not queue_configured:
                issues.append(
                    tr("No queue is configured. Click Settings to select a queue within your farm.")
                )
            if farm_configured and queue_configured and not queue_valid:
                issues.append(
                    tr("Queue parameters are not valid. Check Shared job settings tab for details.")
                )

            self.submit_button.setToolTip(
                tr("Cannot submit job:\n\n\u2022 {issues}").format(
                    issues="\n\n\u2022 ".join(issues)
                )
            )
        else:
            self.submit_button.setToolTip("")

    def refresh_deadline_settings(self):
        self._auto_select_defaults()
        self._set_submit_button_state()

        self.shared_job_settings.deadline_cloud_settings_box.refresh_setting_controls(
            self.deadline_authentication_status.api_availability is True
        )
        # If necessary, this reloads the queue parameters
        self.shared_job_settings.refresh_queue_parameters()

    # Identifies the auto-select task in the AsyncTaskRunner. Reusing one key means a
    # newer auto-select supersedes any in-flight older one (latest-wins).
    _AUTO_SELECT_OPERATION_KEY = "submit_dialog_auto_select_defaults"

    def _auto_select_defaults(self):
        """Kick off an auto-select of the default farm/queue if only one is available.

        The AWS API calls run in a background thread via ``AsyncTaskRunner``; the
        result is applied back on the Qt main thread (see ``_on_auto_select_resolved``)
        so we never touch settings or widgets from the worker thread.
        """
        if self.deadline_authentication_status.api_availability is not True:
            return
        farm_id = get_setting(_SETTING_FARM_ID)
        queue_id = get_setting(_SETTING_QUEUE_ID)
        if farm_id and queue_id:
            # Nothing to select.
            return
        if self._auto_select_runner.is_running(self._AUTO_SELECT_OPERATION_KEY):
            # An auto-select is already in flight; let it finish. When it applies a
            # change it emits ``_auto_select_complete`` which re-enters here to pick
            # up the next step (e.g. select the queue after the farm).
            return

        self._auto_select_runner.run(
            operation_key=self._AUTO_SELECT_OPERATION_KEY,
            fn=self._resolve_auto_select,
            on_success=self._on_auto_select_resolved,
            on_error=self._on_auto_select_error,
            current_farm_id=farm_id,
            current_queue_id=queue_id,
        )

    @staticmethod
    def _resolve_auto_select(
        *, current_farm_id: str, current_queue_id: str
    ) -> dict[str, Optional[str]]:
        """Background worker: resolve the farm/queue to auto-select.

        Runs in a worker thread, so it only calls AWS APIs and returns plain data -
        it does not read or write settings or touch any widgets.
        """
        farm_id_to_set: Optional[str] = None
        queue_id_to_set: Optional[str] = None

        farm_id = current_farm_id
        if not farm_id:
            farms = api.list_farms().get("farms", [])
            if len(farms) == 1:
                farm_id = farms[0]["farmId"]
                farm_id_to_set = farm_id

        if farm_id and not current_queue_id:
            queues = api.list_queues(farmId=farm_id).get("queues", [])
            if len(queues) == 1:
                queue_id_to_set = queues[0]["queueId"]

        return {
            "farm_id": farm_id_to_set,
            "queue_id": queue_id_to_set,
            # The farm the queue was resolved under, so the main thread can confirm
            # it still matches the configured farm before applying the queue.
            "queue_farm_id": farm_id if queue_id_to_set else None,
        }

    def _on_auto_select_resolved(self, result: dict[str, Optional[str]]):
        """Main-thread slot: apply the resolved farm/queue if still applicable.

        Re-checks the current settings before writing so that a result computed
        against now-stale state (e.g. the user changed the farm while the request
        was in flight) is discarded rather than clobbering newer values.
        """
        applied = False

        farm_id = result.get("farm_id")
        if farm_id and not get_setting(_SETTING_FARM_ID):
            set_setting(_SETTING_FARM_ID, farm_id)
            applied = True

        queue_id = result.get("queue_id")
        if queue_id and not get_setting(_SETTING_QUEUE_ID):
            # Only apply the queue if it was resolved for the farm that is still
            # configured; otherwise it would be a queue from a different farm.
            if get_setting(_SETTING_FARM_ID) == result.get("queue_farm_id"):
                set_setting(_SETTING_QUEUE_ID, queue_id)
                applied = True

        if applied:
            self._auto_select_complete.emit()

    def _on_auto_select_error(self, error: BaseException):
        """Main-thread slot: auto-select is best-effort, so just log failures."""
        logger.debug("Auto-select defaults failed", exc_info=error)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """
        Override to capture any enter/return key presses so that the Submit
        button isn't "pressed" when the enter/return key is.
        """
        if event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            return
        super().keyPressEvent(event)

    def _build_shared_job_settings_tab(self, initial_job_settings, initial_shared_parameter_values):
        self.shared_job_settings_tab = QScrollArea()
        self.tabs.addTab(self.shared_job_settings_tab, tr("Shared job settings"))
        self.shared_job_settings = SharedJobSettingsWidget(
            initial_settings=initial_job_settings,
            initial_shared_parameter_values=initial_shared_parameter_values,
            parent=self,
        )
        self.shared_job_settings.parameter_changed.connect(self.on_shared_job_parameter_changed)
        self.shared_job_settings_tab.setWidget(self.shared_job_settings)
        self.shared_job_settings_tab.setWidgetResizable(True)
        self.shared_job_settings.parameter_changed.connect(self.on_shared_job_parameter_changed)

    def _build_job_settings_tab(self, job_setup_widget_type, initial_job_settings):
        self.job_settings_tab = QScrollArea()
        self.tabs.addTab(self.job_settings_tab, tr("Job-specific settings"))
        self.job_settings_tab.setWidgetResizable(True)

        self.job_settings = job_setup_widget_type(
            initial_settings=initial_job_settings, parent=self
        )
        self.job_settings_tab.setWidget(self.job_settings)
        if hasattr(self.job_settings, "parameter_changed"):
            self.job_settings.parameter_changed.connect(self.on_job_template_parameter_changed)

    def _build_job_attachments_tab(
        self, auto_detected_attachments: AssetReferences, attachments: AssetReferences
    ):
        self.job_attachments_tab = QScrollArea()
        self.tabs.addTab(self.job_attachments_tab, tr("Job attachments"))
        self.job_attachments = JobAttachmentsWidget(
            auto_detected_attachments, attachments, parent=self
        )
        self.job_attachments_tab.setWidget(self.job_attachments)
        self.job_attachments_tab.setWidgetResizable(True)

    def _build_host_requirements_tab(self, host_requirements: Optional[HostRequirements]):
        self.host_requirements = HostRequirementsWidget()
        self.host_requirements_tab = QScrollArea()
        self.tabs.addTab(self.host_requirements_tab, tr("Host requirements"))
        self.host_requirements_tab.setWidget(self.host_requirements)
        self.host_requirements_tab.setWidgetResizable(True)
        if host_requirements:
            self.host_requirements.set_requirements(host_requirements)

    def on_shared_job_parameter_changed(self, parameter: dict[str, Any]):
        """
        Handles an edit to a shared job parameter, for example one of the
        queue parameters.

        When a queue parameter and a job template parameter have
        the same name, we update between them to keep them consistent.
        """
        try:
            if hasattr(self.job_settings, "set_parameter_value"):
                with block_signals(self.job_settings):
                    self.job_settings.set_parameter_value(parameter)
        except KeyError:
            # If there is no corresponding job template parameter,
            # just ignore it.
            pass

    def on_job_template_parameter_changed(self, parameter: dict[str, Any]):
        """
        Handles an edit to a job template parameter.

        When a queue parameter and a job template parameter have
        the same name, we update between them to keep them consistent.
        """
        try:
            with block_signals(self.shared_job_settings):
                self.shared_job_settings.set_parameter_value(parameter)
        except KeyError:
            # If there is no corresponding queue parameter,
            # just ignore it.
            pass

    def on_login(self):
        DeadlineLoginDialog.login(parent=self)
        self.refresh_deadline_settings()
        # This widget watches the auth files, but that does
        # not always catch a change so force a refresh here.
        self.deadline_authentication_status.refresh_status()

    def on_logout(self):
        api.logout()
        self.refresh_deadline_settings()
        # This widget watches the auth files, but that does
        # not always catch a change so force a refresh here.
        self.deadline_authentication_status.refresh_status()

    def on_switch_profile_clicked(self):
        if DeadlineConfigDialog.configure_settings(parent=self, set_profile_focus=True):
            self.refresh_deadline_settings()

    def on_settings_button_clicked(self):
        if DeadlineConfigDialog.configure_settings(parent=self):
            self.refresh_deadline_settings()

    def _on_help_button_clicked(self):
        """Show the Help dialog with submitter information."""
        try:
            dialog = _HelpDialog(self.submitter_info, parent=self)
            dialog.exec_()
        except Exception as e:
            logger.error(f"Failed to create HelpDialog: {e}")
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to display Help dialog: {str(e)}",
            )

    def _on_load_bundle(self):
        """Delegates to the job_settings widget's on_load_bundle method."""
        if hasattr(self.job_settings, "on_load_bundle"):
            self.job_settings.on_load_bundle()

    def on_export_bundle(self):
        """Export a job bundle to Queue (S3) or a local directory."""
        # Gather settings
        settings = self.job_settings_type()
        self.shared_job_settings.update_settings(settings)
        self.job_settings.update_settings(settings)
        queue_parameters = self.shared_job_settings.get_parameters()

        # Default export name is the bundle directory name on disk
        resolved_name = (
            os.path.basename(settings.input_job_bundle_dir)
            if settings.input_job_bundle_dir
            else settings.name
        )

        # Try to get queue repo for the dialog
        queue_repo = None
        queue_error = ""
        try:
            queue_repo = S3BundleRepository.from_config()
        except Exception as e:
            queue_error = str(e)

        # Get default local directory
        local_dir = get_setting("settings.job_bundle_default_directory")
        if local_dir:
            local_dir = os.path.expanduser(local_dir)
        else:
            local_dir = os.path.expanduser("~")

        # Show export dialog
        dialog = ExportBundleDialog(
            default_name=resolved_name,
            queue_repo=queue_repo,
            queue_error=queue_error,
            local_dir=local_dir,
            parent=self,
        )
        if dialog.exec_() != ExportBundleDialog.Accepted or not dialog.bundle_name:
            return

        # Create the bundle locally first
        asset_references = self.job_attachments.get_asset_references()
        try:
            self.job_history_bundle_dir = create_job_history_bundle_dir(
                self.submitter_info.submitter_name, settings.name
            )
            if self.show_host_requirements_tab:
                parameters_from_callback = self.on_create_job_bundle_callback(
                    self,
                    self.job_history_bundle_dir,
                    settings,
                    queue_parameters,
                    asset_references,
                    self.host_requirements.get_requirements(),
                    purpose=JobBundlePurpose.EXPORT,
                )
            else:
                parameters_from_callback = self.on_create_job_bundle_callback(
                    self,
                    self.job_history_bundle_dir,
                    settings,
                    queue_parameters,
                    asset_references,
                    purpose=JobBundlePurpose.EXPORT,
                )
            if parameters_from_callback is None:
                parameters_from_callback = {}
            job_parameters = parameters_from_callback.get("job_parameters", [])
            if job_parameters:
                self.save_job_parameters_to_job_bundle(self.job_history_bundle_dir, job_parameters)
        except NonValidInputError as nvie:
            QMessageBox.critical(self, tr("Non valid inputs detected"), str(nvie))
            return
        except Exception as exc:
            logger.exception("Error creating bundle")
            QMessageBox.critical(self, "Export failed", f"Failed to create bundle:\n{exc}")
            return

        bundle_name = dialog.bundle_name

        if dialog.export_to_queue:
            self._export_to_queue(queue_repo, bundle_name)
        else:
            self._export_to_local(dialog.local_directory, bundle_name)

    def _export_to_local(self, dest_dir: str, bundle_name: str):
        """Copy the bundle to a local directory."""
        assert self.job_history_bundle_dir is not None
        dest_path = os.path.join(dest_dir, bundle_name)
        try:
            if os.path.exists(dest_path):
                shutil.rmtree(dest_path)
            shutil.copytree(self.job_history_bundle_dir, dest_path)
            QMessageBox.information(
                self,
                tr("Export bundle"),
                f"Bundle exported to:\n{dest_path}",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"Failed to save bundle:\n{exc}")

    def _export_to_queue(self, queue_repo: Optional[S3BundleRepository], bundle_name: str):
        """Archive and upload the bundle to the queue's S3 job-bundles folder."""
        if not queue_repo:
            QMessageBox.critical(self, "Export failed", "Queue is not available.")
            return

        assert self.job_history_bundle_dir is not None

        # Build S3 metadata
        bundle_metadata: dict[str, str] = {}
        for tname in ("template.yaml", "template.json"):
            tpath = os.path.join(self.job_history_bundle_dir, tname)
            if os.path.isfile(tpath):
                with open(tpath, encoding="utf-8") as f:
                    template = _parse_template(f.read(), tname)
                if template:
                    pv = LocalBundleRepository._read_parameter_values(self.job_history_bundle_dir)
                    info = _extract_bundle_info(template, self.job_history_bundle_dir, pv)
                    bundle_metadata[METADATA_KEY_NAME] = _truncate_metadata(
                        bundle_name, METADATA_LIMIT_NAME, METADATA_KEY_NAME
                    )
                    if info.description:
                        desc = " ".join(info.description.split())
                        bundle_metadata[METADATA_KEY_DESC] = _truncate_metadata(
                            desc, METADATA_LIMIT_DESC, METADATA_KEY_DESC
                        )
                    if info.step_names:
                        bundle_metadata[METADATA_KEY_STEPS] = _truncate_metadata(
                            ",".join(info.step_names), METADATA_LIMIT_STEPS, METADATA_KEY_STEPS
                        )
                    if info.parameters:
                        param_strs = [
                            f"{p.get('name', '?')}:{p.get('type', '?')}" for p in info.parameters
                        ]
                        bundle_metadata[METADATA_KEY_PARAMS] = _truncate_metadata(
                            ",".join(param_strs), METADATA_LIMIT_PARAMS, METADATA_KEY_PARAMS
                        )
                break

        # Archive and upload
        try:
            s3_key = f"{queue_repo._prefix}{bundle_name}.ojd"
            s3 = queue_repo._s3

            # Check if bundle already exists
            try:
                s3.head_object(Bucket=queue_repo._bucket, Key=s3_key)
                reply = QMessageBox.question(
                    self,
                    "Overwrite?",
                    f"Bundle '{bundle_name}' already exists on the queue. Overwrite?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
            except Exception:
                pass

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(self.job_history_bundle_dir, followlinks=False):
                    dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
                    for fname in files:
                        local_path = os.path.join(root, fname)
                        if os.path.islink(local_path):
                            continue
                        arcname = os.path.relpath(local_path, self.job_history_bundle_dir)
                        zf.write(local_path, arcname)

            buf.seek(0)
            s3.upload_fileobj(
                buf,
                queue_repo._bucket,
                s3_key,
                ExtraArgs={"Metadata": bundle_metadata} if bundle_metadata else None,
            )

            QMessageBox.information(
                self,
                tr("Export bundle"),
                f"Bundle exported to queue:\ns3://{queue_repo._bucket}/{s3_key}",
            )
        except Exception as exc:
            from botocore.exceptions import ClientError

            logger.error("Failed to export bundle: %s", exc, exc_info=True)
            if isinstance(exc, ClientError) and exc.response["Error"]["Code"] == "AccessDenied":
                msg = "You don't have permission to share bundles on this queue."
            else:
                msg = f"Failed to upload bundle:\n{exc}"
            QMessageBox.critical(self, "Export failed", msg)

    def save_job_parameters_to_job_bundle(
        self, job_bundle_dir: str, job_parameters: list[JobParameter]
    ):
        """
        Saves the job parameters to the job bundle. If the job bundle already has a parameter_values file,
        it updates it. Otherwise it creates it.
        """
        job_parameters_dict = {param["name"]: param for param in job_parameters}

        job_parameters_file = os.path.join(job_bundle_dir, "parameter_values.yaml")
        if os.path.exists(job_parameters_file):
            with open(job_parameters_file, "r", encoding="utf8") as f:
                existing_job_parameters = yaml.safe_load(f).get("parameterValues", [])
        else:
            job_parameters_file = os.path.join(job_bundle_dir, "parameter_values.json")
            if os.path.exists(job_parameters_file):
                with open(job_parameters_file, "r", encoding="utf8") as f:
                    existing_job_parameters = json.load(f).get("parameterValues", [])
            else:
                existing_job_parameters = []

        # Overwrite any existing values, and add new values at the end
        combined_job_parameters = []
        for param in existing_job_parameters:
            combined_job_parameters.append(job_parameters_dict.pop(param["name"], param))
        combined_job_parameters.extend(job_parameters_dict.values())

        with open(job_parameters_file, "w", encoding="utf8") as f:
            json.dump({"parameterValues": combined_job_parameters}, f, indent=1)

    def on_submit(self):
        """
        Perform a submission when the submit button is pressed
        """
        # Retrieve all the settings into the dataclass
        settings = self.job_settings_type()
        self.shared_job_settings.update_settings(settings)
        self.job_settings.update_settings(settings)

        queue_parameters = self.shared_job_settings.get_parameters()

        asset_references = self.job_attachments.get_asset_references()

        job_progress_dialog = SubmitJobProgressDialog(parent=self)
        job_progress_dialog.submission_thread_succeeded.connect(
            self._submission_succeeded_signal_receiver
        )
        job_progress_dialog.progress_window_closed.connect(self._close_event_receiver)
        job_progress_dialog.setModal(True)
        job_progress_dialog.show()
        QApplication.instance().processEvents()  # type: ignore[union-attr]

        # Submit the job
        try:
            self.job_history_bundle_dir = create_job_history_bundle_dir(
                self.submitter_info.submitter_name, settings.name
            )

            if self.show_host_requirements_tab:
                requirements = self.host_requirements.get_requirements()
                parameters_from_callback = self.on_create_job_bundle_callback(
                    self,
                    self.job_history_bundle_dir,
                    settings,
                    queue_parameters,
                    asset_references,
                    requirements,
                    purpose=JobBundlePurpose.SUBMISSION,
                )
            else:
                # Maintaining backward compatibility for submitters that do not support host_requirements yet
                parameters_from_callback = self.on_create_job_bundle_callback(
                    self,
                    self.job_history_bundle_dir,
                    settings,
                    queue_parameters,
                    asset_references,
                    purpose=JobBundlePurpose.SUBMISSION,
                )
            if parameters_from_callback is None:
                parameters_from_callback = {}

            # If the callback returned job parameters, update them in the job bundle as well so that
            # submission from the job history dir is equivalent.
            job_parameters = parameters_from_callback.get("job_parameters", [])
            if job_parameters:
                self.save_job_parameters_to_job_bundle(self.job_history_bundle_dir, job_parameters)

            job_progress_dialog.start_job_submission(
                job_bundle_dir=self.job_history_bundle_dir,
                submitter_name=self.submitter_info.submitter_name,
                config=config_file.read_config(),
                require_paths_exist=self.job_attachments.get_require_paths_exist(),
                job_parameters=job_parameters,
                known_asset_paths=self.known_asset_paths
                + parameters_from_callback.get("known_asset_paths", []),
            )

        except UserInitiatedCancel as uic:
            logger.info("Canceling submission.")
            QMessageBox.information(
                self,
                tr("{submitter} job submission").format(
                    submitter=self.submitter_info.submitter_name
                ),
                str(uic),
            )
            job_progress_dialog.close()
        except NonValidInputError as nvie:
            QMessageBox.critical(self, tr("Non valid inputs detected"), str(nvie))
            job_progress_dialog.close()
        except Exception as exc:
            logger.exception("error submitting job")
            api.get_deadline_cloud_library_telemetry_client().record_error_with_trace(
                exc, "on_submit", from_gui=True
            )
            QMessageBox.critical(
                self,
                tr("{submitter} job submission").format(
                    submitter=self.submitter_info.submitter_name
                ),
                str(exc),
            )  # type: ignore[call-arg]
            job_progress_dialog.close()
