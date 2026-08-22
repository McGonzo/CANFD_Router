from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Dict, Iterable, List

from can_gateway.core.frame import Frame

try:
    import cantools  # type: ignore
except Exception:  # pragma: no cover
    cantools = None


@dataclass(slots=True)
class ArxmlSignal:
    name: str
    ref: str
    start: int
    length: int
    byte_order: str = "big"
    scale: float = 1.0
    offset: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    initial: float = 0.0
    unit: str = ""


@dataclass(slots=True)
class ArxmlPdu:
    name: str
    ref: str
    length: int
    header_id: int | None = None
    signals: List[ArxmlSignal] = field(default_factory=list)


@dataclass(slots=True)
class ArxmlMessage:
    name: str
    frame_id: int
    length: int
    cluster_name: str
    cycle_time_ms: int | None
    contained_pdus: List[ArxmlPdu] = field(default_factory=list)

    @property
    def signals(self) -> List[ArxmlSignal]:
        out: List[ArxmlSignal] = []
        for pdu in self.contained_pdus:
            out.extend(pdu.signals)
        return out


class ArxmlDatabase:
    def __init__(self, path: str) -> None:
        self.path = str(Path(path).expanduser().resolve())
        self._clusters: List[str] = []
        self._messages_by_cluster: Dict[str, List[ArxmlMessage]] = {}
        self._messages_by_name: Dict[tuple[str, str], ArxmlMessage] = {}
        self._messages_by_id: Dict[tuple[str, int], ArxmlMessage] = {}
        self._element_paths: Dict[int, str] = {}
        self._load()

    def clusters(self) -> List[str]:
        return list(self._clusters)

    def messages(self, cluster_name: str) -> List[ArxmlMessage]:
        return list(self._messages_by_cluster.get(cluster_name, []))

    def get_message_by_name(self, cluster_name: str, message_name: str) -> ArxmlMessage:
        return self._messages_by_name[(cluster_name, message_name)]

    def get_message_by_frame_id(self, cluster_name: str, frame_id: int) -> ArxmlMessage:
        return self._messages_by_id[(cluster_name, int(frame_id))]

    def default_signals(self, cluster_name: str, message_name: str) -> Dict[str, object]:
        msg = self.get_message_by_name(cluster_name, message_name)
        return {signal.name: float(signal.initial) for signal in msg.signals}

    def encode_message(
        self,
        cluster_name: str,
        message_name: str,
        signals: Dict[str, object],
        *,
        use_defaults: bool = False,
    ) -> Frame:
        msg = self.get_message_by_name(cluster_name, message_name)
        values = self.default_signals(cluster_name, message_name) if use_defaults else {}
        values.update(signals)
        data = bytearray(msg.length)
        offset = 0
        for pdu in msg.contained_pdus:
            if pdu.header_id is not None:
                if offset + 4 > len(data):
                    break
                data[offset] = (int(pdu.header_id) >> 16) & 0xFF
                data[offset + 1] = (int(pdu.header_id) >> 8) & 0xFF
                data[offset + 2] = int(pdu.header_id) & 0xFF
                data[offset + 3] = int(pdu.length) & 0xFF
                offset += 4
            payload = bytearray(pdu.length)
            for signal in pdu.signals:
                raw = self._physical_to_raw(signal, values.get(signal.name, signal.initial))
                _pack_bits(payload, signal.start, signal.length, raw, signal.byte_order)
            end = min(len(data), offset + pdu.length)
            data[offset:end] = payload[: max(0, end - offset)]
            offset += pdu.length
        return Frame(
            arbitration_id=msg.frame_id,
            data=bytes(data),
            is_fd=msg.length > 8,
        )

    def decode_frame(self, cluster_name: str, frame: Frame) -> Dict[str, object]:
        msg = self.get_message_by_frame_id(cluster_name, frame.arbitration_id)
        signals: Dict[str, object] = {}
        offset = 0
        data = bytes(frame.data)
        by_header = {
            int(pdu.header_id): pdu for pdu in msg.contained_pdus if pdu.header_id is not None
        }
        if by_header:
            while offset + 4 <= len(data):
                header_id = (data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]
                length = int(data[offset + 3])
                offset += 4
                pdu = by_header.get(header_id)
                payload = data[offset : offset + length]
                if pdu is not None:
                    self._decode_pdu(payload, pdu, signals)
                offset += max(length, 0)
                if length <= 0:
                    break
        else:
            for pdu in msg.contained_pdus:
                payload = data[offset : offset + pdu.length]
                self._decode_pdu(payload, pdu, signals)
                offset += pdu.length
        return {"message_name": msg.name, "frame_id": msg.frame_id, "signals": signals}

    def _decode_pdu(self, payload: bytes, pdu: ArxmlPdu, out: Dict[str, object]) -> None:
        for signal in pdu.signals:
            raw = _unpack_bits(payload, signal.start, signal.length, signal.byte_order)
            out[signal.name] = raw * signal.scale + signal.offset

    def _physical_to_raw(self, signal: ArxmlSignal, value: object) -> int:
        try:
            physical = float(value)
        except Exception:
            physical = float(signal.initial)
        if signal.minimum is not None:
            physical = max(float(signal.minimum), physical)
        if signal.maximum is not None:
            physical = min(float(signal.maximum), physical)
        if signal.scale:
            raw = (physical - signal.offset) / signal.scale
        else:
            raw = physical
        if not isfinite(raw):
            raw = physical
        if not isfinite(raw):
            raw = 0.0
        return int(round(raw))

    def _load(self) -> None:
        root = ET.parse(self.path).getroot()
        elements_by_path: Dict[str, ET.Element] = {}
        elements_by_tag: Dict[str, List[ET.Element]] = {}
        self._index_elements(root, "", elements_by_path, elements_by_tag)

        clusters = self._load_clusters(elements_by_tag)
        frame_meta = self._load_cantools_frame_meta()
        signals = self._load_signals(elements_by_path, elements_by_tag)
        pdus = self._load_pdus(elements_by_path, elements_by_tag, signals)
        pdu_triggers = self._load_pdu_triggers(elements_by_path, elements_by_tag)
        containers = self._load_containers(elements_by_path, elements_by_tag, pdu_triggers, pdus)
        messages = self._load_messages(elements_by_tag, frame_meta, containers, pdus)

        by_cluster: Dict[str, List[ArxmlMessage]] = {name: [] for name in clusters}
        for message in messages:
            by_cluster.setdefault(message.cluster_name, []).append(message)
        self._clusters = sorted(by_cluster.keys())
        self._messages_by_cluster = {
            cluster: sorted(items, key=lambda item: item.name)
            for cluster, items in by_cluster.items()
            if items or cluster in clusters
        }
        for cluster, items in self._messages_by_cluster.items():
            for message in items:
                self._messages_by_name[(cluster, message.name)] = message
                self._messages_by_id[(cluster, message.frame_id)] = message

    def _index_elements(
        self,
        element: ET.Element,
        path: str,
        by_path: Dict[str, ET.Element],
        by_tag: Dict[str, List[ET.Element]],
    ) -> None:
        tag = _local(element.tag)
        by_tag.setdefault(tag, []).append(element)
        name = _child_text(element, "SHORT-NAME")
        next_path = path
        if name:
            next_path = f"{path}/{name}" if path else f"/{name}"
            by_path[next_path] = element
            self._element_paths[id(element)] = next_path
        for child in list(element):
            self._index_elements(child, next_path, by_path, by_tag)

    def _load_clusters(self, by_tag: Dict[str, List[ET.Element]]) -> List[str]:
        names = []
        for cluster in by_tag.get("CAN-CLUSTER", []):
            name = _child_text(cluster, "SHORT-NAME")
            if name and name not in names:
                names.append(name)
        if not names:
            for trigger in by_tag.get("PDU-TRIGGERING", []):
                path = self._path(trigger)
                cluster = _cluster_from_ref(path)
                if cluster and cluster not in names:
                    names.append(cluster)
        return sorted(names)

    def _load_cantools_frame_meta(self) -> Dict[str, dict]:
        meta: Dict[str, dict] = {}
        if cantools is None:
            return meta
        try:
            db = cantools.database.load_file(self.path, strict=False)
        except Exception:
            return meta
        for msg in getattr(db, "messages", []) or []:
            meta[msg.name] = {
                "frame_id": int(getattr(msg, "frame_id", 0) or 0),
                "length": int(getattr(msg, "length", 0) or 0),
                "cluster": str(getattr(msg, "bus_name", "") or ""),
                "cycle": (
                    int(getattr(msg, "cycle_time"))
                    if getattr(msg, "cycle_time", None) is not None
                    else None
                ),
            }
        return meta

    def _load_signals(
        self,
        by_path: Dict[str, ET.Element],
        by_tag: Dict[str, List[ET.Element]],
    ) -> Dict[str, ArxmlSignal]:
        compu = self._load_compu_methods(by_path, by_tag)
        out: Dict[str, ArxmlSignal] = {}
        for element in by_tag.get("I-SIGNAL", []):
            short_name = _child_text(element, "SHORT-NAME")
            if not short_name:
                continue
            ref = self._path(element)
            system_ref = _first_ref_text(element, "SYSTEM-SIGNAL-REF")
            name = _short_ref(system_ref) if system_ref else _strip_signal_prefix(short_name)
            length = _safe_int(_child_text(element, "LENGTH"), 0)
            initial = _safe_float(_first_child_text(element, "VALUE"), 0.0)
            compu_ref = _first_ref_text(element, "COMPU-METHOD-REF")
            scale, offset, minimum, maximum = compu.get(compu_ref, (1.0, 0.0, None, None))
            signal = ArxmlSignal(
                name=name,
                ref=ref,
                start=0,
                length=length,
                scale=scale,
                offset=offset,
                minimum=minimum,
                maximum=maximum,
                initial=initial,
            )
            out[ref] = signal
        return out

    def _load_compu_methods(
        self,
        by_path: Dict[str, ET.Element],
        by_tag: Dict[str, List[ET.Element]],
    ) -> Dict[str, tuple[float, float, float | None, float | None]]:
        out: Dict[str, tuple[float, float, float | None, float | None]] = {}
        for element in by_tag.get("COMPU-METHOD", []):
            ref = self._path(element)
            numbers = [_safe_float(item.text, 0.0) for item in element.iter() if _local(item.tag) == "V"]
            scale = 1.0
            offset = 0.0
            if len(numbers) >= 2:
                offset = float(numbers[0])
                scale = float(numbers[1]) if float(numbers[1]) != 0 else 1.0
            if not isfinite(scale) or not isfinite(offset) or scale == 0:
                scale = 1.0
                offset = 0.0
            lowers: List[float] = []
            uppers: List[float] = []
            for child in element.iter():
                if _local(child.tag) == "LOWER-LIMIT":
                    lowers.append(_safe_float(child.text, 0.0))
                elif _local(child.tag) == "UPPER-LIMIT":
                    uppers.append(_safe_float(child.text, 0.0))
            scaled_limits = [
                float(value) * float(scale) + float(offset)
                for value in [*lowers, *uppers]
            ]
            lower = min(scaled_limits) if scaled_limits else None
            upper = max(scaled_limits) if scaled_limits else None
            out[ref] = (scale, offset, lower, upper)
        return out

    def _load_pdus(
        self,
        by_path: Dict[str, ET.Element],
        by_tag: Dict[str, List[ET.Element]],
        signals: Dict[str, ArxmlSignal],
    ) -> Dict[str, ArxmlPdu]:
        out: Dict[str, ArxmlPdu] = {}
        for element in by_tag.get("I-SIGNAL-I-PDU", []):
            name = _child_text(element, "SHORT-NAME")
            if not name:
                continue
            ref = self._path(element)
            pdu = ArxmlPdu(
                name=name,
                ref=ref,
                length=_safe_int(_child_text(element, "LENGTH"), 0),
                header_id=_safe_optional_int(_first_child_text(element, "HEADER-ID-SHORT-HEADER")),
            )
            for mapping in element.iter():
                if _local(mapping.tag) != "I-SIGNAL-TO-I-PDU-MAPPING":
                    continue
                signal_ref = _first_ref_text(mapping, "I-SIGNAL-REF")
                base = _lookup_ref(signals, signal_ref)
                if base is None:
                    continue
                byte_order = (
                    "big"
                    if _child_text(mapping, "PACKING-BYTE-ORDER") == "MOST-SIGNIFICANT-BYTE-FIRST"
                    else "little"
                )
                pdu.signals.append(
                    ArxmlSignal(
                        name=base.name,
                        ref=base.ref,
                        start=_safe_int(_child_text(mapping, "START-POSITION"), 0),
                        length=base.length,
                        byte_order=byte_order,
                        scale=base.scale,
                        offset=base.offset,
                        minimum=base.minimum,
                        maximum=base.maximum,
                        initial=base.initial,
                    )
                )
            out[ref] = pdu
        return out

    def _load_pdu_triggers(
        self,
        by_path: Dict[str, ET.Element],
        by_tag: Dict[str, List[ET.Element]],
    ) -> Dict[str, tuple[str, str]]:
        out: Dict[str, tuple[str, str]] = {}
        for trigger in by_tag.get("PDU-TRIGGERING", []):
            ref = self._path(trigger)
            pdu_ref = _first_ref_text(trigger, "I-PDU-REF")
            if ref and pdu_ref:
                out[ref] = (_cluster_from_ref(ref), pdu_ref)
        return out

    def _load_containers(
        self,
        by_path: Dict[str, ET.Element],
        by_tag: Dict[str, List[ET.Element]],
        pdu_triggers: Dict[str, tuple[str, str]],
        pdus: Dict[str, ArxmlPdu],
    ) -> Dict[str, tuple[str, List[ArxmlPdu], int | None]]:
        out: Dict[str, tuple[str, List[ArxmlPdu], int | None]] = {}
        for container in by_tag.get("CONTAINER-I-PDU", []):
            ref = self._path(container)
            cluster = ""
            contained: List[ArxmlPdu] = []
            for child in container.iter():
                if _local(child.tag) != "CONTAINED-PDU-TRIGGERING-REF":
                    continue
                trigger_ref = (child.text or "").strip()
                item = _lookup_ref(pdu_triggers, trigger_ref)
                if item is None:
                    continue
                trigger_cluster, pdu_ref = item
                trigger_cluster = trigger_cluster or _cluster_from_ref(trigger_ref)
                cluster = cluster or trigger_cluster
                pdu = _lookup_ref(pdus, pdu_ref)
                if pdu is not None:
                    contained.append(pdu)
            out[ref] = (
                cluster,
                contained,
                _infer_cycle_ms([_child_text(container, "SHORT-NAME") or ""] + [p.name for p in contained]),
            )
        return out

    def _path(self, element: ET.Element) -> str:
        return self._element_paths.get(id(element), "")

    def _load_messages(
        self,
        by_tag: Dict[str, List[ET.Element]],
        frame_meta: Dict[str, dict],
        containers: Dict[str, tuple[str, List[ArxmlPdu], int | None]],
        pdus: Dict[str, ArxmlPdu],
    ) -> List[ArxmlMessage]:
        out: List[ArxmlMessage] = []
        fallback_id = 0
        for frame in by_tag.get("CAN-FRAME", []):
            name = _child_text(frame, "SHORT-NAME")
            if not name:
                continue
            pdu_ref = None
            for mapping in frame.iter():
                if _local(mapping.tag) == "PDU-TO-FRAME-MAPPING":
                    pdu_ref = _first_ref_text(mapping, "PDU-REF")
                    break
            if not pdu_ref:
                continue
            contained: List[ArxmlPdu] = []
            cluster = ""
            cycle = None
            container = _lookup_ref(containers, pdu_ref)
            if container is not None:
                cluster, contained, cycle = container
            else:
                pdu = _lookup_ref(pdus, pdu_ref)
                if pdu is not None:
                    contained = [pdu]
            meta = frame_meta.get(name, {})
            if not cluster:
                cluster = str(meta.get("cluster") or "")
            if not cluster:
                cluster = "default"
            frame_id = int(meta.get("frame_id", fallback_id) or fallback_id)
            fallback_id += 1
            length = int(meta.get("length") or _safe_int(_child_text(frame, "FRAME-LENGTH"), 0))
            cycle = meta.get("cycle") or cycle or _infer_cycle_ms([name, _short_ref(pdu_ref)])
            out.append(
                ArxmlMessage(
                    name=name,
                    frame_id=frame_id,
                    length=length,
                    cluster_name=cluster,
                    cycle_time_ms=cycle,
                    contained_pdus=contained,
                )
            )
        return out


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in list(element):
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return None


def _first_child_text(element: ET.Element, name: str) -> str | None:
    for child in element.iter():
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return None


def _first_ref_text(element: ET.Element, tag_name: str) -> str:
    for child in element.iter():
        if _local(child.tag) == tag_name:
            return (child.text or "").strip()
    return ""


def _short_ref(ref: str) -> str:
    return str(ref or "").rstrip("/").split("/")[-1]


def _lookup_ref(mapping: Dict[str, object], ref: str):
    if ref in mapping:
        return mapping[ref]
    short = _short_ref(ref)
    for key, value in mapping.items():
        if _short_ref(key) == short:
            return value
    return None


def _strip_signal_prefix(name: str) -> str:
    if name.startswith("is") and len(name) > 2 and name[2].isupper():
        return name[2:]
    return name


def _cluster_from_ref(ref: str) -> str:
    parts = [part for part in str(ref or "").split("/") if part]
    if "VehicleTopology" in parts:
        idx = parts.index("VehicleTopology")
        if idx + 2 < len(parts):
            return parts[idx + 1]
    return ""


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip(), 0)
    except Exception:
        return int(default)


def _safe_optional_int(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _safe_int(text, 0)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return float(default)


def _infer_cycle_ms(names: Iterable[str]) -> int | None:
    for name in names:
        match = re.search(r"(?<!\d)(\d{2,5})ms(?![A-Za-z0-9])", str(name), flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _pack_bits(data: bytearray, start: int, length: int, raw: int, byte_order: str) -> None:
    if length <= 0:
        return
    raw = max(0, int(raw))
    positions = _bit_positions(start, length, byte_order)
    for idx, pos in enumerate(positions):
        bit = (raw >> (length - 1 - idx)) & 1 if byte_order == "big" else (raw >> idx) & 1
        byte_idx = pos // 8
        bit_idx = pos % 8
        if 0 <= byte_idx < len(data):
            if bit:
                data[byte_idx] |= 1 << bit_idx
            else:
                data[byte_idx] &= ~(1 << bit_idx)


def _unpack_bits(data: bytes, start: int, length: int, byte_order: str) -> int:
    value = 0
    positions = _bit_positions(start, length, byte_order)
    for idx, pos in enumerate(positions):
        byte_idx = pos // 8
        bit_idx = pos % 8
        bit = 0
        if 0 <= byte_idx < len(data):
            bit = (data[byte_idx] >> bit_idx) & 1
        if byte_order == "big":
            value = (value << 1) | bit
        else:
            value |= bit << idx
    return value


def _bit_positions(start: int, length: int, byte_order: str) -> List[int]:
    if byte_order == "big":
        out = []
        pos = int(start)
        for _ in range(length):
            out.append(pos)
            pos = pos + 15 if pos % 8 == 0 else pos - 1
        return out
    return [int(start) + idx for idx in range(length)]
