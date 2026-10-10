from __future__ import annotations

from dataclasses import asdict
from typing import List

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
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

from can_gateway.core.models import HardwareConfig
from can_gateway.services.dbc_service import DbcService
from can_gateway.services.device_registry import DeviceRegistry
from can_gateway.ui.dialogs import DbcBrowserDialog, DevicePickDialog, HardwareDialog


class HardwareTab(QWidget):
    changed = Signal()

    def __init__(
        self, registry: DeviceRegistry, dbc_service: DbcService, parent=None
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._dbc = dbc_service
        self._hardware: List[HardwareConfig] = []

        self.table = QTableWidget(0, 12)
        self.table.setHorizontalHeaderLabels(
            [
                "Port Name",
                "Driver",
                "Channel",
                "Bitrate",
                "DataBitrate",
                "FD",
                "Database",
                "DB Mode",
                "DB Type",
                "Cluster",
                "Order",
                "Serial",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.scan_btn = QPushButton("Scan Device")
        self.add_virtual_btn = QPushButton("Add 4 Virtual Ports")
        self.browser_btn = QPushButton("DBC Browser")
        self.edit_btn = QPushButton("Edit")
        self.remove_btn = QPushButton("Remove")
        self.up_btn = QPushButton("Up")
        self.down_btn = QPushButton("Down")

        controls = QHBoxLayout()
        for btn in (
            self.scan_btn,
            self.add_virtual_btn,
            self.browser_btn,
            self.edit_btn,
            self.remove_btn,
            self.up_btn,
            self.down_btn,
        ):
            controls.addWidget(btn)
        controls.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.table)

        self.scan_btn.clicked.connect(self.scan_devices)
        self.add_virtual_btn.clicked.connect(self.add_virtual_ports)
        self.browser_btn.clicked.connect(self.open_dbc_browser)
        self.edit_btn.clicked.connect(self.edit_hardware)
        self.table.cellDoubleClicked.connect(self._edit_double_clicked_row)
        self.remove_btn.clicked.connect(self.remove_hardware)
        self.up_btn.clicked.connect(self.move_up)
        self.down_btn.clicked.connect(self.move_down)

    def set_hardware(self, items: List[HardwareConfig]) -> None:
        self._hardware = [HardwareConfig.from_dict(asdict(item)) for item in items]
        self._ensure_unique_names()
        self._normalize_order()
        self._refresh_table()

    def get_hardware(self) -> List[HardwareConfig]:
        self._normalize_order()
        return [HardwareConfig.from_dict(asdict(item)) for item in self._hardware]

    def hardware_names(self) -> List[str]:
        return [item.name for item in self._hardware]

    def open_dbc_browser(self) -> None:
        dialog = DbcBrowserDialog(self._dbc, self.hardware_names(), self)
        dialog.exec()

    def refresh_dbc_status(self) -> None:
        self._refresh_table()

    def scan_devices(self) -> None:
        devices, diagnostics = self._registry.scan_all_report()
        devices = [device for device in devices if not self._is_virtual_device(device)]
        if not devices:
            detail = ""
            if diagnostics:
                detail = "\n\nDiagnostics:\n" + "\n".join(diagnostics[:12])
            QMessageBox.information(
                self,
                "Scan Device",
                "No devices discovered. Check vendor driver/runtime installation."
                + detail,
            )
            return
        pick = DevicePickDialog(devices, self)
        if pick.exec() != DevicePickDialog.DialogCode.Accepted:
            return
        selected = pick.selected_devices()
        if not selected:
            return

        for device in selected:
            base_name = device.channel or device.display_name.replace(" ", "_")
            name = self._unique_name(base_name)
            extra = dict(device.capabilities)
            extra["device_id"] = device.device_id
            if extra.get("fd", "no").lower() == "yes":
                if str(device.driver or "").lower() == "testbase":
                    extra.setdefault("testbase_fd_sample_point_abr", "80.0")
                    extra.setdefault("testbase_fd_sample_point_dbr", "80.0")
                else:
                    extra.setdefault("vector_fd_sample_point_abr", "80.0")
                    extra.setdefault("vector_fd_sample_point_dbr", "80.0")
            cfg = HardwareConfig(
                name=name,
                driver=device.capabilities.get("registry_key", device.driver),
                channel=device.channel,
                fd=device.capabilities.get("fd", "no").lower() == "yes",
                serial=device.capabilities.get("serial", ""),
                order=len(self._hardware),
                extra=extra,
            )
            self._hardware.append(cfg)
        self._normalize_order()
        self._refresh_table()
        self.changed.emit()

    def _is_virtual_device(self, device) -> bool:
        driver = str(device.driver or "").lower()
        interface = str(device.capabilities.get("interface", "")).lower()
        vendor = str(device.capabilities.get("vendor", "")).lower()
        display_name = str(device.display_name or "").lower()
        return any(
            key in driver or key in interface or key in vendor or key in display_name
            for key in ("virtual", "sim-virtual")
        )

    def add_virtual_ports(self) -> None:
        for idx in range(1, 5):
            channel = f"SIM_CH{idx}"
            if any(item.channel == channel for item in self._hardware):
                continue
            self._hardware.append(
                HardwareConfig(
                    name=self._unique_name(f"VirtualPort{idx}"),
                    driver="sim-virtual",
                    channel=channel,
                    fd=True,
                    order=len(self._hardware),
                    extra={"interface": "sim-virtual", "vendor": "sim"},
                )
            )
        self._normalize_order()
        self._refresh_table()
        self.changed.emit()

    def add_hardware(self) -> None:
        dialog = HardwareDialog(
            driver_names=list(self._registry.drivers.keys()),
            base_order=len(self._hardware),
            parent=self,
            dbc_service=self._dbc,
        )
        if dialog.exec() != HardwareDialog.DialogCode.Accepted:
            return
        cfg = dialog.to_config()
        if self._name_taken(cfg.name):
            QMessageBox.warning(self, "Validation", "Port name must be unique.")
            return
        self._hardware.append(cfg)
        self._normalize_order()
        self._refresh_table()
        self.changed.emit()

    def edit_hardware(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        current = self._hardware[row]
        dialog = HardwareDialog(
            driver_names=list(self._registry.drivers.keys()),
            base_order=row,
            parent=self,
            initial=current,
            dbc_service=self._dbc,
        )
        if dialog.exec() != HardwareDialog.DialogCode.Accepted:
            return
        cfg = dialog.to_config()
        if self._name_taken(cfg.name, skip=row):
            QMessageBox.warning(self, "Validation", "Port name must be unique.")
            return
        merged_extra = dict(current.extra)
        merged_extra.update(cfg.extra)
        cfg.extra = merged_extra
        self._hardware[row] = cfg
        self._normalize_order()
        self._refresh_table()
        self.changed.emit()

    def _edit_double_clicked_row(self, row: int, _column: int) -> None:
        if row < 0 or row >= len(self._hardware):
            return
        self.table.selectRow(row)
        self.edit_hardware()

    def remove_hardware(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        self._hardware.pop(row)
        self._normalize_order()
        self._refresh_table()
        self.changed.emit()

    def move_up(self) -> None:
        row = self._selected_row()
        if row <= 0:
            return
        self._hardware[row - 1], self._hardware[row] = (
            self._hardware[row],
            self._hardware[row - 1],
        )
        self._normalize_order()
        self._refresh_table()
        self.table.selectRow(row - 1)
        self.changed.emit()

    def move_down(self) -> None:
        row = self._selected_row()
        if row < 0 or row >= len(self._hardware) - 1:
            return
        self._hardware[row + 1], self._hardware[row] = (
            self._hardware[row],
            self._hardware[row + 1],
        )
        self._normalize_order()
        self._refresh_table()
        self.table.selectRow(row + 1)
        self.changed.emit()

    def _selected_row(self) -> int:
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return -1
        return selected[0].row()

    def _name_taken(self, name: str, skip: int = -1) -> bool:
        for idx, item in enumerate(self._hardware):
            if idx == skip:
                continue
            if item.name == name:
                return True
        return False

    def _unique_name(self, base: str) -> str:
        candidate = base
        idx = 1
        while self._name_taken(candidate):
            idx += 1
            candidate = f"{base}_{idx}"
        return candidate

    def _ensure_unique_names(self) -> None:
        seen: set[str] = set()
        for item in self._hardware:
            base = item.name or "Port"
            name = base
            idx = 1
            while name in seen:
                idx += 1
                name = f"{base}_{idx}"
            item.name = name
            seen.add(name)

    def _normalize_order(self) -> None:
        for idx, item in enumerate(self._hardware):
            item.order = idx

    def _refresh_table(self) -> None:
        self.table.setRowCount(len(self._hardware))
        for row, item in enumerate(self._hardware):
            mode = self._dbc.mode_for_port(item.name)
            dbc_error = self._dbc.dbc_error_for_port(item.name)
            dbc_display = item.database_file_path()
            if dbc_error and dbc_display:
                dbc_display = f"! {dbc_display}"
            values = [
                item.name,
                item.driver,
                item.channel,
                str(item.bitrate),
                str(item.data_bitrate),
                "Yes" if item.fd else "No",
                dbc_display,
                mode,
                item.database_kind() or "",
                item.database_cluster(),
                str(item.order),
                item.serial,
            ]
            for col, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                if dbc_error and col in (6, 7):
                    table_item.setToolTip(dbc_error)
                    table_item.setForeground(QColor("#B42318"))
                self.table.setItem(row, col, table_item)
        self.table.resizeColumnsToContents()
