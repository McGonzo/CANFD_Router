from __future__ import annotations

import ast
import logging
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Callable, Deque, Dict, List, Tuple

from can_gateway.core.frame import Frame
from can_gateway.core.models import (
    AppConfig,
    HardwareConfig,
    LengthStrategy,
    RouteAlgorithmBinding,
    RouteRule,
)
from can_gateway.services.algorithm_service import AlgorithmService
from can_gateway.services.dbc_service import DbcService
from can_gateway.services.device_registry import DeviceRegistry


@dataclass(slots=True)
class RouterStats:
    rx_frames: int = 0
    tx_frames: int = 0
    dropped_frames: int = 0
    errors: int = 0


@dataclass(slots=True)
class RouterTraceEvent:
    timestamp: float
    direction: str
    port: str
    arbitration_id: int
    dlc: int
    is_fd: bool
    data_hex: str
    rule_name: str = ""
    event_type: str = "frame"
    error_text: str = ""
    error_code: int | None = None
    bus_status: str = ""
    tx_error_count: int | None = None
    rx_error_count: int | None = None


def convert_frame_for_target(
    frame: Frame, target_hw: HardwareConfig, length_strategy: LengthStrategy
) -> Frame | None:
    data = bytes(frame.data)
    if target_hw.fd:
        max_len = _target_max_data_length(target_hw, default=64)
        if len(data) > max_len:
            if length_strategy == LengthStrategy.STRICT:
                return None
            data = data[:max_len]
        if length_strategy == LengthStrategy.PAD and len(data) < 8:
            data = data.ljust(8, b"\x00")
        return replace(frame, data=data, is_fd=True, channel=target_hw.channel)

    if len(data) > 8:
        if length_strategy == LengthStrategy.STRICT:
            return None
        data = data[:8]
    if length_strategy == LengthStrategy.PAD and len(data) < 8:
        data = data.ljust(8, b"\x00")
    return replace(frame, data=data, is_fd=False, channel=target_hw.channel)


def _target_max_data_length(target_hw: HardwareConfig, default: int) -> int:
    raw = (
        target_hw.extra.get("tx_max_dlc")
        or target_hw.extra.get("tx_max_data_length")
        or target_hw.extra.get("max_dlc")
        or ""
    )
    try:
        value = int(str(raw).strip(), 0)
    except Exception:
        value = default
    allowed = [8, 12, 16, 20, 24, 32, 48, 64] if target_hw.fd else [8]
    candidates = [item for item in allowed if item <= value]
    return candidates[-1] if candidates else allowed[0]


def _clamp(value: float, vmin: float, vmax: float) -> float:
    return max(vmin, min(vmax, value))


def evaluate_transform_expression(expr: str, x: float) -> float:
    expression = (expr or "x").strip()
    if not expression:
        expression = "x"

    allowed_funcs: Dict[str, Callable[..., float]] = {
        "abs": abs,
        "min": min,
        "max": max,
        "round": round,
        "sqrt": math.sqrt,
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "log": math.log,
        "exp": math.exp,
        "pow": pow,
        "clamp": _clamp,
    }
    allowed_names: Dict[str, float] = {"x": float(x), "pi": math.pi, "e": math.e}

    node = ast.parse(expression, mode="eval")

    def _eval(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return _eval(n.body)
        if isinstance(n, ast.Constant):
            if isinstance(n.value, (int, float)):
                return float(n.value)
            raise ValueError("Only numeric constants are allowed.")
        if isinstance(n, ast.Name):
            if n.id not in allowed_names:
                raise ValueError(f"Unsupported variable '{n.id}'.")
            return float(allowed_names[n.id])
        if isinstance(n, ast.BinOp):
            left = _eval(n.left)
            right = _eval(n.right)
            if isinstance(n.op, ast.Add):
                return left + right
            if isinstance(n.op, ast.Sub):
                return left - right
            if isinstance(n.op, ast.Mult):
                return left * right
            if isinstance(n.op, ast.Div):
                return left / right
            if isinstance(n.op, ast.FloorDiv):
                return left // right
            if isinstance(n.op, ast.Mod):
                return left % right
            if isinstance(n.op, ast.Pow):
                return left**right
            raise ValueError("Unsupported binary operator.")
        if isinstance(n, ast.UnaryOp):
            value = _eval(n.operand)
            if isinstance(n.op, ast.UAdd):
                return +value
            if isinstance(n.op, ast.USub):
                return -value
            raise ValueError("Unsupported unary operator.")
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name):
                raise ValueError("Unsupported function call.")
            name = n.func.id
            if name not in allowed_funcs:
                raise ValueError(f"Unsupported function '{name}'.")
            args = [_eval(arg) for arg in n.args]
            return float(allowed_funcs[name](*args))
        raise ValueError("Unsupported expression node.")

    try:
        result = _eval(node)
    except Exception as exc:
        raise ValueError(f"Invalid transform expression '{expression}': {exc}") from exc
    if not math.isfinite(result):
        raise ValueError("Transform result is not finite.")
    return float(result)


class RouterEngine:
    def __init__(
        self,
        config: AppConfig,
        registry: DeviceRegistry,
        dbc_service: DbcService,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._registry = registry
        self._dbc = dbc_service
        self._config = config
        self._algorithms = AlgorithmService(config.algorithms)

        self._adapters: Dict[str, object] = {}
        self._hardware_by_name: Dict[str, HardwareConfig] = {}
        self._running = threading.Event()
        self._stats = RouterStats()
        self._stats_lock = threading.Lock()
        self._config_lock = threading.RLock()
        self._signal_state_lock = threading.Lock()

        self._queue: queue.Queue[Tuple[str, Frame]] = queue.Queue(
            maxsize=config.runtime.queue_size
        )
        self._rx_threads: List[threading.Thread] = []
        self._dispatch_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._direct_tx_thread: threading.Thread | None = None

        self._target_signal_cache: Dict[Tuple[str, str], Dict[str, object]] = {}
        self._signal_values: Dict[Tuple[str, str, str], object] = {}
        self._direct_last_sent: Dict[str, float] = {}
        self._trace_events: Deque[RouterTraceEvent] = deque(maxlen=20_000)
        self._trace_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    def get_stats(self) -> RouterStats:
        with self._stats_lock:
            return RouterStats(
                rx_frames=self._stats.rx_frames,
                tx_frames=self._stats.tx_frames,
                dropped_frames=self._stats.dropped_frames,
                errors=self._stats.errors,
            )

    def get_trace_events(self, limit: int = 500) -> List[RouterTraceEvent]:
        out: List[RouterTraceEvent] = []
        if limit <= 0:
            return out
        with self._trace_lock:
            for _ in range(min(limit, len(self._trace_events))):
                out.append(self._trace_events.popleft())
        return out

    def get_signal_values(self) -> Dict[Tuple[str, str, str], object]:
        with self._signal_state_lock:
            return dict(self._signal_values)

    def update_config(self, config: AppConfig) -> None:
        with self._config_lock:
            self._config = config
            self._algorithms.update_presets(config.algorithms)
            with self._signal_state_lock:
                self._target_signal_cache.clear()
                self._signal_values.clear()
            self._direct_last_sent.clear()

    def start(self) -> None:
        if self.is_running:
            return
        with self._config_lock:
            config = self._config
        self._queue = queue.Queue(maxsize=config.runtime.queue_size)
        self._algorithms.update_presets(config.algorithms)
        with self._signal_state_lock:
            self._target_signal_cache.clear()
            self._signal_values.clear()
        self._direct_last_sent.clear()
        self._open_adapters(config.hardware)

        self._running.set()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, name="router-dispatch", daemon=True
        )
        self._dispatch_thread.start()

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="router-monitor", daemon=True
        )
        self._monitor_thread.start()

        self._direct_tx_thread = threading.Thread(
            target=self._direct_tx_loop, name="router-direct-tx", daemon=True
        )
        self._direct_tx_thread.start()

        self._rx_threads = []
        for hw in sorted(config.hardware, key=lambda x: x.order):
            thread = threading.Thread(
                target=self._rx_loop,
                args=(hw.name,),
                name=f"router-rx-{hw.name}",
                daemon=True,
            )
            thread.start()
            self._rx_threads.append(thread)

        self._logger.info(
            "Router started with %d ports and %d mapping rules.",
            len(self._adapters),
            len(config.routes),
        )

    def stop(self) -> None:
        if not self.is_running:
            return
        self._running.clear()
        for thread in self._rx_threads:
            thread.join(timeout=1.0)
        self._rx_threads.clear()
        if self._dispatch_thread is not None:
            self._dispatch_thread.join(timeout=1.0)
            self._dispatch_thread = None
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=1.0)
            self._monitor_thread = None
        if self._direct_tx_thread is not None:
            self._direct_tx_thread.join(timeout=1.0)
            self._direct_tx_thread = None

        adapters = list(self._adapters.items())
        self._adapters.clear()
        for name, adapter in adapters:
            try:
                adapter.close()
            except Exception as exc:
                self._logger.warning("close adapter %s failed: %s", name, exc)
        self._logger.info("Router stopped.")

    def inject_frame(self, source_port: str, frame: Frame) -> None:
        if not self.is_running:
            raise RuntimeError("Router is not running.")
        with self._stats_lock:
            self._stats.rx_frames += 1
        self._append_trace("INJECT", source_port, frame, "")
        self._record_frame_signals(source_port, frame)
        self._queue.put_nowait((source_port, frame))

    def inject_signal(
        self, source_port: str, message_name: str, signals: Dict[str, float]
    ) -> None:
        frame = self._dbc.encode_message(
            source_port, message_name, signals, use_defaults=True
        )
        self.inject_frame(source_port, frame)

    def send_message(
        self,
        target_port: str,
        message_name: str,
        signals: Dict[str, float],
        *,
        rc_binding: RouteAlgorithmBinding | None = None,
        crc_binding: RouteAlgorithmBinding | None = None,
    ) -> None:
        if not self.is_running:
            raise RuntimeError("Router is not running.")
        target_adapter = self._adapters.get(target_port)
        target_hw = self._hardware_by_name.get(target_port)
        if target_adapter is None or target_hw is None:
            raise RuntimeError(f"Target port '{target_port}' is not available.")
        out = self._compose_output_frame(
            target_port=target_port,
            target_message=message_name,
            mapped_signals=signals,
            target_hw=target_hw,
            length_strategy=LengthStrategy.STRICT,
            target_id=None,
            rc_binding=rc_binding,
            crc_binding=crc_binding,
            scope_key=f"{target_port}:{message_name}",
        )
        if out is None:
            raise RuntimeError("Target frame was rejected by length strategy.")
        target_adapter.send(out)
        self._append_trace("TX", target_port, out, f"manual:{message_name}")
        self._record_frame_signals(target_port, out)
        with self._stats_lock:
            self._stats.tx_frames += 1

    def _append_trace(
        self, direction: str, port: str, frame: Frame, rule_name: str = ""
    ) -> None:
        event = RouterTraceEvent(
            timestamp=time.time(),
            direction=direction,
            port=port,
            arbitration_id=frame.arbitration_id,
            dlc=frame.dlc,
            is_fd=bool(frame.is_fd),
            data_hex=frame.data.hex(" ").upper(),
            rule_name=rule_name,
        )
        with self._trace_lock:
            self._trace_events.append(event)

    def _append_error_trace(self, direction: str, port: str, frame: Frame) -> None:
        event = RouterTraceEvent(
            timestamp=time.time(),
            direction=direction,
            port=port,
            arbitration_id=int(frame.arbitration_id),
            dlc=frame.dlc,
            is_fd=bool(frame.is_fd),
            data_hex=frame.data.hex(" ").upper(),
            event_type="error",
            error_text=frame.error_text or "CAN bus error",
            error_code=frame.error_code,
            bus_status=frame.bus_status,
            tx_error_count=frame.tx_error_count,
            rx_error_count=frame.rx_error_count,
        )
        with self._trace_lock:
            self._trace_events.append(event)

    def _open_adapters(self, hardware: List[HardwareConfig]) -> None:
        self._adapters.clear()
        self._hardware_by_name = {x.name: x for x in hardware}
        for hw in sorted(hardware, key=lambda x: x.order):
            driver = self._registry.get_driver(hw.driver)
            adapter = driver.create_adapter(hw)
            try:
                adapter.open()
            except Exception:
                self._cleanup_failed_start()
                raise
            self._adapters[hw.name] = adapter

    def _cleanup_failed_start(self) -> None:
        adapters = list(self._adapters.values())
        self._adapters.clear()
        for adapter in adapters:
            try:
                adapter.close()
            except Exception:
                pass

    def _rx_loop(self, source_port: str) -> None:
        while self.is_running:
            adapter = self._adapters.get(source_port)
            if adapter is None:
                time.sleep(0.1)
                continue
            try:
                frame = adapter.recv(timeout=self._config.runtime.rx_timeout_s)
                if frame is None:
                    continue
                if frame.is_error:
                    with self._stats_lock:
                        self._stats.errors += 1
                    self._append_error_trace(
                        frame.error_source or "ERROR", source_port, frame
                    )
                    continue
                with self._stats_lock:
                    self._stats.rx_frames += 1
                self._append_trace("RX", source_port, frame, "")
                self._record_frame_signals(source_port, frame)
                try:
                    self._queue.put_nowait((source_port, frame))
                except queue.Full:
                    with self._stats_lock:
                        self._stats.dropped_frames += 1
            except Exception as exc:
                self._logger.error("rx error on %s: %s", source_port, exc)
                with self._stats_lock:
                    self._stats.errors += 1
                self._reopen_adapter(source_port)
                time.sleep(0.2)

    def _dispatch_loop(self) -> None:
        while self.is_running or not self._queue.empty():
            try:
                source_port, frame = self._queue.get(
                    timeout=self._config.runtime.dispatch_timeout_s
                )
            except queue.Empty:
                continue
            self._route_frame(source_port, frame)

    def _monitor_loop(self) -> None:
        while self.is_running:
            time.sleep(self._config.runtime.monitor_interval_s)
            stats = self.get_stats()
            self._logger.info(
                "stats rx=%d tx=%d drop=%d err=%d queue=%d",
                stats.rx_frames,
                stats.tx_frames,
                stats.dropped_frames,
                stats.errors,
                self._queue.qsize(),
            )

    def _direct_tx_loop(self) -> None:
        while self.is_running:
            with self._config_lock:
                rules = [
                    rule
                    for rule in self._config.routes
                    if rule.enabled and not rule.source_port and rule.target_port and rule.target_message
                ]
            if not rules:
                time.sleep(0.05)
                continue
            now = time.time()
            for rule in rules:
                if rule.force_tx_interval:
                    effective_interval_ms = int(rule.tx_interval_ms)
                else:
                    effective_interval_ms, _source = self._dbc.effective_message_cycle_time(
                        rule.target_port,
                        rule.target_message,
                        default_ms=100,
                    )
                interval_s = max(0.01, float(effective_interval_ms) / 1000.0)
                last_sent = self._direct_last_sent.get(rule.name, 0.0)
                if now - last_sent < interval_s:
                    continue
                self._direct_last_sent[rule.name] = now
                self._send_direct_rule(rule)
            time.sleep(0.01)

    def _send_direct_rule(self, rule: RouteRule) -> None:
        target_adapter = self._adapters.get(rule.target_port)
        target_hw = self._hardware_by_name.get(rule.target_port)
        if target_adapter is None or target_hw is None:
            return
        mapped_signals: Dict[str, float] = {}
        if rule.target_signal:
            base_value = evaluate_transform_expression(rule.transform_expr or "0", 0.0)
            mapped_signals[rule.target_signal] = base_value * float(rule.scale) + float(rule.offset)
        try:
            out = self._compose_output_frame(
                target_port=rule.target_port,
                target_message=rule.target_message,
                mapped_signals=mapped_signals,
                target_hw=target_hw,
                length_strategy=rule.length_strategy,
                target_id=rule.target_id,
                rc_binding=rule.rc_binding,
                crc_binding=rule.crc_binding,
                scope_key=f"{rule.target_port}:{rule.target_message}",
            )
            if out is None:
                with self._stats_lock:
                    self._stats.dropped_frames += 1
                return
            target_adapter.send(out)
            self._append_trace("TX", rule.target_port, out, rule.name)
            self._record_frame_signals(rule.target_port, out)
            with self._stats_lock:
                self._stats.tx_frames += 1
        except Exception as exc:
            self._logger.error("direct route error for rule '%s': %s", rule.name, exc)
            with self._stats_lock:
                self._stats.errors += 1

    def _route_frame(self, source_port: str, frame: Frame) -> None:
        with self._config_lock:
            rules = [
                rule
                for rule in self._config.routes
                if rule.enabled and rule.source_port == source_port
            ]
        if not rules:
            return

        decoded: Dict[str, object] | None = None
        groups: Dict[Tuple[object, ...], dict] = {}
        for rule in rules:
            if rule.source_id is not None and frame.arbitration_id != rule.source_id:
                continue
            target_adapter = self._adapters.get(rule.target_port)
            target_hw = self._hardware_by_name.get(rule.target_port)
            if target_adapter is None or target_hw is None:
                continue
            if not (rule.source_signal and rule.target_message and rule.target_signal):
                continue

            if decoded is None:
                try:
                    decoded = self._dbc.decode_frame(source_port, frame)
                except Exception:
                    return

            if rule.source_message and decoded["message_name"] != rule.source_message:
                continue

            signals = decoded.get("signals", {})
            if not isinstance(signals, dict):
                continue
            if rule.source_signal not in signals:
                continue

            try:
                source_value = float(signals[rule.source_signal])
                expr_value = evaluate_transform_expression(
                    rule.transform_expr, source_value
                )
                mapped_value = expr_value * float(rule.scale) + float(rule.offset)
            except Exception as exc:
                self._logger.error("route error for rule '%s': %s", rule.name, exc)
                with self._stats_lock:
                    self._stats.errors += 1
                continue

            group_key = (
                rule.target_port,
                rule.target_message,
                rule.target_id,
                rule.length_strategy.value,
                self._binding_key(rule.rc_binding),
                self._binding_key(rule.crc_binding),
            )
            group = groups.get(group_key)
            if group is None:
                group = {
                    "target_adapter": target_adapter,
                    "target_hw": target_hw,
                    "target_port": rule.target_port,
                    "target_message": rule.target_message,
                    "target_id": rule.target_id,
                    "length_strategy": rule.length_strategy,
                    "rc_binding": rule.rc_binding,
                    "crc_binding": rule.crc_binding,
                    "tx_interval_ms": int(rule.tx_interval_ms),
                    "force_tx_interval": bool(rule.force_tx_interval),
                    "forced_tx_interval_ms": int(rule.tx_interval_ms),
                    "mapped_signals": {},
                    "rule_names": [],
                }
                groups[group_key] = group
            group["tx_interval_ms"] = min(
                int(group["tx_interval_ms"]),
                int(rule.tx_interval_ms),
            )
            if rule.force_tx_interval:
                group["force_tx_interval"] = True
                group["forced_tx_interval_ms"] = min(
                    int(group["forced_tx_interval_ms"]),
                    int(rule.tx_interval_ms),
                )
            group["mapped_signals"][rule.target_signal] = mapped_value
            group["rule_names"].append(rule.name)

        for group in groups.values():
            rule_label = ",".join(group["rule_names"])
            if bool(group.get("force_tx_interval", False)):
                effective_interval_ms = int(group["forced_tx_interval_ms"])
            else:
                cycle_time_ms = self._dbc.message_cycle_time(
                    group["target_port"],
                    group["target_message"],
                )
                effective_interval_ms = (
                    int(cycle_time_ms)
                    if cycle_time_ms is not None and int(cycle_time_ms) > 0
                    else int(group["tx_interval_ms"])
                )
            interval_s = max(0.001, float(effective_interval_ms) / 1000.0)
            throttle_key = f"route:{group['target_port']}:{group['target_message']}"
            now = time.time()
            last_sent = self._direct_last_sent.get(throttle_key, 0.0)
            if now - last_sent < interval_s:
                continue
            self._direct_last_sent[throttle_key] = now
            try:
                out = self._compose_output_frame(
                    target_port=group["target_port"],
                    target_message=group["target_message"],
                    mapped_signals=group["mapped_signals"],
                    target_hw=group["target_hw"],
                    length_strategy=group["length_strategy"],
                    target_id=group["target_id"],
                    rc_binding=group["rc_binding"],
                    crc_binding=group["crc_binding"],
                    scope_key=f"{group['target_port']}:{group['target_message']}",
                )
                if out is None:
                    with self._stats_lock:
                        self._stats.dropped_frames += 1
                    continue
                group["target_adapter"].send(out)
                self._append_trace("TX", group["target_port"], out, rule_label)
                self._record_frame_signals(group["target_port"], out)
                with self._stats_lock:
                    self._stats.tx_frames += 1
            except Exception as exc:
                self._logger.error("route error for rules '%s': %s", rule_label, exc)
                with self._stats_lock:
                    self._stats.errors += 1

    def _binding_key(self, binding: RouteAlgorithmBinding | None) -> Tuple[str, str]:
        if binding is None:
            return ("", "")
        return (binding.preset_name, binding.target_signal)

    def _record_frame_signals(self, port_name: str, frame: Frame) -> None:
        try:
            decoded = self._dbc.decode_frame(port_name, frame)
        except Exception:
            return
        self._record_decoded_signals(port_name, decoded)

    def _record_decoded_signals(self, port_name: str, decoded: Dict[str, object]) -> None:
        message_name = str(decoded.get("message_name", "")).strip()
        if not message_name:
            return
        signals = decoded.get("signals", {})
        if not isinstance(signals, dict):
            return
        with self._signal_state_lock:
            for signal_name, value in signals.items():
                self._signal_values[(port_name, message_name, str(signal_name))] = value

    def _build_target_frame(
        self, rule: RouteRule, target_hw: HardwareConfig, mapped_value: float
    ) -> Frame | None:
        return self._compose_output_frame(
            target_port=rule.target_port,
            target_message=rule.target_message,
            mapped_signals={rule.target_signal: mapped_value},
            target_hw=target_hw,
            length_strategy=rule.length_strategy,
            target_id=rule.target_id,
            rc_binding=rule.rc_binding,
            crc_binding=rule.crc_binding,
            scope_key=f"{rule.target_port}:{rule.target_message}",
        )

    def _compose_output_frame(
        self,
        *,
        target_port: str,
        target_message: str,
        mapped_signals: Dict[str, float],
        target_hw: HardwareConfig,
        length_strategy: LengthStrategy,
        target_id: int | None,
        rc_binding: RouteAlgorithmBinding | None,
        crc_binding: RouteAlgorithmBinding | None,
        scope_key: str,
    ) -> Frame | None:
        with self._signal_state_lock:
            target_signals = self._get_target_signal_state(target_port, target_message)
            for signal_name, signal_value in mapped_signals.items():
                target_signals[signal_name] = signal_value
            self._apply_algorithm_bindings(
                target_port=target_port,
                target_message=target_message,
                target_signals=target_signals,
                rc_binding=rc_binding,
                crc_binding=crc_binding,
                scope_key=scope_key,
            )
            encoded = self._dbc.encode_message(
                target_port,
                target_message,
                dict(target_signals),
            )
        if target_id is not None:
            encoded = replace(
                encoded,
                arbitration_id=target_id,
                is_extended_id=target_id > 0x7FF,
            )
        return convert_frame_for_target(encoded, target_hw, length_strategy)

    def _get_target_signal_state(
        self, target_port: str, target_message: str
    ) -> Dict[str, object]:
        cache_key = (target_port, target_message)
        target_signals = self._target_signal_cache.get(cache_key)
        if target_signals is None:
            target_signals = self._dbc.default_signals(
                target_port, target_message
            )
            self._target_signal_cache[cache_key] = target_signals
        return target_signals

    def _apply_algorithm_bindings(
        self,
        *,
        target_port: str,
        target_message: str,
        target_signals: Dict[str, object],
        rc_binding: RouteAlgorithmBinding | None,
        crc_binding: RouteAlgorithmBinding | None,
        scope_key: str,
    ) -> None:
        if rc_binding and rc_binding.preset_name and rc_binding.target_signal:
            rc_value = self._algorithms.next_rolling_counter(
                rc_binding.preset_name,
                scope_key,
            )
            target_signals[rc_binding.target_signal] = rc_value
        if crc_binding and crc_binding.preset_name and crc_binding.target_signal:
            scratch = dict(target_signals)
            scratch[crc_binding.target_signal] = 0
            preset = self._algorithms.get_preset(crc_binding.preset_name)
            for signal_name in preset.exclude_signals:
                if signal_name:
                    scratch[signal_name] = 0
            encoded = self._dbc.encode_message(
                target_port,
                target_message,
                scratch,
            )
            crc_value = self._algorithms.compute_crc(
                crc_binding.preset_name,
                encoded.data,
            )
            target_signals[crc_binding.target_signal] = crc_value

    def _reopen_adapter(self, hw_name: str) -> None:
        hw = self._hardware_by_name.get(hw_name)
        if hw is None:
            return
        try:
            old = self._adapters.get(hw_name)
            if old is not None:
                old.close()
            driver = self._registry.get_driver(hw.driver)
            adapter = driver.create_adapter(hw)
            adapter.open()
            self._adapters[hw_name] = adapter
            self._logger.info("adapter %s reconnected", hw_name)
        except Exception as exc:
            self._logger.error("reconnect failed on %s: %s", hw_name, exc)
