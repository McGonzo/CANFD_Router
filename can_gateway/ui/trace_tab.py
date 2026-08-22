from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Tuple

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from can_gateway.core.frame import Frame
from can_gateway.services import DbcService, RouterTraceEvent


def _format_signal_value(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:.6g}"
    return str(value)


class TraceTab(QWidget):
    def __init__(self, dbc_service: DbcService, parent=None) -> None:
        super().__init__(parent)
        self._dbc = dbc_service
        self._paused = False
        self._rows: Dict[Tuple[str, str, str, str], Dict[str, object]] = {}
        self._display_order: List[Tuple[str, str, str, str]] = []

        self.port_filter = QComboBox()
        self.port_filter.addItem("All", "")
        self.dir_filter = QComboBox()
        self.dir_filter.addItems(["All", "RX", "TX", "INJECT", "ERROR", "RX_ERROR", "TX_ERROR", "CHIP_STATE"])
        self.changed_only_check = QCheckBox("Changed Only")
        self.freeze_order_check = QCheckBox("Freeze Order")
        self.freeze_order_check.setChecked(True)
        self.max_rows_input = QSpinBox()
        self.max_rows_input.setRange(100, 50_000)
        self.max_rows_input.setValue(5000)
        self.clear_btn = QPushButton("Clear")
        self.pause_btn = QPushButton("Pause")

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Port"))
        controls.addWidget(self.port_filter)
        controls.addWidget(QLabel("Direction"))
        controls.addWidget(self.dir_filter)
        controls.addWidget(self.changed_only_check)
        controls.addWidget(self.freeze_order_check)
        controls.addWidget(QLabel("Max Rows"))
        controls.addWidget(self.max_rows_input)
        controls.addWidget(self.pause_btn)
        controls.addWidget(self.clear_btn)
        controls.addStretch(1)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["Time", "Dir", "Port", "Message", "Signal", "FD", "Data", "Value", "Rule"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.table)

        self.clear_btn.clicked.connect(self.clear)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.port_filter.currentIndexChanged.connect(self._rebuild_table)
        self.dir_filter.currentIndexChanged.connect(self._rebuild_table)
        self.changed_only_check.stateChanged.connect(self._rebuild_table)
        self.freeze_order_check.stateChanged.connect(self._rebuild_table)
        self.max_rows_input.valueChanged.connect(self._trim_and_rebuild)

    def set_ports(self, port_names: List[str]) -> None:
        current = self.port_filter.currentText()
        self.port_filter.blockSignals(True)
        self.port_filter.clear()
        self.port_filter.addItem("All", "")
        for name in sorted(port_names):
            self.port_filter.addItem(name, name)
        if current:
            self.port_filter.setCurrentText(current)
        self.port_filter.blockSignals(False)
        self._rebuild_table()

    def clear(self) -> None:
        self._rows.clear()
        self._display_order.clear()
        self.table.setRowCount(0)

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        self.pause_btn.setText("Resume" if self._paused else "Pause")

    def add_events(self, events: Iterable[RouterTraceEvent]) -> None:
        if self._paused:
            return
        for payload in self._rows.values():
            payload["changed_recently"] = False
        got_events = False
        for event in events:
            got_events = True
            for key, payload in self._expand_event(event):
                current = self._rows.get(key)
                if current is not None and current.get("data") == payload.get("data") and current.get("value") == payload.get("value"):
                    current["changed_recently"] = False
                    continue
                payload["changed_recently"] = True
                self._rows[key] = payload
                if key not in self._display_order:
                    self._display_order.append(key)
        if got_events:
            self._trim_rows()
            self._rebuild_table()

    def _trim_and_rebuild(self, *_args) -> None:
        self._trim_rows()
        self._rebuild_table()

    def _trim_rows(self) -> None:
        max_rows = int(self.max_rows_input.value())
        while len(self._rows) > max_rows:
            oldest_key = min(
                self._rows,
                key=lambda key: float(self._rows[key].get("timestamp_raw", 0.0)),
            )
            self._rows.pop(oldest_key, None)
            if oldest_key in self._display_order:
                self._display_order.remove(oldest_key)

    def _allow_row(self, payload: Dict[str, object]) -> bool:
        port = str(self.port_filter.currentData() or "")
        if port and str(payload["port"]) != port:
            return False
        direction = self.dir_filter.currentText()
        if direction != "All" and str(payload["dir"]) != direction:
            return False
        if self.changed_only_check.isChecked() and not bool(payload.get("changed_recently", False)):
            return False
        return True

    def _rebuild_table(self, *_args) -> None:
        visible_keys = [key for key, payload in self._rows.items() if self._allow_row(payload)]
        if self.freeze_order_check.isChecked():
            ordered_keys = [key for key in self._display_order if key in visible_keys]
            ordered_keys.extend([key for key in visible_keys if key not in ordered_keys])
        else:
            ordered_keys = sorted(
                visible_keys,
                key=lambda key: (
                    -float(self._rows[key].get("timestamp_raw", 0.0)),
                    str(self._rows[key].get("dir", "")),
                    str(self._rows[key].get("port", "")),
                    str(self._rows[key].get("message", "")),
                    str(self._rows[key].get("signal", "")),
                ),
            )
            self._display_order = ordered_keys + [
                key for key in self._display_order if key not in ordered_keys
            ]
        self.table.setRowCount(len(ordered_keys))
        for row, key in enumerate(ordered_keys):
            payload = self._rows[key]
            values = [
                str(payload["time"]),
                str(payload["dir"]),
                str(payload["port"]),
                str(payload["message"]),
                str(payload["signal"]),
                str(payload["fd"]),
                str(payload["data"]),
                str(payload["value"]),
                str(payload["rule"]),
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()

    def _expand_event(
        self, event: RouterTraceEvent
    ) -> List[Tuple[Tuple[str, str, str, str], Dict[str, object]]]:
        ts_text = datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S.%f")[:-3]
        base = {
            "time": ts_text,
            "timestamp_raw": float(event.timestamp),
            "dir": event.direction,
            "port": event.port,
            "fd": "Yes" if event.is_fd else "No",
            "data": event.data_hex,
            "rule": event.rule_name,
            "changed_recently": True,
        }
        if event.event_type == "error":
            code = "-" if event.error_code is None else f"0x{int(event.error_code):X}"
            counters = []
            if event.tx_error_count is not None:
                counters.append(f"TXerr={event.tx_error_count}")
            if event.rx_error_count is not None:
                counters.append(f"RXerr={event.rx_error_count}")
            value = " ".join(part for part in [code, event.bus_status, " ".join(counters)] if part and part != "-")
            payload = {
                **base,
                "message": "BUS ERROR",
                "signal": event.error_text or "CAN bus error",
                "value": value or code,
                "rule": event.rule_name or "driver",
            }
            key = (
                event.direction,
                event.port,
                str(payload["message"]),
                str(payload["signal"]),
            )
            return [(key, payload)]
        frame = Frame(
            arbitration_id=event.arbitration_id,
            data=bytes.fromhex(event.data_hex) if event.data_hex else b"",
            is_fd=event.is_fd,
            channel=event.port,
            timestamp=event.timestamp,
        )
        try:
            decoded = self._dbc.decode_frame(event.port, frame)
        except Exception:
            decoded = None
        if isinstance(decoded, dict):
            message_name = str(decoded.get("message_name", "")).strip()
            signals = decoded.get("signals", {})
            if message_name and isinstance(signals, dict) and signals:
                rows: List[Tuple[Tuple[str, str, str, str], Dict[str, object]]] = []
                for signal_name, value in signals.items():
                    payload = {
                        **base,
                        "message": message_name,
                        "signal": str(signal_name),
                        "value": _format_signal_value(value),
                    }
                    key = (
                        event.direction,
                        event.port,
                        message_name,
                        str(signal_name),
                    )
                    rows.append((key, payload))
                return rows
        message_name = f"0x{event.arbitration_id:X}"
        payload = {**base, "message": message_name, "signal": "", "value": ""}
        return [((event.direction, event.port, message_name, ""), payload)]
