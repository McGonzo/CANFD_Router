from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from can_gateway.core.models import (
    AlgorithmKind,
    AlgorithmPreset,
    HardwareConfig,
    LengthStrategy,
    RouteAlgorithmBinding,
    RouteRule,
)
from can_gateway.drivers.base import DeviceInfo
from can_gateway.services.dbc_service import DbcService
from can_gateway.services.router_engine import evaluate_transform_expression


def _parse_can_id(value: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    return int(raw, 0)


def _parse_int_text(value: str, default: int = 0) -> int:
    raw = value.strip()
    if not raw:
        return int(default)
    return int(raw, 0)


def _sample_point_from_extra(extra: dict, primary: str, fallback: str) -> float:
    raw = extra.get(primary, extra.get(fallback, 80.0))
    try:
        value = float(str(raw).strip())
    except Exception:
        value = 80.0
    return min(90.0, max(50.0, value))


def _driver_supports_sample_point(driver_name: str) -> bool:
    return driver_name.strip().lower() in {"vector", "testbase"}


def _algorithm_kind_from_combo(value: object) -> AlgorithmKind:
    if isinstance(value, AlgorithmKind):
        return value
    text = str(value or "").strip() or AlgorithmKind.ROLLING_COUNTER.value
    return AlgorithmKind(text)


def _length_strategy_from_combo(value: object) -> LengthStrategy:
    if isinstance(value, LengthStrategy):
        return value
    text = str(value or "").strip() or LengthStrategy.TRUNCATE.value
    return LengthStrategy(text)


class HardwareDialog(QDialog):
    def __init__(
        self,
        driver_names: List[str],
        base_order: int,
        parent=None,
        initial=None,
        dbc_service: DbcService | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Port Configuration")
        self._dbc_service = dbc_service
        self._initial_extra = dict(getattr(initial, "extra", {}) or {})

        self.name_input = QLineEdit()
        self.driver_input = QComboBox()
        self.driver_input.addItems(sorted(driver_names))
        self.channel_input = QLineEdit()
        self.serial_input = QLineEdit()
        self.fd_input = QCheckBox("CAN FD")
        self.bitrate_input = QSpinBox()
        self.bitrate_input.setRange(10_000, 1_000_000)
        self.bitrate_input.setValue(500_000)
        self.data_bitrate_input = QSpinBox()
        self.data_bitrate_input.setRange(10_000, 8_000_000)
        self.data_bitrate_input.setValue(2_000_000)
        self.nominal_sample_point_input = QDoubleSpinBox()
        self.nominal_sample_point_input.setRange(50.0, 90.0)
        self.nominal_sample_point_input.setDecimals(1)
        self.nominal_sample_point_input.setSingleStep(0.5)
        self.nominal_sample_point_input.setSuffix(" %")
        self.nominal_sample_point_input.setValue(80.0)
        self.data_sample_point_input = QDoubleSpinBox()
        self.data_sample_point_input.setRange(50.0, 90.0)
        self.data_sample_point_input.setDecimals(1)
        self.data_sample_point_input.setSingleStep(0.5)
        self.data_sample_point_input.setSuffix(" %")
        self.data_sample_point_input.setValue(80.0)
        self.order_input = QSpinBox()
        self.order_input.setRange(0, 999)
        self.order_input.setValue(base_order)
        self.database_format_input = QComboBox()
        self.database_format_input.addItem("DBC", "dbc")
        self.database_format_input.addItem("ARXML", "arxml")
        self.database_format_input.currentIndexChanged.connect(
            self._on_database_format_changed
        )
        self.dbc_path_input = QLineEdit()
        self.dbc_browse_btn = QPushButton("Browse")
        self.dbc_browse_btn.clicked.connect(self._browse_database)
        self.cluster_input = QComboBox()

        form = QFormLayout()
        form.addRow("Port Name", self.name_input)
        form.addRow("Driver", self.driver_input)
        form.addRow("Device Channel", self.channel_input)
        form.addRow("Bitrate", self.bitrate_input)
        form.addRow("Data Bitrate", self.data_bitrate_input)
        form.addRow("Nominal Sample Point", self.nominal_sample_point_input)
        form.addRow("Data Sample Point", self.data_sample_point_input)
        form.addRow("Serial", self.serial_input)
        form.addRow("Order", self.order_input)
        form.addRow("", self.fd_input)
        form.addRow("Database Type", self.database_format_input)

        dbc_row = QVBoxLayout()
        dbc_row.addWidget(self.dbc_path_input)
        dbc_row.addWidget(self.dbc_browse_btn)
        form.addRow("Database Path", dbc_row)
        form.addRow("ARXML Cluster", self.cluster_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        self.driver_input.currentTextChanged.connect(self._refresh_sample_point_enabled)
        self.fd_input.toggled.connect(self._refresh_sample_point_enabled)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        if initial is not None:
            self.name_input.setText(initial.name)
            idx = self.driver_input.findText(initial.driver)
            if idx >= 0:
                self.driver_input.setCurrentIndex(idx)
            self.channel_input.setText(initial.channel)
            self.bitrate_input.setValue(initial.bitrate)
            self.data_bitrate_input.setValue(initial.data_bitrate)
            self.nominal_sample_point_input.setValue(
                _sample_point_from_extra(
                    initial.extra,
                    (
                        "testbase_fd_sample_point_abr"
                        if initial.driver.strip().lower() == "testbase"
                        else "vector_fd_sample_point_abr"
                    ),
                    "vector_fd_nominal_sample_point",
                )
            )
            self.data_sample_point_input.setValue(
                _sample_point_from_extra(
                    initial.extra,
                    (
                        "testbase_fd_sample_point_dbr"
                        if initial.driver.strip().lower() == "testbase"
                        else "vector_fd_sample_point_dbr"
                    ),
                    "vector_fd_data_sample_point",
                )
            )
            self.serial_input.setText(initial.serial)
            self.fd_input.setChecked(initial.fd)
            self.order_input.setValue(initial.order)
            kind = initial.database_kind()
            idx = self.database_format_input.findData(kind or "dbc")
            if idx >= 0:
                self.database_format_input.setCurrentIndex(idx)
                self.dbc_path_input.setText(initial.database_file_path())
                self._refresh_clusters(initial.database_cluster())
        self._on_database_format_changed()
        self._refresh_sample_point_enabled()

    def _browse_database(self) -> None:
        database_format = str(self.database_format_input.currentData() or "dbc")
        file_filter = (
            "ARXML Files (*.arxml *.xml);;All Files (*.*)"
            if database_format == "arxml"
            else "DBC Files (*.dbc);;All Files (*.*)"
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select network database",
            "",
            file_filter,
        )
        if path:
            self.dbc_path_input.setText(path)
            self._refresh_clusters()

    def _on_database_format_changed(self) -> None:
        is_arxml = str(self.database_format_input.currentData() or "dbc") == "arxml"
        self.cluster_input.setEnabled(is_arxml)
        if is_arxml:
            self._refresh_clusters()
        else:
            self.cluster_input.clear()

    def _refresh_sample_point_enabled(self) -> None:
        driver = self.driver_input.currentText().strip().lower()
        supports_sample_point = _driver_supports_sample_point(driver)
        enabled = bool(self.fd_input.isChecked()) and supports_sample_point
        if driver == "testbase" and not self.fd_input.isChecked():
            self.nominal_sample_point_input.setValue(75.0)
        reason = ""
        if not self.fd_input.isChecked():
            reason = (
                "TestBase classic CAN uses its fixed 75% sample point."
                if driver == "testbase"
                else "Sample point is applied only when CAN FD is enabled."
            )
        elif not supports_sample_point:
            reason = "Sample point is currently applied by Vector and TestBase CAN FD drivers."
        for widget in (
            self.nominal_sample_point_input,
            self.data_sample_point_input,
        ):
            widget.setEnabled(enabled)
            widget.setToolTip("" if enabled else reason)

    def _refresh_clusters(self, selected: str = "") -> None:
        if str(self.database_format_input.currentData() or "dbc") != "arxml":
            return
        current = selected or self.cluster_input.currentText().strip()
        self.cluster_input.clear()
        path = self.dbc_path_input.text().strip()
        if not path or self._dbc_service is None:
            return
        try:
            clusters = self._dbc_service.available_clusters(path)
        except Exception:
            clusters = []
        self.cluster_input.addItems(clusters)
        if current:
            idx = self.cluster_input.findText(current)
            if idx >= 0:
                self.cluster_input.setCurrentIndex(idx)

    def _on_accept(self) -> None:
        if not self.name_input.text().strip():
            QMessageBox.warning(self, "Validation", "Port name is required.")
            return
        if not self.channel_input.text().strip():
            QMessageBox.warning(self, "Validation", "Device channel is required.")
            return
        if str(self.database_format_input.currentData() or "dbc") == "arxml":
            if not self.dbc_path_input.text().strip():
                QMessageBox.warning(self, "Validation", "ARXML path is required.")
                return
            if not self.cluster_input.currentText().strip():
                QMessageBox.warning(self, "Validation", "ARXML cluster is required.")
                return
        self.accept()

    def to_config(self) -> HardwareConfig:
        database_format = str(self.database_format_input.currentData() or "dbc")
        database_path = self.dbc_path_input.text().strip()
        cluster_name = (
            self.cluster_input.currentText().strip()
            if database_format == "arxml"
            else ""
        )
        extra = dict(self._initial_extra)
        driver = self.driver_input.currentText().strip().lower()
        if driver == "testbase" and self.fd_input.isChecked():
            extra["testbase_fd_sample_point_abr"] = (
                f"{self.nominal_sample_point_input.value():.1f}"
            )
            extra["testbase_fd_sample_point_dbr"] = (
                f"{self.data_sample_point_input.value():.1f}"
            )
        elif driver == "vector":
            extra["vector_fd_sample_point_abr"] = (
                f"{self.nominal_sample_point_input.value():.1f}"
            )
            extra["vector_fd_sample_point_dbr"] = (
                f"{self.data_sample_point_input.value():.1f}"
            )
        return HardwareConfig(
            name=self.name_input.text().strip(),
            driver=self.driver_input.currentText().strip(),
            channel=self.channel_input.text().strip(),
            bitrate=int(self.bitrate_input.value()),
            data_bitrate=int(self.data_bitrate_input.value()),
            fd=bool(self.fd_input.isChecked()),
            serial=self.serial_input.text().strip(),
            order=int(self.order_input.value()),
            dbc_path=database_path if database_format == "dbc" else "",
            database_format=database_format,
            database_path=database_path,
            cluster_name=cluster_name,
            extra=extra,
        )


class AlgorithmPresetDialog(QDialog):
    def __init__(self, parent=None, initial: AlgorithmPreset | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Algorithm Preset")

        self.name_input = QLineEdit()
        self.kind_input = QComboBox()
        self.kind_input.addItem("Rolling Counter", AlgorithmKind.ROLLING_COUNTER.value)
        self.kind_input.addItem("CRC", AlgorithmKind.CRC.value)

        self.start_value_input = QLineEdit("0")
        self.step_input = QSpinBox()
        self.step_input.setRange(-4096, 4096)
        self.step_input.setValue(1)
        self.minimum_input = QLineEdit("0")
        self.maximum_input = QLineEdit("15")

        self.width_input = QSpinBox()
        self.width_input.setRange(1, 32)
        self.width_input.setValue(8)
        self.polynomial_input = QLineEdit("0x1D")
        self.initial_value_input = QLineEdit("0xFF")
        self.xor_out_input = QLineEdit("0x00")
        self.reflect_input_box = QCheckBox("Reflect Input Bytes")
        self.reflect_output_box = QCheckBox("Reflect Output CRC")
        self.exclude_signals_input = QLineEdit()
        self.exclude_signals_input.setPlaceholderText("e.g. Checksum,CRC")

        form = QFormLayout()
        form.addRow("Preset Name", self.name_input)
        form.addRow("Type", self.kind_input)
        form.addRow("RC Start", self.start_value_input)
        form.addRow("RC Step", self.step_input)
        form.addRow("RC Min", self.minimum_input)
        form.addRow("RC Max", self.maximum_input)
        form.addRow("CRC Width", self.width_input)
        form.addRow("Polynomial", self.polynomial_input)
        form.addRow("Initial Value", self.initial_value_input)
        form.addRow("Xor Out", self.xor_out_input)
        form.addRow("", self.reflect_input_box)
        form.addRow("", self.reflect_output_box)
        form.addRow("Zeroed Signals", self.exclude_signals_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.kind_input.currentIndexChanged.connect(self._refresh_enabled_state)

        if initial is not None:
            self.name_input.setText(initial.name)
            self.kind_input.setCurrentIndex(
                max(0, self.kind_input.findData(_algorithm_kind_from_combo(initial.kind).value))
            )
            self.start_value_input.setText(str(initial.start_value))
            self.step_input.setValue(int(initial.step))
            self.minimum_input.setText(str(initial.minimum))
            self.maximum_input.setText(str(initial.maximum))
            self.width_input.setValue(int(initial.width))
            self.polynomial_input.setText(hex(int(initial.polynomial)))
            self.initial_value_input.setText(hex(int(initial.initial_value)))
            self.xor_out_input.setText(hex(int(initial.xor_out)))
            self.reflect_input_box.setChecked(bool(initial.reflect_input))
            self.reflect_output_box.setChecked(bool(initial.reflect_output))
            self.exclude_signals_input.setText(
                ", ".join(initial.exclude_signals)
            )
        self._refresh_enabled_state()

    def _refresh_enabled_state(self) -> None:
        kind = _algorithm_kind_from_combo(self.kind_input.currentData())
        is_rc = kind == AlgorithmKind.ROLLING_COUNTER
        is_crc = kind == AlgorithmKind.CRC
        for widget in (
            self.start_value_input,
            self.step_input,
            self.minimum_input,
            self.maximum_input,
        ):
            widget.setEnabled(is_rc)
        for widget in (
            self.width_input,
            self.polynomial_input,
            self.initial_value_input,
            self.xor_out_input,
            self.reflect_input_box,
            self.reflect_output_box,
            self.exclude_signals_input,
        ):
            widget.setEnabled(is_crc)

    def _on_accept(self) -> None:
        if not self.name_input.text().strip():
            QMessageBox.warning(self, "Validation", "Preset name is required.")
            return
        try:
            if _algorithm_kind_from_combo(self.kind_input.currentData()) == AlgorithmKind.ROLLING_COUNTER:
                minimum = _parse_int_text(self.minimum_input.text(), 0)
                maximum = _parse_int_text(self.maximum_input.text(), 15)
                _ = _parse_int_text(self.start_value_input.text(), 0)
                if maximum < minimum:
                    raise ValueError("RC max must be greater than or equal to RC min.")
            else:
                _ = _parse_int_text(self.polynomial_input.text(), 0x1D)
                _ = _parse_int_text(self.initial_value_input.text(), 0xFF)
                _ = _parse_int_text(self.xor_out_input.text(), 0x00)
        except Exception as exc:
            QMessageBox.warning(self, "Validation", str(exc))
            return
        self.accept()

    def to_config(self) -> AlgorithmPreset:
        exclude_signals = [
            item.strip()
            for item in self.exclude_signals_input.text().split(",")
            if item.strip()
        ]
        return AlgorithmPreset(
            name=self.name_input.text().strip(),
            kind=_algorithm_kind_from_combo(self.kind_input.currentData()),
            width=int(self.width_input.value()),
            start_value=_parse_int_text(self.start_value_input.text(), 0),
            step=int(self.step_input.value()),
            minimum=_parse_int_text(self.minimum_input.text(), 0),
            maximum=_parse_int_text(self.maximum_input.text(), 15),
            polynomial=_parse_int_text(self.polynomial_input.text(), 0x1D),
            initial_value=_parse_int_text(self.initial_value_input.text(), 0xFF),
            xor_out=_parse_int_text(self.xor_out_input.text(), 0x00),
            reflect_input=bool(self.reflect_input_box.isChecked()),
            reflect_output=bool(self.reflect_output_box.isChecked()),
            exclude_signals=exclude_signals,
        )


class SignalMapDialog(QDialog):
    def __init__(
        self,
        port_names: List[str],
        dbc_service: DbcService,
        algorithm_presets: List[AlgorithmPreset] | None = None,
        parent=None,
        initial: RouteRule | None = None,
        fixed_source: str = "",
        fixed_target: str = "",
        fixed_source_message: str = "",
        fixed_source_signal: str = "",
        fixed_target_message: str = "",
        fixed_target_signal: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Signal Mapping Rule")
        self._dbc = dbc_service
        self._algorithm_presets = list(algorithm_presets or [])
        self._port_names = list(port_names)

        self.enabled_input = QCheckBox("Enabled")
        self.enabled_input.setChecked(True)
        self.name_input = QLineEdit()

        self.source_port_input = QComboBox()
        self.source_port_input.addItem("None", "")
        self.source_port_input.addItems(port_names)
        self.source_msg_input = QLineEdit()
        self.source_msg_input.setReadOnly(True)
        self.source_sig_input = QLineEdit()
        self.source_sig_input.setReadOnly(True)
        self.source_search_btn = QPushButton("Search")

        self.target_port_input = QComboBox()
        self.target_port_input.addItems(port_names)
        self.target_msg_input = QLineEdit()
        self.target_msg_input.setReadOnly(True)
        self.target_sig_input = QLineEdit()
        self.target_sig_input.setReadOnly(True)
        self.target_search_btn = QPushButton("Search")
        self.transform_expr_input = QLineEdit("x")

        self.scale_input = QDoubleSpinBox()
        self.scale_input.setRange(-1_000_000, 1_000_000)
        self.scale_input.setDecimals(6)
        self.scale_input.setValue(1.0)
        self.offset_input = QDoubleSpinBox()
        self.offset_input.setRange(-1_000_000, 1_000_000)
        self.offset_input.setDecimals(6)
        self.offset_input.setValue(0.0)

        self.source_id_input = QLineEdit()
        self.source_id_input.setPlaceholderText("optional, e.g. 0x100")
        self.target_id_input = QLineEdit()
        self.target_id_input.setPlaceholderText("optional, e.g. 0x200")

        self.length_strategy_input = QComboBox()
        for strategy in LengthStrategy:
            self.length_strategy_input.addItem(strategy.value, strategy.value)
        self.tx_interval_input = QSpinBox()
        self.tx_interval_input.setRange(10, 60_000)
        self.tx_interval_input.setValue(100)
        self.force_tx_interval_input = QCheckBox("Force fallback interval")

        self.rc_preset_input = QComboBox()
        self.rc_signal_input = QComboBox()
        self.crc_preset_input = QComboBox()
        self.crc_signal_input = QComboBox()
        self._populate_algorithm_presets()

        source_sig_row = QHBoxLayout()
        source_sig_row.addWidget(self.source_sig_input)
        source_sig_row.addWidget(self.source_search_btn)

        target_sig_row = QHBoxLayout()
        target_sig_row.addWidget(self.target_sig_input)
        target_sig_row.addWidget(self.target_search_btn)

        form = QFormLayout()
        form.addRow("", self.enabled_input)
        form.addRow("Rule Name", self.name_input)
        form.addRow("Source Port", self.source_port_input)
        form.addRow("Source Message", self.source_msg_input)
        form.addRow("Source Signal", source_sig_row)
        form.addRow("Target Port", self.target_port_input)
        form.addRow("Target Message", self.target_msg_input)
        form.addRow("Target Signal", target_sig_row)
        form.addRow("Transform(x)", self.transform_expr_input)
        form.addRow("Scale", self.scale_input)
        form.addRow("Offset", self.offset_input)
        form.addRow("Source ID Filter", self.source_id_input)
        form.addRow("Target ID Override", self.target_id_input)
        form.addRow("Length Strategy", self.length_strategy_input)
        form.addRow("Fallback Interval(ms)", self.tx_interval_input)
        form.addRow("", self.force_tx_interval_input)
        form.addRow("RC Preset", self.rc_preset_input)
        form.addRow("RC Signal", self.rc_signal_input)
        form.addRow("CRC Preset", self.crc_preset_input)
        form.addRow("CRC Signal", self.crc_signal_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.source_port_input.currentTextChanged.connect(self._clear_source_selection)
        self.source_port_input.currentIndexChanged.connect(self._refresh_source_enabled_state)
        self.target_port_input.currentTextChanged.connect(self._clear_target_selection)
        self.force_tx_interval_input.stateChanged.connect(self._refresh_source_enabled_state)
        self.source_search_btn.clicked.connect(self._select_source_signal)
        self.target_search_btn.clicked.connect(self._select_target_signal)

        self._clear_source_selection()
        self._clear_target_selection()

        if fixed_source:
            self.source_port_input.setCurrentText(fixed_source)
            self.source_port_input.setEnabled(False)
        if fixed_target:
            self.target_port_input.setCurrentText(fixed_target)
            self.target_port_input.setEnabled(False)
        if fixed_source_message:
            self.source_msg_input.setText(fixed_source_message)
            self.source_msg_input.setEnabled(False)
        if fixed_source_signal:
            self.source_sig_input.setText(fixed_source_signal)
            self.source_sig_input.setEnabled(False)
            self.source_search_btn.setEnabled(False)
        if fixed_target_message:
            self.target_msg_input.setText(fixed_target_message)
            self.target_msg_input.setEnabled(False)
        if fixed_target_signal:
            self.target_sig_input.setText(fixed_target_signal)
            self.target_sig_input.setEnabled(False)
            self.target_search_btn.setEnabled(False)

        if initial is not None:
            self.enabled_input.setChecked(initial.enabled)
            self.name_input.setText(initial.name)
            if initial.source_port:
                self.source_port_input.setCurrentText(initial.source_port)
            else:
                self.source_port_input.setCurrentIndex(0)
            self.source_msg_input.setText(initial.source_message)
            self.source_sig_input.setText(initial.source_signal)
            self.target_port_input.setCurrentText(initial.target_port)
            self.target_msg_input.setText(initial.target_message)
            self.target_sig_input.setText(initial.target_signal)
            self.transform_expr_input.setText(initial.transform_expr or "x")
            self.scale_input.setValue(initial.scale)
            self.offset_input.setValue(initial.offset)
            if initial.source_id is not None:
                self.source_id_input.setText(f"0x{initial.source_id:X}")
            if initial.target_id is not None:
                self.target_id_input.setText(f"0x{initial.target_id:X}")
            self.length_strategy_input.setCurrentText(
                _length_strategy_from_combo(initial.length_strategy).value
            )
            self.tx_interval_input.setValue(max(10, int(initial.tx_interval_ms)))
            self.force_tx_interval_input.setChecked(bool(initial.force_tx_interval))
            if initial.rc_binding is not None:
                self.rc_preset_input.setCurrentText(initial.rc_binding.preset_name)
                self.rc_signal_input.setCurrentText(initial.rc_binding.target_signal)
            if initial.crc_binding is not None:
                self.crc_preset_input.setCurrentText(initial.crc_binding.preset_name)
                self.crc_signal_input.setCurrentText(initial.crc_binding.target_signal)
        self._refresh_target_algorithm_signals()
        self._refresh_source_enabled_state()

    def _selected_source_port(self) -> str:
        data = self.source_port_input.currentData()
        if data is not None:
            return str(data).strip()
        text = self.source_port_input.currentText().strip()
        if text.lower() == "none":
            return ""
        return text

    def _clear_source_selection(self) -> None:
        self.source_msg_input.clear()
        self.source_sig_input.clear()

    def _refresh_source_enabled_state(self) -> None:
        has_source = bool(self._selected_source_port())
        self.source_msg_input.setEnabled(has_source)
        self.source_sig_input.setEnabled(has_source)
        self.source_search_btn.setEnabled(has_source)
        self.source_id_input.setEnabled(has_source)
        self.tx_interval_input.setEnabled(not has_source or self.force_tx_interval_input.isChecked())

    def _clear_target_selection(self) -> None:
        self.target_msg_input.clear()
        self.target_sig_input.clear()
        self._refresh_target_algorithm_signals()

    def _populate_algorithm_presets(self) -> None:
        self.rc_preset_input.clear()
        self.crc_preset_input.clear()
        self.rc_preset_input.addItem("", "")
        self.crc_preset_input.addItem("", "")
        for preset in self._algorithm_presets:
            if preset.kind == AlgorithmKind.ROLLING_COUNTER:
                self.rc_preset_input.addItem(preset.name, preset.name)
            elif preset.kind == AlgorithmKind.CRC:
                self.crc_preset_input.addItem(preset.name, preset.name)

    def _refresh_target_algorithm_signals(self) -> None:
        port_name = self.target_port_input.currentText()
        message_name = self.target_msg_input.text().strip()
        current_rc = self.rc_signal_input.currentText()
        current_crc = self.crc_signal_input.currentText()
        signal_names = self._dbc.list_signals(port_name, message_name) if message_name else []
        self.rc_signal_input.clear()
        self.rc_signal_input.addItem("", "")
        self.rc_signal_input.addItems(signal_names)
        if current_rc:
            self.rc_signal_input.setCurrentText(current_rc)
        self.crc_signal_input.clear()
        self.crc_signal_input.addItem("", "")
        self.crc_signal_input.addItems(signal_names)
        if current_crc:
            self.crc_signal_input.setCurrentText(current_crc)

    def _select_source_signal(self) -> None:
        port_name = self._selected_source_port()
        if not port_name:
            QMessageBox.warning(self, "Select Source", "Please select source port first.")
            return
        dialog = SignalSearchDialog(
            dbc_service=self._dbc,
            port_names=[port_name],
            title="Select Source Signal",
            default_port=port_name,
            parent=self,
        )
        if dialog.exec() != SignalSearchDialog.DialogCode.Accepted:
            return
        selected = dialog.selected()
        if not isinstance(selected, dict):
            return
        self.source_msg_input.setText(str(selected.get("message", "")))
        self.source_sig_input.setText(str(selected.get("signal", "")))

    def _select_target_signal(self) -> None:
        port_name = self.target_port_input.currentText().strip()
        if not port_name:
            QMessageBox.warning(self, "Select Target", "Please select target port first.")
            return
        dialog = SignalSearchDialog(
            dbc_service=self._dbc,
            port_names=[port_name],
            title="Select Target Signal",
            default_port=port_name,
            parent=self,
        )
        if dialog.exec() != SignalSearchDialog.DialogCode.Accepted:
            return
        selected = dialog.selected()
        if not isinstance(selected, dict):
            return
        self.target_msg_input.setText(str(selected.get("message", "")))
        self.target_sig_input.setText(str(selected.get("signal", "")))
        self._refresh_target_algorithm_signals()

    def _on_accept(self) -> None:
        has_source = bool(self._selected_source_port())
        required = [
            self.name_input.text().strip(),
            self.target_port_input.currentText().strip(),
            self.target_msg_input.text().strip(),
        ]
        if has_source:
            required.extend(
                [
                    self.source_port_input.currentText().strip(),
                    self.source_msg_input.text().strip(),
                    self.source_sig_input.text().strip(),
                    self.target_sig_input.text().strip(),
                ]
            )
        if any(not x for x in required):
            QMessageBox.warning(self, "Validation", "Please complete required mapping fields.")
            return
        if has_source and self.source_port_input.currentText() == self.target_port_input.currentText():
            QMessageBox.warning(
                self, "Validation", "Source and target port should be different."
            )
            return
        try:
            if has_source:
                _ = _parse_can_id(self.source_id_input.text())
            _ = _parse_can_id(self.target_id_input.text())
            if has_source:
                _ = evaluate_transform_expression(
                    self.transform_expr_input.text().strip() or "x",
                    1.0,
                )
        except Exception as exc:
            QMessageBox.warning(self, "Validation", str(exc))
            return
        rc_preset = self.rc_preset_input.currentText().strip()
        rc_signal = self.rc_signal_input.currentText().strip()
        crc_preset = self.crc_preset_input.currentText().strip()
        crc_signal = self.crc_signal_input.currentText().strip()
        if bool(rc_preset) != bool(rc_signal):
            QMessageBox.warning(
                self,
                "Validation",
                "Rolling counter preset and signal must be configured together.",
            )
            return
        if bool(crc_preset) != bool(crc_signal):
            QMessageBox.warning(
                self,
                "Validation",
                "CRC preset and signal must be configured together.",
            )
            return
        signal_names = [
            name
            for name in (
                self.target_sig_input.text().strip(),
                rc_signal,
                crc_signal,
            )
            if name
        ]
        if len(signal_names) != len(set(signal_names)):
            QMessageBox.warning(
                self,
                "Validation",
                "Target signal, RC signal and CRC signal must be different.",
            )
            return
        if not has_source:
            if not (rc_preset or crc_preset or self.target_sig_input.text().strip()):
                QMessageBox.warning(
                    self,
                    "Validation",
                    "For direct output rows, configure a target signal or RC/CRC binding.",
                )
                return
            if int(self.tx_interval_input.value()) <= 0:
                QMessageBox.warning(
                    self,
                    "Validation",
                    "Direct TX interval must be greater than 0.",
                )
                return
        self.accept()

    def to_config(self) -> RouteRule:
        rc_binding = None
        if self.rc_preset_input.currentText().strip() and self.rc_signal_input.currentText().strip():
            rc_binding = RouteAlgorithmBinding(
                preset_name=self.rc_preset_input.currentText().strip(),
                target_signal=self.rc_signal_input.currentText().strip(),
            )
        crc_binding = None
        if self.crc_preset_input.currentText().strip() and self.crc_signal_input.currentText().strip():
            crc_binding = RouteAlgorithmBinding(
                preset_name=self.crc_preset_input.currentText().strip(),
                target_signal=self.crc_signal_input.currentText().strip(),
            )
        return RouteRule(
            enabled=bool(self.enabled_input.isChecked()),
            name=self.name_input.text().strip(),
            source_port=self._selected_source_port(),
            source_message=self.source_msg_input.text().strip() if bool(self._selected_source_port()) else "",
            source_signal=self.source_sig_input.text().strip() if bool(self._selected_source_port()) else "",
            target_port=self.target_port_input.currentText().strip(),
            target_message=self.target_msg_input.text().strip(),
            target_signal=self.target_sig_input.text().strip(),
            transform_expr=self.transform_expr_input.text().strip() or "x",
            scale=float(self.scale_input.value()),
            offset=float(self.offset_input.value()),
            source_id=_parse_can_id(self.source_id_input.text()) if bool(self._selected_source_port()) else None,
            target_id=_parse_can_id(self.target_id_input.text()),
            length_strategy=_length_strategy_from_combo(self.length_strategy_input.currentData()),
            tx_interval_ms=int(self.tx_interval_input.value()),
            force_tx_interval=bool(self.force_tx_interval_input.isChecked()),
            rc_binding=rc_binding,
            crc_binding=crc_binding,
        )


class SignalSearchDialog(QDialog):
    def __init__(
        self,
        dbc_service: DbcService,
        port_names: List[str],
        title: str,
        default_port: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(860, 560)
        self._dbc = dbc_service
        self._selected: dict | None = None

        self.port_filter = QComboBox()
        for name in sorted(port_names):
            self.port_filter.addItem(name, name)
        if default_port:
            self.port_filter.setCurrentText(default_port)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search message/signal")

        top = QHBoxLayout()
        top.addWidget(QLabel("Port"))
        top.addWidget(self.port_filter)
        top.addWidget(QLabel("Search"))
        top.addWidget(self.search_input, 1)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Port", "Message", "Signal", "Frame ID"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.cellDoubleClicked.connect(lambda *_: self._accept_selected())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_selected)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table)
        layout.addWidget(buttons)

        self.port_filter.currentIndexChanged.connect(self._refresh)
        self.search_input.textChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        port = str(self.port_filter.currentData() or "")
        rows = self._dbc.list_signal_rows(port) if port else []
        key = self.search_input.text().strip().lower()
        if key:
            rows = [
                row
                for row in rows
                if key in row.message_name.lower() or key in row.signal_name.lower()
            ]
        self.table.setRowCount(len(rows))
        for idx, row in enumerate(rows):
            payload = {
                "port": row.port_name,
                "message": row.message_name,
                "signal": row.signal_name,
                "frame_id": row.frame_id,
            }
            values = [
                row.port_name,
                row.message_name,
                row.signal_name,
                f"0x{row.frame_id:X}",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, payload)
                self.table.setItem(idx, col, item)
        self.table.resizeColumnsToContents()
        if rows:
            self.table.selectRow(0)

    def _accept_selected(self) -> None:
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            QMessageBox.warning(self, "Select Signal", "Please select one signal row.")
            return
        row = selected[0].row()
        item = self.table.item(row, 0)
        if item is None:
            QMessageBox.warning(self, "Select Signal", "Invalid selection.")
            return
        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            QMessageBox.warning(self, "Select Signal", "Invalid selection.")
            return
        self._selected = payload
        self.accept()

    def selected(self) -> dict | None:
        return self._selected


class DbcBrowserDialog(QDialog):
    def __init__(self, dbc_service: DbcService, port_names: List[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("DBC Browser")
        self.resize(1060, 680)
        self._dbc = dbc_service
        self._port_names = list(port_names)

        self.port_filter = QComboBox()
        self.port_filter.addItem("All Ports", "")
        for port_name in sorted(self._port_names):
            self.port_filter.addItem(port_name, port_name)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by port/message/signal")
        self.refresh_btn = QPushButton("Refresh")

        top = QHBoxLayout()
        top.addWidget(QLabel("Port"))
        top.addWidget(self.port_filter)
        top.addWidget(QLabel("Search"))
        top.addWidget(self.search_input, 1)
        top.addWidget(self.refresh_btn)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "Port",
                "Message",
                "Signal",
                "Frame ID",
                "DLC",
                "Start",
                "Length",
                "Unit",
                "DBC Mode",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Node", "Frame ID", "DLC", "Start", "Length", "Unit", "Mode"])

        self.view_tabs = QTabWidget()
        table_page = QWidget()
        table_layout = QVBoxLayout(table_page)
        table_layout.addWidget(self.table)
        tree_page = QWidget()
        tree_layout = QVBoxLayout(tree_page)
        tree_layout.addWidget(self.tree)
        self.view_tabs.addTab(table_page, "Table")
        self.view_tabs.addTab(tree_page, "Tree")

        self.note = QPlainTextEdit()
        self.note.setReadOnly(True)
        self.note.setMaximumHeight(140)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.view_tabs, 1)
        layout.addWidget(QLabel("Parse Notes"))
        layout.addWidget(self.note)
        layout.addWidget(buttons)

        self.port_filter.currentIndexChanged.connect(self._refresh)
        self.search_input.textChanged.connect(self._refresh)
        self.refresh_btn.clicked.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        filter_port = str(self.port_filter.currentData() or "")
        keyword = self.search_input.text().strip().lower()
        ports = [filter_port] if filter_port else sorted(self._port_names)

        rows = []
        notes: List[str] = []
        tree_rows = []
        for port in ports:
            mode = self._dbc.mode_for_port(port)
            warning = self._dbc.warning_for_port(port).strip()
            if warning:
                notes.append(f"[{port}] strict parse fallback: {warning}")
            for row in self._dbc.list_signal_rows(port):
                text = f"{row.port_name} {row.message_name} {row.signal_name}".lower()
                if keyword and keyword not in text:
                    continue
                rows.append((row, mode))
            tree_rows.append((port, mode, self._dbc.list_messages(port)))

        self.table.setRowCount(len(rows))
        for idx, (row, mode) in enumerate(rows):
            values = [
                row.port_name,
                row.message_name,
                row.signal_name,
                f"0x{row.frame_id:X}",
                str(row.message_length),
                str(row.start),
                str(row.length),
                row.unit,
                mode,
            ]
            for col, value in enumerate(values):
                self.table.setItem(idx, col, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()

        self.tree.clear()
        for port, mode, messages in tree_rows:
            port_item = QTreeWidgetItem([port, "", "", "", "", "", mode])
            message_count = 0
            for message in messages:
                match_message = keyword and keyword in message.name.lower()
                signal_items = []
                for signal in message.signals:
                    s_text = f"{port} {message.name} {signal.name}".lower()
                    if keyword and (not match_message) and keyword not in s_text:
                        continue
                    signal_items.append(signal)
                if keyword and not match_message and not signal_items:
                    continue
                msg_item = QTreeWidgetItem(
                    [message.name, f"0x{message.frame_id:X}", str(message.length), "", "", "", ""]
                )
                for signal in signal_items:
                    sig_item = QTreeWidgetItem(
                        [
                            signal.name,
                            "",
                            "",
                            str(signal.start),
                            str(signal.length),
                            signal.unit,
                            "",
                        ]
                    )
                    msg_item.addChild(sig_item)
                port_item.addChild(msg_item)
                message_count += 1
            if message_count > 0 or not keyword:
                self.tree.addTopLevelItem(port_item)
                port_item.setExpanded(True)
                for i in range(port_item.childCount()):
                    port_item.child(i).setExpanded(True)

        self.tree.resizeColumnToContents(0)
        self.note.setPlainText("\n".join(notes) if notes else "No parser warnings.")


class DevicePickDialog(QDialog):
    def __init__(self, devices: List[DeviceInfo], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Scanned Device Ports")
        self._devices = devices
        self.list_widget = QListWidget()
        for idx, device in enumerate(devices):
            item = QListWidgetItem(
                f"[{device.driver}] {device.display_name} (channel={device.channel})"
            )
            item.setData(Qt.ItemDataRole.UserRole, idx)
            self.list_widget.addItem(item)
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        if devices:
            self.list_widget.setCurrentRow(0)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.list_widget)
        layout.addWidget(buttons)

    def selected_devices(self) -> List[DeviceInfo]:
        out: List[DeviceInfo] = []
        for item in self.list_widget.selectedItems():
            idx = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(idx, int) and 0 <= idx < len(self._devices):
                out.append(self._devices[idx])
        return out
