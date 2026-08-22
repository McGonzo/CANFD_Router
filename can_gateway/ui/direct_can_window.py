from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Dict, List

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from can_gateway.core.frame import Frame
from can_gateway.core.models import AlgorithmKind, AlgorithmPreset, HardwareConfig
from can_gateway.drivers.base import BusAdapter
from can_gateway.services import AlgorithmService, DbcService, DeviceRegistry
from can_gateway.ui.dialogs import SignalSearchDialog


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
        self.resize(1440, 860)

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
        self._rx_polling = False

        central = QWidget(self)
        self.setCentralWidget(central)
        root = QHBoxLayout(central)

        self.rx_group = QGroupBox("Receive")
        self.tx_group = QGroupBox("Send")
        root.addWidget(self.rx_group, 1)
        root.addWidget(self.tx_group, 1)

        self._build_rx_ui()
        self._build_tx_ui()

        self.rx_timer = QTimer(self)
        self.rx_timer.setInterval(50)
        self.rx_timer.timeout.connect(self._poll_receive)

        self.tx_timer = QTimer(self)
        self.tx_timer.setInterval(10)
        self.tx_timer.timeout.connect(self._send_due_messages)

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

        self.rx_port_input.currentTextChanged.connect(self._refresh_rx_info)
        self.rx_toggle_btn.clicked.connect(self._toggle_receive)
        self.rx_clear_btn.clicked.connect(self._clear_rx_rows)

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

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Port"))
        row1.addWidget(self.tx_port_input)
        row1.addWidget(self.tx_add_signal_btn)
        row1.addWidget(self.tx_add_rc_btn)
        row1.addWidget(self.tx_add_crc_btn)
        row1.addWidget(self.tx_remove_btn)
        row1.addWidget(self.tx_clear_btn)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Fallback Interval(ms)"))
        row2.addWidget(self.tx_interval_input)
        row2.addWidget(self.tx_start_btn)
        row2.addWidget(self.tx_stop_btn)
        row2.addStretch(1)
        row2.addWidget(self.tx_status)

        self.tx_table = QTableWidget(0, 6)
        self.tx_table.setHorizontalHeaderLabels(
            ["Message", "Cycle", "Signal", "Mode", "Preset", "Value"]
        )
        self.tx_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tx_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tx_table.verticalHeader().setVisible(False)
        self.tx_table.horizontalHeader().setStretchLastSection(True)

        layout = QVBoxLayout(self.tx_group)
        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addWidget(QLabel("DBC source: uses the DBC path configured in Hardware Config."))
        layout.addWidget(self.tx_dbc_label)
        layout.addWidget(self.tx_table)

        self.tx_port_input.currentTextChanged.connect(self._on_tx_port_changed)
        self.tx_add_signal_btn.clicked.connect(self._add_manual_signal_row)
        self.tx_add_rc_btn.clicked.connect(lambda: self._add_algorithm_row("rc"))
        self.tx_add_crc_btn.clicked.connect(lambda: self._add_algorithm_row("crc"))
        self.tx_remove_btn.clicked.connect(self._remove_tx_row)
        self.tx_clear_btn.clicked.connect(self._clear_tx_rows)
        self.tx_start_btn.clicked.connect(self._start_tx)
        self.tx_stop_btn.clicked.connect(self._stop_tx)

    def set_hardware(self, items: List[HardwareConfig]) -> None:
        if self._adapters:
            self._stop_tx()
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
            self._append_raw_rx_frame(frame)
            return

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
        self.tx_table.setItem(
            row,
            1,
            _make_readonly_item(f"{interval_ms}ms ({interval_source})"),
        )
        self.tx_table.setItem(row, 2, _make_readonly_item(signal_name))
        self.tx_table.setItem(row, 3, _make_readonly_item(mode.upper()))
        self.tx_table.setItem(row, 4, _make_readonly_item(preset_name))
        value_item = QTableWidgetItem(value_text)
        if mode != "manual":
            value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.tx_table.setItem(row, 5, value_item)
        self.tx_table.resizeColumnsToContents()

    def _default_signal_value(self, port_name: str, message_name: str, signal_name: str) -> str:
        try:
            defaults = self._dbc.default_signals(port_name, message_name)
        except Exception:
            defaults = {}
        value = defaults.get(signal_name, 0)
        return str(value)

    def _add_manual_signal_row(self) -> None:
        port_name = self.tx_port_input.currentText().strip()
        selected = self._open_signal_search(port_name)
        if selected is None:
            return
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
        self._stop_tx()
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
