from __future__ import annotations

import csv
import math
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from can_gateway.core.frame import Frame
from can_gateway.services.dbc_service import DbcService

_DECODE = object()


class ReceiveRecorder:
    """Stream selected incoming values to CSV without retaining frame history."""

    COLUMNS = ("timestamp", "elapsed_s", "port", "can_id", "extended", "fd", "brs",
               "message", "signal", "value", "enum", "data")

    def __init__(self, dbc: DbcService, port: str, signals: dict,
                 raw_ids: list | tuple, directory: Path) -> None:
        self._dbc = dbc
        self.port = port
        self.signals = MappingProxyType({name: frozenset(values) for name, values in signals.items() if values})
        self.raw_ids = frozenset((int(can_id), bool(extended)) for can_id, extended in raw_ids)
        if not self.signals and not self.raw_ids:
            raise ValueError("Please select at least one message, signal or CAN ID.")
        self._cycles = {}
        self._choices = {}
        for message, names in self.signals.items():
            missing = names - set(dbc.list_signals(port, message))
            if missing:
                raise ValueError(f"{message}: signals no longer exist: {', '.join(sorted(missing))}. Select recording items again.")
            cycle = dbc.message_cycle_time(port, message)
            self._cycles[message] = max(0, int(cycle or 0)) / 1000.0
            for name in names:
                self._choices[(message, name)] = dbc.signal_choices(port, message, name)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self._stream = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", suffix=".csv", delete=False,
            prefix=f"CAN_{datetime.now():%Y%m%d_%H%M%S}_", dir=directory)
        self.path = Path(self._stream.name)
        self._writer = csv.writer(self._stream)
        self._writer.writerow(self.COLUMNS)
        self._stream.flush()
        self.active = True
        self.row_count = 0
        self.frame_count = 0
        self._last = {}
        self._started = time.monotonic()
        self._flushed = self._started

    def record(self, frame: Frame, decoded=_DECODE) -> None:
        if not self.active:
            raise RuntimeError("Recording is stopped.")
        if frame.is_error:
            return
        if decoded is _DECODE:
            try:
                decoded = self._dbc.decode_frame(self.port, frame)
            except Exception:
                decoded = None
        now = time.monotonic()
        stamp = float(frame.timestamp)
        cycle_stamp = stamp if math.isfinite(stamp) and stamp > 0 else now
        message = str(decoded.get("message_name", "")) if isinstance(decoded, dict) else ""
        values = decoded.get("signals", {}) if isinstance(decoded, dict) else {}
        chosen = self.signals.get(message, frozenset())
        rows = []
        if chosen and isinstance(values, dict):
            key = (message, frame.arbitration_id, frame.is_extended_id)
            last = self._last.get(key)
            if last is not None and cycle_stamp >= last and cycle_stamp - last + 1e-9 < self._cycles[message]:
                return
            for name, value in values.items():
                if name in chosen:
                    label = self._choices[(message, name)].get(value, "")
                    if isinstance(value, int) and not isinstance(value, bool):
                        text = str(value)
                    elif isinstance(value, float):
                        text = format(value, ".15g")
                    else:
                        text = str(value)
                    rows.append((message, name, text, label))
            if rows:
                self._last[key] = cycle_stamp
        elif (int(frame.arbitration_id), bool(frame.is_extended_id)) in self.raw_ids:
            rows.append(("", "", "", ""))
        if not rows:
            return
        prefix = (format(stamp, ".9f"), format(now - self._started, ".6f"), self.port,
                  f"0x{frame.arbitration_id:X}", int(frame.is_extended_id), int(frame.is_fd),
                  int(bool(frame.bitrate_switch)))
        data = frame.data.hex(" ").upper()
        for row in rows:
            self._writer.writerow((*prefix, *row, data))
            self.row_count += 1
        self.frame_count += 1
        if now - self._flushed >= 1.0:
            self._stream.flush()
            self._flushed = now

    def stop(self) -> Path:
        if self.active:
            self.active = False
            self._stream.close()
        return self.path
