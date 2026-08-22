from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class InteractionMode(str, Enum):
    PASSTHROUGH = "passthrough"
    ID_REMAP = "id_remap"
    SIGNAL_BRIDGE = "signal_bridge"


class LengthStrategy(str, Enum):
    STRICT = "strict"
    TRUNCATE = "truncate"
    PAD = "pad"


class AlgorithmKind(str, Enum):
    ROLLING_COUNTER = "rolling_counter"
    CRC = "crc"


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return int(raw, 0)


def _enum_value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value or "").strip()


def _normalize_optional_name(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() == "none":
        return ""
    return text


@dataclass(slots=True)
class HardwareConfig:
    name: str
    driver: str
    channel: str
    bitrate: int = 500_000
    data_bitrate: int = 2_000_000
    fd: bool = False
    serial: str = ""
    order: int = 0
    dbc_path: str = ""
    database_format: str = ""
    database_path: str = ""
    cluster_name: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HardwareConfig":
        extra = dict(data.get("extra", {}))
        dbc_path = str(data.get("dbc_path", ""))
        database_path = str(data.get("database_path", extra.get("database_path", "")))
        database_format = str(data.get("database_format", extra.get("database_format", ""))).strip().lower()
        cluster_name = str(data.get("cluster_name", extra.get("cluster_name", ""))).strip()
        if not database_path:
            database_path = dbc_path
        if not database_format and database_path:
            database_format = "arxml" if database_path.lower().endswith((".arxml", ".xml")) else "dbc"
        return cls(
            name=data["name"],
            driver=data["driver"],
            channel=str(data.get("channel", "")),
            bitrate=int(data.get("bitrate", 500_000)),
            data_bitrate=int(data.get("data_bitrate", 2_000_000)),
            fd=bool(data.get("fd", False)),
            serial=str(data.get("serial", "")),
            order=int(data.get("order", 0)),
            dbc_path=dbc_path,
            database_format=database_format,
            database_path=database_path,
            cluster_name=cluster_name,
            extra=extra,
        )

    def database_kind(self) -> str:
        kind = (self.database_format or self.extra.get("database_format") or "").strip().lower()
        path = self.database_file_path()
        if kind:
            return kind
        if path.lower().endswith((".arxml", ".xml")):
            return "arxml"
        return "dbc" if path else ""

    def database_file_path(self) -> str:
        return str(
            self.database_path
            or self.extra.get("database_path")
            or self.dbc_path
            or ""
        ).strip()

    def database_cluster(self) -> str:
        return str(self.cluster_name or self.extra.get("cluster_name") or "").strip()


@dataclass(slots=True)
class RouteRule:
    name: str
    source_port: str
    source_message: str
    source_signal: str
    target_port: str
    target_message: str
    target_signal: str
    enabled: bool = True
    source_id: Optional[int] = None
    target_id: Optional[int] = None
    transform_expr: str = "x"
    scale: float = 1.0
    offset: float = 0.0
    length_strategy: LengthStrategy = LengthStrategy.TRUNCATE
    tx_interval_ms: int = 100
    force_tx_interval: bool = False
    rc_binding: Optional["RouteAlgorithmBinding"] = None
    crc_binding: Optional["RouteAlgorithmBinding"] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RouteRule":
        length_strategy = LengthStrategy(
            _enum_value(data.get("length_strategy", LengthStrategy.TRUNCATE.value))
            or LengthStrategy.TRUNCATE.value
        )
        source_port = _normalize_optional_name(
            data.get("source_port", data.get("source_hw", ""))
        )
        target_port = _normalize_optional_name(
            data.get("target_port", data.get("target_hw", ""))
        )
        source_message = str(data.get("source_message", ""))
        target_message = str(data.get("target_message", data.get("dbc_message", "")))
        source_signal = str(data.get("source_signal", ""))
        target_signal = str(data.get("target_signal", ""))

        # Backward compatibility for old "signal_overrides" style.
        if not target_signal:
            overrides = data.get("signal_overrides", {})
            if isinstance(overrides, dict) and overrides:
                target_signal = str(next(iter(overrides.keys())))
        scale = float(data.get("scale", 1.0))
        offset = float(data.get("offset", 0.0))
        transform_expr = str(data.get("transform_expr", "x") or "x")
        rc_binding = _parse_route_algorithm_binding(
            data.get("rc_binding"),
            preset_key="rc_preset",
            signal_key="rc_signal",
            source=data,
        )
        crc_binding = _parse_route_algorithm_binding(
            data.get("crc_binding"),
            preset_key="crc_preset",
            signal_key="crc_signal",
            source=data,
        )

        return cls(
            name=str(data["name"]),
            source_port=source_port,
            source_message=source_message,
            source_signal=source_signal,
            target_port=target_port,
            target_message=target_message,
            target_signal=target_signal,
            enabled=bool(data.get("enabled", True)),
            source_id=_parse_int(data.get("source_id")),
            target_id=_parse_int(data.get("target_id")),
            transform_expr=transform_expr,
            scale=scale,
            offset=offset,
            length_strategy=length_strategy,
            tx_interval_ms=int(data.get("tx_interval_ms", 100)),
            force_tx_interval=bool(data.get("force_tx_interval", False)),
            rc_binding=rc_binding,
            crc_binding=crc_binding,
        )

    @property
    def source_hw(self) -> str:
        return self.source_port

    @property
    def target_hw(self) -> str:
        return self.target_port


@dataclass(slots=True)
class RuntimeOptions:
    queue_size: int = 5000
    rx_timeout_s: float = 0.02
    dispatch_timeout_s: float = 0.05
    monitor_interval_s: float = 2.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuntimeOptions":
        return cls(
            queue_size=int(data.get("queue_size", 5000)),
            rx_timeout_s=float(data.get("rx_timeout_s", 0.02)),
            dispatch_timeout_s=float(data.get("dispatch_timeout_s", 0.05)),
            monitor_interval_s=float(data.get("monitor_interval_s", 2.0)),
        )


@dataclass(slots=True)
class RouteAlgorithmBinding:
    preset_name: str
    target_signal: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RouteAlgorithmBinding":
        return cls(
            preset_name=str(data.get("preset_name", "")).strip(),
            target_signal=str(data.get("target_signal", "")).strip(),
        )


def _parse_route_algorithm_binding(
    data: Any,
    *,
    preset_key: str,
    signal_key: str,
    source: Dict[str, Any],
) -> Optional[RouteAlgorithmBinding]:
    if isinstance(data, dict):
        binding = RouteAlgorithmBinding.from_dict(data)
        if binding.preset_name and binding.target_signal:
            return binding
        return None
    preset_name = str(source.get(preset_key, "")).strip()
    target_signal = str(source.get(signal_key, "")).strip()
    if not preset_name or not target_signal:
        return None
    return RouteAlgorithmBinding(preset_name=preset_name, target_signal=target_signal)


@dataclass(slots=True)
class AlgorithmPreset:
    name: str
    kind: AlgorithmKind = AlgorithmKind.ROLLING_COUNTER
    width: int = 8
    start_value: int = 0
    step: int = 1
    minimum: int = 0
    maximum: int = 15
    polynomial: int = 0x1D
    initial_value: int = 0xFF
    xor_out: int = 0x00
    reflect_input: bool = False
    reflect_output: bool = False
    exclude_signals: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AlgorithmPreset":
        return cls(
            name=str(data.get("name", "")).strip(),
            kind=AlgorithmKind(
                _enum_value(data.get("kind", AlgorithmKind.ROLLING_COUNTER.value))
                or AlgorithmKind.ROLLING_COUNTER.value
            ),
            width=int(data.get("width", 8)),
            start_value=int(data.get("start_value", 0)),
            step=int(data.get("step", 1)),
            minimum=int(data.get("minimum", 0)),
            maximum=int(data.get("maximum", 15)),
            polynomial=int(data.get("polynomial", 0x1D)),
            initial_value=int(data.get("initial_value", 0xFF)),
            xor_out=int(data.get("xor_out", 0x00)),
            reflect_input=bool(data.get("reflect_input", False)),
            reflect_output=bool(data.get("reflect_output", False)),
            exclude_signals=[str(x).strip() for x in data.get("exclude_signals", []) if str(x).strip()],
        )


@dataclass(slots=True)
class AppConfig:
    hardware: List[HardwareConfig] = field(default_factory=list)
    routes: List[RouteRule] = field(default_factory=list)
    algorithms: List[AlgorithmPreset] = field(default_factory=list)
    runtime: RuntimeOptions = field(default_factory=RuntimeOptions)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        hardware = [HardwareConfig.from_dict(x) for x in data.get("hardware", [])]
        routes = [RouteRule.from_dict(x) for x in data.get("routes", [])]
        algorithms = [AlgorithmPreset.from_dict(x) for x in data.get("algorithms", [])]
        runtime = RuntimeOptions.from_dict(data.get("runtime", {}))
        return cls(
            hardware=hardware,
            routes=routes,
            algorithms=algorithms,
            runtime=runtime,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
