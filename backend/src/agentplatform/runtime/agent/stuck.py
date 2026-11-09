"""Stuck detector (SPEC §10.6).

The agent loop feeds one ``StepSignals`` per step into ``StuckDetector.observe``.
The detector watches four signals (thresholds in ``StuckConfig``):

* **screen_unchanged**: the screenshot's pHash moved by at most
  ``unchanged_max_distance`` bits after a GUI action, ``unchanged_steps`` times in a
  row. Only GUI actions count (``run_command``, ``wait`` and ``zoom`` legitimately
  leave the screen as it is).
* **repetition**: the same action with the same arguments (numbers within
  ``repeat_tolerance_px``) ``repeat_count`` times within the last ``repeat_window``
  steps.
* **no_progress**: no progress evidence (board post, window title/URL change,
  ``submit_result`` attempt) for ``no_progress_steps`` steps.
* **error_streak**: ``error_streak`` tool errors in a row (counted per executed call).

The loop also reports ``no_tool`` (the model produced no tool call after retries)
through ``report_no_tool``.

A detection opens an *episode*: no further signal is reported until the episode
closes, so the Supervisor gets one ``agent.stuck_detected`` per episode. The episode
closes on real progress: the screen changed after an action, or progress evidence
was reported.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agentplatform.contracts.events import StuckReason

__all__ = [
    "GUI_TOOLS",
    "StepSignals",
    "StuckConfig",
    "StuckDetector",
    "StuckSignal",
    "phash_distance",
]

GUI_TOOLS = frozenset({"click", "move", "drag", "scroll", "type_text", "key"})
"""Desktop tools that are expected to change the screen."""


@dataclass(frozen=True, slots=True)
class StuckConfig:
    unchanged_steps: int = 6
    unchanged_max_distance: int = 2
    repeat_count: int = 4
    repeat_window: int = 8
    repeat_tolerance_px: float = 10.0
    no_progress_steps: int = 40
    error_streak: int = 5


@dataclass(frozen=True, slots=True)
class StepSignals:
    """What happened in one step, from the detector's point of view.

    ``phash`` is the pHash of the observation the model saw at this step (taken
    *before* this step's action). ``action``/``action_args`` describe the terminal
    call that was executed (``None`` if none). ``call_errors`` has one bool per
    executed call, in order. ``progress`` is true when the step produced progress
    evidence (successful board post, window title change, submit attempt).
    """

    step: int
    phash: str
    action: str | None = None
    action_args: Mapping[str, Any] = field(default_factory=dict)
    call_errors: tuple[bool, ...] = ()
    progress: bool = False
    frame_id: str | None = None


@dataclass(frozen=True, slots=True)
class StuckSignal:
    reason: StuckReason
    detail: str
    frame_ids: list[str]


def phash_distance(a: str, b: str) -> int:
    """Hamming distance between two hex perceptual hashes of equal length."""
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _same_args(a: Mapping[str, Any], b: Mapping[str, Any], tolerance: float) -> bool:
    if a.keys() != b.keys():
        return False
    for key, va in a.items():
        vb = b[key]
        numeric = (int, float)
        if (
            isinstance(va, numeric)
            and isinstance(vb, numeric)
            and not isinstance(va, bool)
            and not isinstance(vb, bool)
        ):
            if abs(float(va) - float(vb)) > tolerance:
                return False
        elif va != vb:
            return False
    return True


class StuckDetector:
    """Stateful detector for one agent working on one task. See the module docstring."""

    def __init__(self, config: StuckConfig | None = None) -> None:
        self.config = config or StuckConfig()
        self._prev_phash: str | None = None
        self._prev_was_gui = False
        self._unchanged = 0
        self._actions: deque[tuple[int, str, Mapping[str, Any]]] = deque()
        self._steps_without_progress = 0
        self._error_streak = 0
        self._frames: deque[str] = deque(maxlen=3)
        self._episode: StuckReason | None = None

    @property
    def episode(self) -> StuckReason | None:
        """Reason of the open stuck episode, or ``None`` when not stuck."""
        return self._episode

    def observe(self, s: StepSignals) -> StuckSignal | None:
        """Feed one step. Returns a signal when a new stuck episode starts."""
        cfg = self.config
        if s.frame_id:
            self._frames.append(s.frame_id)
        screen_changed = False
        if self._prev_phash is not None and self._prev_was_gui:
            if phash_distance(self._prev_phash, s.phash) <= cfg.unchanged_max_distance:
                self._unchanged += 1
            else:
                self._unchanged = 0
                screen_changed = True
        self._prev_phash = s.phash
        self._prev_was_gui = s.action in GUI_TOOLS

        if s.action is not None:
            self._actions.append((s.step, s.action, dict(s.action_args)))
        while self._actions and self._actions[0][0] <= s.step - cfg.repeat_window:
            self._actions.popleft()

        for is_error in s.call_errors:
            self._error_streak = self._error_streak + 1 if is_error else 0

        if s.progress:
            self._steps_without_progress = 0
        else:
            self._steps_without_progress += 1

        if self._episode is not None:
            if screen_changed or s.progress:
                self._episode = None
            return None

        if self._unchanged >= cfg.unchanged_steps:
            return self._open(
                "screen_unchanged",
                f"screen did not change after {self._unchanged} GUI actions in a row",
            )
        if s.action is not None:
            repeats = sum(
                1
                for _, name, args in self._actions
                if name == s.action and _same_args(args, s.action_args, cfg.repeat_tolerance_px)
            )
            if repeats >= cfg.repeat_count:
                self._actions.clear()
                return self._open(
                    "repetition",
                    f"{s.action} repeated {repeats} times within {cfg.repeat_window} steps",
                )
        if self._error_streak >= cfg.error_streak:
            streak = self._error_streak
            self._error_streak = 0
            return self._open("error_streak", f"{streak} tool errors in a row")
        if self._steps_without_progress >= cfg.no_progress_steps:
            steps = self._steps_without_progress
            self._steps_without_progress = 0
            return self._open("no_progress", f"no progress for {steps} steps")
        return None

    def report_no_tool(self, detail: str = "model produced no tool call") -> StuckSignal | None:
        """The loop gave up getting a tool call for a step. Opens an episode unless
        one is already open."""
        if self._episode is not None:
            return None
        return self._open("no_tool", detail)

    def _open(self, reason: StuckReason, detail: str) -> StuckSignal:
        self._episode = reason
        self._unchanged = 0
        return StuckSignal(reason=reason, detail=detail, frame_ids=list(self._frames))
