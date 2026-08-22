from __future__ import annotations

import json
from pathlib import Path
from typing import List

from can_gateway.core.models import AppConfig


def find_invalid_route_ports(config: AppConfig) -> List[str]:
    known_ports = {item.name for item in config.hardware if item.name}
    issues: List[str] = []
    for rule in config.routes:
        if rule.source_port and rule.source_port not in known_ports:
            issues.append(
                f"Rule '{rule.name}' references missing source port '{rule.source_port}'."
            )
        if rule.target_port and rule.target_port not in known_ports:
            issues.append(
                f"Rule '{rule.name}' references missing target port '{rule.target_port}'."
            )
    return issues


class ConfigStore:
    def load(self, path: str) -> AppConfig:
        file_path = Path(path)
        if not file_path.exists():
            return AppConfig()
        with file_path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return AppConfig.from_dict(data)

    def save(self, path: str, config: AppConfig) -> None:
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, indent=2, ensure_ascii=False)
