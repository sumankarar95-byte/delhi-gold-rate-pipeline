from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    raw: dict

    @classmethod
    def load(cls, path: Path | str = ROOT / "config" / "settings.yaml") -> "Settings":
        with open(path) as f:
            return cls(yaml.safe_load(f))

    def __getitem__(self, key):
        return self.raw[key]

    def path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else ROOT / p
