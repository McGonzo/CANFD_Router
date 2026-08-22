from __future__ import annotations

from dataclasses import asdict
from typing import List

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from can_gateway.core.models import AlgorithmKind, AlgorithmPreset
from can_gateway.ui.dialogs import AlgorithmPresetDialog


def _kind_of(value: object) -> AlgorithmKind:
    if isinstance(value, AlgorithmKind):
        return value
    return AlgorithmKind(str(value or "").strip() or AlgorithmKind.ROLLING_COUNTER.value)


class AlgorithmsTab(QWidget):
    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._algorithms: List[AlgorithmPreset] = []

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Type", "Counter", "CRC", "Zeroed Signals"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.add_btn = QPushButton("Add")
        self.edit_btn = QPushButton("Edit")
        self.copy_btn = QPushButton("Copy")
        self.remove_btn = QPushButton("Remove")

        controls = QHBoxLayout()
        controls.addWidget(self.add_btn)
        controls.addWidget(self.edit_btn)
        controls.addWidget(self.copy_btn)
        controls.addWidget(self.remove_btn)
        controls.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.table)

        self.add_btn.clicked.connect(self.add_algorithm)
        self.edit_btn.clicked.connect(self.edit_algorithm)
        self.copy_btn.clicked.connect(self.copy_algorithm)
        self.remove_btn.clicked.connect(self.remove_algorithm)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit_algorithm())

    def set_algorithms(self, items: List[AlgorithmPreset]) -> None:
        self._algorithms = [AlgorithmPreset.from_dict(asdict(item)) for item in items]
        self._refresh_table()

    def get_algorithms(self) -> List[AlgorithmPreset]:
        return [AlgorithmPreset.from_dict(asdict(item)) for item in self._algorithms]

    def add_algorithm(self) -> None:
        dialog = AlgorithmPresetDialog(self)
        if dialog.exec() != AlgorithmPresetDialog.DialogCode.Accepted:
            return
        cfg = dialog.to_config()
        if self._name_taken(cfg.name):
            QMessageBox.warning(self, "Validation", "Preset name must be unique.")
            return
        self._algorithms.append(cfg)
        self._refresh_table()
        self.changed.emit()

    def edit_algorithm(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        dialog = AlgorithmPresetDialog(self, initial=self._algorithms[row])
        if dialog.exec() != AlgorithmPresetDialog.DialogCode.Accepted:
            return
        cfg = dialog.to_config()
        if self._name_taken(cfg.name, skip=row):
            QMessageBox.warning(self, "Validation", "Preset name must be unique.")
            return
        self._algorithms[row] = cfg
        self._refresh_table()
        self.table.selectRow(row)
        self.changed.emit()

    def copy_algorithm(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        copied = AlgorithmPreset.from_dict(asdict(self._algorithms[row]))
        copied.name = self._unique_name(f"{copied.name}_copy")
        self._algorithms.append(copied)
        self._refresh_table()
        self.table.selectRow(len(self._algorithms) - 1)
        self.changed.emit()

    def remove_algorithm(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        self._algorithms.pop(row)
        self._refresh_table()
        if self._algorithms:
            self.table.selectRow(min(row, len(self._algorithms) - 1))
        self.changed.emit()

    def _selected_row(self) -> int:
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return -1
        return selected[0].row()

    def _name_taken(self, name: str, skip: int = -1) -> bool:
        for idx, item in enumerate(self._algorithms):
            if idx == skip:
                continue
            if item.name == name:
                return True
        return False

    def _unique_name(self, base: str) -> str:
        candidate = base
        index = 1
        while self._name_taken(candidate):
            index += 1
            candidate = f"{base}_{index}"
        return candidate

    def _refresh_table(self) -> None:
        self.table.setRowCount(len(self._algorithms))
        for row, item in enumerate(self._algorithms):
            kind = _kind_of(item.kind)
            counter_text = ""
            crc_text = ""
            if kind == AlgorithmKind.ROLLING_COUNTER:
                counter_text = (
                    f"start={item.start_value} step={item.step} "
                    f"range=[{item.minimum},{item.maximum}]"
                )
            if kind == AlgorithmKind.CRC:
                crc_text = (
                    f"width={item.width} poly=0x{int(item.polynomial):X} "
                    f"init=0x{int(item.initial_value):X} xor=0x{int(item.xor_out):X}"
                )
            values = [
                item.name,
                kind.value,
                counter_text,
                crc_text,
                ", ".join(item.exclude_signals),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
