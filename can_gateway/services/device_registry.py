from __future__ import annotations

import logging
from typing import Dict, List

from can_gateway.drivers import (
    DriverPlugin,
    NIDriver,
    PythonCanDriver,
    SimVirtualDriver,
    TestBaseDriver,
    VectorDriver,
    ZLGDriver,
)
from can_gateway.drivers.base import DeviceInfo


class DeviceRegistry:
    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger(__name__)
        self._drivers: Dict[str, DriverPlugin] = {}
        self._load_builtin_drivers()

    @property
    def drivers(self) -> Dict[str, DriverPlugin]:
        return dict(self._drivers)

    def _load_builtin_drivers(self) -> None:
        self.register_driver(SimVirtualDriver(channels=4))
        self.register_driver(NIDriver())
        self.register_driver(TestBaseDriver())
        self.register_driver(VectorDriver())
        self.register_driver(ZLGDriver())
        for interface in ("pcan", "kvaser", "socketcan", "virtual"):
            self.register_driver(PythonCanDriver(interface=interface))

    def register_driver(self, driver: DriverPlugin) -> None:
        key = driver.name
        if key in self._drivers:
            # Keep user-selected alias stable, append interface when duplicated.
            key = f"{driver.name}:{getattr(driver, 'interface', 'default')}"
        self._drivers[key] = driver

    def get_driver(self, name: str) -> DriverPlugin:
        if name in self._drivers:
            return self._drivers[name]
        if ":" in name:
            short = name.split(":", 1)[0]
            if short in self._drivers:
                return self._drivers[short]
        raise KeyError(f"Unknown driver '{name}'.")

    def scan_all(self) -> List[DeviceInfo]:
        items, _ = self.scan_all_report()
        return items

    def scan_all_report(self) -> tuple[List[DeviceInfo], List[str]]:
        items: List[DeviceInfo] = []
        diagnostics: List[str] = []
        for key, driver in self._drivers.items():
            try:
                result = driver.scan_devices()
            except Exception as exc:
                message = f"{key}: {exc}"
                diagnostics.append(message)
                self._logger.warning("scan failed for driver '%s': %s", key, exc)
                continue
            try:
                extra = list(driver.diagnostics())
            except Exception:
                extra = []
            diagnostics.extend([f"{key}: {item}" for item in extra])
            for device in result:
                device.capabilities.setdefault("registry_key", key)
            items.extend(result)
        return (
            sorted(items, key=lambda x: (x.driver, x.display_name.lower())),
            diagnostics,
        )
