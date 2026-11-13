"""The supervisor only gets screenshots when its model reads images (T13).

In the first live run every supervisor call to GLM-5.3 (text-only on Nebius) failed
with HTTP 400 "This model does not support image input" because stuck runs attach
screenshots.
"""

from __future__ import annotations

from agentplatform.config import RoleConfig, RoleName
from agentplatform.runtime.container import supervisor_frames
from agentplatform.testing import InMemoryArtifactStore


class _Roles:
    def __init__(self, role: RoleConfig | None) -> None:
        self._role = role
        self.asked: list[tuple[str, RoleName]] = []

    def role(self, profile: str, role: RoleName) -> RoleConfig | None:
        self.asked.append((profile, role))
        return self._role


def _role(*, vision: bool) -> RoleConfig:
    return RoleConfig(
        provider="openai_compatible", base_url="https://x.test/v1", model="m", vision=vision
    )


def test_text_only_supervisor_gets_no_frames() -> None:
    frames = InMemoryArtifactStore()
    roles = _Roles(_role(vision=False))
    assert supervisor_frames(roles, "nebius", frames) is None
    assert roles.asked == [("nebius", "supervisor")]


def test_vision_supervisor_and_undescribed_roles_keep_frames() -> None:
    frames = InMemoryArtifactStore()
    assert supervisor_frames(_Roles(_role(vision=True)), "p", frames) is frames
    assert supervisor_frames(_Roles(None), "p", frames) is frames
