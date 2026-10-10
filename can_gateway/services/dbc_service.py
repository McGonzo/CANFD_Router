from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from can_gateway.core.frame import Frame
from can_gateway.core.models import HardwareConfig
from can_gateway.services.arxml_database import ArxmlDatabase

try:
    import cantools  # type: ignore
except Exception:  # pragma: no cover
    cantools = None


@dataclass(slots=True)
class DbcSignalInfo:
    name: str
    start: int
    length: int
    unit: str


@dataclass(slots=True)
class DbcMessageInfo:
    name: str
    frame_id: int
    length: int
    cycle_time_ms: int | None
    signals: List[DbcSignalInfo]


@dataclass(slots=True)
class DbcSignalRow:
    port_name: str
    message_name: str
    signal_name: str
    frame_id: int
    message_length: int
    start: int
    length: int
    unit: str


class DbcService:
    def __init__(self) -> None:
        self._db_cache: Dict[str, object] = {}
        self._db_cache_signature: Dict[str, tuple[int, int]] = {}
        self._arxml_cache: Dict[str, ArxmlDatabase] = {}
        self._arxml_cache_signature: Dict[str, tuple[int, int]] = {}
        self._port_path: Dict[str, str] = {}
        self._port_format: Dict[str, str] = {}
        self._port_cluster: Dict[str, str] = {}
        self._port_errors: Dict[str, str] = {}
        self._db_mode: Dict[str, str] = {}
        self._db_warning: Dict[str, str] = {}
        self._last_sync_errors: List[str] = []

    def _normalize(self, path: str) -> str:
        return str(Path(path).expanduser().resolve())

    def _file_signature(self, path: str) -> tuple[int, int]:
        stat = Path(path).stat()
        return int(stat.st_mtime_ns), int(stat.st_size)

    def _format_compat_note(self, error_text: str) -> str:
        lower = error_text.lower()
        if "overlap" in lower:
            return (
                "Loaded in compatible mode: overlapping signals detected in strict mode."
            )
        if "does not fit" in lower:
            return (
                "Loaded in compatible mode: signal layout exceeds strict boundaries "
                "(often seen in some CANFD DBC exports)."
            )
        if "length" in lower and "> 8" in lower:
            return (
                "Loaded in compatible mode: DLC > 8 detected (CANFD layout accepted)."
            )
        return (
            "Loaded in compatible mode due to non-standard DBC structure "
            "that strict mode rejected."
        )

    def _load_db(self, path: str) -> object:
        if cantools is None:
            raise RuntimeError("cantools is not installed.")
        normalized = self._normalize(path)
        if not Path(normalized).is_file():
            raise RuntimeError(f"DBC file not found: {normalized}")
        signature = self._file_signature(normalized)
        db = self._db_cache.get(normalized)
        if db is None or self._db_cache_signature.get(normalized) != signature:
            try:
                Path(normalized).read_bytes().decode("utf-8-sig")
                encoding = "utf-8-sig"
            except UnicodeDecodeError:
                encoding = None  # Retain cantools' legacy DBC encoding fallback.
            try:
                db = cantools.database.load_file(normalized, strict=True, encoding=encoding)
                self._db_mode[normalized] = "strict"
                self._db_warning.pop(normalized, None)
            except Exception as strict_exc:
                # Compatible mode tolerates some non-standard DBC definitions
                # (e.g., overlap and signal-size inconsistencies).
                try:
                    db = cantools.database.load_file(normalized, strict=False, encoding=encoding)
                    self._db_mode[normalized] = "compatible"
                    self._db_warning[normalized] = self._format_compat_note(
                        str(strict_exc)
                    )
                except Exception as compat_exc:
                    raise RuntimeError(
                        f"DBC parse failed: strict={strict_exc}; compatible={compat_exc}"
                    ) from compat_exc
            self._db_cache[normalized] = db
            self._db_cache_signature[normalized] = signature
        return db

    def _load_arxml(self, path: str) -> ArxmlDatabase:
        normalized = self._normalize(path)
        if not Path(normalized).is_file():
            raise RuntimeError(f"ARXML file not found: {normalized}")
        signature = self._file_signature(normalized)
        db = self._arxml_cache.get(normalized)
        if db is None or self._arxml_cache_signature.get(normalized) != signature:
            db = ArxmlDatabase(normalized)
            self._arxml_cache[normalized] = db
            self._arxml_cache_signature[normalized] = signature
            self._db_mode[normalized] = "arxml"
            self._db_warning.pop(normalized, None)
        return db

    def available_clusters(self, path: str) -> List[str]:
        normalized = self._normalize(path)
        if normalized.lower().endswith((".arxml", ".xml")):
            return self._load_arxml(normalized).clusters()
        return []

    def _hardware_database_path(self, item: HardwareConfig) -> str:
        if hasattr(item, "database_file_path"):
            return item.database_file_path()
        return str(item.dbc_path or "").strip()

    def _hardware_database_format(self, item: HardwareConfig, path: str) -> str:
        if hasattr(item, "database_kind"):
            kind = item.database_kind()
        else:
            kind = str(item.extra.get("database_format", "")).strip().lower()
        if kind:
            return kind
        return "arxml" if path.lower().endswith((".arxml", ".xml")) else "dbc"

    def _hardware_cluster(self, item: HardwareConfig) -> str:
        if hasattr(item, "database_cluster"):
            return item.database_cluster()
        return str(item.extra.get("cluster_name", "")).strip()

    def sync_hardware(
        self, hardware: List[HardwareConfig], *, raise_on_error: bool = False
    ) -> List[str]:
        valid_ports = {item.name for item in hardware}
        self._port_path = {
            port: dbc
            for port, dbc in self._port_path.items()
            if port in valid_ports and dbc
        }
        self._port_format = {
            port: kind for port, kind in self._port_format.items() if port in valid_ports
        }
        self._port_cluster = {
            port: cluster
            for port, cluster in self._port_cluster.items()
            if port in valid_ports
        }
        self._port_errors = {
            port: error for port, error in self._port_errors.items() if port in valid_ports
        }
        errors: List[str] = []
        for item in hardware:
            database_path = self._hardware_database_path(item)
            if not database_path:
                self._port_path.pop(item.name, None)
                self._port_format.pop(item.name, None)
                self._port_cluster.pop(item.name, None)
                self._port_errors.pop(item.name, None)
                continue
            normalized = self._normalize(database_path)
            database_format = self._hardware_database_format(item, normalized)
            try:
                if database_format == "arxml":
                    arxml = self._load_arxml(normalized)
                    cluster = self._hardware_cluster(item)
                    clusters = arxml.clusters()
                    if not cluster and len(clusters) == 1:
                        cluster = clusters[0]
                    if not cluster:
                        raise RuntimeError(
                            f"ARXML cluster is required. Available clusters: {', '.join(clusters)}"
                        )
                    if cluster not in clusters:
                        raise RuntimeError(
                            f"ARXML cluster '{cluster}' not found. Available clusters: {', '.join(clusters)}"
                        )
                    self._port_cluster[item.name] = cluster
                else:
                    self._load_db(normalized)
                    database_format = "dbc"
                    self._port_cluster.pop(item.name, None)
                self._port_path[item.name] = normalized
                self._port_format[item.name] = database_format
                self._port_errors.pop(item.name, None)
            except Exception as exc:
                self._port_path.pop(item.name, None)
                self._port_format.pop(item.name, None)
                self._port_cluster.pop(item.name, None)
                message = f"{item.name}: {exc}"
                self._port_errors[item.name] = message
                errors.append(message)
        self._last_sync_errors = list(errors)
        if errors and raise_on_error:
            raise RuntimeError("\n".join(errors))
        return list(errors)

    def set_port_dbc(self, port_name: str, path: str) -> None:
        normalized = self._normalize(path)
        self._load_db(normalized)
        self._port_path[port_name] = normalized
        self._port_format[port_name] = "dbc"
        self._port_cluster.pop(port_name, None)
        self._port_errors.pop(port_name, None)

    def remove_port(self, port_name: str) -> None:
        self._port_path.pop(port_name, None)
        self._port_format.pop(port_name, None)
        self._port_cluster.pop(port_name, None)
        self._port_errors.pop(port_name, None)

    def clear(self) -> None:
        self._db_cache.clear()
        self._arxml_cache.clear()
        self._port_path.clear()
        self._port_format.clear()
        self._port_cluster.clear()
        self._port_errors.clear()
        self._db_mode.clear()
        self._db_warning.clear()
        self._last_sync_errors.clear()

    def last_sync_errors(self) -> List[str]:
        return list(self._last_sync_errors)

    def port_path(self, port_name: str) -> str:
        return self._port_path.get(port_name, "")

    def has_port(self, port_name: str) -> bool:
        return port_name in self._port_path

    def mode_for_port(self, port_name: str) -> str:
        if port_name in self._port_errors:
            return "error"
        path = self._port_path.get(port_name, "")
        if not path:
            return "none"
        return self._port_format.get(port_name) or self._db_mode.get(path, "unknown")

    def dbc_error_for_port(self, port_name: str) -> str:
        return self._port_errors.get(port_name, "")

    def warning_for_port(self, port_name: str) -> str:
        path = self._port_path.get(port_name, "")
        if not path:
            return ""
        return self._db_warning.get(path, "")

    def _get_db_for_port(self, port_name: str) -> object:
        path = self._port_path.get(port_name, "")
        if not path:
            raise RuntimeError(f"Port '{port_name}' has no DBC assigned.")
        if self._port_format.get(port_name) == "arxml":
            db = self._arxml_cache.get(path)
            if db is None:
                db = self._load_arxml(path)
            return db
        db = self._db_cache.get(path)
        if db is None:
            db = self._load_db(path)
        return db

    def _cluster_for_port(self, port_name: str) -> str:
        return self._port_cluster.get(port_name, "")

    def list_messages(self, port_name: str) -> List[DbcMessageInfo]:
        try:
            db = self._get_db_for_port(port_name)
        except Exception:
            return []
        out: List[DbcMessageInfo] = []
        messages = (
            db.messages(self._cluster_for_port(port_name))
            if isinstance(db, ArxmlDatabase)
            else db.messages
        )
        for msg in messages:
            signals: List[DbcSignalInfo] = []
            for sig in msg.signals:
                signals.append(
                    DbcSignalInfo(
                        name=sig.name,
                        start=int(sig.start),
                        length=int(sig.length),
                        unit=str(sig.unit or ""),
                    )
                )
            out.append(
                DbcMessageInfo(
                    name=msg.name,
                    frame_id=int(msg.frame_id),
                    length=int(msg.length),
                    cycle_time_ms=(
                        msg.cycle_time_ms if isinstance(db, ArxmlDatabase) else
                        int(msg.cycle_time) if getattr(msg, "cycle_time", None) is not None else None
                    ),
                    signals=signals,
                )
            )
        return out

    def message_cycle_time(self, port_name: str, message_name: str) -> int | None:
        if not message_name:
            return None
        db = self._get_db_for_port(port_name)
        if isinstance(db, ArxmlDatabase):
            message = db.get_message_by_name(self._cluster_for_port(port_name), message_name)
            return message.cycle_time_ms
        try:
            message = db.get_message_by_name(message_name)
        except Exception:
            return None
        cycle_time = getattr(message, "cycle_time", None)
        if cycle_time is None:
            return None
        return int(cycle_time)

    def effective_message_cycle_time(
        self, port_name: str, message_name: str, default_ms: int = 100
    ) -> tuple[int, str]:
        cycle_time = self.message_cycle_time(port_name, message_name)
        if cycle_time is not None and int(cycle_time) > 0:
            return int(cycle_time), "DB"
        return int(default_ms), "Default"

    def list_message_names(self, port_name: str) -> List[str]:
        return [msg.name for msg in self.list_messages(port_name)]

    def list_signals(self, port_name: str, message_name: str) -> List[str]:
        if not message_name:
            return []
        db = self._get_db_for_port(port_name)
        if isinstance(db, ArxmlDatabase):
            try:
                message = db.get_message_by_name(self._cluster_for_port(port_name), message_name)
            except Exception:
                return []
            return [signal.name for signal in message.signals]
        try:
            message = db.get_message_by_name(message_name)
        except Exception:
            return []
        return [signal.name for signal in message.signals]

    def list_signal_rows(self, port_name: str) -> List[DbcSignalRow]:
        rows: List[DbcSignalRow] = []
        for message in self.list_messages(port_name):
            for signal in message.signals:
                rows.append(
                    DbcSignalRow(
                        port_name=port_name,
                        message_name=message.name,
                        signal_name=signal.name,
                        frame_id=message.frame_id,
                        message_length=message.length,
                        start=signal.start,
                        length=signal.length,
                        unit=signal.unit,
                    )
                )
        rows.sort(
            key=lambda row: (
                row.port_name.lower(),
                row.message_name.lower(),
                row.signal_name.lower(),
            )
        )
        return rows

    def decode_frame(self, port_name: str, frame: Frame) -> Dict[str, object]:
        db = self._get_db_for_port(port_name)
        if isinstance(db, ArxmlDatabase):
            return db.decode_frame(self._cluster_for_port(port_name), frame)
        msg = db.get_message_by_frame_id(frame.arbitration_id)
        signals = msg.decode(frame.data, decode_choices=False, allow_truncated=True)
        return {"message_name": msg.name, "frame_id": msg.frame_id, "signals": signals}

    def signal_choices(self, port_name: str, message_name: str, signal_name: str) -> Dict[float, str]:
        """Return enum labels keyed by the physical values accepted by encode_message."""
        db = self._get_db_for_port(port_name)
        if isinstance(db, ArxmlDatabase):
            message = db.get_message_by_name(self._cluster_for_port(port_name), message_name)
        else:
            message = db.get_message_by_name(message_name)
        signal = next((item for item in message.signals if item.name == signal_name), None)
        if signal is None:
            return {}
        return {
            float(raw) * float(signal.scale) + float(signal.offset): str(label)
            for raw, label in (signal.choices or {}).items()
        }

    def default_signals(self, port_name: str, message_name: str) -> Dict[str, object]:
        db = self._get_db_for_port(port_name)
        if isinstance(db, ArxmlDatabase):
            return db.default_signals(self._cluster_for_port(port_name), message_name)
        msg = db.get_message_by_name(message_name)
        defaults: Dict[str, object] = {}
        for signal in msg.signals:
            raw_initial = getattr(signal, "raw_initial", None)
            if raw_initial is not None:
                value = raw_initial
            else:
                value = signal.initial if signal.initial is not None else 0
            value = self._normalize_default_value(signal, value)
            value, _ = self._coerce_signal_value_for_encode(signal, value)
            try:
                defaults[signal.name] = float(value)
            except Exception:
                defaults[signal.name] = value
        return defaults

    def _normalize_default_value(self, signal: object, value: object) -> object:
        try:
            numeric_value = float(value)
        except Exception:
            return value

        minimum = getattr(signal, "minimum", None)
        maximum = getattr(signal, "maximum", None)
        is_signed = bool(getattr(signal, "is_signed", False))

        if not is_signed and numeric_value < 0:
            if minimum is not None:
                numeric_value = float(minimum)
            else:
                numeric_value = 0.0
        if minimum is not None and numeric_value < float(minimum):
            numeric_value = float(minimum)
        if maximum is not None and numeric_value > float(maximum):
            numeric_value = float(maximum)
        return numeric_value

    def _signal_encode_limits(self, signal: object) -> tuple[float | None, float | None]:
        minimum = getattr(signal, "minimum", None)
        maximum = getattr(signal, "maximum", None)
        derived_min = float(minimum) if minimum is not None else None
        derived_max = float(maximum) if maximum is not None else None

        if bool(getattr(signal, "is_float", False)):
            return derived_min, derived_max

        try:
            length = int(getattr(signal, "length", 0) or 0)
        except Exception:
            length = 0
        if length <= 0:
            return derived_min, derived_max

        is_signed = bool(getattr(signal, "is_signed", False))
        if is_signed:
            raw_min = -(1 << (length - 1))
            raw_max = (1 << (length - 1)) - 1
        else:
            raw_min = 0
            raw_max = (1 << length) - 1

        try:
            conversion = getattr(signal, "conversion", None)
            if conversion is not None:
                scaled_min = float(conversion.raw_to_scaled(raw_min))
                scaled_max = float(conversion.raw_to_scaled(raw_max))
            else:
                raise ValueError("No conversion object.")
        except Exception:
            scale = float(getattr(signal, "scale", 1.0) or 1.0)
            offset = float(getattr(signal, "offset", 0.0) or 0.0)
            scaled_min = float(raw_min * scale + offset)
            scaled_max = float(raw_max * scale + offset)

        encode_min = min(scaled_min, scaled_max)
        encode_max = max(scaled_min, scaled_max)
        if derived_min is not None:
            encode_min = max(encode_min, derived_min)
        if derived_max is not None:
            encode_max = min(encode_max, derived_max)
        return float(encode_min), float(encode_max)

    def _coerce_signal_value_for_encode(
        self,
        signal: object,
        value: object,
    ) -> tuple[object, str]:
        try:
            numeric_value = float(value)
        except Exception:
            return value, ""

        encode_min, encode_max = self._signal_encode_limits(signal)
        coerced_value = numeric_value
        if encode_min is not None and coerced_value < encode_min:
            coerced_value = float(encode_min)
        if encode_max is not None and coerced_value > encode_max:
            coerced_value = float(encode_max)
        if coerced_value == numeric_value:
            return value, ""

        signal_name = str(getattr(signal, "name", "signal"))
        return (
            coerced_value,
            f"{signal_name} adjusted from {numeric_value:g} to {coerced_value:g} to fit the DBC encoding range.",
        )

    def encode_message(
        self,
        port_name: str,
        message_name: str,
        signals: Dict[str, float],
        *,
        use_defaults: bool = False,
        best_effort: bool = False,
        warnings: List[str] | None = None,
    ) -> Frame:
        db = self._get_db_for_port(port_name)
        if isinstance(db, ArxmlDatabase):
            return db.encode_message(
                self._cluster_for_port(port_name),
                message_name,
                signals,
                use_defaults=use_defaults,
            )
        msg = db.get_message_by_name(message_name)
        payload: Dict[str, object]
        if use_defaults:
            payload = self.default_signals(port_name, message_name)
            for key, value in signals.items():
                payload[key] = value
        else:
            payload = dict(signals)
        for signal in msg.signals:
            if signal.name in payload:
                value = self._normalize_default_value(
                    signal,
                    payload[signal.name],
                )
                if best_effort:
                    value, note = self._coerce_signal_value_for_encode(signal, value)
                    if note and warnings is not None and note not in warnings:
                        warnings.append(note)
                payload[signal.name] = value
        data = bytes(msg.encode(payload, strict=False))
        return Frame(arbitration_id=int(msg.frame_id), data=data, is_fd=msg.length > 8)
