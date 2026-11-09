"""StuckDetector: the four signals of SPEC §10.6, episodes and resets."""

from __future__ import annotations

from typing import Any

from agentplatform.runtime.agent.stuck import (
    StepSignals,
    StuckConfig,
    StuckDetector,
    phash_distance,
)

SAME = "8f373714acfcf4d0"
OTHER = "70c8c8eb53030b2f"


def feed(
    det: StuckDetector,
    step: int,
    phash: str = SAME,
    action: str | None = "click",
    progress: bool = False,
    errors: tuple[bool, ...] = (False,),
    **args: Any,
) -> Any:
    return det.observe(
        StepSignals(
            step=step,
            phash=phash,
            action=action,
            action_args=args or {"x": step * 50, "y": 10},
            call_errors=errors,
            progress=progress,
            frame_id=f"f{step}",
        )
    )


def episode(det: StuckDetector) -> str | None:
    """Read the property through a call so mypy does not narrow it across steps."""
    return det.episode


def test_phash_distance() -> None:
    assert phash_distance(SAME, SAME) == 0
    assert phash_distance("0000000000000000", "0000000000000003") == 2
    assert phash_distance(SAME, OTHER) > 2


def test_screen_unchanged_after_six_gui_actions() -> None:
    det = StuckDetector()
    signals = [feed(det, step) for step in range(1, 8)]
    # step 1 has no predecessor; steps 2..7 are 6 unchanged screens after GUI actions
    assert signals[:6] == [None] * 6
    sig = signals[6]
    assert sig is not None
    assert sig.reason == "screen_unchanged"
    assert sig.frame_ids == ["f5", "f6", "f7"]


def test_non_gui_actions_do_not_count_as_unchanged_screen() -> None:
    det = StuckDetector(StuckConfig(no_progress_steps=1000))
    for step in range(1, 20):
        assert feed(det, step, action="run_command", cmd=f"echo {step}") is None


def test_screen_change_resets_unchanged_counter() -> None:
    det = StuckDetector()
    for step in range(1, 6):
        assert feed(det, step) is None
    assert feed(det, 6, phash=OTHER) is None  # changed
    for step in range(7, 12):
        assert feed(det, step, phash=OTHER) is None
    assert feed(det, 12, phash=OTHER) is not None


def test_repetition_within_tolerance() -> None:
    det = StuckDetector()
    hashes = [SAME, OTHER]
    results = [
        feed(det, step, phash=hashes[step % 2], x=500 + step, y=300 - step) for step in range(1, 5)
    ]
    assert results[:3] == [None, None, None]
    assert results[3] is not None
    assert results[3].reason == "repetition"


def test_different_args_are_not_repetition() -> None:
    det = StuckDetector()
    hashes = [SAME, OTHER]
    for step in range(1, 12):
        assert feed(det, step, phash=hashes[step % 2], x=step * 40, y=300) is None


def test_repetition_outside_window_is_ignored() -> None:
    det = StuckDetector(StuckConfig(repeat_window=4))
    hashes = [SAME, OTHER]
    for step in range(1, 20, 2):  # same action only every other step: 2 per window of 4
        assert feed(det, step, phash=hashes[step % 2], x=10, y=10) is None
        assert feed(det, step + 1, phash=hashes[(step + 1) % 2], x=900, y=step) is None


def test_error_streak() -> None:
    det = StuckDetector()
    hashes = [SAME, OTHER]
    assert feed(det, 1, phash=OTHER, errors=(True, True)) is None
    assert feed(det, 2, phash=SAME, errors=(True, True)) is None
    sig = feed(det, 3, phash=hashes[1], errors=(True,))
    assert sig is not None
    assert sig.reason == "error_streak"


def test_success_breaks_error_streak() -> None:
    det = StuckDetector()
    hashes = [SAME, OTHER]
    for step in range(1, 12):
        errs = (True, True) if step % 2 else (False,)
        assert feed(det, step, phash=hashes[step % 2], errors=errs) is None


def test_no_progress() -> None:
    det = StuckDetector(StuckConfig(no_progress_steps=5))
    hashes = [SAME, OTHER]
    results = [feed(det, s, phash=hashes[s % 2]) for s in range(1, 6)]
    assert results[:4] == [None] * 4
    assert results[4] is not None
    assert results[4].reason == "no_progress"


def test_progress_resets_no_progress() -> None:
    det = StuckDetector(StuckConfig(no_progress_steps=5))
    hashes = [SAME, OTHER]
    for s in range(1, 30):
        assert feed(det, s, phash=hashes[s % 2], progress=s % 4 == 0) is None


def test_one_signal_per_episode_and_reset_after_progress() -> None:
    det = StuckDetector()
    for step in range(1, 7):
        feed(det, step)
    assert feed(det, 7) is not None
    assert episode(det) == "screen_unchanged"
    for step in range(8, 20):
        assert feed(det, step) is None  # still stuck, not reported again
    assert feed(det, 20, phash=OTHER) is None  # screen changed -> episode closed
    assert episode(det) is None
    for step in range(21, 26):
        assert feed(det, step, phash=OTHER) is None
    again = feed(det, 26, phash=OTHER)
    assert again is not None
    assert again.reason == "screen_unchanged"


def test_report_no_tool_respects_episode() -> None:
    det = StuckDetector()
    first = det.report_no_tool()
    assert first is not None
    assert first.reason == "no_tool"
    assert det.report_no_tool() is None
    feed(det, 1, progress=True)
    assert episode(det) is None
    assert det.report_no_tool() is not None
