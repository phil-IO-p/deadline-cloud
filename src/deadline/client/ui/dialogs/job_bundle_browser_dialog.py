# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Custom job bundle browser dialog that replaces the native folder picker.
Shows a navigable tree of directories/bundles with a preview panel.
"""

from __future__ import annotations

import os
from logging import getLogger
from typing import Optional

from qtpy.QtCore import Qt, QModelIndex, QSortFilterProxyModel, QTimer, Signal  # type: ignore
from qtpy.QtGui import QStandardItemModel, QStandardItem  # type: ignore
from qtpy.QtWidgets import (  # type: ignore
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .._utils import tr
from ...job_bundle.repository import (
    BrowseEntry,
    BundleRepository,
    LocalBundleRepository,
    S3BundleRepository,
)

logger = getLogger(__name__)

# Custom data roles
ROLE_PATH = Qt.UserRole + 1
ROLE_IS_BUNDLE = Qt.UserRole + 2
ROLE_LOADED = Qt.UserRole + 3
ROLE_IS_ARCHIVE = Qt.UserRole + 4


class JobBundleBrowserDialog(QDialog):
    """
    A dialog for browsing and selecting job bundles from local filesystem or queue.

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
        s3_error: str = "",
        job_history_dir: str = "",
        session=None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent=parent)
        self.setWindowTitle(tr("Browse Job Bundles"))
        self.setMinimumSize(750, 550)
        self.resize(850, 620)

        self._local_repo = LocalBundleRepository(root=local_root, include_archives=False)
        self._s3_repo: Optional[S3BundleRepository] = None
        self._s3_error = s3_error
        self._s3_available = bool(s3_bucket_name)
        if s3_bucket_name:
            self._s3_repo = S3BundleRepository(
                bucket_name=s3_bucket_name, root_prefix=s3_root_prefix, session=session
            )

        self._history_dir = job_history_dir
        self._history_repo: Optional[LocalBundleRepository] = None
        if job_history_dir and os.path.isdir(job_history_dir):
            self._history_repo = LocalBundleRepository(root=job_history_dir, include_archives=False)

        self._current_repo: BundleRepository = self._local_repo
        self._selected_path: Optional[str] = None
        self._selected_is_s3 = False
        self._selected_is_archive = False
        self._ready = False

        self._build_ui()
        self._ready = True
        self._populate_root()

    @property
    def selected_path(self) -> Optional[str]:
        return self._selected_path

    @property
    def selected_is_s3(self) -> bool:
        return self._selected_is_s3

    @property
    def selected_is_archive(self) -> bool:
        return self._selected_is_archive

    @property
    def s3_repo(self) -> Optional[S3BundleRepository]:
        return self._s3_repo

    # ── UI Construction ──────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Source toggle row — at the top so users select source before browsing
        source_row = QHBoxLayout()
        source_label = QLabel(tr("Source:"))
        source_row.addWidget(source_label)
        self._radio_s3 = QRadioButton(tr("Queue"))
        self._radio_s3.setEnabled(self._s3_available)
        self._radio_s3.toggled.connect(self._on_source_changed)
        source_row.addWidget(self._radio_s3)
        self._radio_local = QRadioButton(tr("Local"))
        self._radio_local.toggled.connect(self._on_source_changed)
        source_row.addWidget(self._radio_local)
        self._radio_history = QRadioButton(tr("History"))
        self._radio_history.setEnabled(self._history_repo is not None)
        self._radio_history.toggled.connect(self._on_source_changed)
        source_row.addWidget(self._radio_history)
        source_row.addStretch()
        layout.addLayout(source_row)

        # Inline warning when queue source is unavailable
        self._queue_warning = QLabel()
        self._queue_warning.setWordWrap(True)
        self._queue_warning.setStyleSheet(
            "QLabel { color: #b35900; background-color: #fff3e0;"
            " border: 1px solid #ffcc80; border-radius: 4px;"
            " padding: 4px 8px; }"
        )
        if not self._s3_available and self._s3_error:
            self._queue_warning.setText(
                f"\u26a0 <b>Queue browsing unavailable:</b> {self._s3_error}"
            )
            self._queue_warning.setTextFormat(Qt.RichText)
            self._queue_warning.setVisible(True)
        else:
            self._queue_warning.setVisible(False)
        layout.addWidget(self._queue_warning)

        # Show hidden folders checkbox
        self._show_hidden_cb = QCheckBox(tr("Show hidden folders"), parent=self)
        self._show_hidden_cb.setChecked(False)
        self._show_hidden_cb.toggled.connect(self._on_hidden_toggled)
        layout.addWidget(self._show_hidden_cb)

        # Default to Queue if available, otherwise Local
        if self._s3_available:
            self._radio_s3.setChecked(True)
            self._current_repo = self._s3_repo
        else:
            self._radio_local.setChecked(True)

        # Main splitter: tree on left, preview on right
        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, stretch=1)

        # Left: tree view with filter
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter bundles...")
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        left_layout.addWidget(self._filter_edit)

        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels([tr("Name")])

        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setRecursiveFilteringEnabled(True)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)

        self._tree = QTreeView()
        self._tree.setModel(self._proxy)
        self._tree.setHeaderHidden(True)
        self._tree.setEditTriggers(QTreeView.NoEditTriggers)
        self._tree.expanded.connect(self._on_expanded)
        self._tree.clicked.connect(self._on_clicked)
        self._tree.doubleClicked.connect(self._on_double_clicked)
        self._tree.selectionModel().currentChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self._tree)

        splitter.addWidget(left_widget)

        # Right: preview panel in a scroll area
        preview_widget = QWidget()
        preview_layout = QVBoxLayout(preview_widget)

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
        self._preview_params = QTableWidget()
        self._preview_params.setColumnCount(3)
        self._preview_params.setHorizontalHeaderLabels(["Name", "Type", "Value"])
        self._preview_params.horizontalHeader().setStretchLastSection(True)
        self._preview_params.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._preview_params.verticalHeader().setVisible(False)
        self._preview_params.setEditTriggers(QTableWidget.NoEditTriggers)
        self._preview_params.setSelectionMode(QTableWidget.NoSelection)
        preview_layout.addWidget(self._preview_params, stretch=1)

        self._clear_preview()

        preview_scroll = QScrollArea()
        preview_scroll.setWidget(preview_widget)
        preview_scroll.setWidgetResizable(True)
        splitter.addWidget(preview_scroll)
        splitter.setSizes([350, 350])

        # Bottom: path display + buttons
        bottom_layout = QVBoxLayout()
        bottom_layout.setContentsMargins(0, 8, 0, 0)

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

    def _filter_entries(self, entries: list) -> list:
        """Filter out hidden entries (names starting with '.') unless show hidden is checked."""
        if self._show_hidden_cb.isChecked():
            return entries
        return [e for e in entries if not e.name.startswith(".")]

    def _populate_root(self):
        self._model.clear()
        self._model.setHorizontalHeaderLabels([tr("Name")])
        root_path = self._current_repo.root_path()
        self._path_display.setText(root_path)
        try:
            entries = self._current_repo.list_entries(root_path)
        except Exception as e:
            logger.warning("Failed to list bundles: %s", e, exc_info=True)
            self._show_error_preview(f"Failed to list bundles:\n{e}")
            entries = []
        root = self._model.invisibleRootItem()
        for entry in self._filter_entries(entries):
            self._add_entry_item(root, entry)

    def _add_entry_item(self, parent_item: QStandardItem, entry: BrowseEntry):
        item = QStandardItem(self._entry_display(entry))
        item.setData(entry.path, ROLE_PATH)
        item.setData(entry.is_bundle, ROLE_IS_BUNDLE)
        item.setData(False, ROLE_LOADED)
        item.setData(entry.is_archive, ROLE_IS_ARCHIVE)
        if not entry.is_bundle:
            # Add a placeholder child so the expand arrow shows
            placeholder = QStandardItem()
            item.appendRow(placeholder)
        parent_item.appendRow(item)

    @staticmethod
    def _entry_display(entry: BrowseEntry) -> str:
        icon = "\U0001f4e6" if entry.is_bundle else "\U0001f4c1"  # 📦 or 📁
        return f"{icon} {entry.name}"

    # ── Event Handlers ───────────────────────────────────────────

    def _source_item(self, proxy_index: QModelIndex):
        """Map a proxy model index to the source model item."""
        source_index = self._proxy.mapToSource(proxy_index)
        return self._model.itemFromIndex(source_index)

    def _on_expanded(self, proxy_index: QModelIndex):
        item = self._source_item(proxy_index)
        if not item or item.data(ROLE_IS_BUNDLE) or item.data(ROLE_LOADED):
            return
        # Mark as loaded and replace placeholder with real children
        item.setData(True, ROLE_LOADED)
        item.removeRows(0, item.rowCount())
        path = item.data(ROLE_PATH)
        try:
            entries = self._current_repo.list_entries(path)
        except Exception as e:
            logger.warning("Failed to list bundles in %s: %s", path, e, exc_info=True)
            error_item = QStandardItem(f"\u26a0 Error: {e}")
            error_item.setEnabled(False)
            item.appendRow(error_item)
            return
        for entry in self._filter_entries(entries):
            self._add_entry_item(item, entry)

    def _on_clicked(self, proxy_index: QModelIndex):
        self._update_selection(proxy_index)

    def _on_double_clicked(self, proxy_index: QModelIndex):
        item = self._source_item(proxy_index)
        if item and item.data(ROLE_IS_BUNDLE):
            self._update_selection(proxy_index)
            self.accept()

    def _on_selection_changed(self, current: QModelIndex, previous: QModelIndex):
        self._update_selection(current)

    def _on_filter_changed(self, text: str):
        self._proxy.setFilterFixedString(text)
        if text:
            self._tree.expandAll()

    def _update_selection(self, proxy_index: QModelIndex):
        item = self._source_item(proxy_index)
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
            self._selected_is_s3 = self._radio_s3.isChecked()
            self._selected_is_archive = bool(item.data(ROLE_IS_ARCHIVE))
            self._select_button.setEnabled(True)
            self._load_preview(path, item)
        else:
            self._selected_path = None
            self._select_button.setEnabled(False)
            self._selected_is_archive = False
            self._clear_preview()
            # Auto-expand folders when clicked — clear filter first so children are visible
            if self._filter_edit.text():
                folder_path = item.data(ROLE_PATH)
                self._filter_edit.clear()
                # Defer select+expand to after Qt processes the filter change
                QTimer.singleShot(0, lambda p=folder_path: self._select_and_expand_path(p))
            else:
                if not self._tree.isExpanded(proxy_index):
                    self._tree.expand(proxy_index)

    def _select_and_expand_path(self, path: str):
        """Find an item by path in the proxy model, select it, and expand it."""
        proxy_index = self._find_proxy_index_by_path(path)
        if proxy_index and proxy_index.isValid():
            self._tree.setCurrentIndex(proxy_index)
            self._tree.expand(proxy_index)
            self._tree.scrollTo(proxy_index, QTreeView.PositionAtTop)

    def _find_proxy_index_by_path(self, path: str) -> Optional[QModelIndex]:
        """Walk the source model to find an item by ROLE_PATH, return its proxy index."""

        def _search(parent_item):
            for row in range(parent_item.rowCount()):
                child = parent_item.child(row)
                if child and child.data(ROLE_PATH) == path:
                    return self._proxy.mapFromSource(child.index())
                result = _search(child)
                if result:
                    return result
            return None

        return _search(self._model.invisibleRootItem())

    def _on_source_changed(self, checked: bool):
        if not self._ready:
            return
        if self._radio_local.isChecked():
            self._current_repo = self._local_repo
        elif self._radio_s3.isChecked() and self._s3_repo:
            self._current_repo = self._s3_repo
        elif self._radio_history.isChecked() and self._history_repo:
            self._current_repo = self._history_repo
        self._selected_path = None
        self._select_button.setEnabled(False)
        self._clear_preview()
        self._populate_root()

    def _on_hidden_toggled(self, checked: bool):
        if not self._ready:
            return
        self._populate_root()

    # ── Preview ──────────────────────────────────────────────────

    def _load_preview(self, path: str, item: Optional[QStandardItem] = None):
        try:
            info = self._current_repo.get_bundle_info(path)
        except Exception as e:
            logger.warning("Failed to load bundle info for %s: %s", path, e, exc_info=True)
            self._show_error_preview(f"Failed to load bundle info:\n{e}")
            if item:
                self._mark_item_error(item)
            self._select_button.setEnabled(False)
            return
        if not info:
            self._show_error_preview(
                "Could not read bundle template.\nThe template may be missing or malformed."
            )
            if item:
                self._mark_item_error(item)
            self._select_button.setEnabled(False)
            return

        self._preview_name.setText(info.name)
        self._preview_name.setStyleSheet("font-weight: bold; font-size: 14px;")
        self._preview_name.setVisible(True)

        if info.description:
            self._preview_desc.setText(info.description)
            self._preview_desc.setVisible(True)
        else:
            self._preview_desc.setVisible(False)

        if info.step_names:
            self._preview_steps_label.setVisible(True)
            self._preview_steps.setText("\n".join(f"  \u2022 {name}" for name in info.step_names))
            self._preview_steps.setVisible(True)
        else:
            self._preview_steps_label.setVisible(False)
            self._preview_steps.setVisible(False)

        if info.parameters:
            self._preview_params_label.setVisible(True)
            self._preview_params.setRowCount(len(info.parameters))
            for row, p in enumerate(info.parameters):
                self._preview_params.setItem(row, 0, QTableWidgetItem(p.get("name", "?")))
                self._preview_params.setItem(row, 1, QTableWidgetItem(p.get("type", "?")))
                value = p.get("_display_value", "")
                self._preview_params.setItem(row, 2, QTableWidgetItem(str(value) if value else ""))
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
        self._preview_params.setRowCount(0)
        self._preview_params.setVisible(False)

    def _mark_item_error(self, item: QStandardItem) -> None:
        """Replace the bundle/folder icon with a warning icon."""
        text = item.text()
        # Remove existing icon prefix
        for prefix in ("\U0001f4e6 ", "\U0001f4c1 ", "\u26a0 "):
            if text.startswith(prefix):
                text = text[len(prefix) :]
                break
        item.setText(f"\u26a0 {text}")

    def _show_error_preview(self, message: str):
        """Show an error message in the preview panel."""
        self._preview_name.setText("\u26a0 Error")
        self._preview_name.setStyleSheet("font-weight: bold; font-size: 14px; color: red;")
        self._preview_name.setVisible(True)
        self._preview_desc.setText(message)
        self._preview_desc.setVisible(True)
        self._preview_steps_label.setVisible(False)
        self._preview_steps.setVisible(False)
        self._preview_params_label.setVisible(False)
        self._preview_params.setVisible(False)
