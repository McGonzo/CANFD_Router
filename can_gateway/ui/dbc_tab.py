from __future__ import annotations

from typing import List

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from can_gateway.services.dbc_service import DbcService


class DbcTab(QWidget):
    dbc_loaded = Signal(str, object)

    def __init__(self, dbc_service: DbcService, parent=None) -> None:
        super().__init__(parent)
        self._dbc = dbc_service

        self.path_label = QLabel("No DBC loaded")
        self.load_btn = QPushButton("Load DBC")
        self.clear_btn = QPushButton("Clear")

        top = QHBoxLayout()
        top.addWidget(self.path_label)
        top.addStretch(1)
        top.addWidget(self.load_btn)
        top.addWidget(self.clear_btn)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Message/Signal", "FrameID", "StartBit", "Length", "Unit"])

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.tree)

        self.load_btn.clicked.connect(self.load_dbc)
        self.clear_btn.clicked.connect(self.clear_dbc)

    def load_dbc(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select DBC file",
            "",
            "DBC Files (*.dbc);;All Files (*.*)",
        )
        if not path:
            return
        self.load_dbc_from_path(path)

    def load_dbc_from_path(self, path: str) -> None:
        try:
            self._dbc.load_file(path)
        except Exception as exc:
            QMessageBox.critical(self, "DBC", f"Failed to load DBC:\n{exc}")
            return
        self._refresh_tree()
        messages = [msg.name for msg in self._dbc.list_messages()]
        self.dbc_loaded.emit(self._dbc.path, messages)

    def clear_dbc(self) -> None:
        self._dbc.clear()
        self.path_label.setText("No DBC loaded")
        self.tree.clear()
        self.dbc_loaded.emit("", [])

    def _refresh_tree(self) -> None:
        self.path_label.setText(self._dbc.path or "No DBC loaded")
        self.tree.clear()
        for message in self._dbc.list_messages():
            root = QTreeWidgetItem(
                [
                    message.name,
                    f"0x{message.frame_id:X}",
                    "",
                    str(message.length),
                    "",
                ]
            )
            self.tree.addTopLevelItem(root)
            for signal in message.signals:
                child = QTreeWidgetItem(
                    [
                        signal.name,
                        "",
                        str(signal.start),
                        str(signal.length),
                        signal.unit,
                    ]
                )
                root.addChild(child)
            root.setExpanded(True)
