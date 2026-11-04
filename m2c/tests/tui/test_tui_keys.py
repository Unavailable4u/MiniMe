"""Patch C09a: Esc versus Esc Esc (m2c.tui.keys)."""
from m2c.tui.keys import EscapeTracker


def test_first_press_interrupts_second_within_the_window_rewinds():
    t = [0.0]
    tracker = EscapeTracker(clock=lambda: t[0], window_s=0.6)
    assert tracker.press() == "interrupt"
    t[0] = 0.3
    assert tracker.press() == "rewind"


def test_a_slow_second_press_is_a_new_interrupt_and_a_third_starts_over():
    t = [0.0]
    tracker = EscapeTracker(clock=lambda: t[0], window_s=0.6)
    assert tracker.press() == "interrupt"
    t[0] = 5.0
    assert tracker.press() == "interrupt"
    t[0] = 5.2
    assert tracker.press() == "rewind"
    t[0] = 5.3
    assert tracker.press() == "interrupt"      # the pair was consumed
