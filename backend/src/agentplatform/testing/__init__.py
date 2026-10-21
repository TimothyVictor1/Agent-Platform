"""Fakes and builders for tests: FakeModel, FakeSandbox, in-memory bus/stores.

Importable by any test (unit, integration, other packages' tests). Nothing here
touches the network, Docker or a real model.
"""

from agentplatform.testing.builders import (
    make_agent,
    make_board_entry,
    make_plan,
    make_session,
    make_task,
    make_task_draft,
    make_tasks,
)
from agentplatform.testing.fake_model import (
    FakeModel,
    assert_has_image,
    multi_tool_response,
    text_response,
    tool_response,
)
from agentplatform.testing.fake_sandbox import FakeSandbox, FakeSandboxProvider, RecordedAction
from agentplatform.testing.images import make_png, solid_png
from agentplatform.testing.memory_artifact_store import InMemoryArtifactStore
from agentplatform.testing.memory_bus import InMemoryBus, InMemoryLeaseStore
from agentplatform.testing.memory_event_store import InMemoryEventStore

__all__ = [
    "FakeModel",
    "FakeSandbox",
    "FakeSandboxProvider",
    "InMemoryArtifactStore",
    "InMemoryBus",
    "InMemoryEventStore",
    "InMemoryLeaseStore",
    "RecordedAction",
    "assert_has_image",
    "make_agent",
    "make_board_entry",
    "make_plan",
    "make_png",
    "make_session",
    "make_task",
    "make_task_draft",
    "make_tasks",
    "multi_tool_response",
    "solid_png",
    "text_response",
    "tool_response",
]
