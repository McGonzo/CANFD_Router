from __future__ import annotations

import time
from dataclasses import asdict
from typing import Dict, List, Tuple

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from can_gateway.core.frame import Frame
from can_gateway.core.models import HardwareConfig
from can_gateway.drivers.base import BusAdapter
from can_gateway.services.device_registry import DeviceRegistry


def _item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class DiagnosticsTab(QWidget):
    def __init__(self, registry: DeviceRegistry, parent=None) -> None:
        super().__init__(parent)
        self._registry = registry
        self._hardware: Dict[str, HardwareConfig] = {}
        self._adapter: BusAdapter | None = None
        self._running = False
        self._polling = False
        self._started_at = 0.0
        self._total_frames = 0
        self._fd_frames = 0
        self._extended_frames = 0
        self._last_frame_at = 0.0
        self._rows: Dict[Tuple[int, bool], int] = {}
        self._stats: Dict[Tuple[int, bool], dict] = {}
        self._current_port_name = ""
        self._current_driver_name = ""
        self._total_errors = 0
        self._last_error_at = 0.0
        self._last_error_text = ""
        self._error_rows: Dict[Tuple[str, str, str, int | None, str, str], int] = {}
        self._error_stats: Dict[Tuple[str, str, str, int | None, str, str], dict] = {}

        self.port_input = QComboBox()
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.clear_btn = QPushButton("Clear")
        self.status_label = QLabel("Stopped")
        self.port_detail_label = QLabel("Port: -")
        self.summary_label = QLabel("Frames:0  IDs:0  FD:0  Ext:0  Rate:0/s")

        top = QHBoxLayout()
        top.addWidget(QLabel("Port"))
        top.addWidget(self.port_input)
        top.addWidget(self.start_btn)
        top.addWidget(self.stop_btn)
        top.addWidget(self.clear_btn)
        top.addStretch(1)
        top.addWidget(self.status_label)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["ID", "Format", "FD", "DLC", "Count", "Rate/s", "Period ms", "Last Data", "Last Seen"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.error_table = QTableWidget(0, 11)
        self.error_table.setHorizontalHeaderLabels(
            [
                "Time",
                "Port",
                "Driver",
                "Source",
                "Error",
                "Code",
                "Bus Status",
                "TX Err",
                "RX Err",
                "Count",
                "Last Seen",
            ]
        )
        self.error_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.error_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.error_table.verticalHeader().setVisible(False)
        self.error_table.horizontalHeader().setStretchLastSection(True)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.port_detail_label)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.table)
        layout.addWidget(QLabel("Bus Errors"))
        layout.addWidget(self.error_table)

        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._poll)
        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(500)
        self.ui_timer.timeout.connect(self._refresh_summary)

        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.clear_btn.clicked.connect(self.clear)
        self.port_input.currentTextChanged.connect(self._refresh_port_detail)

    def set_hardware(self, items: List[HardwareConfig]) -> None:
        if self._running:
            self.stop()
        self._hardware = {item.name: HardwareConfig.from_dict(asdict(item)) for item in items}
        current = self.port_input.currentText().strip()
        self.port_input.blockSignals(True)
        self.port_input.clear()
        self.port_input.addItems(sorted(self._hardware.keys()))
        if current:
            self.port_input.setCurrentText(current)
        self.port_input.blockSignals(False)
        self._refresh_port_detail(self.port_input.currentText())

    def _refresh_port_detail(self, port_name: str) -> None:
        self.port_detail_label.setText(f"Port: {self._describe_hw(self._hardware.get(port_name))}")

    def start(self) -> None:
        if self._running:
            return
        port_name = self.port_input.currentText().strip()
        if not port_name:
            QMessageBox.warning(self, "Diagnostics", "Please select a port.")
            return
        hw = self._hardware.get(port_name)
        if hw is None:
            QMessageBox.warning(self, "Diagnostics", f"Unknown port '{port_name}'.")
            return
        try:
            adapter = self._registry.get_driver(hw.driver).create_adapter(hw)
            adapter.open()
        except Exception as exc:
            QMessageBox.critical(self, "Diagnostics", f"Open port failed:\n{exc}")
            return
        self._adapter = adapter
        self._current_port_name = port_name
        self._current_driver_name = hw.driver
        self._running = True
        self._started_at = time.monotonic()
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Running")
        self.timer.start()
        self.ui_timer.start()

    def stop(self) -> None:
        self.timer.stop()
        self.ui_timer.stop()
        self._running = False
        self._polling = False
        adapter = self._adapter
        self._adapter = None
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Stopped")
        self._refresh_summary()

    def clear(self) -> None:
        self.table.setRowCount(0)
        self.error_table.setRowCount(0)
        self._rows.clear()
        self._stats.clear()
        self._error_rows.clear()
        self._error_stats.clear()
        self._total_frames = 0
        self._fd_frames = 0
        self._extended_frames = 0
        self._last_frame_at = 0.0
        self._total_errors = 0
        self._last_error_at = 0.0
        self._last_error_text = ""
        self._started_at = time.monotonic()
        self._refresh_summary()

    def _poll(self) -> None:
        if not self._running or self._polling or self._adapter is None:
            return
        self._polling = True
        try:
            received = False
            for _ in range(250):
                try:
                    frame = self._adapter.recv(timeout=0.0)
                except Exception as exc:
                    self.status_label.setText(f"Receive error: {exc}")
                    self.stop()
                    return
                if frame is None:
                    break
                received = True
                if frame.is_error:
                    self._record_error_event(frame)
                else:
                    self._record_frame(frame)
            if not received:
                self._refresh_idle_status()
        finally:
            self._polling = False

    def _record_frame(self, frame: Frame) -> None:
        now = time.monotonic()
        key = (int(frame.arbitration_id), bool(frame.is_extended_id))
        stat = self._stats.get(key)
        if stat is None:
            stat = {
                "count": 0,
                "first": now,
                "last": 0.0,
                "period_min": None,
                "period_max": None,
                "period_sum": 0.0,
                "period_count": 0,
            }
            self._stats[key] = stat
            self._insert_row(key, frame)
        if stat["last"]:
            period_ms = (now - float(stat["last"])) * 1000.0
            stat["period_min"] = period_ms if stat["period_min"] is None else min(stat["period_min"], period_ms)
            stat["period_max"] = period_ms if stat["period_max"] is None else max(stat["period_max"], period_ms)
            stat["period_sum"] += period_ms
            stat["period_count"] += 1
        stat["count"] += 1
        stat["last"] = now
        self._total_frames += 1
        self._fd_frames += 1 if frame.is_fd else 0
        self._extended_frames += 1 if frame.is_extended_id else 0
        self._last_frame_at = now
        self._update_row(key, frame, stat, now)

    def _insert_row(self, key: Tuple[int, bool], frame: Frame) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows[key] = row
        id_width = 8 if frame.is_extended_id else 3
        self.table.setItem(row, 0, _item(f"0x{int(frame.arbitration_id):0{id_width}X}"))
        self.table.setItem(row, 1, _item("Ext" if frame.is_extended_id else "Std"))

    def _update_row(self, key: Tuple[int, bool], frame: Frame, stat: dict, now: float) -> None:
        row = self._rows[key]
        elapsed = max(0.001, now - float(stat["first"]))
        rate = float(stat["count"]) / elapsed
        if stat["period_count"]:
            avg = float(stat["period_sum"]) / float(stat["period_count"])
            period = f"{avg:.1f} ({stat['period_min']:.1f}/{stat['period_max']:.1f})"
        else:
            period = "-"
        self.table.setItem(row, 2, _item("Yes" if frame.is_fd else "No"))
        self.table.setItem(row, 3, _item(str(frame.dlc)))
        self.table.setItem(row, 4, _item(str(stat["count"])))
        self.table.setItem(row, 5, _item(f"{rate:.1f}"))
        self.table.setItem(row, 6, _item(period))
        self.table.setItem(row, 7, _item(frame.data.hex(" ").upper()))
        self.table.setItem(row, 8, _item(f"{now - self._started_at:.3f}s"))

    def _record_error_event(self, frame: Frame) -> None:
        now = time.monotonic()
        port = self._current_port_name or frame.channel or "-"
        driver = frame.driver or self._current_driver_name or "-"
        source = frame.error_source or "ERROR"
        error_text = frame.error_text or "CAN bus error"
        bus_status = frame.bus_status or ""
        key = (port, driver, source, frame.error_code, error_text, bus_status)
        stat = self._error_stats.get(key)
        if stat is None:
            stat = {"count": 0, "first": now, "last": now}
            self._error_stats[key] = stat
            row = self.error_table.rowCount()
            self.error_table.insertRow(row)
            self._error_rows[key] = row
            self.error_table.setItem(row, 0, _item(f"{now - self._started_at:.3f}s"))
            self.error_table.setItem(row, 1, _item(port))
            self.error_table.setItem(row, 2, _item(driver))
            self.error_table.setItem(row, 3, _item(source))
            self.error_table.setItem(row, 4, _item(error_text))
            code_text = "-" if frame.error_code is None else f"0x{int(frame.error_code):X}"
            self.error_table.setItem(row, 5, _item(code_text))
            self.error_table.setItem(row, 6, _item(bus_status or "-"))
            tx_text = "-" if frame.tx_error_count is None else str(frame.tx_error_count)
            rx_text = "-" if frame.rx_error_count is None else str(frame.rx_error_count)
            self.error_table.setItem(row, 7, _item(tx_text))
            self.error_table.setItem(row, 8, _item(rx_text))
        stat["count"] += 1
        stat["last"] = now
        self._total_errors += 1
        self._last_error_at = now
        self._last_error_text = error_text
        row = self._error_rows[key]
        self.error_table.setItem(row, 9, _item(str(stat["count"])))
        self.error_table.setItem(row, 10, _item(f"{now - self._started_at:.3f}s"))
        self.status_label.setText(f"Bus error: {source} {error_text}")
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        elapsed = max(0.001, time.monotonic() - self._started_at) if self._started_at else 0.001
        rate = float(self._total_frames) / elapsed
        self.summary_label.setText(
            f"Frames:{self._total_frames}  IDs:{len(self._stats)}  "
            f"FD:{self._fd_frames}  Ext:{self._extended_frames}  Rate:{rate:.1f}/s  "
            f"Errors:{self._total_errors}  Last:{self._last_error_text or '-'}"
        )
        if self._last_error_at and self._running:
            self.status_label.setText(
                f"Running, last error {time.monotonic() - self._last_error_at:.2f}s ago"
            )
        elif self._last_frame_at and self._running:
            self.status_label.setText(f"Running, last frame {time.monotonic() - self._last_frame_at:.2f}s ago")

    def _refresh_idle_status(self) -> None:
        if not self._running or self._total_frames > 0:
            return
        elapsed = time.monotonic() - self._started_at if self._started_at else 0.0
        if elapsed >= 1.0:
            port_name = self.port_input.currentText().strip()
            self.status_label.setText(
                f"Running, no frames on {self._describe_hw(self._hardware.get(port_name))}"
            )

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

    def close(self) -> bool:  # noqa: A003
        self.stop()
        return super().close()
