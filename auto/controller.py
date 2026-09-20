from __future__ import annotations

from .state import AutoModeSpec


class AutoController:
    """Registry for each delegate CLI's native autonomous permission mode."""

    _SPECS = {
        "codex": AutoModeSpec(
            target="codex",
            summary=(
                "workspace-write sandbox with approval prompts disabled; "
                "avoids the Codex auto-review quota path"
            ),
            cli_args=("-c", 'approval_policy="never"'),
        ),
        "claude": AutoModeSpec(
            target="claude",
            summary=(
                "Claude native auto permission classifier; unresolved prompts "
                "are denied instead of blocking the MCP call"
            ),
            cli_args=("--permission-mode", "auto", "--permission-prompts", "none"),
        ),
        "kiro": AutoModeSpec(
            target="kiro",
            summary="trust every tool exposed by the bounded Kiro worker profile",
            cli_args=("--trust-all-tools",),
        ),
        "gemini": AutoModeSpec(
            target="gemini",
            summary="Gemini autonomous YOLO approval mode",
            cli_args=("--approval-mode", "yolo"),
        ),
        "opencode": AutoModeSpec(
            target="opencode",
            summary="OpenCode auto-approves permissions that are not explicitly denied",
            cli_args=("--auto",),
        ),
    }

    @classmethod
    def spec(cls, target: str) -> AutoModeSpec:
        try:
            return cls._SPECS[target]
        except KeyError as exc:
            raise ValueError(f"auto mode is not configured for target: {target}") from exc

    @classmethod
    def args(cls, target: str) -> list[str]:
        return list(cls.spec(target).cli_args)

    @classmethod
    def describe(cls) -> dict[str, dict[str, object]]:
        return {target: spec.as_dict() for target, spec in cls._SPECS.items()}
