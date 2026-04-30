# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Custom job bundle browser dialog that replaces the native folder picker.
Shows a navigable tree of directories/bundles with a preview panel.
"""

from __future__ import annotations

import os
from logging import getLogger
from typing import Optional, Union

from qtpy.QtCore import Qt, QModelIndex, Signal  # type: ignore
from qtpy.QtGui import QStandardItemModel, QStandardItem, QIcon  # type: ignore
from qtpy.QtWidgets import (  # type: ignore
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .._utils import tr
from ...job_bundle.repository import (
    BrowseEntry,
    BundleInfo,
    BundleRepository,
    LocalBundleRepository,
    S3BundleRepository,
)

logger = getLogger(__name__)

# Custom data roles
ROLE_PATH = Qt.UserRole + 1
ROLE_IS_BUNDLE = Qt.UserRole + 2
ROLE_LOADED = Qt.UserRole + 3


class JobBundleBrowserDialog(QDialog):
    """
    A dialog for browsing and selecting job bundles from local filesystem or S3.

    Args:
        local_root: Default local directory to browse.
        s3_bucket_name: The queue's job attachment S3 bucket name (optional).
        s3_root_prefix: The queue's job attachment S3 root prefix (optional).
        parent: Parent widget.
    """

    bundle_selected = Signal(str)  # Emits the selected bundle path

    def __init__(
        self,
        local_root: str = "",
        s3_bucket_name: str = "",
        s3_root_prefix: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent=parent)
        self.setWindowTitle(tr("Browse Job Bundles"))
        self.setMinimumSize(700, 500)
        self.resize(800, 550)

        self._local_repo = LocalBundleRepository(root=local_root)
        self._s3_repo: Optional[S3BundleRepository] = None
        self._s3_available = bool(s3_bucket_name)
        if s3_bucket_name:
            self._s3_repo = S3BundleRepository(
                bucket_name=s3_bucket_name, root_prefix=s3_root_prefix
            )

        self._current_repo: BundleRepository = self._local_repo
        self._selected_path: Optional[str] = None
        self._selected_is_s3 = False

        self._build_ui()
        self._populate_root()

    @property
    def selected_path(self) -> Optional[str]:
        return self._selected_path

    @property
    def selected_is_s3(self) -> bool:
        return self._selected_is_s3

    @property
    def s3_repo(self) -> Optional[S3BundleRepository]:
        return self._s3_repo

    # ── UI Construction ──────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Main splitter: tree on left, preview on right
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, stretch=1)

        # Left: tree view
        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels([tr("Name")])
        self._tree = QTreeView()
        self._tree.setModel(self._model)
        self._tree.setHeaderHidden(True)
        self._tree.setEditTriggers(QTreeView.NoEditTriggers)
        self._tree.expanded.connect(self._on_expanded)
        self._tree.clicked.connect(self._on_clicked)
        self._tree.selectionModel().currentChanged.connect(self._on_selection_changed)
        splitter.addWidget(self._tree)

        # Right: preview panel
        preview_widget = QWidget()
        preview_layout = QVBoxLayout(preview_widget)
        preview_layout.setAlignment(Qt.AlignTop)

        self._preview_name = QLabel()
        self._preview_name.setWordWrap(True)
        self._preview_name.setStyleSheet("font-weight: bold; font-size: 14px;")
        preview_layout.addWidget(self._preview_name)

        self._preview_desc = QLabel()
        self._preview_desc.setWordWrap(True)
        preview_layout.addWidget(self._preview_desc)

        self._preview_steps_label = QLabel(tr("Steps:"))
        self._preview_steps_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        preview_layout.addWidget(self._preview_steps_label)
        self._preview_steps = QLabel()
        self._preview_steps.setWordWrap(True)
        preview_layout.addWidget(self._preview_steps)

        self._preview_params_label = QLabel(tr("Parameters:"))
        self._preview_params_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        preview_layout.addWidget(self._preview_params_label)
        self._preview_params = QLabel()
        self._preview_params.setWordWrap(True)
        preview_layout.addWidget(self._preview_params)

        self._clear_preview()
        splitter.addWidget(preview_widget)
        splitter.setSizes([350, 350])

        # Bottom: source toggle + path + buttons
        bottom_layout = QVBoxLayout()

        # Source toggle row
        source_row = QHBoxLayout()
        source_label = QLabel(tr("Source:"))
        source_row.addWidget(source_label)
        self._radio_local = QRadioButton(tr("Local"))
        self._radio_local.setChecked(True)
        self._radio_local.toggled.connect(self._on_source_changed)
        source_row.addWidget(self._radio_local)
        self._radio_s3 = QRadioButton(
            tr("S3 ({bucket})").format(
                bucket=self._s3_repo._bucket if self._s3_repo else tr("not configured")
            )
        )
        self._radio_s3.setEnabled(self._s3_available)
        source_row.addWidget(self._radio_s3)
        source_row.addStretch()
        bottom_layout.addLayout(source_row)

        # Path row
        path_row = QHBoxLayout()
        path_label = QLabel(tr("Path:"))
        path_row.addWidget(path_label)
        self._path_display = QLineEdit()
        self._path_display.setReadOnly(True)
        path_row.addWidget(self._path_display)
        bottom_layout.addLayout(path_row)

        layout.addLayout(bottom_layout)

        # Dialog buttons
        self._button_box = QDialogButtonBox(QDialogButtonBox.Cancel)
        self._select_button = QPushButton(tr("Select"))
        self._select_button.setEnabled(False)
        self._button_box.addButton(self._select_button, QDialogButtonBox.AcceptRole)
        self._select_button.clicked.connect(self.accept)
        self._button_box.rejected.connect(self.reject)
        layout.addWidget(self._button_box)

    # ── Tree Population ──────────────────────────────────────────

    def _populate_root(self):
        self._model.clear()
        self._model.setHorizontalHeaderLabels([tr("Name")])
        root_path = self._current_repo.root_path()
        self._path_display.setText(root_path)
        entries = self._current_repo.list_entries(root_path)
        root = self._model.invisibleRootItem()
        for entry in entries:
            self._add_entry_item(root, entry)

    def _add_entry_item(self, parent_item: QStandardItem, entry: BrowseEntry):
        item = QStandardItem(self._entry_display(entry))
        item.setData(entry.path, ROLE_PATH)
        item.setData(entry.is_bundle, ROLE_IS_BUNDLE)
        item.setData(False, ROLE_LOADED)
        if not entry.is_bundle:
            # Add a placeholder child so the expand arrow shows
            placeholder = QStandardItem()
            item.appendRow(placeholder)
        parent_item.appendRow(item)

    @staticmethod
    def _entry_display(entry: BrowseEntry) -> str:
        icon = "\U0001F4E6" if entry.is_bundle else "\U0001F4C1"  # 📦 or 📁
        return f"{icon} {entry.name}"

    # ── Event Handlers ───────────────────────────────────────────

    def _on_expanded(self, index: QModelIndex):
        item = self._model.itemFromIndex(index)
        if not item or item.data(ROLE_IS_BUNDLE) or item.data(ROLE_LOADED):
            return
        # Mark as loaded and replace placeholder with real children
        item.setData(True, ROLE_LOADED)
        item.removeRows(0, item.rowCount())
        path = item.data(ROLE_PATH)
        entries = self._current_repo.list_entries(path)
        for entry in entries:
            self._add_entry_item(item, entry)

    def _on_clicked(self, index: QModelIndex):
        self._update_selection(index)

    def _on_selection_changed(self, current: QModelIndex, previous: QModelIndex):
        self._update_selection(current)

    def _update_selection(self, index: QModelIndex):
        item = self._model.itemFromIndex(index)
        if not item:
            self._clear_preview()
            self._select_button.setEnabled(False)
            self._selected_path = None
            return

        path = item.data(ROLE_PATH)
        is_bundle = item.data(ROLE_IS_BUNDLE)
        self._path_display.setText(path)

        if is_bundle:
            self._selected_path = path
            self._selected_is_s3 = not self._radio_local.isChecked()
            self._select_button.setEnabled(True)
            self._load_preview(path)
        else:
            self._selected_path = None
            self._select_button.setEnabled(False)
            self._clear_preview()

    def _on_source_changed(self, checked: bool):
        if self._radio_local.isChecked():
            self._current_repo = self._local_repo
        elif self._s3_repo:
            self._current_repo = self._s3_repo
        self._selected_path = None
        self._select_button.setEnabled(False)
        self._clear_preview()
        self._populate_root()

    # ── Preview ──────────────────────────────────────────────────

    def _load_preview(self, path: str):
        info = self._current_repo.get_bundle_info(path)
        if not info:
            self._clear_preview()
            return

        self._preview_name.setText(info.name)
        self._preview_name.setVisible(True)

        if info.description:
            self._preview_desc.setText(info.description)
            self._preview_desc.setVisible(True)
        else:
            self._preview_desc.setVisible(False)

        if info.step_names:
            self._preview_steps_label.setVisible(True)
            self._preview_steps.setText(
                "\n".join(f"  \u2022 {name}" for name in info.step_names)
            )
            self._preview_steps.setVisible(True)
        else:
            self._preview_steps_label.setVisible(False)
            self._preview_steps.setVisible(False)

        if info.parameters:
            self._preview_params_label.setVisible(True)
            lines = []
            for p in info.parameters:
                pname = p.get("name", "?")
                ptype = p.get("type", "?")
                lines.append(f"  \u2022 {pname} ({ptype})")
            self._preview_params.setText("\n".join(lines))
            self._preview_params.setVisible(True)
        else:
            self._preview_params_label.setVisible(False)
            self._preview_params.setVisible(False)

    def _clear_preview(self):
        self._preview_name.setText(tr("Select a job bundle to see details"))
        self._preview_name.setStyleSheet("font-weight: bold; font-size: 14px; color: gray;")
        self._preview_desc.setVisible(False)
        self._preview_steps_label.setVisible(False)
        self._preview_steps.setVisible(False)
        self._preview_params_label.setVisible(False)
        self._preview_params.setVisible(False)
