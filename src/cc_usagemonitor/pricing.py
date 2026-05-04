from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .paths import PRICING_FILE


@dataclass(frozen=True)
class ModelPrice:
    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float

    def cost(self, usage: dict) -> float:
        """Compute USD cost for a single usage block (per-million pricing)."""
        cc = usage.get("cache_creation") or {}
        c_5m = cc.get("ephemeral_5m_input_tokens", 0)
        c_1h = cc.get("ephemeral_1h_input_tokens", 0)
        if not cc:
            # Fallback: lump sum cache_creation_input_tokens treated as 5m.
            c_5m = usage.get("cache_creation_input_tokens", 0)

        total = (
            usage.get("input_tokens", 0) * self.input
            + usage.get("output_tokens", 0) * self.output
            + usage.get("cache_read_input_tokens", 0) * self.cache_read
            + c_5m * self.cache_write_5m
            + c_1h * self.cache_write_1h
        )
        return total / 1_000_000


class PricingTable:
    def __init__(self, path: Path | None = None):
        self.path = path or PRICING_FILE
        self._models: dict[str, ModelPrice] = {}
        self.reload()

    def reload(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        models = data.get("models", {})
        self._models = {
            name: ModelPrice(**price)
            for name, price in models.items()
            if not name.startswith("_") or name == "_default"
        }

    def for_model(self, model: str | None) -> ModelPrice:
        if not model:
            return self._models["_default"]
        if model in self._models:
            return self._models[model]
        # Try stripping date suffix / [1m] tag.
        stripped = re.sub(r"\[.*?\]$", "", model)
        stripped = re.sub(r"-\d{8}$", "", stripped)
        if stripped in self._models:
            return self._models[stripped]
        return self._models["_default"]
