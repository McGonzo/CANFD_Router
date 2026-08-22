from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from can_gateway.core.models import AppConfig, RuntimeOptions
from can_gateway.services import ConfigStore, DbcService, DeviceRegistry, RouterEngine
from can_gateway.services.config_store import find_invalid_route_ports
from can_gateway.ui.algorithms_tab import AlgorithmsTab
from can_gateway.ui.direct_can_window import DirectCanToolWindow
from can_gateway.ui.diagnostics_tab import DiagnosticsTab
from can_gateway.ui.hardware_tab import HardwareTab
from can_gateway.ui.routes_tab import RoutesTab
from can_gateway.ui.trace_tab import TraceTab
from can_gateway.utils import app_path, resource_path, save_path, setup_logging


class GatewayMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CAN/CANFD Signal Gateway Router")
        self.resize(1360, 860)

        self.logger = setup_logging()
        self.registry = DeviceRegistry(self.logger.getChild("device_registry"))
        self.dbc_service = DbcService()
        self.config_store = ConfigStore()
        self._startup_warning = ""

        self.session_path = str(save_path(".last_session.json"))
        self.autosave_config_path = str(save_path("autosave_config.json"))
        self._session_state = self._load_session_state()
        self.manual_config_path = self._load_last_manual_config_path()
        self.config_path = self._load_last_config_path()
        self.runtime_options = RuntimeOptions()
        self.router: RouterEngine | None = None
        self.direct_can_window: DirectCanToolWindow | None = None
        self._direct_can_state = dict(self._session_state.get("direct_can_tool", {}))

        self.tabs = QTabWidget()
        self.hardware_tab = HardwareTab(self.registry, self.dbc_service, self)
        self.algorithms_tab = AlgorithmsTab(self)
        self.routes_tab = RoutesTab(self.dbc_service, self)
        self.trace_tab = TraceTab(self.dbc_service, self)
        self.diagnostics_tab = DiagnosticsTab(self.registry, self)
        self.tabs.addTab(self.hardware_tab, "Hardware Config")
        self.tabs.addTab(self.algorithms_tab, "Algorithm Config")
        self.tabs.addTab(self.routes_tab, "Route Config")
        self.tabs.addTab(self.diagnostics_tab, "Diagnostics")
        self.tabs.addTab(self.trace_tab, "Trace")
        self.tabs.setStyleSheet(
            "QTabBar::tab { min-height: 22px; }"
        )

        self.load_btn = QPushButton("Load Config")
        self.save_btn = QPushButton("Save Config")
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stats_label = QLabel("RX:0 TX:0 Drop:0 Err:0")
        self.mode_label = QLabel("Stopped")

        controls = QHBoxLayout()
        controls.addWidget(self.load_btn)
        controls.addWidget(self.save_btn)
        controls.addStretch(1)
        controls.addWidget(self.mode_label)
        controls.addWidget(self.stats_label)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(self.tabs)
        layout.addLayout(controls)
        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar(self))

        self.stats_timer = QTimer(self)
        self.stats_timer.setInterval(1000)
        self.stats_timer.timeout.connect(self._update_stats)

        self.load_btn.clicked.connect(self.load_config)
        self.save_btn.clicked.connect(self.save_config)
        self.start_btn.clicked.connect(self.start_router)
        self.stop_btn.clicked.connect(self.stop_router)
        self.hardware_tab.changed.connect(self._on_hardware_changed)
        self.routes_tab.changed.connect(self._on_routes_changed)
        self.algorithms_tab.changed.connect(self._on_algorithms_changed)
        self.routes_tab.set_router_running(False)

        self._build_menu()
        self._load_initial()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        tools_menu = self.menuBar().addMenu("Tools")
        load_action = QAction("Load Config", self)
        save_action = QAction("Save Config", self)
        direct_can_action = QAction("Direct CAN Tool", self)
        exit_action = QAction("Exit", self)
        load_action.triggered.connect(self.load_config)
        save_action.triggered.connect(self.save_config)
        direct_can_action.triggered.connect(self.open_direct_can_tool)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(load_action)
        file_menu.addAction(save_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)
        tools_menu.addAction(direct_can_action)

    def _default_config_path(self) -> str:
        writable = app_path("configs", "default_config.json")
        if not writable.exists():
            bundled = resource_path("configs", "default_config.json")
            if bundled.exists():
                writable.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(bundled, writable)
        return str(writable)

    def _load_session_state(self) -> dict:
        session_file = Path(self.session_path)
        legacy_session_file = app_path("configs", ".last_session.json")
        candidates = [session_file]
        if legacy_session_file != session_file:
            candidates.append(legacy_session_file)
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                raw = candidate.read_text(encoding="utf-8")
                data = json.loads(raw)
            except Exception:
                continue
            if candidate != session_file:
                try:
                    session_file.parent.mkdir(parents=True, exist_ok=True)
                    session_file.write_text(raw, encoding="utf-8")
                except Exception:
                    pass
            return data if isinstance(data, dict) else {}
        return {}

    def _load_last_config_path(self) -> str:
        autosave = Path(self.autosave_config_path)
        if autosave.exists():
            autosave_cfg = self.config_store.load(str(autosave))
            autosave_issues = find_invalid_route_ports(autosave_cfg)
            if autosave_issues and self.manual_config_path:
                manual_cfg = self.config_store.load(self.manual_config_path)
                if not find_invalid_route_ports(manual_cfg):
                    self._startup_warning = (
                        "Autosave config referenced missing ports, so the last manual config was loaded instead.\n"
                        + "\n".join(autosave_issues[:12])
                    )
                    return self.manual_config_path
            return str(autosave.resolve())
        if self.manual_config_path:
            return self.manual_config_path
        path = str(self._session_state.get("config_path", "")).strip()
        candidate = Path(path) if path else None
        if candidate and candidate.exists():
            return str(candidate.resolve())
        return self._default_config_path()

    def _load_last_manual_config_path(self) -> str:
        path = str(
            self._session_state.get(
                "manual_config_path",
                self._session_state.get("config_path", ""),
            )
        ).strip()
        if not path:
            return ""
        candidate = Path(path)
        if not candidate.exists():
            return ""
        return str(candidate.resolve())

    def _save_session_state(self) -> None:
        session_file = Path(self.session_path)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "config_path": self.config_path,
            "manual_config_path": self.manual_config_path,
            "direct_can_tool": self._direct_can_state,
        }
        session_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _store_direct_can_state(self, state: dict) -> None:
        self._direct_can_state = dict(state) if isinstance(state, dict) else {}
        self._save_session_state()

    def _autosave_current_config(self) -> None:
        cfg = self._collect_config()
        autosave = Path(self.autosave_config_path)
        self.config_store.save(str(autosave), cfg)
        self.config_path = str(autosave.resolve())
        self._save_session_state()

    def _load_initial(self) -> None:
        cfg = self.config_store.load(self.config_path)
        self._apply_config(cfg)
        if self._startup_warning:
            QMessageBox.warning(self, "Config Recovery", self._startup_warning)
        self.statusBar().showMessage(f"Loaded config: {self.config_path}", 5000)

    def _apply_config(self, cfg: AppConfig) -> None:
        self.runtime_options = cfg.runtime
        self.hardware_tab.set_hardware(cfg.hardware)
        self.algorithms_tab.set_algorithms(cfg.algorithms)
        self._sync_hardware_and_dbc()
        self.routes_tab.set_algorithms(cfg.algorithms)
        self.routes_tab.set_routes(cfg.routes)
        self._sync_direct_can_tool()

    def _collect_config(self) -> AppConfig:
        return AppConfig(
            hardware=self.hardware_tab.get_hardware(),
            algorithms=self.algorithms_tab.get_algorithms(),
            routes=self.routes_tab.get_routes(),
            runtime=self.runtime_options,
        )

    def _sync_hardware_and_dbc(self) -> None:
        hardware = self.hardware_tab.get_hardware()
        errors = self.dbc_service.sync_hardware(hardware, raise_on_error=False)
        if errors:
            self.logger.warning("DBC load warning: %s", " | ".join(errors))
            self.statusBar().showMessage(
                "DBC load warning: check ! marks in Hardware Config.", 5000
            )
        self.hardware_tab.refresh_dbc_status()
        self.routes_tab.set_hardware(hardware)
        self.trace_tab.set_ports([item.name for item in hardware])
        self.diagnostics_tab.set_hardware(hardware)
        if self.direct_can_window is not None:
            self.direct_can_window.set_hardware(hardware)

    def _on_hardware_changed(self) -> None:
        self._sync_hardware_and_dbc()

    def _on_routes_changed(self) -> None:
        if self.router is not None and self.router.is_running:
            self.router.update_config(self._collect_config())

    def _on_algorithms_changed(self) -> None:
        self.routes_tab.set_algorithms(self.algorithms_tab.get_algorithms())
        if self.direct_can_window is not None:
            self.direct_can_window.set_algorithms(self.algorithms_tab.get_algorithms())
        if self.router is not None and self.router.is_running:
            self.router.update_config(self._collect_config())

    def _sync_direct_can_tool(self) -> None:
        if self.direct_can_window is None:
            return
        self.direct_can_window.set_hardware(self.hardware_tab.get_hardware())
        self.direct_can_window.set_algorithms(self.algorithms_tab.get_algorithms())

    def open_direct_can_tool(self) -> None:
        if self.direct_can_window is None:
            self.direct_can_window = DirectCanToolWindow(
                registry=self.registry,
                dbc_service=self.dbc_service,
                parent=self,
            )
        self._sync_direct_can_tool()
        if self._direct_can_state:
            self.direct_can_window.restore_state(self._direct_can_state)
        if self.router is not None and self.router.is_running:
            QMessageBox.information(
                self,
                "Direct CAN Tool",
                "Router is currently running. Stop the router before opening the same hardware port in direct mode.",
            )
        self.direct_can_window.show()
        self.direct_can_window.raise_()
        self.direct_can_window.activateWindow()

    def load_config(self) -> None:
        initial_dir = str(Path(self.manual_config_path or self.config_path).parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Config", initial_dir, "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            cfg = self.config_store.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Load", f"Failed to load config:\n{exc}")
            return
        self.config_path = str(Path(path).resolve())
        self.manual_config_path = self.config_path
        self._save_session_state()
        self._apply_config(cfg)
        self.statusBar().showMessage(f"Loaded config: {self.config_path}", 5000)

    def save_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Config",
            self.manual_config_path or self.config_path,
            "JSON Files (*.json)",
        )
        if not path:
            return
        cfg = self._collect_config()
        try:
            self.config_store.save(path, cfg)
        except Exception as exc:
            QMessageBox.critical(self, "Save", f"Failed to save config:\n{exc}")
            return
        self.config_path = str(Path(path).resolve())
        self.manual_config_path = self.config_path
        self._save_session_state()
        self.statusBar().showMessage(f"Saved config: {self.config_path}", 5000)

    def start_router(self) -> None:
        cfg = self._collect_config()
        if not cfg.hardware:
            QMessageBox.warning(self, "Start", "Please add at least one hardware port.")
            return
        route_issues = find_invalid_route_ports(cfg)
        if route_issues:
            QMessageBox.warning(
                self,
                "Start",
                "Route config references missing hardware ports:\n"
                + "\n".join(route_issues[:12])
                + "\n\nLoad the correct config or rescan the hardware ports before starting.",
            )
            return
        if self.router is not None and self.router.is_running:
            return
        errors = self.dbc_service.sync_hardware(cfg.hardware, raise_on_error=False)
        if errors:
            self.logger.warning("DBC load warning on start: %s", " | ".join(errors))
            self.statusBar().showMessage(
                "DBC load warning: check ! marks in Hardware Config.", 5000
            )
            self.hardware_tab.refresh_dbc_status()

        self.router = RouterEngine(
            config=cfg,
            registry=self.registry,
            dbc_service=self.dbc_service,
            logger=self.logger.getChild("router"),
        )
        try:
            self.router.start()
        except Exception as exc:
            QMessageBox.critical(self, "Start", f"Router start failed:\n{exc}")
            self.router = None
            return
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.mode_label.setText("Running")
        self.routes_tab.set_router_running(True)
        self.stats_timer.start()
        self.statusBar().showMessage("Router started", 3000)

    def stop_router(self) -> None:
        if self.router is None:
            return
        self.router.stop()
        self.router = None
        self.stats_timer.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.mode_label.setText("Stopped")
        self.routes_tab.set_router_running(False)
        self.routes_tab.set_runtime_values({})
        self.statusBar().showMessage("Router stopped", 3000)

    def _update_stats(self) -> None:
        if self.router is None:
            return
        stats = self.router.get_stats()
        self.routes_tab.set_runtime_values(self.router.get_signal_values())
        self.stats_label.setText(
            f"RX:{stats.rx_frames} TX:{stats.tx_frames} Drop:{stats.dropped_frames} Err:{stats.errors}"
        )
        events = self.router.get_trace_events(limit=1000)
        if events:
            self.trace_tab.add_events(events)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.stop_router()
        self.diagnostics_tab.stop()
        if self.direct_can_window is not None:
            try:
                self._store_direct_can_state(self.direct_can_window.snapshot_state())
            except Exception:
                pass
            self.direct_can_window.close()
        try:
            self._autosave_current_config()
        except Exception as exc:
            QMessageBox.warning(self, "Auto Save", f"Failed to auto save:\n{exc}")
        super().closeEvent(event)


def run_app() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    window = GatewayMainWindow()
    window.show()
    sys.exit(app.exec())
