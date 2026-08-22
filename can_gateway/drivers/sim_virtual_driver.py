from __future__ import annotations

import queue
import threading
import time
from collections import defaultdict
from dataclasses import replace
from typing import DefaultDict, List, Set

from can_gateway.core.frame import Frame
from can_gateway.core.models import HardwareConfig
from can_gateway.drivers.base import BusAdapter, DeviceInfo, DriverPlugin


class _SimBusHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._channels: DefaultDict[str, Set["SimVirtualAdapter"]] = defaultdict(set)

    def register(self, channel: str, adapter: "SimVirtualAdapter") -> None:
        with self._lock:
            self._channels[channel].add(adapter)

    def unregister(self, channel: str, adapter: "SimVirtualAdapter") -> None:
        with self._lock:
            group = self._channels.get(channel)
            if not group:
                return
            group.discard(adapter)
            if not group:
                self._channels.pop(channel, None)

    def broadcast(self, channel: str, frame: Frame, sender: "SimVirtualAdapter") -> None:
        with self._lock:
            listeners = list(self._channels.get(channel, set()))
        for adapter in listeners:
            if adapter is sender:
                continue
            adapter.inject(frame)


_HUB = _SimBusHub()


class SimVirtualAdapter(BusAdapter):
    def __init__(self, config: HardwareConfig):
        self._config = config
        self._rx: queue.Queue[Frame] = queue.Queue(maxsize=20_000)
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        if self._open:
            return
        _HUB.register(self._config.channel, self)
        self._open = True

    def close(self) -> None:
        if not self._open:
            return
        _HUB.unregister(self._config.channel, self)
        self._open = False
        self._drain()

    def _drain(self) -> None:
        while True:
            try:
                self._rx.get_nowait()
            except queue.Empty:
                return

    def send(self, frame: Frame) -> None:
        if not self._open:
            raise RuntimeError("Adapter not opened.")
        outbound = replace(frame, channel=self._config.channel, timestamp=time.time())
        _HUB.broadcast(self._config.channel, outbound, self)

    def recv(self, timeout: float) -> Frame | None:
        if not self._open:
            return None
        try:
            return self._rx.get(timeout=timeout)
        except queue.Empty:
            return None

    def inject(self, frame: Frame) -> None:
        if not self._open:
            return
        try:
            self._rx.put_nowait(frame)
        except queue.Full:
            pass


class SimVirtualDriver(DriverPlugin):
    name = "sim-virtual"

    def __init__(self, channels: int = 4) -> None:
        self._channels = max(1, channels)

    def scan_devices(self) -> List[DeviceInfo]:
        out: List[DeviceInfo] = []
        for index in range(1, self._channels + 1):
            channel = f"SIM_CH{index}"
            out.append(
                DeviceInfo(
                    device_id=f"sim-{index}",
                    display_name=f"Virtual CAN Port {index}",
                    driver=self.name,
                    channel=channel,
                    capabilities={
                        "interface": "sim-virtual",
                        "fd": "yes",
                        "vendor": "sim",
                    },
                )
            )
        return out

    def create_adapter(self, config: HardwareConfig) -> BusAdapter:
        return SimVirtualAdapter(config)
