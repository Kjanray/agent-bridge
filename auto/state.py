from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AutoModeSpec:
    """Native CLI mapping for one harness's trusted autonomous mode."""

    target: str
    summary: str
    cli_args: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["cli_args"] = list(self.cli_args)
        return data
