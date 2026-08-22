from __future__ import annotations

from dataclasses import asdict
from typing import Dict, List

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from can_gateway.core.models import AlgorithmPreset, HardwareConfig, RouteAlgorithmBinding, RouteRule
from can_gateway.services import DbcService, evaluate_transform_expression
from can_gateway.ui.dialogs import SignalMapDialog, SignalSearchDialog
from can_gateway.ui.topology_widget import TopologyPanel


def _algorithm_kind_value(value: object) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _make_readonly_item(value: object) -> QTableWidgetItem:
    item = QTableWidgetItem(str(value))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class RoutesTab(QWidget):
    changed = Signal()

    def __init__(self, dbc_service: DbcService, parent=None) -> None:
        super().__init__(parent)
        self._dbc = dbc_service
        self._routes: List[RouteRule] = []
        self._hardware: Dict[str, HardwareConfig] = {}
        self._algorithms: List[AlgorithmPreset] = []
        self._runtime_signal_values: Dict[tuple[str, str, str], object] = {}
        self._sync_selection = False
        self._updating_tables = False
        self._router_running = False

        self.route_tabs = QTabWidget(self)
        self.mapping_tab = QWidget(self)
        self.route_tabs.addTab(self.mapping_tab, "Mapping")

        self.topology = TopologyPanel(self)
        self.topology.connection_requested.connect(self._add_from_topology)
        self.topology_hint = QLabel(
            "Topology: click source port then target port to create a mapping row."
        )

        self.add_row_btn = QPushButton("Add Row")
        self.edit_row_btn = QPushButton("Edit Row")
        self.copy_row_btn = QPushButton("Copy Row")
        self.remove_row_btn = QPushButton("Remove Row")
        controls = QHBoxLayout()
        controls.addWidget(self.add_row_btn)
        controls.addWidget(self.edit_row_btn)
        controls.addWidget(self.copy_row_btn)
        controls.addWidget(self.remove_row_btn)
        controls.addStretch(1)

        self.left_group = QGroupBox("Source Mapping Table")
        left_layout = QVBoxLayout(self.left_group)
        self.left_table = QTableWidget(0, 6)
        self.left_table.setHorizontalHeaderLabels(
            ["No.", "Port", "Message", "Signal", "Calc(x)", "Value"]
        )
        self.left_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.left_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.left_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.left_table.verticalHeader().setVisible(False)
        self.left_table.horizontalHeader().setStretchLastSection(True)
        left_layout.addWidget(self.left_table)

        self.right_group = QGroupBox("Target Mapping Table")
        right_layout = QVBoxLayout(self.right_group)
        self.right_table = QTableWidget(0, 7)
        self.right_table.setHorizontalHeaderLabels(
            ["No.", "Port", "Message", "Signal", "Cycle", "RC/CRC", "Value"]
        )
        self.right_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.right_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.right_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.right_table.verticalHeader().setVisible(False)
        self.right_table.horizontalHeader().setStretchLastSection(True)
        right_layout.addWidget(self.right_table)

        pair_layout = QHBoxLayout()
        pair_layout.addWidget(self.left_group, 1)
        pair_layout.addWidget(self.right_group, 1)

        mapping_layout = QVBoxLayout(self.mapping_tab)
        mapping_layout.addWidget(self.topology_hint)
        mapping_layout.addWidget(self.topology, 2)
        mapping_layout.addLayout(controls)
        mapping_layout.addLayout(pair_layout, 3)

        root_layout = QVBoxLayout(self)
        root_layout.addWidget(self.route_tabs)

        self.add_row_btn.clicked.connect(self._add_row)
        self.edit_row_btn.clicked.connect(self._edit_row)
        self.copy_row_btn.clicked.connect(self._copy_row)
        self.remove_row_btn.clicked.connect(self._remove_row)
        self.left_table.cellDoubleClicked.connect(self._on_left_double_click)
        self.right_table.cellDoubleClicked.connect(self._on_right_double_click)
        self.right_table.itemChanged.connect(self._on_right_item_changed)
        self.left_table.itemSelectionChanged.connect(self._sync_from_left_selection)
        self.right_table.itemSelectionChanged.connect(self._sync_from_right_selection)

    def set_hardware(self, items: List[HardwareConfig]) -> None:
        self._hardware = {item.name: item for item in items}
        names = sorted(self._hardware.keys())
        self.topology.set_ports(names)
        self._refresh_topology()
        if self._routes:
            self._refresh_mapping_tables()

    def set_algorithms(self, items: List[AlgorithmPreset]) -> None:
        self._algorithms = [AlgorithmPreset.from_dict(asdict(item)) for item in items]

    def set_routes(self, items: List[RouteRule]) -> None:
        self._routes = [RouteRule.from_dict(asdict(item)) for item in items]
        self._refresh_mapping_tables()
        self._refresh_topology()

    def get_routes(self) -> List[RouteRule]:
        return [RouteRule.from_dict(asdict(item)) for item in self._routes]

    def set_router_running(self, running: bool) -> None:
        self._router_running = bool(running)

    def set_runtime_values(self, values: Dict[tuple[str, str, str], object]) -> None:
        self._runtime_signal_values = dict(values)
        self._refresh_value_columns()

    def _is_virtual_port(self, item: HardwareConfig) -> bool:
        driver = item.driver.lower()
        interface = str(item.extra.get("interface", "")).lower()
        return "virtual" in driver or "virtual" in interface or driver == "sim-virtual"

    def _virtual_port_names(self) -> List[str]:
        return sorted(
            [name for name, item in self._hardware.items() if self._is_virtual_port(item)]
        )

    def _add_row(self) -> None:
        names = sorted(self._hardware.keys())
        source = names[0] if names else ""
        target = ""
        for candidate in names:
            if candidate != source:
                target = candidate
                break
        route = RouteRule(
            name=self._new_rule_name(),
            source_port=source,
            source_message="",
            source_signal="",
            target_port=target,
            target_message="",
            target_signal="",
            transform_expr="x",
        )
        self._routes.append(route)
        self._refresh_mapping_tables()
        self._select_row(len(self._routes) - 1)
        self._refresh_topology()
        self.changed.emit()

    def _edit_row(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        dialog = SignalMapDialog(
            port_names=sorted(self._hardware.keys()),
            dbc_service=self._dbc,
            algorithm_presets=self._algorithms,
            parent=self,
            initial=self._routes[row],
        )
        if dialog.exec() != SignalMapDialog.DialogCode.Accepted:
            return
        cfg = dialog.to_config()
        duplicate_row = self.duplicate_target_row(cfg, skip_row=row)
        if duplicate_row >= 0:
            self._warn_duplicate_target(duplicate_row)
            return
        self._routes[row] = cfg
        self._refresh_mapping_tables()
        self._select_row(row)
        self._refresh_topology()
        self.changed.emit()

    def _copy_row(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        copied = RouteRule.from_dict(asdict(self._routes[row]))
        copied.name = self._new_rule_name(base=f"{copied.name}_copy")
        duplicate_row = self.duplicate_target_row(copied)
        if duplicate_row >= 0:
            self._warn_duplicate_target(duplicate_row)
            return
        self._routes.append(copied)
        self._refresh_mapping_tables()
        self._select_row(len(self._routes) - 1)
        self._refresh_topology()
        self.changed.emit()

    def _remove_row(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        self._routes.pop(row)
        self._refresh_mapping_tables()
        if self._routes:
            self._select_row(min(row, len(self._routes) - 1))
        self._refresh_topology()
        self.changed.emit()

    def _on_left_double_click(self, row: int, col: int) -> None:
        if not (0 <= row < len(self._routes)):
            return
        route = self._routes[row]
        if col == 0:
            self._select_row(row)
            self._edit_row()
            return
        if not route.source_port:
            self._select_row(row)
            self._edit_row()
            return
        if col == 5:
            return
        if col == 4:
            self._edit_transform_expression(row)
            return
        selected = self._open_signal_search(
            title="Select Source Signal",
            default_port=route.source_port,
        )
        if selected is None:
            return
        route.source_port = selected["port"]
        route.source_message = selected["message"]
        route.source_signal = selected["signal"]
        self._refresh_mapping_tables()
        self._select_row(row)
        self._refresh_topology()
        self.changed.emit()

    def _on_right_double_click(self, row: int, _col: int) -> None:
        if not (0 <= row < len(self._routes)):
            return
        if _col == 6:
            return
        if _col in (0, 4, 5):
            self._select_row(row)
            self._edit_row()
            return
        route = self._routes[row]
        selected = self._open_signal_search(
            title="Select Target Signal",
            default_port=route.target_port,
        )
        if selected is None:
            return
        candidate = RouteRule.from_dict(asdict(route))
        candidate.target_port = selected["port"]
        candidate.target_message = selected["message"]
        candidate.target_signal = selected["signal"]
        duplicate_row = self.duplicate_target_row(candidate, skip_row=row)
        if duplicate_row >= 0:
            self._warn_duplicate_target(duplicate_row)
            return
        route.target_port = candidate.target_port
        route.target_message = candidate.target_message
        route.target_signal = candidate.target_signal
        self._refresh_mapping_tables()
        self._select_row(row)
        self._refresh_topology()
        self.changed.emit()

    def _edit_transform_expression(self, row: int) -> None:
        route = self._routes[row]
        current = route.transform_expr or "x"
        text, ok = QInputDialog.getText(
            self,
            "Edit Calculation",
            "Expression using x (example: x*0.1+5, clamp(x,0,100)):",
            text=current,
        )
        if not ok:
            return
        expr = text.strip() or "x"
        try:
            _ = evaluate_transform_expression(expr, 1.0)
        except Exception as exc:
            QMessageBox.warning(self, "Expression", str(exc))
            return
        route.transform_expr = expr
        self._refresh_mapping_tables()
        self._select_row(row)
        self.changed.emit()

    def _open_signal_search(self, title: str, default_port: str) -> Dict[str, str] | None:
        ports = sorted(self._hardware.keys())
        if not ports:
            QMessageBox.warning(self, "Signal Search", "No port is configured.")
            return None
        dialog = SignalSearchDialog(
            dbc_service=self._dbc,
            port_names=ports,
            title=title,
            default_port=default_port,
            parent=self,
        )
        if dialog.exec() != SignalSearchDialog.DialogCode.Accepted:
            return None
        selected = dialog.selected()
        if not isinstance(selected, dict):
            return None
        return {
            "port": str(selected.get("port", "")),
            "message": str(selected.get("message", "")),
            "signal": str(selected.get("signal", "")),
        }

    def _selected_row(self) -> int:
        left_rows = self.left_table.selectionModel().selectedRows()
        if left_rows:
            return left_rows[0].row()
        right_rows = self.right_table.selectionModel().selectedRows()
        if right_rows:
            return right_rows[0].row()
        return -1

    def duplicate_target_row(self, candidate: RouteRule, skip_row: int = -1) -> int:
        candidate_keys = self._target_output_keys(candidate)
        if not candidate_keys:
            return -1
        for idx, route in enumerate(self._routes):
            if idx == skip_row:
                continue
            if candidate_keys & self._target_output_keys(route):
                return idx
        return -1

    def _target_output_keys(self, route: RouteRule) -> set[tuple[str, str, str, int | None]]:
        if not route.enabled or not route.target_port or not route.target_message:
            return set()
        signal_names = [route.target_signal]
        if route.rc_binding is not None:
            signal_names.append(route.rc_binding.target_signal)
        if route.crc_binding is not None:
            signal_names.append(route.crc_binding.target_signal)
        return {
            (route.target_port, route.target_message, signal_name, route.target_id)
            for signal_name in signal_names
            if signal_name
        }

    def _warn_duplicate_target(self, duplicate_row: int) -> None:
        QMessageBox.warning(
            self,
            "Duplicate Target",
            f"Target signal is already configured in row {duplicate_row + 1}.",
        )

    def _select_row(self, row: int) -> None:
        if not (0 <= row < len(self._routes)):
            return
        self._sync_selection = True
        self.left_table.clearSelection()
        self.right_table.clearSelection()
        self.left_table.selectRow(row)
        self.right_table.selectRow(row)
        self._sync_selection = False

    def _sync_from_left_selection(self) -> None:
        if self._sync_selection:
            return
        row = self._selected_row()
        if row < 0:
            return
        self._sync_selection = True
        self.right_table.clearSelection()
        self.right_table.selectRow(row)
        self._sync_selection = False

    def _sync_from_right_selection(self) -> None:
        if self._sync_selection:
            return
        row = self._selected_row()
        if row < 0:
            return
        self._sync_selection = True
        self.left_table.clearSelection()
        self.left_table.selectRow(row)
        self._sync_selection = False

    def _refresh_mapping_tables(self) -> None:
        self._updating_tables = True
        self.left_table.setRowCount(len(self._routes))
        self.right_table.setRowCount(len(self._routes))
        try:
            for idx, route in enumerate(self._routes):
                left_values = [
                    str(idx + 1),
                    route.source_port or "None",
                    route.source_message,
                    route.source_signal,
                    route.transform_expr or "x",
                    self._format_runtime_value(
                        route.source_port,
                        route.source_message,
                        route.source_signal,
                    ),
                ]
                right_values = [
                    str(idx + 1),
                    route.target_port,
                    route.target_message,
                    route.target_signal,
                    self._format_target_cycle(route),
                    self._format_algorithm_summary(route),
                ]
                for col, value in enumerate(left_values):
                    self.left_table.setItem(idx, col, _make_readonly_item(value))
                for col, value in enumerate(right_values):
                    self.right_table.setItem(idx, col, _make_readonly_item(value))
                self.right_table.setItem(idx, 6, self._make_target_value_item(route))
        finally:
            self._updating_tables = False
        self.left_table.resizeColumnsToContents()
        self.right_table.resizeColumnsToContents()

    def _refresh_value_columns(self) -> None:
        if self.left_table.rowCount() != len(self._routes) or self.right_table.rowCount() != len(self._routes):
            self._refresh_mapping_tables()
            return
        self._updating_tables = True
        try:
            for idx, route in enumerate(self._routes):
                self.left_table.setItem(
                    idx,
                    5,
                    _make_readonly_item(
                        self._format_runtime_value(
                            route.source_port,
                            route.source_message,
                            route.source_signal,
                        )
                    ),
                )
                self.right_table.setItem(idx, 6, self._make_target_value_item(route))
        finally:
            self._updating_tables = False

    def _make_target_value_item(self, route: RouteRule) -> QTableWidgetItem:
        if not route.source_port and route.target_signal:
            item = QTableWidgetItem((route.transform_expr or "0").strip())
            item.setToolTip("Direct output value")
            return item
        return _make_readonly_item(
            self._format_runtime_value(
                route.target_port,
                route.target_message,
                route.target_signal,
            )
        )

    def _on_right_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_tables or item.column() != 6:
            return
        row = item.row()
        if not (0 <= row < len(self._routes)):
            return
        route = self._routes[row]
        if route.source_port or not route.target_signal:
            return
        value_text = item.text().strip()
        try:
            _ = evaluate_transform_expression(value_text or "0", 0.0)
        except Exception as exc:
            self._updating_tables = True
            try:
                item.setText((route.transform_expr or "0").strip())
            finally:
                self._updating_tables = False
            QMessageBox.warning(self, "Direct Value", str(exc))
            return
        route.transform_expr = value_text or "0"
        self.changed.emit()

    def _format_runtime_value(self, port_name: str, message_name: str, signal_name: str) -> str:
        if not port_name or not message_name or not signal_name:
            return ""
        value = self._runtime_signal_values.get((port_name, message_name, signal_name))
        if value is None:
            return ""
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return f"{value:.6g}"
        return str(value)

    def _format_target_cycle(self, route: RouteRule) -> str:
        if not route.target_port or not route.target_message:
            return ""
        if route.force_tx_interval:
            return f"{int(route.tx_interval_ms)}ms (Fallback)"
        try:
            interval_ms, source = self._dbc.effective_message_cycle_time(
                route.target_port,
                route.target_message,
                default_ms=int(route.tx_interval_ms),
            )
        except Exception:
            interval_ms, source = int(route.tx_interval_ms), "Fallback"
        return f"{interval_ms}ms ({source})"

    def _format_algorithm_summary(self, route: RouteRule) -> str:
        parts: List[str] = []
        if not route.source_port:
            parts.append("Direct")
        if route.rc_binding is not None:
            parts.append(
                f"RC:{route.rc_binding.preset_name}->{route.rc_binding.target_signal}"
            )
        if route.crc_binding is not None:
            parts.append(
                f"CRC:{route.crc_binding.preset_name}->{route.crc_binding.target_signal}"
            )
        return " | ".join(parts)

    def _refresh_topology(self) -> None:
        links = []
        for route in self._routes:
            if route.enabled and route.source_port and route.target_port:
                links.append((route.source_port, route.target_port))
        self.topology.set_connections(links)

    def _new_rule_name(self, base: str = "map_rule") -> str:
        existing = {route.name for route in self._routes}
        if base not in existing:
            return base
        idx = 2
        while f"{base}_{idx}" in existing:
            idx += 1
        return f"{base}_{idx}"

    def _add_from_topology(self, source: str, target: str) -> None:
        route = RouteRule(
            name=self._new_rule_name(),
            source_port=source,
            source_message="",
            source_signal="",
            target_port=target,
            target_message="",
            target_signal="",
            transform_expr="x",
        )
        self._routes.append(route)
        row = len(self._routes) - 1
        self._refresh_mapping_tables()
        self._select_row(row)
        self._refresh_topology()
        self.changed.emit()

    def _refresh_inject_messages(self, port_name: str) -> None:
        current = self.inject_message_input.currentText()
        self.inject_message_input.clear()
        self.inject_message_input.addItems(self._dbc.list_message_names(port_name))
        if current:
            self.inject_message_input.setCurrentText(current)

    def _on_inject_clicked(self) -> None:
        port = self.inject_port_input.currentText().strip()
        message = self.inject_message_input.currentText().strip()
        if not port or not message:
            QMessageBox.warning(self, "Inject", "Please select port and message.")
            return
        raw = self.inject_signals_input.text().strip()
        if not raw:
            QMessageBox.warning(self, "Inject", "Signals JSON cannot be empty.")
            return
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("Signals JSON must be an object.")
            signals = {str(key): float(value) for key, value in parsed.items()}
        except Exception as exc:
            QMessageBox.warning(self, "Inject", str(exc))
            return
        self.inject_requested.emit(port, message, signals, False)

    def _refresh_algo_presets(self) -> None:
        current_rc = self.algo_rc_preset_input.currentText()
        current_crc = self.algo_crc_preset_input.currentText()
        self.algo_rc_preset_input.clear()
        self.algo_crc_preset_input.clear()
        self.algo_rc_preset_input.addItem("", "")
        self.algo_crc_preset_input.addItem("", "")
        for preset in self._algorithms:
            kind_value = _algorithm_kind_value(preset.kind)
            if kind_value == "rolling_counter":
                self.algo_rc_preset_input.addItem(preset.name, preset.name)
            elif kind_value == "crc":
                self.algo_crc_preset_input.addItem(preset.name, preset.name)
        if current_rc:
            self.algo_rc_preset_input.setCurrentText(current_rc)
        if current_crc:
            self.algo_crc_preset_input.setCurrentText(current_crc)

    def _refresh_algo_messages(self, port_name: str) -> None:
        current = self.algo_message_input.currentText()
        self.algo_message_input.clear()
        self.algo_message_input.addItems(self._dbc.list_message_names(port_name))
        if current:
            self.algo_message_input.setCurrentText(current)
        self._refresh_algo_signals(self.algo_message_input.currentText())

    def _refresh_algo_signals(self, message_name: str) -> None:
        port_name = self.algo_port_input.currentText().strip()
        current_rc_signal = self.algo_rc_signal_input.currentText()
        current_crc_signal = self.algo_crc_signal_input.currentText()
        signal_names = self._dbc.list_signals(port_name, message_name)
        self.algo_rc_signal_input.clear()
        self.algo_rc_signal_input.addItem("", "")
        self.algo_rc_signal_input.addItems(signal_names)
        if current_rc_signal:
            self.algo_rc_signal_input.setCurrentText(current_rc_signal)
        self.algo_crc_signal_input.clear()
        self.algo_crc_signal_input.addItem("", "")
        self.algo_crc_signal_input.addItems(signal_names)
        if current_crc_signal:
            self.algo_crc_signal_input.setCurrentText(current_crc_signal)

    def _on_algorithm_send_clicked(self) -> None:
        port = self.algo_port_input.currentText().strip()
        message = self.algo_message_input.currentText().strip()
        if not port or not message:
            QMessageBox.warning(self, "Algorithm Sender", "Please select port and message.")
            return
        raw = self.algo_signals_input.text().strip()
        signals: Dict[str, float] = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("Base signals JSON must be an object.")
                signals = {str(key): float(value) for key, value in parsed.items()}
            except Exception as exc:
                QMessageBox.warning(self, "Algorithm Sender", str(exc))
                return

        rc_preset = self.algo_rc_preset_input.currentText().strip()
        rc_signal = self.algo_rc_signal_input.currentText().strip()
        crc_preset = self.algo_crc_preset_input.currentText().strip()
        crc_signal = self.algo_crc_signal_input.currentText().strip()
        if bool(rc_preset) != bool(rc_signal):
            QMessageBox.warning(
                self,
                "Algorithm Sender",
                "RC preset and RC signal must be configured together.",
            )
            return
        if bool(crc_preset) != bool(crc_signal):
            QMessageBox.warning(
                self,
                "Algorithm Sender",
                "CRC preset and CRC signal must be configured together.",
            )
            return
        signal_names = [name for name in (rc_signal, crc_signal) if name]
        if len(signal_names) != len(set(signal_names)):
            QMessageBox.warning(
                self,
                "Algorithm Sender",
                "RC signal and CRC signal must be different.",
            )
            return

        payload = {
            "rc_binding": None,
            "crc_binding": None,
        }
        if rc_preset and rc_signal:
            payload["rc_binding"] = RouteAlgorithmBinding(
                preset_name=rc_preset,
                target_signal=rc_signal,
            )
        if crc_preset and crc_signal:
            payload["crc_binding"] = RouteAlgorithmBinding(
                preset_name=crc_preset,
                target_signal=crc_signal,
            )
        self.algorithm_send_requested.emit(port, message, signals, payload)

    def _refresh_rand_messages(self, port_name: str) -> None:
        current = self.rand_message_input.currentText()
        self.rand_message_input.clear()
        self.rand_message_input.addItems(self._dbc.list_message_names(port_name))
        if current:
            self.rand_message_input.setCurrentText(current)
        self._refresh_rand_signals(self.rand_message_input.currentText())

    def _refresh_rand_signals(self, message_name: str) -> None:
        port_name = self.rand_port_input.currentText().strip()
        current = self.rand_signal_input.currentText()
        self.rand_signal_input.clear()
        self.rand_signal_input.addItems(self._dbc.list_signals(port_name, message_name))
        if current:
            self.rand_signal_input.setCurrentText(current)

    def _toggle_random_generator(self) -> None:
        if self._rand_timer.isActive():
            self._rand_timer.stop()
            self.rand_start_btn.setText("Start Random")
            self.rand_status.setText("Stopped")
            return
        if not self._router_running:
            QMessageBox.warning(
                self,
                "Random Generator",
                "Please start router first, then start random generator.",
            )
            return
        interval = int(self.rand_interval_input.value())
        if interval <= 0:
            QMessageBox.warning(self, "Random Generator", "Invalid interval.")
            return
        if self.rand_min_input.value() > self.rand_max_input.value():
            QMessageBox.warning(self, "Random Generator", "Min cannot exceed max.")
            return
        if not self.rand_port_input.currentText().strip():
            QMessageBox.warning(
                self, "Random Generator", "No virtual source port available."
            )
            return
        if not self.rand_message_input.currentText().strip():
            QMessageBox.warning(self, "Random Generator", "Please select message.")
            return
        if not self.rand_signal_input.currentText().strip():
            QMessageBox.warning(self, "Random Generator", "Please select signal.")
            return
        self._rand_timer.start(interval)
        self.rand_start_btn.setText("Stop Random")
        self.rand_status.setText("Running")

    def _send_manual_random_value(self) -> None:
        port = self.rand_port_input.currentText().strip()
        message = self.rand_message_input.currentText().strip()
        signal = self.rand_signal_input.currentText().strip()
        if not port or not message or not signal:
            QMessageBox.warning(
                self, "Manual Send", "Please select port/message/signal first."
            )
            return
        value = float(self.rand_manual_value.value())
        if not self._router_running:
            QMessageBox.warning(self, "Manual Send", "Please start router first.")
            return
        self.inject_requested.emit(port, message, {signal: value}, False)

    def _on_random_tick(self) -> None:
        if not self._router_running:
            self._rand_timer.stop()
            self.rand_start_btn.setText("Start Random")
            self.rand_status.setText("Stopped")
            return
        port = self.rand_port_input.currentText().strip()
        message = self.rand_message_input.currentText().strip()
        signal = self.rand_signal_input.currentText().strip()
        if not port or not message or not signal:
            self._rand_timer.stop()
            self.rand_start_btn.setText("Start Random")
            self.rand_status.setText("Stopped")
            return
        min_v = float(self.rand_min_input.value())
        max_v = float(self.rand_max_input.value())
        value = random.uniform(min_v, max_v)
        self.rand_manual_value.setValue(value)
        self.inject_requested.emit(port, message, {signal: value}, True)
