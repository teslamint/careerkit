from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TheVCAdapter:
    name: str = "thevc"
    supports_search: bool = False

    def native_role_mapping(self) -> dict[str, object]:
        return {}
