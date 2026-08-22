from __future__ import annotations

from typing import Dict, Iterable, Tuple

from can_gateway.core.models import AlgorithmKind, AlgorithmPreset


def _reflect_bits(value: int, width: int) -> int:
    out = 0
    for index in range(width):
        if value & (1 << index):
            out |= 1 << (width - 1 - index)
    return out


def compute_crc(data: bytes, preset: AlgorithmPreset) -> int:
    width = max(1, min(int(preset.width), 32))
    mask = (1 << width) - 1
    polynomial = int(preset.polynomial) & mask
    crc = int(preset.initial_value) & mask
    top_bit = 1 << (width - 1)

    for raw_byte in data:
        byte_value = int(raw_byte) & 0xFF
        if preset.reflect_input:
            byte_value = _reflect_bits(byte_value, 8)
        crc ^= byte_value << max(0, width - 8)
        crc &= mask
        for _ in range(8):
            if crc & top_bit:
                crc = ((crc << 1) ^ polynomial) & mask
            else:
                crc = (crc << 1) & mask

    if preset.reflect_output:
        crc = _reflect_bits(crc, width)
    crc ^= int(preset.xor_out) & mask
    return crc & mask


class AlgorithmService:
    def __init__(self, presets: Iterable[AlgorithmPreset] | None = None) -> None:
        self._presets: Dict[str, AlgorithmPreset] = {}
        self._rolling_state: Dict[Tuple[str, str], int] = {}
        self.update_presets(presets or [])

    def update_presets(self, presets: Iterable[AlgorithmPreset]) -> None:
        self._presets = {preset.name: preset for preset in presets if preset.name}
        self._rolling_state.clear()

    def preset_names(self, kind: AlgorithmKind | None = None) -> list[str]:
        items = list(self._presets.values())
        if kind is not None:
            items = [item for item in items if item.kind == kind]
        return [item.name for item in items]

    def get_preset(self, name: str) -> AlgorithmPreset:
        if name not in self._presets:
            raise KeyError(f"Unknown algorithm preset '{name}'.")
        return self._presets[name]

    def next_rolling_counter(self, preset_name: str, scope_key: str) -> int:
        preset = self.get_preset(preset_name)
        if preset.kind != AlgorithmKind.ROLLING_COUNTER:
            raise ValueError(f"Preset '{preset_name}' is not a rolling counter preset.")
        minimum = int(preset.minimum)
        maximum = int(preset.maximum)
        if maximum < minimum:
            raise ValueError(f"Rolling counter preset '{preset_name}' has invalid min/max.")
        current = self._rolling_state.get((preset_name, scope_key))
        if current is None:
            current = int(preset.start_value)
            if current < minimum or current > maximum:
                span = maximum - minimum + 1
                current = minimum if span <= 0 else minimum + ((current - minimum) % span)
        result = current
        span = maximum - minimum + 1
        next_value = minimum if span <= 0 else minimum + ((current - minimum + int(preset.step)) % span)
        self._rolling_state[(preset_name, scope_key)] = next_value
        return result

    def compute_crc(self, preset_name: str, data: bytes) -> int:
        preset = self.get_preset(preset_name)
        if preset.kind != AlgorithmKind.CRC:
            raise ValueError(f"Preset '{preset_name}' is not a CRC preset.")
        return compute_crc(data, preset)

