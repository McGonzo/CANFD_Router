from __future__ import annotations

import time
import shutil
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from can_gateway.core.frame import Frame
from can_gateway.core.models import AlgorithmKind, AlgorithmPreset, HardwareConfig
from can_gateway.drivers.base import BusAdapter
from can_gateway.services import AlgorithmService, DbcService, DeviceRegistry
from can_gateway.ui.dialogs import SignalSearchDialog
from can_gateway.ui.signal_selection_dialog import SignalSelectionDialog
from can_gateway.services.receive_recorder import ReceiveRecorder
from can_gateway.utils.app_paths import app_path


_RAW_MESSAGE_TYPES = (
    "标准数据帧",
    "扩展数据帧",
    "FD标准帧",
    "FD扩展帧",
)
_RAW_TYPE_FLAGS = {
    "标准数据帧": (False, False),
    "扩展数据帧": (False, True),
    "FD标准帧": (True, False),
    "FD扩展帧": (True, True),
}
_RAW_DLC_LENGTHS = {
    **{index: index for index in range(9)},
    9: 12,
    10: 16,
    11: 20,
    12: 24,
    13: 32,
    14: 48,
    15: 64,
}


def _raw_payload_length(dlc: int, fd: bool) -> int:
    value = int(dlc)
    if value < 0 or value > (15 if fd else 8):
        raise ValueError("DLC is outside the selected frame type range.")
    return _RAW_DLC_LENGTHS[value]


def _normalize_raw_byte(value: object) -> str:
    raw = str(value or "").strip()
    if raw.lower().startswith("0x"):
        raw = raw[2:]
    if not raw and not str(value or "").strip():
        return "00"
    if not raw or len(raw) > 2 or any(char not in "0123456789abcdefABCDEF" for char in raw):
        raise ValueError(f"Invalid CAN data byte '{value}'.")
    try:
        parsed = int(raw, 16)
    except ValueError as exc:
        raise ValueError(f"Invalid CAN data byte '{value}'.") from exc
    return f"{parsed:02X}"


def _raw_frame_from_row_values(
    *,
    can_id: str,
    message_type: str,
    dlc: str | int,
    brs: bool,
    bytes_text: list[str] | tuple[str, ...] | str,
    extended_data: str = "",
) -> Frame:
    if message_type not in _RAW_TYPE_FLAGS:
        raise ValueError(f"Unknown RAW CAN message type '{message_type}'.")
    fd, extended = _RAW_TYPE_FLAGS[message_type]
    payload_length = _raw_payload_length(int(dlc), fd)
    if isinstance(bytes_text, str):
        main_bytes = bytes_text.replace(",", " ").split()
    else:
        main_bytes = list(bytes_text)
    extra_bytes = str(extended_data or "").replace(",", " ").split()
    values = [_normalize_raw_byte(value) for value in main_bytes + extra_bytes]
    if len(values) > payload_length:
        raise ValueError(f"Payload exceeds the selected DLC length of {payload_length} bytes.")
    values.extend(["00"] * (payload_length - len(values)))
    return _raw_frame_from_values(
        can_id=can_id,
        extended=extended,
        fd=fd,
        brs=brs,
        data=" ".join(values),
    )


def _raw_frame_from_values(
    *,
    can_id: str,
    extended: bool,
    fd: bool,
    brs: bool,
    data: str,
) -> Frame:
    raw_id = str(can_id or "").strip()
    try:
        arbitration_id = int(raw_id, 0)
    except ValueError as exc:
        raise ValueError("CAN ID must be a decimal or 0x-prefixed integer.") from exc
    maximum_id = 0x1FFFFFFF if extended else 0x7FF
    if not 0 <= arbitration_id <= maximum_id:
        frame_type = "extended" if extended else "standard"
        raise ValueError(f"{frame_type.capitalize()} CAN ID must be between 0 and 0x{maximum_id:X}.")

    raw_data = str(data or "").strip()
    tokens = raw_data.replace(",", " ").split()
    payload = bytearray()
    for token in tokens:
        normalized = token[2:] if token.lower().startswith("0x") else token
        if len(normalized) not in {1, 2}:
            raise ValueError(f"Invalid data byte '{token}'; use one or two hex digits per byte.")
        try:
            payload.append(int(normalized, 16))
        except ValueError as exc:
            raise ValueError(f"Invalid data byte '{token}'; use hexadecimal bytes.") from exc
    maximum_length = 64 if fd else 8
    if len(payload) > maximum_length:
        frame_type = "CAN FD" if fd else "Classic CAN"
        raise ValueError(f"{frame_type} payload cannot exceed {maximum_length} bytes.")
    return Frame(
        arbitration_id=arbitration_id,
        data=bytes(payload),
        is_fd=bool(fd),
        is_extended_id=bool(extended),
        bitrate_switch=bool(fd and brs),
    )


def _parse_signal_value(text: str) -> float:
    raw = str(text or "").strip()
    if not raw:
        return 0.0
    lower = raw.lower()
    if any(token in lower for token in (".", "e")):
        return float(raw)
    return float(int(raw, 0))


def _make_readonly_item(value: str, payload: dict | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(value)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if payload is not None:
        item.setData(Qt.ItemDataRole.UserRole, payload)
    return item


class DirectCanToolWindow(QMainWindow):
    def __init__(
        self,
        registry: DeviceRegistry,
        dbc_service: DbcService,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Direct CAN Tool")
        self.resize(1680, 900)

        self._registry = registry
        self._dbc = dbc_service
        self._hardware: Dict[str, HardwareConfig] = {}
        self._algorithms: List[AlgorithmPreset] = []
        self._algorithm_service = AlgorithmService([])
        self._adapters: Dict[str, BusAdapter] = {}
        self._rx_running = False
        self._tx_running = False
        self._rx_signal_rows: Dict[tuple[str, str], int] = {}
        self._rx_signal_values: Dict[tuple[str, str], str] = {}
        self._rx_raw_rows: Dict[tuple[int, bool], int] = {}
        self._rx_raw_counts: Dict[tuple[int, bool], int] = {}
        self._rx_raw_frame_count = 0
        self._rx_decoded_frame_count = 0
        self._rx_decoded_signal_count = 0
        self._rx_last_table_refresh = 0.0
        self._rx_started_at = 0.0
        self._rx_last_frame_at = 0.0
        self._tx_last_sent: Dict[str, float] = {}
        self._raw_tx_last_sent: Dict[int, float] = {}
        self._raw_tx_running = False
        self._updating_raw_layout = False
        self._updating_raw_table = False
        self._rx_polling = False
        self._recorder: ReceiveRecorder | None = None
        self._recording_path: Path | None = None
        self._record_directory = app_path("logs", "recordings")
        self._record_selections: dict[str, dict] = {}
        self._rx_known_raw_ids: dict[str, set[tuple[int, bool]]] = {}

        central = QWidget(self)
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        self.rx_group = QGroupBox("Receive")
        self.tx_group = QGroupBox("Send")
        panels = QSplitter(Qt.Orientation.Horizontal)
        panels.addWidget(self.rx_group)
        panels.addWidget(self.tx_group)
        panels.setStretchFactor(0, 1)
        panels.setStretchFactor(1, 3)
        panels.setSizes([420, 1240])
        root.addWidget(panels)

        self._build_rx_ui()
        self._build_tx_ui()

        self.rx_timer = QTimer(self)
        self.rx_timer.setInterval(50)
        self.rx_timer.timeout.connect(self._poll_receive)

        self.tx_timer = QTimer(self)
        self.tx_timer.setInterval(10)
        self.tx_timer.timeout.connect(self._send_due_messages)
        self.raw_tx_timer = QTimer(self)
        self.raw_tx_timer.setInterval(10)
        self.raw_tx_timer.timeout.connect(self._send_due_raw_rows)

    def snapshot_state(self) -> Dict[str, Any]:
        tx_rows: List[Dict[str, str]] = []
        for row in range(self.tx_table.rowCount()):
            message_item = self.tx_table.item(row, 0)
            value_item = self.tx_table.item(row, 5)
            if message_item is None:
                continue
            payload = message_item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(payload, dict):
                continue
            tx_rows.append(
                {
                    "message": str(payload.get("message", "")),
                    "signal": str(payload.get("signal", "")),
                    "mode": str(payload.get("mode", "")),
                    "preset": str(payload.get("preset", "")),
                    "value": value_item.text().strip() if value_item is not None else "0",
                }
            )
        return {
            "rx_port": self.rx_port_input.currentText().strip(),
            "rx_changes_only": bool(self.rx_changes_only_input.isChecked()),
            "tx_port": self.tx_port_input.currentText().strip(),
            "tx_interval_ms": int(self.tx_interval_input.value()),
            "tx_rows": tx_rows,
            "raw_tx_rows": self._raw_snapshot_rows(),
            "send_tab": self.send_tabs.currentIndex(),
            "record_selections": deepcopy(self._record_selections),
        }

    def restore_state(self, state: Dict[str, Any]) -> None:
        if not isinstance(state, dict):
            return
        rx_port = str(state.get("rx_port", "")).strip()
        tx_port = str(state.get("tx_port", "")).strip()
        if rx_port:
            self.rx_port_input.setCurrentText(rx_port)
        self.rx_changes_only_input.setChecked(bool(state.get("rx_changes_only", True)))
        if tx_port:
            self.tx_port_input.setCurrentText(tx_port)
        self.tx_interval_input.setValue(int(state.get("tx_interval_ms", 100) or 100))
        self._clear_tx_rows()
        self._clear_raw_rows()
        port_name = self.tx_port_input.currentText().strip()
        rows = state.get("tx_rows", [])
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                message_name = str(row.get("message", "")).strip()
                signal_name = str(row.get("signal", "")).strip()
                mode = str(row.get("mode", "")).strip()
                preset_name = str(row.get("preset", "")).strip()
                value_text = str(row.get("value", "0"))
                if not message_name or not signal_name or not mode:
                    continue
                self._append_tx_row(
                    port_name=port_name,
                    message_name=message_name,
                    signal_name=signal_name,
                    mode=mode,
                    preset_name=preset_name,
                    value_text=value_text,
                )
        raw_rows = state.get("raw_tx_rows", [])
        if isinstance(raw_rows, list):
            for row in raw_rows:
                if isinstance(row, dict):
                    self._append_raw_row(row)
        if state.get("send_tab") in (0, 1):
            self.send_tabs.setCurrentIndex(int(state["send_tab"]))
        selections = state.get("record_selections", {})
        if isinstance(selections, dict):
            for port, selection in selections.items():
                if isinstance(selection, dict):
                    self._set_record_selection(port, selection.get("signals", {}), selection.get("raw", []))

    def _build_rx_ui(self) -> None:
        self.rx_port_input = QComboBox()
        self.rx_dbc_label = QLabel("DBC: -")
        self.rx_port_detail_label = QLabel("Port: -")
        self.rx_toggle_btn = QPushButton("Start Receive")
        self.rx_clear_btn = QPushButton("Clear")
        self.rx_changes_only_input = QCheckBox("Changes only")
        self.rx_changes_only_input.setChecked(True)
        self.rx_status = QLabel("Stopped")

        top = QHBoxLayout()
        top.addWidget(QLabel("Port"))
        top.addWidget(self.rx_port_input)
        top.addWidget(self.rx_toggle_btn)
        top.addWidget(self.rx_clear_btn)
        top.addWidget(self.rx_changes_only_input)
        top.addStretch(1)
        top.addWidget(self.rx_status)

        self.rx_table = QTableWidget(0, 5)
        self.rx_table.setHorizontalHeaderLabels(
            ["No.", "Message", "Signal", "Value", "Data"]
        )
        self.rx_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rx_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rx_table.verticalHeader().setVisible(False)
        self.rx_table.horizontalHeader().setStretchLastSection(True)

        layout = QVBoxLayout(self.rx_group)
        layout.addLayout(top)
        layout.addWidget(QLabel("DBC source: uses the DBC path configured in Hardware Config."))
        layout.addWidget(self.rx_dbc_label)
        layout.addWidget(self.rx_port_detail_label)
        layout.addWidget(self.rx_table)

        record_group = QGroupBox("Receive Recording")
        record_layout = QGridLayout(record_group)
        self.record_select_btn = QPushButton("Record Items…")
        self.record_start_btn = QPushButton("Start Record")
        self.record_stop_btn = QPushButton("Stop && Save")
        self.record_save_btn = QPushButton("Save CSV…")
        self.record_stop_btn.setEnabled(False)
        self.record_save_btn.setEnabled(False)
        self.record_selection_label = QLabel("Selected: 0 signals, 0 RAW IDs")
        self.record_status = QLabel("Stopped")
        self.record_status.setWordWrap(True)
        record_layout.addWidget(self.record_select_btn, 0, 0)
        record_layout.addWidget(self.record_start_btn, 0, 1)
        record_layout.addWidget(self.record_stop_btn, 1, 0)
        record_layout.addWidget(self.record_save_btn, 1, 1)
        record_layout.addWidget(self.record_selection_label, 2, 0, 1, 2)
        record_layout.addWidget(self.record_status, 3, 0, 1, 2)
        layout.addWidget(record_group)
        self.record_select_btn.clicked.connect(self._select_record_items)
        self.record_start_btn.clicked.connect(self._start_recording)
        self.record_stop_btn.clicked.connect(self._stop_recording)
        self.record_save_btn.clicked.connect(self._save_recording)

        self.rx_port_input.currentTextChanged.connect(self._refresh_rx_info)
        self.rx_toggle_btn.clicked.connect(self._toggle_receive)
        self.rx_clear_btn.clicked.connect(self._clear_rx_rows)

    def _is_recording(self) -> bool:
        return self._recorder is not None and self._recorder.active

    def _set_record_selection(self, port: str, signals: dict, raw_ids: list) -> bool:
        if self._is_recording():
            return False
        if not isinstance(signals, dict) or not isinstance(raw_ids, (list, tuple)):
            return False
        self._record_selections[port] = {
            "signals": {str(name): list(dict.fromkeys(str(value) for value in values))
                        for name, values in signals.items() if isinstance(values, (list, tuple))},
            "raw": [(int(value[0]), bool(value[1])) for value in raw_ids
                    if isinstance(value, (list, tuple)) and len(value) == 2],
        }
        self._refresh_record_selection_label()
        return True

    def _refresh_record_selection_label(self) -> None:
        selection = self._record_selections.get(self.rx_port_input.currentText().strip(), {})
        count = sum(len(values) for values in selection.get("signals", {}).values())
        raw_count = len(selection.get("raw", []))
        self.record_selection_label.setText(f"Selected: {count} signals, {raw_count} RAW IDs")

    def _select_record_items(self) -> None:
        if self._is_recording():
            return
        port = self.rx_port_input.currentText().strip()
        if not port:
            QMessageBox.warning(self, "Recording", "Please select a receive port first.")
            return
        selection = self._record_selections.get(port, {})
        raw = self._rx_known_raw_ids.get(port, set()) | set(tuple(value) for value in selection.get("raw", []))
        dialog = SignalSelectionDialog(self._dbc, port, "Select Recording Items", self,
            selected_signals=selection.get("signals", {}), raw_ids=raw,
            selected_raw=[tuple(value) for value in selection.get("raw", [])])
        if not self._dbc.has_port(port) and not raw:
            dialog.count_label.setText("No CAN IDs discovered. Start Receive first, then select recording items.")
        if dialog.exec() == dialog.DialogCode.Accepted:
            signals: dict[str, list[str]] = {}
            for row in dialog.selected_rows():
                signals.setdefault(row["message"], []).append(row["signal"])
            self._set_record_selection(port, signals, dialog.selected_raw_ids())

    def _start_recording(self) -> None:
        if self._is_recording():
            return
        port = self.rx_port_input.currentText().strip()
        selection = self._record_selections.get(port, {})
        if not port or not (any(selection.get("signals", {}).values()) or selection.get("raw")):
            QMessageBox.warning(self, "Recording", "Use Record Items to select messages, signals or discovered CAN IDs first.")
            return
        try:
            self._ensure_port_open(port)
            self._recorder = ReceiveRecorder(self._dbc, port, selection.get("signals", {}),
                selection.get("raw", []), self._record_directory)
        except Exception as exc:
            QMessageBox.critical(self, "Recording", f"Cannot start recording:\n{exc}")
            return
        self._recording_path = self._recorder.path
        if not self._rx_running:
            self._toggle_receive()
        if not self._rx_running:
            self._stop_recording(save=False)
            return
        self.record_select_btn.setEnabled(False)
        self.record_start_btn.setEnabled(False)
        self.record_stop_btn.setEnabled(True)
        self.record_save_btn.setEnabled(False)
        self.rx_port_input.setEnabled(False)
        self.record_status.setText("Recording · 0 frames / 0 CSV rows · selection locked")
        self.record_status.setToolTip(str(self._recording_path))

    def _record_received_frame(self, port: str, frame: Frame, decoded: dict | None) -> None:
        if not self._is_recording() or self._recorder.port != port:
            return
        try:
            self._recorder.record(frame, decoded)
            self.record_status.setText(f"Recording · {self._recorder.frame_count} frames / {self._recorder.row_count} CSV rows · selection locked")
        except Exception as exc:
            self._stop_recording(save=False)
            self.record_status.setText(f"Recording error: {exc}. Retained: {self._recording_path}")

    def _stop_recording(self, *_args, save: bool = True) -> None:
        if not self._is_recording():
            return
        try:
            self._recording_path = self._recorder.stop()
        except Exception as exc:
            self.record_status.setText(f"CSV flush error: {exc}. Retained: {self._recording_path}")
            save = False
        else:
            self.record_status.setText(f"Stopped · {self._recorder.row_count} CSV rows. Retained: {self._recording_path}")
        self.record_status.setToolTip(str(self._recording_path))
        self.record_select_btn.setEnabled(True)
        self.record_start_btn.setEnabled(True)
        self.record_stop_btn.setEnabled(False)
        self.record_save_btn.setEnabled(True)
        self.rx_port_input.setEnabled(True)
        if save:
            self._save_recording()

    def _save_recording(self) -> None:
        if self._is_recording() or self._recording_path is None:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Save Receive Recording",
            str(self._recording_path), "CSV files (*.csv)")
        if not filename:
            return
        target = Path(filename)
        if target.suffix.lower() != ".csv":
            target = target.with_suffix(".csv")
        try:
            if target.resolve() != self._recording_path.resolve():
                shutil.copy2(self._recording_path, target)
        except Exception as exc:
            QMessageBox.critical(self, "Recording", f"CSV save failed:\n{exc}\nOriginal retained: {self._recording_path}")
            return
        self.record_status.setText(f"CSV saved: {target}")
        self.record_status.setToolTip(str(target))

    def _build_tx_ui(self) -> None:
        self.tx_port_input = QComboBox()
        self.tx_dbc_label = QLabel("DBC: -")
        self.tx_add_signal_btn = QPushButton("Add Signal")
        self.tx_add_rc_btn = QPushButton("Add RC")
        self.tx_add_crc_btn = QPushButton("Add CRC")
        self.tx_remove_btn = QPushButton("Remove Row")
        self.tx_clear_btn = QPushButton("Clear")
        self.tx_interval_input = QSpinBox()
        self.tx_interval_input.setRange(10, 60_000)
        self.tx_interval_input.setValue(100)
        self.tx_start_btn = QPushButton("Start Send")
        self.tx_stop_btn = QPushButton("Stop Send")
        self.tx_stop_btn.setEnabled(False)
        self.tx_status = QLabel("Stopped")

        self.raw_add_btn = QPushButton("Add RAW")
        self.raw_remove_btn = QPushButton("Remove RAW")
        self.raw_clear_btn = QPushButton("Clear RAW")
        self.raw_once_btn = QPushButton("Send RAW Once")
        self.raw_start_btn = QPushButton("Start RAW")
        self.raw_stop_btn = QPushButton("Stop RAW")
        self.raw_stop_btn.setEnabled(False)
        self.raw_status = QLabel("RAW stopped")

        self.tx_table = QTableWidget(0, 7)
        self.tx_table.setHorizontalHeaderLabels(
            ["Message", "Cycle", "Signal", "Mode", "Preset", "Value", "Enum"]
        )
        self.tx_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tx_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tx_table.verticalHeader().setVisible(False)
        tx_header = self.tx_table.horizontalHeader()
        tx_header.setStretchLastSection(False)
        tx_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        tx_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tx_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column, width in {1: 118, 3: 76, 4: 110, 5: 84, 6: 180}.items():
            tx_header.resizeSection(column, width)
        self.tx_table.itemChanged.connect(self._sync_tx_enum)

        self.send_tabs = QTabWidget()
        dbc_page = QWidget()
        dbc_layout = QVBoxLayout(dbc_page)
        dbc_row1 = QHBoxLayout()
        dbc_row1.addWidget(self.tx_add_signal_btn)
        dbc_row1.addWidget(self.tx_add_rc_btn)
        dbc_row1.addWidget(self.tx_add_crc_btn)
        dbc_row1.addWidget(self.tx_remove_btn)
        dbc_row1.addWidget(self.tx_clear_btn)
        dbc_row1.addStretch(1)
        dbc_row2 = QHBoxLayout()
        dbc_row2.addWidget(QLabel("Fallback Interval(ms)"))
        dbc_row2.addWidget(self.tx_interval_input)
        dbc_row2.addWidget(self.tx_start_btn)
        dbc_row2.addWidget(self.tx_stop_btn)
        dbc_row2.addStretch(1)
        dbc_row2.addWidget(self.tx_status)
        dbc_layout.addLayout(dbc_row1)
        dbc_layout.addLayout(dbc_row2)
        dbc_layout.addWidget(QLabel("DBC source: uses the DBC path configured in Hardware Config."))
        dbc_layout.addWidget(self.tx_dbc_label)
        dbc_layout.addWidget(self.tx_table)
        self.send_tabs.addTab(dbc_page, "DBC/Signal Send")

        raw_page = QWidget()
        raw_layout = QVBoxLayout(raw_page)
        raw_controls = QHBoxLayout()
        for widget in (self.raw_add_btn, self.raw_remove_btn, self.raw_clear_btn, self.raw_once_btn, self.raw_start_btn, self.raw_stop_btn):
            raw_controls.addWidget(widget)
        raw_controls.addStretch(1)
        self.raw_table = QTableWidget(0, 19)
        self.raw_table.setHorizontalHeaderLabels([
            "On", "Row", "Send", "Trigger", "CAN ID", "Channel", "Type", "DLC", "BRS",
            "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "Period (ms)", "Notes",
        ])
        self.raw_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.raw_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.raw_table.verticalHeader().setVisible(False)
        self.raw_table.horizontalHeader().setStretchLastSection(True)
        self.raw_table.currentCellChanged.connect(self._refresh_raw_data_layout)
        self.raw_table.itemChanged.connect(
            lambda item: self._on_raw_main_byte_changed(item.row(), item.column())
        )
        raw_layout.addLayout(raw_controls)
        raw_layout.addWidget(self.raw_status)
        raw_panels = QSplitter(Qt.Orientation.Vertical)
        raw_panels.addWidget(self.raw_table)
        byte_panel = QWidget()
        byte_layout = QVBoxLayout(byte_panel)
        byte_layout.setContentsMargins(0, 0, 0, 0)
        self.raw_data_label = QLabel("Data bytes — select a message row")
        byte_layout.addWidget(self.raw_data_label)
        self.raw_data_layout = QTableWidget(0, 9)
        self.raw_data_layout.setHorizontalHeaderLabels(["Byte Index", "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7"])
        self.raw_data_layout.verticalHeader().setVisible(False)
        self.raw_data_layout.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.raw_data_layout.itemChanged.connect(self._on_raw_layout_item_changed)
        byte_layout.addWidget(self.raw_data_layout)
        raw_panels.addWidget(byte_panel)
        raw_panels.setStretchFactor(0, 3)
        raw_panels.setStretchFactor(1, 2)
        raw_panels.setSizes([480, 260])
        raw_layout.addWidget(raw_panels)
        for column in (0, 1, 2, 8):
            self.raw_table.setColumnWidth(column, 45)
        for column in range(9, 17):
            self.raw_table.setColumnWidth(column, 42)
        self.raw_table.setColumnWidth(6, 130)
        self.raw_table.setColumnWidth(7, 70)
        self.raw_data_layout.horizontalHeader().setDefaultSectionSize(85)
        self.send_tabs.addTab(raw_page, "RAW CAN Send")

        send_layout = QVBoxLayout(self.tx_group)
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port"))
        port_row.addWidget(self.tx_port_input)
        port_row.addStretch(1)
        send_layout.addLayout(port_row)
        send_layout.addWidget(self.send_tabs)

        self.tx_port_input.currentTextChanged.connect(self._on_tx_port_changed)
        self.tx_add_signal_btn.clicked.connect(self._add_manual_signal_row)
        self.tx_add_rc_btn.clicked.connect(lambda: self._add_algorithm_row("rc"))
        self.tx_add_crc_btn.clicked.connect(lambda: self._add_algorithm_row("crc"))
        self.tx_remove_btn.clicked.connect(self._remove_tx_row)
        self.tx_clear_btn.clicked.connect(self._clear_tx_rows)
        self.tx_start_btn.clicked.connect(self._start_tx)
        self.tx_stop_btn.clicked.connect(self._stop_tx)
        self.raw_add_btn.clicked.connect(lambda: self._append_raw_row({}))
        self.raw_remove_btn.clicked.connect(self._remove_raw_row)
        self.raw_clear_btn.clicked.connect(self._clear_raw_rows)
        self.raw_once_btn.clicked.connect(self._send_raw_once)
        self.raw_start_btn.clicked.connect(self._start_raw_tx)
        self.raw_stop_btn.clicked.connect(self._stop_raw_tx)

    def set_hardware(self, items: List[HardwareConfig]) -> None:
        self._stop_recording()
        if self._adapters:
            self._stop_tx()
            self._stop_raw_tx()
            self.rx_timer.stop()
            self._rx_running = False
            self.rx_toggle_btn.setText("Start Receive")
            self.rx_status.setText("Stopped")
            self.close_all_ports()

        self._hardware = {item.name: HardwareConfig.from_dict(asdict(item)) for item in items}
        names = sorted(self._hardware.keys())

        current_rx = self.rx_port_input.currentText()
        self.rx_port_input.blockSignals(True)
        self.rx_port_input.clear()
        self.rx_port_input.addItems(names)
        if current_rx:
            self.rx_port_input.setCurrentText(current_rx)
        self.rx_port_input.blockSignals(False)
        self._refresh_rx_info(self.rx_port_input.currentText())
        self._clear_rx_rows()

        current_tx = self.tx_port_input.currentText()
        self.tx_port_input.blockSignals(True)
        self.tx_port_input.clear()
        self.tx_port_input.addItems(names)
        if current_tx:
            self.tx_port_input.setCurrentText(current_tx)
        self.tx_port_input.blockSignals(False)
        self._on_tx_port_changed(self.tx_port_input.currentText())

    def set_algorithms(self, items: List[AlgorithmPreset]) -> None:
        self._algorithms = [AlgorithmPreset.from_dict(asdict(item)) for item in items]
        self._algorithm_service.update_presets(self._algorithms)

    def _refresh_rx_info(self, port_name: str) -> None:
        hw = self._hardware.get(port_name)
        self.rx_dbc_label.setText(f"DBC: {hw.dbc_path if hw and hw.dbc_path else '-'}")
        self.rx_port_detail_label.setText(f"Port: {self._describe_hw(hw)}")
        self._refresh_record_selection_label()

    def _clear_rx_rows(self) -> None:
        self.rx_table.setRowCount(0)
        self._rx_signal_rows.clear()
        self._rx_signal_values.clear()
        self._rx_raw_rows.clear()
        self._rx_raw_counts.clear()
        self._rx_raw_frame_count = 0
        self._rx_decoded_frame_count = 0
        self._rx_decoded_signal_count = 0
        self._rx_last_table_refresh = 0.0
        self._rx_started_at = 0.0
        self._rx_last_frame_at = 0.0

    def _on_tx_port_changed(self, port_name: str) -> None:
        hw = self._hardware.get(port_name)
        self.tx_dbc_label.setText(f"DBC: {hw.dbc_path if hw and hw.dbc_path else '-'}")
        self._clear_tx_rows()
        self._stop_raw_tx()
        for row in range(self.raw_table.rowCount()):
            self.raw_table.item(row, 5).setText(hw.channel if hw else "")

    def _ensure_port_open(self, port_name: str) -> BusAdapter:
        adapter = self._adapters.get(port_name)
        if adapter is not None and adapter.is_open:
            return adapter
        hw = self._hardware.get(port_name)
        if hw is None:
            raise RuntimeError(f"Unknown port '{port_name}'.")
        driver = self._registry.get_driver(hw.driver)
        adapter = driver.create_adapter(hw)
        adapter.open()
        self._adapters[port_name] = adapter
        return adapter

    def _toggle_receive(self) -> None:
        if self._rx_running:
            self._stop_recording()
            self._rx_running = False
            self.rx_timer.stop()
            self.rx_toggle_btn.setText("Start Receive")
            self.rx_status.setText("Stopped")
            return

        port_name = self.rx_port_input.currentText().strip()
        if not port_name:
            QMessageBox.warning(self, "Receive", "Please select a receive port.")
            return
        try:
            self._ensure_port_open(port_name)
        except Exception as exc:
            QMessageBox.critical(self, "Receive", f"Open port failed:\n{exc}")
            return
        self._rx_running = True
        self._rx_started_at = time.monotonic()
        self._rx_last_frame_at = 0.0
        self.rx_timer.start()
        self.rx_toggle_btn.setText("Stop Receive")
        self.rx_status.setText(f"Running on {port_name}")

    def _poll_receive(self) -> None:
        if not self._rx_running:
            return
        if self._rx_polling:
            return
        port_name = self.rx_port_input.currentText().strip()
        adapter = self._adapters.get(port_name)
        if not port_name or adapter is None:
            return
        self._rx_polling = True
        try:
            received = False
            for _ in range(100):
                try:
                    frame = adapter.recv(timeout=0.0)
                except Exception as exc:
                    self.rx_status.setText(f"Receive error: {exc}")
                    self._rx_running = False
                    self.rx_timer.stop()
                    self.rx_toggle_btn.setText("Start Receive")
                    self._stop_recording()
                    return
                if frame is None:
                    break
                received = True
                self._rx_last_frame_at = time.monotonic()
                self._append_rx_frame(port_name, frame)
            if not received:
                self._refresh_rx_idle_status(port_name)
        finally:
            self._rx_polling = False

    def _append_rx_frame(self, port_name: str, frame: Frame) -> None:
        try:
            decoded = self._dbc.decode_frame(port_name, frame)
        except Exception:
            if not frame.is_error:
                self._rx_known_raw_ids.setdefault(port_name, set()).add((int(frame.arbitration_id), bool(frame.is_extended_id)))
            self._record_received_frame(port_name, frame, None)
            self._append_raw_rx_frame(frame)
            return

        self._record_received_frame(port_name, frame, decoded)

        message_name = str(decoded.get("message_name", ""))
        signals = decoded.get("signals", {})
        if not isinstance(signals, dict):
            return

        self._rx_decoded_frame_count += 1
        updated_signals = 0
        data_text = frame.data.hex(" ").upper()
        for signal_name, signal_value in signals.items():
            key = (message_name, str(signal_name))
            value_text = str(signal_value)
            previous = self._rx_signal_values.get(key)
            if self.rx_changes_only_input.isChecked() and previous == value_text:
                continue
            row = self._rx_signal_rows.get(key)
            if row is None:
                row = self.rx_table.rowCount()
                self.rx_table.insertRow(row)
                self._rx_signal_rows[key] = row
                self.rx_table.setItem(row, 0, _make_readonly_item(str(row + 1)))
                self.rx_table.setItem(row, 1, _make_readonly_item(message_name))
                self.rx_table.setItem(row, 2, _make_readonly_item(str(signal_name)))
            self.rx_table.setItem(row, 3, _make_readonly_item(value_text))
            self.rx_table.setItem(row, 4, _make_readonly_item(data_text))
            self._rx_signal_values[key] = value_text
            updated_signals += 1
        self._rx_decoded_signal_count += updated_signals
        self._refresh_rx_status()
        self._refresh_rx_table_layout()

    def _append_raw_rx_frame(self, frame: Frame) -> None:
        key = (int(frame.arbitration_id), bool(frame.is_extended_id))
        row = self._rx_raw_rows.get(key)
        count = self._rx_raw_counts.get(key, 0) + 1
        self._rx_raw_counts[key] = count
        self._rx_raw_frame_count += 1
        if row is None:
            row = self.rx_table.rowCount()
            self.rx_table.insertRow(row)
            self._rx_raw_rows[key] = row
            self.rx_table.setItem(row, 0, _make_readonly_item(str(row + 1)))
            id_width = 8 if frame.is_extended_id else 3
            self.rx_table.setItem(
                row,
                1,
                _make_readonly_item(f"RAW 0x{int(frame.arbitration_id):0{id_width}X}"),
            )
            self.rx_table.setItem(row, 2, _make_readonly_item("Frame"))
        self.rx_table.setItem(row, 3, _make_readonly_item(f"count={count} dlc={frame.dlc}"))
        self.rx_table.setItem(row, 4, _make_readonly_item(frame.data.hex(" ").upper()))
        self._refresh_rx_status()
        self._refresh_rx_table_layout()

    def _refresh_rx_status(self) -> None:
        self.rx_status.setText(
            "Running, "
            f"decoded frames: {self._rx_decoded_frame_count}, "
            f"updated signals: {self._rx_decoded_signal_count}, "
            f"raw frames: {self._rx_raw_frame_count}"
        )

    def _refresh_rx_table_layout(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._rx_last_table_refresh >= 0.5:
            self._rx_last_table_refresh = now
            self.rx_table.resizeColumnsToContents()

    def _refresh_rx_idle_status(self, port_name: str) -> None:
        if not self._rx_running or not self._rx_started_at:
            return
        now = time.monotonic()
        if self._rx_last_frame_at:
            return
        if now - self._rx_started_at >= 1.0:
            hw = self._hardware.get(port_name)
            self.rx_status.setText(f"Running, no frames on {self._describe_hw(hw)}")

    def _describe_hw(self, hw: HardwareConfig | None) -> str:
        if hw is None:
            return "-"
        mask = str(hw.extra.get("channel_mask", "")).strip()
        mask_text = ""
        if mask:
            try:
                mask_text = f", mask=0x{int(mask, 0):X}"
            except ValueError:
                mask_text = f", mask={mask}"
        return (
            f"{hw.name} [{hw.driver}] ch={hw.channel}{mask_text}, "
            f"{hw.bitrate}/{hw.data_bitrate}, FD={'yes' if hw.fd else 'no'}"
        )

    def _interval_display(self, port_name: str, message_name: str) -> tuple[int, str]:
        try:
            cycle_time_ms = self._dbc.message_cycle_time(port_name, message_name)
        except Exception:
            cycle_time_ms = None
        if cycle_time_ms is not None and int(cycle_time_ms) > 0:
            return int(cycle_time_ms), "DBC"
        return int(self.tx_interval_input.value()), "Fallback"

    def _open_signal_search(self, port_name: str) -> Dict[str, str] | None:
        if not port_name:
            QMessageBox.warning(self, "Send", "Please select a send port first.")
            return None
        dialog = SignalSearchDialog(
            dbc_service=self._dbc,
            port_names=[port_name],
            title="Select Signal",
            default_port=port_name,
            parent=self,
        )
        if dialog.exec() != SignalSearchDialog.DialogCode.Accepted:
            return None
        payload = dialog.selected()
        if not isinstance(payload, dict):
            return None
        return {
            "message": str(payload.get("message", "")),
            "signal": str(payload.get("signal", "")),
        }

    def _open_signal_selection(self, port_name: str) -> list[dict]:
        if not port_name:
            QMessageBox.warning(self, "Send", "Please select a send port first.")
            return []
        dialog = SignalSelectionDialog(self._dbc, port_name, "Select Send Signals", self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            return dialog.selected_rows()
        return []

    def _select_algorithm_preset(self, kind: AlgorithmKind) -> str:
        names = [item.name for item in self._algorithms if item.kind == kind]
        if not names:
            raise RuntimeError(
                f"No {'RC' if kind == AlgorithmKind.ROLLING_COUNTER else 'CRC'} preset is configured in Algorithm Config."
            )
        selected, ok = QInputDialog.getItem(
            self,
            "Select Preset",
            "Preset",
            names,
            0,
            False,
        )
        if not ok or not selected:
            return ""
        return str(selected)

    def _row_exists(self, message_name: str, signal_name: str, mode: str, preset_name: str) -> bool:
        for row in range(self.tx_table.rowCount()):
            payload = self.tx_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if not isinstance(payload, dict):
                continue
            if (
                payload.get("message") == message_name
                and payload.get("signal") == signal_name
                and payload.get("mode") == mode
                and payload.get("preset") == preset_name
            ):
                return True
        return False

    def _append_tx_row(
        self,
        *,
        port_name: str,
        message_name: str,
        signal_name: str,
        mode: str,
        preset_name: str,
        value_text: str,
    ) -> None:
        if self._row_exists(message_name, signal_name, mode, preset_name):
            return
        row = self.tx_table.rowCount()
        self.tx_table.insertRow(row)
        interval_ms, interval_source = self._interval_display(port_name, message_name)
        payload = {
            "message": message_name,
            "signal": signal_name,
            "mode": mode,
            "preset": preset_name,
        }
        self.tx_table.setItem(row, 0, _make_readonly_item(message_name, payload))
        self.tx_table.item(row, 0).setToolTip(message_name)
        self.tx_table.setItem(
            row,
            1,
            _make_readonly_item(f"{interval_ms}ms ({interval_source})"),
        )
        self.tx_table.setItem(row, 2, _make_readonly_item(signal_name))
        self.tx_table.item(row, 2).setToolTip(signal_name)
        self.tx_table.setItem(row, 3, _make_readonly_item(mode.upper()))
        self.tx_table.setItem(row, 4, _make_readonly_item(preset_name))
        self.tx_table.item(row, 4).setToolTip(preset_name)
        value_item = QTableWidgetItem(value_text)
        if mode != "manual":
            value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.tx_table.setItem(row, 5, value_item)
        try:
            choices = self._dbc.signal_choices(port_name, message_name, signal_name)
        except Exception:
            choices = {}
        if mode == "manual" and choices:
            combo = QComboBox()
            combo.addItem("Custom numeric value", None)
            for value, label in choices.items():
                combo.addItem(f"{label} ({value:g})", value)
            combo.setToolTip("Choose an enum label or edit the numeric Value column.")
            self.tx_table.setCellWidget(row, 6, combo)
            self._sync_tx_enum(value_item)
            combo.currentIndexChanged.connect(lambda _index, widget=combo: self._apply_tx_enum(widget))
        else:
            self.tx_table.setItem(row, 6, _make_readonly_item("Automatic" if mode != "manual" else "—"))

    def _sync_tx_enum(self, item: QTableWidgetItem) -> None:
        if item.column() != 5:
            return
        combo = self.tx_table.cellWidget(item.row(), 6)
        if not isinstance(combo, QComboBox):
            return
        try:
            index = combo.findData(_parse_signal_value(item.text()))
        except (ValueError, OverflowError):
            index = 0
        previous = combo.blockSignals(True)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(previous)

    def _apply_tx_enum(self, combo: QComboBox) -> None:
        value = combo.currentData()
        if value is None:
            return
        for row in range(self.tx_table.rowCount()):
            if self.tx_table.cellWidget(row, 6) is combo:
                self.tx_table.item(row, 5).setText(format(value, ".15g"))
                break

    def _default_signal_value(self, port_name: str, message_name: str, signal_name: str) -> str:
        try:
            defaults = self._dbc.default_signals(port_name, message_name)
        except Exception:
            defaults = {}
        value = defaults.get(signal_name, 0)
        return str(value)

    def _add_manual_signal_row(self) -> None:
        port_name = self.tx_port_input.currentText().strip()
        for selected in self._open_signal_selection(port_name):
            message_name = selected["message"]
            signal_name = selected["signal"]
            self._append_tx_row(
                port_name=port_name,
                message_name=message_name,
                signal_name=signal_name,
                mode="manual",
                preset_name="",
                value_text=self._default_signal_value(port_name, message_name, signal_name),
            )

    def _add_algorithm_row(self, mode: str) -> None:
        port_name = self.tx_port_input.currentText().strip()
        selected = self._open_signal_search(port_name)
        if selected is None:
            return
        kind = AlgorithmKind.ROLLING_COUNTER if mode == "rc" else AlgorithmKind.CRC
        try:
            preset_name = self._select_algorithm_preset(kind)
        except Exception as exc:
            QMessageBox.warning(self, "Algorithm", str(exc))
            return
        if not preset_name:
            return
        self._append_tx_row(
            port_name=port_name,
            message_name=selected["message"],
            signal_name=selected["signal"],
            mode=mode,
            preset_name=preset_name,
            value_text="0",
        )

    def _remove_tx_row(self) -> None:
        selected = self.tx_table.selectionModel().selectedRows()
        if not selected:
            return
        self.tx_table.removeRow(selected[0].row())

    def _clear_tx_rows(self) -> None:
        self._stop_tx()
        self.tx_table.setRowCount(0)
        self._tx_last_sent.clear()

    def _append_raw_row(self, values: Dict[str, Any]) -> None:
        row = self.raw_table.rowCount()
        self.raw_table.insertRow(row)
        enabled = bool(values.get("enabled", True))
        enabled_item = QTableWidgetItem()
        enabled_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
        enabled_item.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        self.raw_table.setItem(row, 0, enabled_item)
        self.raw_table.setItem(row, 1, _make_readonly_item(str(row + 1)))
        send_button = QPushButton("▶")
        send_button.clicked.connect(
            lambda _checked=False, button=send_button: self._send_raw_row(
                self._raw_widget_row(button, 2)
            )
        )
        self.raw_table.setCellWidget(row, 2, send_button)

        trigger = str(values.get("trigger", "周期" if values.get("period_ms") else "手动"))
        trigger_combo = QComboBox()
        trigger_combo.addItems(["手动", "周期"])
        trigger_combo.setCurrentText(trigger if trigger in {"手动", "周期"} else "手动")
        self.raw_table.setCellWidget(row, 3, trigger_combo)
        self.raw_table.setItem(row, 4, QTableWidgetItem(str(values.get("can_id", "0x100"))))
        hw = self._hardware.get(self.tx_port_input.currentText().strip())
        self.raw_table.setItem(row, 5, _make_readonly_item(hw.channel if hw else ""))

        message_type = str(values.get("message_type", ""))
        if message_type not in _RAW_MESSAGE_TYPES:
            is_fd = str(values.get("mode", "")).strip().lower() in {"fd", "canfd", "can-fd", "can fd"}
            is_extended = str(values.get("format", "")).strip().lower() in {"extended", "ext", "29-bit"}
            message_type = ("FD" if is_fd else "") + ("扩展帧" if is_extended else "标准帧")
            message_type = {"标准帧": "标准数据帧", "扩展帧": "扩展数据帧", "FD标准帧": "FD标准帧", "FD扩展帧": "FD扩展帧"}.get(message_type, "标准数据帧")
        type_combo = QComboBox()
        type_combo.addItems(_RAW_MESSAGE_TYPES)
        type_combo.setCurrentText(message_type)
        self.raw_table.setCellWidget(row, 6, type_combo)

        fd = _RAW_TYPE_FLAGS[message_type][0]
        raw_dlc = values.get("dlc")
        if raw_dlc is None:
            old_data = str(values.get("data", "00 " * 8)).replace(",", " ").split()
            raw_dlc = next((code for code, length in _RAW_DLC_LENGTHS.items() if length >= len(old_data)), 15) if fd else min(len(old_data), 8)
        dlc_combo = QComboBox()
        dlc_combo.addItems([str(index) for index in range(16 if fd else 9)])
        dlc_combo.setCurrentText(str(raw_dlc) if str(raw_dlc) in [dlc_combo.itemText(i) for i in range(dlc_combo.count())] else "0")
        dlc_combo.setToolTip("DLC 9/10/11/12/13/14/15 = 12/16/20/24/32/48/64 bytes")
        self.raw_table.setCellWidget(row, 7, dlc_combo)
        brs_item = QTableWidgetItem()
        brs_item.setFlags(brs_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        brs_item.setCheckState(Qt.CheckState.Checked if values.get("brs", False) else Qt.CheckState.Unchecked)
        self.raw_table.setItem(row, 8, brs_item)

        payload_length = _raw_payload_length(int(dlc_combo.currentText()), fd)
        raw_bytes = values.get("bytes")
        if raw_bytes is None:
            raw_bytes = str(values.get("data", "00")).replace(",", " ").split()
        else:
            raw_bytes = list(raw_bytes) + str(values.get("extended_data", "")).replace(",", " ").split()
        normalized = []
        for value in list(raw_bytes):
            try:
                normalized.append(_normalize_raw_byte(value))
            except ValueError:
                normalized.append("00")
        normalized.extend(["00"] * max(0, payload_length - len(normalized)))
        metadata = {"bytes": normalized[:payload_length], "notes": str(values.get("notes", ""))}
        self.raw_table.item(row, 1).setData(Qt.ItemDataRole.UserRole, metadata)
        for index in range(8):
            self.raw_table.setItem(row, 9 + index, QTableWidgetItem(normalized[index] if index < payload_length else "00"))
        period_input = QSpinBox()
        period_input.setRange(10, 60_000)
        try:
            period_input.setValue(int(values.get("period_ms", 100)))
        except (TypeError, ValueError):
            period_input.setValue(100)
        period_input.setEnabled(trigger_combo.currentText() == "周期")
        trigger_combo.currentTextChanged.connect(lambda value, period=period_input: period.setEnabled(value == "周期"))
        self.raw_table.setCellWidget(row, 17, period_input)
        self.raw_table.setItem(row, 18, QTableWidgetItem(str(values.get("notes", ""))))
        type_combo.currentTextChanged.connect(lambda _value, widget=type_combo: self._on_raw_row_settings_changed(self._raw_widget_row(widget, 6)))
        dlc_combo.currentTextChanged.connect(lambda _value, widget=dlc_combo: self._on_raw_row_settings_changed(self._raw_widget_row(widget, 7)))
        self._on_raw_row_settings_changed(row)
        self.raw_table.setCurrentCell(row, 4)
        self._refresh_raw_data_layout()
        self.raw_table.resizeColumnsToContents()
        for column in range(9, 17):
            self.raw_table.setColumnWidth(column, 48)

    def _raw_widget_row(self, widget: QWidget, column: int) -> int:
        for row in range(self.raw_table.rowCount()):
            if self.raw_table.cellWidget(row, column) is widget:
                return row
        return -1

    def _remove_raw_row(self) -> None:
        row = self.raw_table.currentRow()
        if row >= 0:
            self.raw_table.removeRow(row)
            self._raw_tx_last_sent.clear()
            for row in range(self.raw_table.rowCount()):
                self.raw_table.item(row, 1).setText(str(row + 1))
            self._refresh_raw_data_layout()

    def _clear_raw_rows(self) -> None:
        self._stop_raw_tx()
        self.raw_table.setRowCount(0)
        self._raw_tx_last_sent.clear()
        self.raw_data_layout.setRowCount(0)

    def _raw_metadata(self, row: int) -> Dict[str, Any]:
        item = self.raw_table.item(row, 1)
        metadata = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(metadata, dict):
            metadata = {"bytes": ["00"] * 8, "notes": ""}
            if item is not None:
                item.setData(Qt.ItemDataRole.UserRole, metadata)
        return metadata

    def _on_raw_row_settings_changed(self, row: int) -> None:
        if self._updating_raw_table or not 0 <= row < self.raw_table.rowCount():
            return
        type_combo = self.raw_table.cellWidget(row, 6)
        dlc_combo = self.raw_table.cellWidget(row, 7)
        if not isinstance(type_combo, QComboBox) or not isinstance(dlc_combo, QComboBox):
            return
        fd = _RAW_TYPE_FLAGS[type_combo.currentText()][0]
        current_dlc = int(dlc_combo.currentText() or "0")
        maximum = 15 if fd else 8
        dlc_combo.blockSignals(True)
        dlc_combo.clear()
        dlc_combo.addItems([str(value) for value in range(maximum + 1)])
        dlc_combo.setCurrentText(str(min(current_dlc, maximum)))
        dlc_combo.blockSignals(False)
        required = _raw_payload_length(int(dlc_combo.currentText()), fd)
        brs_item = self.raw_table.item(row, 8)
        brs_item.setFlags(Qt.ItemFlag.ItemIsSelectable | (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable if fd else Qt.ItemFlag.NoItemFlags))
        if not fd:
            brs_item.setCheckState(Qt.CheckState.Unchecked)
        metadata = self._raw_metadata(row)
        payload = list(metadata.get("bytes", []))
        payload = [_normalize_raw_byte(value) for value in payload[:required]]
        payload.extend(["00"] * (required - len(payload)))
        metadata["bytes"] = payload
        self.raw_table.item(row, 1).setData(Qt.ItemDataRole.UserRole, metadata)
        self._updating_raw_table = True
        try:
            for index in range(8):
                cell = QTableWidgetItem(payload[index] if index < required else "")
                if index >= required:
                    cell.setFlags(Qt.ItemFlag.NoItemFlags)
                self.raw_table.setItem(row, 9 + index, cell)
        finally:
            self._updating_raw_table = False
        self._refresh_raw_data_layout()

    def _refresh_raw_data_layout(self, *_args) -> None:
        row = self.raw_table.currentRow()
        self._updating_raw_layout = True
        try:
            self.raw_data_layout.setRowCount(0)
            if row < 0 or row >= self.raw_table.rowCount():
                self.raw_data_label.setText("Data bytes — select a message row")
                return
            values = self._raw_row_values(row)
            payload = values["bytes"]
            self.raw_data_label.setText(f"Row {row + 1} · {values['can_id']} · DLC {values['dlc']} · {len(payload)} bytes")
            for start in range(0, len(payload), 8):
                layout_row = self.raw_data_layout.rowCount()
                self.raw_data_layout.insertRow(layout_row)
                self.raw_data_layout.setItem(layout_row, 0, _make_readonly_item(str(start)))
                for column in range(8):
                    index = start + column
                    if index >= len(payload):
                        self.raw_data_layout.setItem(layout_row, column + 1, _make_readonly_item(""))
                    else:
                        self.raw_data_layout.setItem(layout_row, column + 1, QTableWidgetItem(payload[index]))
        finally:
            self._updating_raw_layout = False

    def _on_raw_layout_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_raw_layout or item.column() == 0:
            return
        row = self.raw_table.currentRow()
        if row < 0:
            return
        index_item = self.raw_data_layout.item(item.row(), 0)
        if index_item is None:
            return
        payload_index = int(index_item.text()) + item.column() - 1
        values = self._raw_row_values(row)
        if payload_index >= len(values["bytes"]):
            return
        try:
            normalized = _normalize_raw_byte(item.text())
        except ValueError as exc:
            self.raw_status.setText(f"Row {row + 1}, D{payload_index}: {exc}")
            self._refresh_raw_data_layout()
            return
        metadata = self._raw_metadata(row)
        payload = list(metadata.get("bytes", []))
        payload[payload_index] = normalized
        metadata["bytes"] = payload
        self.raw_table.item(row, 1).setData(Qt.ItemDataRole.UserRole, metadata)
        self._updating_raw_table = True
        try:
            if payload_index < 8:
                self.raw_table.setItem(row, 9 + payload_index, QTableWidgetItem(normalized))
        finally:
            self._updating_raw_table = False
        self._updating_raw_layout = True
        try:
            item.setText(normalized)
        finally:
            self._updating_raw_layout = False

    def _on_raw_main_byte_changed(self, row: int, column: int) -> None:
        if self._updating_raw_table or not 9 <= column <= 16:
            return
        item = self.raw_table.item(row, column)
        if item is None:
            return
        metadata = self._raw_metadata(row)
        payload = list(metadata.get("bytes", []))
        index = column - 9
        if index < len(payload):
            try:
                normalized = _normalize_raw_byte(item.text())
            except ValueError as exc:
                self.raw_status.setText(f"Row {row + 1}, D{index}: {exc}")
                normalized = payload[index]
            payload[index] = normalized
            metadata["bytes"] = payload
            self._updating_raw_table = True
            try:
                self.raw_table.item(row, 1).setData(Qt.ItemDataRole.UserRole, metadata)
                item.setText(normalized)
            finally:
                self._updating_raw_table = False
            if row == self.raw_table.currentRow():
                self._refresh_raw_data_layout()

    def _raw_snapshot_rows(self) -> List[Dict[str, Any]]:
        rows = []
        for row in range(self.raw_table.rowCount()):
            def text(column: int, fallback: str = "") -> str:
                item = self.raw_table.item(row, column)
                return item.text().strip() if item is not None else fallback
            enabled_item = self.raw_table.item(row, 0)
            trigger_widget = self.raw_table.cellWidget(row, 3)
            type_widget = self.raw_table.cellWidget(row, 6)
            dlc_widget = self.raw_table.cellWidget(row, 7)
            brs_item = self.raw_table.item(row, 8)
            metadata = self.raw_table.item(row, 1).data(Qt.ItemDataRole.UserRole) if self.raw_table.item(row, 1) else {}
            message_type = type_widget.currentText() if isinstance(type_widget, QComboBox) else "标准数据帧"
            dlc = dlc_widget.currentText() if isinstance(dlc_widget, QComboBox) else "0"
            fd = _RAW_TYPE_FLAGS.get(message_type, (False, False))[0]
            payload_length = _raw_payload_length(int(dlc), fd)
            byte_values = [text(9 + index, "00") for index in range(8)]
            if isinstance(metadata, dict):
                byte_values.extend(list(metadata.get("bytes", []))[8:])
            byte_values = [(str(value).upper() if str(value).strip() else "00") for value in byte_values[:payload_length]]
            byte_values.extend(["00"] * (payload_length - len(byte_values)))
            period_widget = self.raw_table.cellWidget(row, 17)
            period_ms = period_widget.value() if isinstance(period_widget, QSpinBox) else 100
            rows.append({"enabled": enabled_item is not None and enabled_item.checkState() == Qt.CheckState.Checked, "trigger": trigger_widget.currentText() if isinstance(trigger_widget, QComboBox) else "手动", "can_id": text(4, "0x100"), "channel": text(5), "message_type": message_type, "dlc": str(dlc), "brs": brs_item is not None and brs_item.checkState() == Qt.CheckState.Checked, "bytes": byte_values, "data": " ".join(byte_values), "period_ms": period_ms, "notes": text(18)})
        return rows

    def _raw_row_values(self, row: int) -> Dict[str, Any]:
        return self._raw_snapshot_rows()[row]

    def _raw_frame_for_row(self, row: int) -> Frame:
        values = self._raw_row_values(row)
        return _raw_frame_from_row_values(can_id=values["can_id"], message_type=values["message_type"], dlc=values["dlc"], brs=values["brs"], bytes_text=values["bytes"])

    def _send_raw_once(self) -> None:
        port_name = self.tx_port_input.currentText().strip()
        if not port_name:
            QMessageBox.warning(self, "RAW CAN", "Please select a send port.")
            return
        try:
            adapter = self._ensure_port_open(port_name)
            sent = 0
            errors = []
            for row in range(self.raw_table.rowCount()):
                item = self.raw_table.item(row, 0)
                if item is None or item.checkState() != Qt.CheckState.Checked:
                    continue
                try:
                    adapter.send(self._raw_frame_for_row(row))
                    sent += 1
                except Exception as exc:
                    errors.append(f"row {row + 1}: {exc}")
            self.raw_status.setText(f"RAW sent {sent} row(s)" + (f"; {len(errors)} error(s)" if errors else ""))
            if errors:
                self.raw_status.setToolTip("\n".join(errors))
        except Exception as exc:
            QMessageBox.critical(self, "RAW CAN", f"Open/send failed:\n{exc}")

    def _send_raw_row(self, row: int) -> None:
        if not 0 <= row < self.raw_table.rowCount():
            return
        port_name = self.tx_port_input.currentText().strip()
        if not port_name:
            self.raw_status.setText("RAW send requires a port")
            return
        try:
            self._ensure_port_open(port_name).send(self._raw_frame_for_row(row))
            self.raw_status.setText(f"RAW row {row + 1} sent")
        except Exception as exc:
            self.raw_status.setText(f"RAW row {row + 1} error: {exc}")

    def _start_raw_tx(self) -> None:
        port_name = self.tx_port_input.currentText().strip()
        if not port_name:
            QMessageBox.warning(self, "RAW CAN", "Please select a send port.")
            return
        try:
            self._ensure_port_open(port_name)
        except Exception as exc:
            QMessageBox.critical(self, "RAW CAN", f"Open port failed:\n{exc}")
            return
        self._raw_tx_running = True
        self._raw_tx_last_sent.clear()
        self.raw_tx_timer.start()
        self.raw_start_btn.setEnabled(False)
        self.raw_stop_btn.setEnabled(True)
        self.raw_status.setText("RAW running")

    def _stop_raw_tx(self) -> None:
        self.raw_tx_timer.stop()
        self._raw_tx_running = False
        self._raw_tx_last_sent.clear()
        if hasattr(self, "raw_start_btn"):
            self.raw_start_btn.setEnabled(True)
            self.raw_stop_btn.setEnabled(False)
            self.raw_status.setText("RAW stopped")

    def _send_due_raw_rows(self) -> None:
        if not self._raw_tx_running:
            return
        port_name = self.tx_port_input.currentText().strip()
        adapter = self._adapters.get(port_name)
        if adapter is None:
            return
        now = time.monotonic()
        sent = 0
        errors = []
        for row in range(self.raw_table.rowCount()):
            try:
                values = self._raw_row_values(row)
                if not values["enabled"]:
                    continue
                if values.get("trigger", "周期") != "周期":
                    continue
                period = max(10, int(values["period_ms"]))
                if now - self._raw_tx_last_sent.get(row, 0.0) < period / 1000.0:
                    continue
                adapter.send(self._raw_frame_for_row(row))
                self._raw_tx_last_sent[row] = now
                sent += 1
            except Exception as exc:
                errors.append(f"row {row + 1}: {exc}")
        self.raw_status.setText(f"RAW running, sent {sent} row(s)" + (f", {len(errors)} error(s)" if errors else ""))
        if errors:
            self.raw_status.setToolTip("\n".join(errors))

    def _start_tx(self) -> None:
        port_name = self.tx_port_input.currentText().strip()
        if not port_name:
            QMessageBox.warning(self, "Send", "Please select a send port.")
            return
        if self.tx_table.rowCount() <= 0:
            QMessageBox.warning(self, "Send", "Please add at least one signal row.")
            return
        try:
            self._ensure_port_open(port_name)
        except Exception as exc:
            QMessageBox.critical(self, "Send", f"Open port failed:\n{exc}")
            return
        self._tx_last_sent.clear()
        self.tx_timer.start()
        self._tx_running = True
        self.tx_start_btn.setEnabled(False)
        self.tx_stop_btn.setEnabled(True)
        self.tx_status.setText("Running")
        self._send_due_messages(force_all=True)

    def _stop_tx(self) -> None:
        was_running = self._tx_running
        self.tx_timer.stop()
        self._tx_running = False
        self._tx_last_sent.clear()
        self.tx_start_btn.setEnabled(True)
        self.tx_stop_btn.setEnabled(False)
        self.tx_status.setToolTip("")
        if was_running:
            self.tx_status.setText("Stopped")

    def _apply_tx_warning_status(
        self,
        *,
        sent_count: int,
        rows_by_message: Dict[str, List[dict]],
        warnings: List[str],
    ) -> None:
        details = []
        for message_name in rows_by_message.keys():
            interval_ms, interval_source = self._interval_display(
                self.tx_port_input.currentText().strip(),
                message_name,
            )
            details.append(f"{message_name}:{interval_ms}ms({interval_source})")
        status = f"Running, sent {sent_count} message(s) [{', '.join(details[:4])}]"
        if warnings:
            status += f" | DBC adjusted {len(warnings)} signal(s)"
            self.tx_status.setToolTip("\n".join(warnings[:20]))
        else:
            self.tx_status.setToolTip("")
        self.tx_status.setText(status)

    def _collect_rows_by_message(self) -> Dict[str, List[dict]]:
        grouped: Dict[str, List[dict]] = {}
        for row in range(self.tx_table.rowCount()):
            message_item = self.tx_table.item(row, 0)
            value_item = self.tx_table.item(row, 5)
            if message_item is None:
                continue
            payload = message_item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(payload, dict):
                continue
            payload = dict(payload)
            payload["row"] = row
            payload["value_text"] = value_item.text().strip() if value_item is not None else "0"
            grouped.setdefault(str(payload["message"]), []).append(payload)
        return grouped

    def _effective_interval_ms(self, port_name: str, message_name: str) -> int:
        return self._interval_display(port_name, message_name)[0]

    def _send_due_messages(self, force_all: bool = False) -> None:
        port_name = self.tx_port_input.currentText().strip()
        if not port_name:
            return
        adapter = self._adapters.get(port_name)
        if adapter is None:
            return
        rows_by_message = self._collect_rows_by_message()
        try:
            now = time.monotonic()
            sent_count = 0
            warnings: List[str] = []
            for message_name, rows in rows_by_message.items():
                interval_ms = self._effective_interval_ms(port_name, message_name)
                last_sent = self._tx_last_sent.get(message_name, 0.0)
                if not force_all and now - last_sent < max(0.01, float(interval_ms) / 1000.0):
                    continue
                payload = self._dbc.default_signals(port_name, message_name)
                for row in rows:
                    if row["mode"] == "manual":
                        payload[row["signal"]] = _parse_signal_value(row["value_text"])
                for row in rows:
                    if row["mode"] == "rc":
                        scope_key = f"direct:{port_name}:{message_name}:{row['signal']}"
                        rc_value = self._algorithm_service.next_rolling_counter(
                            row["preset"],
                            scope_key,
                        )
                        payload[row["signal"]] = float(rc_value)
                        self.tx_table.item(int(row["row"]), 5).setText(str(rc_value))
                for row in rows:
                    if row["mode"] == "crc":
                        scratch = dict(payload)
                        scratch[row["signal"]] = 0.0
                        preset = self._algorithm_service.get_preset(row["preset"])
                        for signal_name in preset.exclude_signals:
                            if signal_name:
                                scratch[signal_name] = 0.0
                        encoded = self._dbc.encode_message(
                            port_name,
                            message_name,
                            scratch,
                            best_effort=True,
                            warnings=warnings,
                        )
                        crc_value = self._algorithm_service.compute_crc(row["preset"], encoded.data)
                        payload[row["signal"]] = float(crc_value)
                        self.tx_table.item(int(row["row"]), 5).setText(str(crc_value))
                frame = self._dbc.encode_message(
                    port_name,
                    message_name,
                    payload,
                    best_effort=True,
                    warnings=warnings,
                )
                adapter.send(frame)
                self._tx_last_sent[message_name] = now
                sent_count += 1
            if sent_count > 0:
                self._apply_tx_warning_status(
                    sent_count=sent_count,
                    rows_by_message=rows_by_message,
                    warnings=warnings,
                )
        except Exception as exc:
            self._stop_tx()
            QMessageBox.critical(self, "Send", f"Send failed:\n{exc}")

    def close_all_ports(self) -> None:
        adapters = list(self._adapters.values())
        self._adapters.clear()
        for adapter in adapters:
            try:
                adapter.close()
            except Exception:
                pass

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._stop_recording()
        self._stop_tx()
        self._stop_raw_tx()
        self.rx_timer.stop()
        self._rx_running = False
        self.close_all_ports()
        parent = self.parent()
        if parent is not None and hasattr(parent, "_store_direct_can_state"):
            try:
                parent._store_direct_can_state(self.snapshot_state())
            except Exception:
                pass
        super().closeEvent(event)
