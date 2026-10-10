from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from can_gateway.services.dbc_service import DbcService


class SignalSelectionDialog(QDialog):
    """Check individual signals or whole messages; hidden checks are retained."""

    def __init__(self, dbc: DbcService, port: str, title: str, parent=None, *,
                 selected_signals: dict | None = None, raw_ids=(), selected_raw=()) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(780, 580)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search message / signal / CAN ID")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Message / Signal", "CAN ID", "Cycle"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        selected_signals = selected_signals or {}
        selected_raw = set(selected_raw)
        for message in dbc.list_messages(port):
            parent_item = QTreeWidgetItem(self.tree, [message.name, f"0x{message.frame_id:X}",
                f"{message.cycle_time_ms} ms" if message.cycle_time_ms and message.cycle_time_ms > 0 else "Every frame"])
            parent_item.setFlags(parent_item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            parent_item.setCheckState(0, Qt.CheckState.Unchecked)
            for signal in message.signals:
                item = QTreeWidgetItem(parent_item, [signal.name, "", ""])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setData(0, Qt.ItemDataRole.UserRole, {
                    "port": port, "message": message.name, "signal": signal.name, "frame_id": message.frame_id})
                item.setCheckState(0, Qt.CheckState.Checked if signal.name in selected_signals.get(message.name, ()) else Qt.CheckState.Unchecked)
        for can_id, extended in sorted(set(raw_ids)):
            item = QTreeWidgetItem(self.tree, [f"RAW {'EXT' if extended else 'STD'} 0x{can_id:X}", f"0x{can_id:X}", "Every frame"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(0, Qt.ItemDataRole.UserRole, {"raw_id": int(can_id), "extended": bool(extended)})
            item.setCheckState(0, Qt.CheckState.Checked if (can_id, extended) in selected_raw else Qt.CheckState.Unchecked)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)
        self.check_selected_btn = QPushButton("Check highlighted")
        self.uncheck_selected_btn = QPushButton("Uncheck highlighted")
        self.check_all_btn = QPushButton("Check all")
        self.clear_btn = QPushButton("Clear checks")
        self.count_label = QLabel()
        controls = QHBoxLayout()
        for button in (self.check_selected_btn, self.uncheck_selected_btn, self.check_all_btn, self.clear_btn):
            controls.addWidget(button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Port: {port} · Check a message to include all its signals. Ctrl / Shift allows multiple highlights."))
        layout.addWidget(self.search_input)
        layout.addLayout(controls)
        layout.addWidget(self.tree)
        layout.addWidget(self.count_label)
        layout.addWidget(buttons)
        self.search_input.textChanged.connect(self._filter)
        self.check_selected_btn.clicked.connect(lambda: self._check_highlighted(Qt.CheckState.Checked))
        self.uncheck_selected_btn.clicked.connect(lambda: self._check_highlighted(Qt.CheckState.Unchecked))
        self.check_all_btn.clicked.connect(lambda: self._check_all(Qt.CheckState.Checked))
        self.clear_btn.clicked.connect(lambda: self._check_all(Qt.CheckState.Unchecked))
        self.tree.itemChanged.connect(self._update_count)
        self._update_count()

    def _leaves(self):
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            if parent.childCount():
                for child in range(parent.childCount()):
                    yield parent.child(child)
            else:
                yield parent

    def selected_rows(self) -> list[dict]:
        return [item.data(0, Qt.ItemDataRole.UserRole) for item in self._leaves()
                if item.checkState(0) == Qt.CheckState.Checked and
                "signal" in (item.data(0, Qt.ItemDataRole.UserRole) or {})]

    def selected_raw_ids(self) -> list[tuple[int, bool]]:
        return [(payload["raw_id"], payload["extended"]) for item in self._leaves()
                if item.checkState(0) == Qt.CheckState.Checked and
                "raw_id" in (payload := item.data(0, Qt.ItemDataRole.UserRole) or {})]

    def _check_highlighted(self, state) -> None:
        previous = self.tree.blockSignals(True)
        try:
            for item in self.tree.selectedItems():
                item.setCheckState(0, state)
        finally:
            self.tree.blockSignals(previous)
        self._update_count()

    def _check_all(self, state) -> None:
        previous = self.tree.blockSignals(True)
        try:
            for index in range(self.tree.topLevelItemCount()):
                self.tree.topLevelItem(index).setCheckState(0, state)
        finally:
            self.tree.blockSignals(previous)
        self._update_count()

    def _update_count(self, *_args) -> None:
        self.count_label.setText(f"Selected: {len(self.selected_rows())} signal(s), {len(self.selected_raw_ids())} RAW ID(s)")

    def _filter(self, text: str) -> None:
        key = text.strip().lower()
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            match = key in (parent.text(0) + " " + parent.text(1)).lower()
            visible = match
            for child in range(parent.childCount()):
                item = parent.child(child)
                show = match or key in item.text(0).lower()
                item.setHidden(not show)
                visible = visible or show
            parent.setHidden(not visible)
