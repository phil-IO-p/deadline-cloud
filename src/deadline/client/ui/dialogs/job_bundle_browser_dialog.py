# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Custom job bundle browser dialog that replaces the native folder picker.
Shows a navigable tree of directories/bundles with a preview panel.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import tempfile
from logging import getLogger
from typing import Optional

from qtpy.QtCore import Qt, QModelIndex, QSortFilterProxyModel, QTimer, Signal  # type: ignore
from qtpy.QtGui import QColor, QPalette, QStandardItemModel, QStandardItem  # type: ignore
from qtpy.QtWidgets import (  # type: ignore
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QRadioButton,
    QGraphicsOpacityEffect,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .._utils import tr, warning_banner_qss
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
ROLE_IS_HIDDEN = Qt.UserRole + 5

# Semantic warning/accent color, matched to the queue-unavailable banner used
# elsewhere in this dialog so the panel reads as part of the same UI. Legible on
# both light and dark themes.
REQUIRED_COLOR = QColor("#b35900")

# Shared "quiet section label" style — small, bold, sentence-case, muted.
# Color is set per-widget from the palette so it adapts to the theme.
_SECTION_LABEL_QSS = "font-size: 13px; font-weight: bold;"

# Friendly, artist-facing labels for the OpenJD parameter type enums.
_FRIENDLY_PARAM_TYPES = {
    "STRING": "Text",
    "PATH": "Path",
    "INT": "Number",
    "FLOAT": "Number",
}


def _friendly_param_type(raw_type: str) -> str:
    """Map an OpenJD parameter type enum to an artist-facing label."""
    return _FRIENDLY_PARAM_TYPES.get(raw_type.upper(), raw_type.title() if raw_type else "?")


def _folders_first(entries: list) -> list:
    """Order entries folders-first, preserving the repo's name-sort within each group."""
    folders = [e for e in entries if not e.is_bundle]
    bundles = [e for e in entries if e.is_bundle]
    return folders + bundles


def _steps_list_text(step_names: list[str]) -> str:
    """Render step names as a plain bulleted list."""
    return "\n".join(f"  •  {name}" for name in step_names if name)


def _normalize_description(text: str) -> str:
    """Collapse hard line breaks within a paragraph so the label can word-wrap to
    the panel width, while preserving intentional blank-line paragraph breaks.

    Template descriptions are often authored as multi-line YAML blocks, which would
    otherwise display with awkward breaks mid-sentence.
    """
    paragraphs = re.split(r"\n\s*\n", text.strip())
    return "\n\n".join(" ".join(p.split()) for p in paragraphs if p.strip())


class _BundleFilterProxy(QSortFilterProxyModel):
    """Proxy that filters by text and optionally hides items marked as hidden."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._show_hidden = False

    def set_show_hidden(self, show: bool):
        self._show_hidden = show
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent) -> bool:  # type: ignore[override]
        index = self.sourceModel().index(source_row, 0, source_parent)
        if not self._show_hidden and index.data(ROLE_IS_HIDDEN):
            return False
        return super().filterAcceptsRow(source_row, source_parent)


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
        *,
        queue_source: Optional[S3BundleRepository] = None,
        queue_error: str = "",
        local_source: str = "",
        history_source: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent=parent)
        self.setWindowTitle(tr("Browse Job Bundles"))
        self.setMinimumSize(750, 550)
        self.resize(850, 620)

        self._s3_repo: Optional[S3BundleRepository] = queue_source
        self._s3_error = queue_error
        self._s3_available = self._s3_repo is not None

        self._local_repo = LocalBundleRepository(root=local_source, include_archives=False)

        self._history_repo: Optional[LocalBundleRepository] = None
        if history_source and os.path.isdir(history_source):
            self._history_repo = LocalBundleRepository(root=history_source, include_archives=False)

        self._current_repo: BundleRepository = self._local_repo
        self._selected_path: Optional[str] = None
        self._selected_is_s3 = False
        self._selected_is_archive = False
        self._cached_root_entries: list[BrowseEntry] = []
        self._hidden_set: set[str] = set()
        self._last_preview_path: Optional[str] = None
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

    def resolve_selection(self) -> Optional[str]:
        """Resolve the selected bundle to a local directory path.

        Handles S3 download/cache, archive extraction, and direct directory paths.
        Returns None if no selection.
        """
        if not self._selected_path:
            return None

        if self._selected_is_s3 and self._s3_repo:
            if self._selected_is_archive:
                return self._s3_repo.resolve_bundle(self._selected_path, "")
            else:
                temp_dir = tempfile.mkdtemp(prefix="deadline-bundle-")
                atexit.register(shutil.rmtree, temp_dir, True)
                return self._s3_repo.resolve_bundle(self._selected_path, temp_dir)
        elif self._selected_is_archive:
            temp_dir = tempfile.mkdtemp(prefix="deadline-bundle-")
            atexit.register(shutil.rmtree, temp_dir, True)
            return self._local_repo.extract_bundle(self._selected_path, temp_dir)
        else:
            return self._selected_path

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
        self._queue_warning.setStyleSheet(warning_banner_qss(self))
        if not self._s3_available and self._s3_error:
            self._queue_warning.setText(
                f"\u26a0 <b>Queue browsing unavailable:</b> {self._s3_error}"
            )
            self._queue_warning.setTextFormat(Qt.RichText)
            self._queue_warning.setVisible(True)
        else:
            self._queue_warning.setVisible(False)
        layout.addWidget(self._queue_warning)

        # #7 — Path display sits just under the source selection (both describe
        # "where am I browsing"), rather than at the bottom by the buttons.
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel(tr("Path:")))
        self._path_display = QLineEdit()
        self._path_display.setReadOnly(True)
        path_row.addWidget(self._path_display)
        layout.addLayout(path_row)

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

        # Row 1: filter + "Show hidden" (both are list-view controls, grouped above
        # the tree). Show hidden is a filter toggle, so it lives with the filter.
        filter_row = QHBoxLayout()
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(tr("Filter bundles..."))
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._filter_edit, stretch=1)

        self._show_hidden_cb = QCheckBox(tr("Show hidden"), parent=self)
        self._show_hidden_cb.setChecked(False)
        self._show_hidden_cb.toggled.connect(self._on_hidden_toggled)
        filter_row.addSpacing(8)
        filter_row.addWidget(self._show_hidden_cb)
        left_layout.addLayout(filter_row)

        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels([tr("Name")])

        self._proxy = _BundleFilterProxy()
        self._proxy.setSourceModel(self._model)
        self._proxy.setRecursiveFilteringEnabled(True)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)

        self._tree = QTreeView()
        self._tree.setModel(self._proxy)
        self._tree.setHeaderHidden(True)
        self._tree.setEditTriggers(QTreeView.NoEditTriggers)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.expanded.connect(self._on_expanded)
        self._tree.clicked.connect(self._on_clicked)
        self._tree.doubleClicked.connect(self._on_double_clicked)
        self._tree.selectionModel().currentChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self._tree)

        splitter.addWidget(left_widget)

        # Right: preview panel. A QStackedWidget switches between an empty-state
        # page (centered prompt) and the detail page (scrollable bundle info).
        self._muted_hex = self.palette().color(QPalette.PlaceholderText).name()
        muted_qss = f"color: {self._muted_hex};"

        self._preview_stack = QStackedWidget()

        # Empty-state page — icon + prompt + hint, centered both ways.
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setSpacing(4)

        # Large, low-opacity bundle glyph as a backdrop (matches the tree's 📦).
        empty_icon = QLabel("\U0001f4e6")
        empty_icon.setAlignment(Qt.AlignCenter)
        empty_icon.setStyleSheet("font-size: 44px;")
        empty_icon.setGraphicsEffect(self._make_opacity(0.35))

        empty_prompt = QLabel(tr("Select a job bundle"))
        empty_prompt.setAlignment(Qt.AlignCenter)
        empty_prompt.setStyleSheet("font-size: 15px; font-weight: bold;")

        self._empty_label = QLabel(tr("Choose one from the list to preview its details"))
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.setStyleSheet(f"font-size: 12px; {muted_qss}")

        empty_layout.addStretch(1)
        empty_layout.addWidget(empty_icon)
        empty_layout.addWidget(empty_prompt)
        empty_layout.addWidget(self._empty_label)
        empty_layout.addStretch(1)
        self._preview_stack.addWidget(empty_page)  # index 0

        # Detail page — scrollable bundle info.
        preview_widget = QWidget()
        preview_layout = QVBoxLayout(preview_widget)
        # One consistent vertical rhythm instead of scattered per-widget margins.
        preview_layout.setSpacing(6)

        # Title — top of a 3-step scale (title / body / section-label).
        self._preview_name = QLabel()
        self._preview_name.setWordWrap(True)
        self._preview_name.setStyleSheet("font-weight: bold; font-size: 15px;")
        preview_layout.addWidget(self._preview_name)

        # Muted subline: bundle type + source (e.g. "📦 Folder · Local")
        self._preview_subline = QLabel()
        self._preview_subline.setStyleSheet(f"font-size: 11px; {muted_qss}")
        preview_layout.addWidget(self._preview_subline)

        # Extra breathing room between the title/subline block and the description.
        preview_layout.addSpacing(12)

        # Description — with a section label to match Steps/Parameters.
        self._preview_desc_label = QLabel(tr("Description"))
        self._preview_desc_label.setStyleSheet(_SECTION_LABEL_QSS + muted_qss)
        preview_layout.addWidget(self._preview_desc_label)
        self._preview_desc = QLabel()
        self._preview_desc.setWordWrap(True)
        preview_layout.addWidget(self._preview_desc)

        preview_layout.addSpacing(8)
        self._preview_steps_label = QLabel(tr("Steps"))
        self._preview_steps_label.setStyleSheet(_SECTION_LABEL_QSS + muted_qss)
        preview_layout.addWidget(self._preview_steps_label)
        # Steps as a plain bulleted list — no background fill (it drew the eye
        # more than it should).
        self._preview_steps = QLabel()
        self._preview_steps.setWordWrap(True)
        preview_layout.addWidget(self._preview_steps)

        preview_layout.addSpacing(8)
        self._preview_params_label = QLabel(tr("Parameters"))
        self._preview_params_label.setStyleSheet(_SECTION_LABEL_QSS + muted_qss)
        preview_layout.addWidget(self._preview_params_label)
        self._preview_params = QTableWidget()
        self._preview_params.setColumnCount(3)
        self._preview_params.setHorizontalHeaderLabels([tr("Name"), tr("Type"), tr("Value")])
        header = self._preview_params.horizontalHeader()
        # Name/Type size to content but are capped (see _size_params_table below);
        # Value takes the rest and wraps long content rather than eliding/scrolling.
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setHighlightSections(False)
        # Left-align the column headers to match the cell text below them.
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._preview_params.verticalHeader().setVisible(False)
        self._preview_params.setEditTriggers(QTableWidget.NoEditTriggers)
        self._preview_params.setSelectionMode(QTableWidget.NoSelection)
        # #2 — subtle alternating row tint so rows are easier to scan.
        self._preview_params.setAlternatingRowColors(True)
        # Wrap long Name/Value text onto multiple lines instead of eliding it.
        self._preview_params.setWordWrap(True)
        self._preview_params.setTextElideMode(Qt.ElideNone)
        self._preview_params.setShowGrid(False)
        self._preview_params.setFocusPolicy(Qt.NoFocus)
        # No frame on the table itself — the panel outline already contains it.
        self._preview_params.setFrameShape(QFrame.NoFrame)
        # #5 — header divider; header is transparent so it sits on the panel surface.
        hdr_line = self._muted_hex
        header.setStyleSheet(
            "QHeaderView::section {"
            " background: transparent;"
            f" border: none; border-bottom: 1px solid {hdr_line};"
            " padding: 4px 6px; font-weight: bold; }"
        )
        # Table is transparent (inherits the panel surface); alternating rows provide
        # the only fill, so they read as subtle stripes on the panel.
        self._preview_params.setStyleSheet(
            "QTableWidget { background: transparent; }"
        )
        # Let the panel's own scroll area handle overflow; the table sizes to its rows.
        self._preview_params.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._preview_params.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._preview_params.setSizeAdjustPolicy(QTableWidget.AdjustToContents)
        preview_layout.addWidget(self._preview_params)
        preview_layout.addStretch(1)

        self._clear_preview()

        # Inner padding so content doesn't crowd the panel edges.
        preview_layout.setContentsMargins(14, 14, 14, 14)

        preview_scroll = QScrollArea()
        preview_scroll.setWidget(preview_widget)
        preview_scroll.setWidgetResizable(True)
        preview_scroll.setFrameShape(QFrame.NoFrame)
        # Make the detail page transparent so the panel's gradient shows through here
        # too (a QScrollArea + its content otherwise paint an opaque Base background).
        preview_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        preview_widget.setAttribute(Qt.WA_TranslucentBackground, False)
        preview_widget.setStyleSheet("background: transparent;")
        self._preview_stack.addWidget(preview_scroll)  # index 1

        # #4 + #5 — the whole panel is a raised, rounded surface distinct from the
        # tree, with a subtle top-to-bottom gradient for a bit of depth. Both stops
        # are derived from QPalette.Base so it adapts to the theme. objectName
        # scoping keeps the styling off child widgets.
        base = self.palette().color(QPalette.Base)
        is_dark = base.lightness() < 128
        # Lighten the top / darken the bottom. Kept subtle: enough to add depth but
        # gentle enough not to compete with the dense detail content for readability.
        # (Near-black Base needs a larger % shift than a light Base to be visible.)
        top = base.lighter(128) if is_dark else base.lighter(104)
        bottom = base.darker(110) if is_dark else base.darker(106)
        tc = self.palette().color(QPalette.WindowText)
        border = f"rgba({tc.red()}, {tc.green()}, {tc.blue()}, 70)"
        self._preview_stack.setObjectName("previewPanel")
        self._preview_stack.setStyleSheet(
            "#previewPanel {"
            " background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f" stop:0 {top.name()}, stop:1 {bottom.name()});"
            f" border: 1px solid {border}; border-radius: 8px; }}"
        )
        splitter.addWidget(self._preview_stack)
        splitter.setSizes([350, 350])

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
        try:
            self._cached_root_entries = self._current_repo.list_entries(root_path)
        except Exception as e:
            logger.warning("Failed to list bundles: %s", e, exc_info=True)
            self._show_error_preview(f"Failed to list bundles:\n{e}")
            self._cached_root_entries = []

        # Fetch S3 hidden set for Queue source
        self._hidden_set = set()
        if isinstance(self._current_repo, S3BundleRepository):
            try:
                self._hidden_set = self._current_repo.get_hidden_set()
            except Exception:
                logger.debug("Failed to fetch visibility manifest", exc_info=True)

        # Folders first, then bundles — each group already name-sorted by the repo.
        self._cached_root_entries = _folders_first(self._cached_root_entries)

        root = self._model.invisibleRootItem()
        for entry in self._cached_root_entries:
            is_hidden = entry.name.startswith(".") or entry.name in self._hidden_set
            self._add_entry_item(root, entry, is_hidden=is_hidden)

    def _add_entry_item(
        self, parent_item: QStandardItem, entry: BrowseEntry, *, is_hidden: bool = False
    ):
        item = QStandardItem(self._entry_display(entry))
        item.setData(entry.path, ROLE_PATH)
        item.setData(entry.is_bundle, ROLE_IS_BUNDLE)
        item.setData(False, ROLE_LOADED)
        item.setData(entry.is_archive, ROLE_IS_ARCHIVE)
        item.setData(is_hidden, ROLE_IS_HIDDEN)
        if is_hidden:
            item.setForeground(QColor(150, 150, 150))
        if not entry.is_bundle:
            # Add a placeholder child so the expand arrow shows
            placeholder = QStandardItem()
            placeholder.setData(is_hidden, ROLE_IS_HIDDEN)
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
        for entry in _folders_first(entries):
            is_hidden = entry.name.startswith(".") or entry.name in self._hidden_set
            # If parent is hidden, children inherit hidden state
            if item.data(ROLE_IS_HIDDEN):
                is_hidden = True
            self._add_entry_item(item, entry, is_hidden=is_hidden)

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
            if path != self._last_preview_path:
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
        self._proxy.set_show_hidden(checked)

    def _on_context_menu(self, position):
        """Show hide/unhide context menu for Queue source bundles."""
        if not isinstance(self._current_repo, S3BundleRepository):
            return
        proxy_index = self._tree.indexAt(position)
        if not proxy_index.isValid():
            return
        source_index = self._proxy.mapToSource(proxy_index)
        item = self._model.itemFromIndex(source_index)
        if not item or not item.data(ROLE_IS_BUNDLE):
            return

        path = item.data(ROLE_PATH)
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".ojd"):
            name = name[:-4]
        is_hidden = bool(item.data(ROLE_IS_HIDDEN))

        menu = QMenu(self)
        if is_hidden:
            action = menu.addAction(tr("Unhide bundle"))
        else:
            action = menu.addAction(tr("Hide bundle"))

        chosen = menu.exec_(self._tree.viewport().mapToGlobal(position))
        if chosen != action:
            return

        try:
            self._current_repo.set_bundle_visibility(name, hidden=not is_hidden)
            item.setData(not is_hidden, ROLE_IS_HIDDEN)
            if not is_hidden:
                item.setForeground(QColor(150, 150, 150))
                self._hidden_set.add(name)
            else:
                item.setForeground(QColor(0, 0, 0))
                self._hidden_set.discard(name)
            self._proxy.invalidateFilter()
        except Exception as e:
            logger.warning("Failed to update visibility: %s", e, exc_info=True)
            self._show_error_preview(
                f"\u26a0 Could not {'hide' if not is_hidden else 'unhide'} bundle: {e}"
            )

    # ── Preview ──────────────────────────────────────────────────

    def _load_preview(self, path: str, item: Optional[QStandardItem] = None):
        self._last_preview_path = path
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

        self._preview_stack.setCurrentIndex(1)  # show detail page
        self._preview_name.setText(info.name)
        self._preview_name.setStyleSheet("font-weight: bold; font-size: 15px;")
        self._preview_name.setVisible(True)

        # Subline: bundle type + source
        type_label = tr("Archive") if self._selected_is_archive else tr("Folder")
        type_icon = "\U0001f4e6"  # \ud83d\udce6
        if self._radio_s3.isChecked():
            source_label = tr("Queue")
        elif self._radio_history.isChecked():
            source_label = tr("History")
        else:
            source_label = tr("Local")
        self._preview_subline.setText(f"{type_icon} {type_label} \u00b7 {source_label}")
        self._preview_subline.setVisible(True)

        if info.description:
            self._preview_desc_label.setVisible(True)
            self._preview_desc.setText(_normalize_description(info.description))
            self._preview_desc.setVisible(True)
        else:
            self._preview_desc_label.setVisible(False)
            self._preview_desc.setVisible(False)

        if info.step_names:
            self._preview_steps_label.setText(tr("Steps") + f" ({len(info.step_names)})")
            self._preview_steps_label.setVisible(True)
            self._preview_steps.setText(_steps_list_text(info.step_names))
            self._preview_steps.setVisible(True)
        else:
            self._preview_steps_label.setVisible(False)
            self._preview_steps.setVisible(False)

        if info.parameters:
            muted_color = self._preview_params.palette().color(QPalette.PlaceholderText)
            # Detect if parameters were truncated in metadata
            truncated = any(
                p.get("name", "").endswith("...") or p.get("type", "").endswith("...")
                for p in info.parameters
            )
            # Drop the last entry if it's garbled from truncation
            params = info.parameters[:-1] if truncated else info.parameters
            # Count reflects shown params; "+" hints there may be more when truncated.
            count_str = f"{len(params)}+" if truncated else str(len(params))
            self._preview_params_label.setText(tr("Parameters") + f" ({count_str})")
            self._preview_params_label.setVisible(True)
            row_count = len(params) + (1 if truncated else 0)
            self._preview_params.setRowCount(row_count)
            for row, p in enumerate(params):
                value = p.get("_display_value", "")
                # "Required" = the artist must supply it because the bundle gives no
                # default/value. (Metadata-only previews have no value info, so we can
                # only flag this reliably when default/value keys are present.)
                is_required = not value and ("default" not in p and "value" not in p)

                # Name is plain; the "(required)" value cell carries the signal.
                self._preview_params.setItem(row, 0, QTableWidgetItem(p.get("name", "?")))

                self._preview_params.setItem(
                    row, 1, QTableWidgetItem(_friendly_param_type(p.get("type", "")))
                )

                if value:
                    value_item = QTableWidgetItem(str(value))
                elif is_required:
                    value_item = QTableWidgetItem(tr("(required)"))
                    value_item.setForeground(REQUIRED_COLOR)  # warm = needs input
                else:
                    value_item = QTableWidgetItem(tr("(no default)"))
                    value_item.setForeground(muted_color)
                self._preview_params.setItem(row, 2, value_item)
            if truncated:
                truncation_item = QTableWidgetItem("\u2026 additional parameters not shown")
                truncation_item.setForeground(muted_color)
                self._preview_params.setItem(len(params), 0, truncation_item)
            self._preview_params.setVisible(True)
            self._size_params_table_to_contents()
        else:
            self._preview_params_label.setVisible(False)
            self._preview_params.setVisible(False)

    def _size_params_table_to_contents(self) -> None:
        """Fix the table's height to exactly its rows + header so it doesn't leave
        a large empty body, and cap the Name column so a long name can't squeeze
        the Value column. Value wraps within its remaining width."""
        # Cap the Name column at ~40% of the table width so it can't dominate.
        table_width = self._preview_params.viewport().width()
        if table_width > 0:
            name_w = self._preview_params.columnWidth(0)
            self._preview_params.setColumnWidth(0, min(name_w, int(table_width * 0.4)))
        # Recompute row heights now that wrapping/column widths are settled.
        self._preview_params.resizeRowsToContents()
        total = self._preview_params.horizontalHeader().height()
        for row in range(self._preview_params.rowCount()):
            total += self._preview_params.rowHeight(row)
        # +2 for the frame border
        self._preview_params.setFixedHeight(total + 2)

    @staticmethod
    def _make_opacity(value: float) -> QGraphicsOpacityEffect:
        """Opacity effect for a widget (QLabel CSS 'opacity' has no effect)."""
        eff = QGraphicsOpacityEffect()
        eff.setOpacity(value)
        return eff

    def _clear_preview(self):
        # Switch to the centered empty-state page. (The empty prompt is its own
        # widget, so it can't be polluted by the error state's red styling.)
        self._last_preview_path = None
        self._preview_stack.setCurrentIndex(0)

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
        self._preview_stack.setCurrentIndex(1)  # show detail page
        self._preview_name.setText("\u26a0 Error")
        self._preview_name.setStyleSheet("font-weight: bold; font-size: 14px; color: red;")
        self._preview_name.setVisible(True)
        self._preview_subline.setVisible(False)
        self._preview_desc_label.setVisible(False)
        self._preview_desc.setText(message)
        self._preview_desc.setVisible(True)
        self._preview_steps_label.setVisible(False)
        self._preview_steps.setVisible(False)
        self._preview_params_label.setVisible(False)
        self._preview_params.setVisible(False)
