from __future__ import annotations

import sys
from pathlib import Path


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def app_path(*parts: str) -> Path:
    return app_root().joinpath(*parts)


def save_root() -> Path:
    return app_path("save")


def save_path(*parts: str) -> Path:
    return save_root().joinpath(*parts)


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return Path(meipass)
    return app_root()


def resource_path(*parts: str) -> Path:
    return resource_root().joinpath(*parts)
