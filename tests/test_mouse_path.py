"""The bot's mouse moves like a hand: a curve from where it was, never a jump straight onto the button."""

import math
import random
from pathlib import Path

import pytest

from src.core.claimer import mouse_path


class TestMousePath:
    @pytest.mark.parametrize("seed", range(20))
    def test_it_ends_exactly_on_the_target(self, seed):
        points = mouse_path((100, 600), (900, 200), random.Random(seed))
        assert points[-1] == pytest.approx((900, 200))

    def test_a_long_move_takes_more_steps_than_a_short_one(self):
        rng = random.Random(1)
        assert len(mouse_path((0, 0), (1000, 0), rng)) > len(mouse_path((0, 0), (50, 0), rng))

    @pytest.mark.parametrize("start,end", [((0, 0), (5, 5)), ((0, 0), (5000, 5000))])
    def test_the_step_count_stays_within_bounds(self, start, end):
        assert 8 <= len(mouse_path(start, end, random.Random(2))) <= 40

    def test_it_is_not_a_straight_line(self):
        # A hand never draws a ruler line; most seeds bend the path well off it.
        bent = 0
        for seed in range(20):
            points = mouse_path((0, 0), (1000, 0), random.Random(seed))
            bent += max(abs(y) for _, y in points) > 20
        assert bent >= 15

    def test_it_starts_slow_and_ends_slow(self):
        points = mouse_path((0, 0), (1000, 0), random.Random(3))
        steps = [math.dist(a, b) for a, b in zip([(0, 0)] + points, points)]
        middle = steps[len(steps) // 2]
        assert steps[0] < middle and steps[-1] < middle


class TestTheClickLooksLikeAMouse:
    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "core" / "claimer.py").read_text(encoding="utf-8")
    CLICK = SOURCE.split("async def _human_click", 1)[1].split("\n    async def ", 1)[0]
    MOVE = SOURCE.split("async def _move_mouse", 1)[1].split("\n    async def ", 1)[0]
    WHEEL = SOURCE.split("async def _wheel_scroll", 1)[1].split("\n    async def ", 1)[0]

    def test_whole_pixels_only(self):
        # A real mouse never reports clientX 101.37 (checked in Chrome on 2026-10-09).
        assert "point = (round(px), round(py))" in self.MOVE and "tx = round(" in self.CLICK
        assert "WHEEL_NOTCH" in self.WHEEL

    def test_a_pressed_button_has_pressure(self):
        assert "force=0.5" in self.CLICK.split('type_="mousePressed"', 1)[1].split(")", 1)[0]


class TestTheWheel:
    class _Page:
        def __init__(self):
            self.sent = []

        async def send(self, command):
            # nodriver commands are generators that yield the raw CDP message first.
            self.sent.append(next(command))

        async def evaluate(self, js):
            return "[1280, 720]"

    def test_the_notches_add_up_to_the_distance(self, monkeypatch):
        import asyncio
        from src.core.claimer import BaseClaimer

        async def no_wait(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", no_wait)
        stub = type("Stub", (), {})()
        stub.page, stub._mouse_xy = self._Page(), (600, 300)
        stub._move_mouse = lambda x, y: BaseClaimer._move_mouse(stub, x, y)
        asyncio.run(BaseClaimer._wheel_scroll(stub, 640, 360, 700))
        wheel = [c["params"] for c in stub.page.sent if c["params"].get("type") == "mouseWheel"]
        # 700 px is six notches of 120, the size a real wheel gives under TurboVNC.
        assert [w["deltaY"] for w in wheel] == [120] * 6

    def test_up_is_negative_notches(self, monkeypatch):
        import asyncio
        from src.core.claimer import BaseClaimer

        async def no_wait(seconds):
            pass
        monkeypatch.setattr(asyncio, "sleep", no_wait)
        stub = type("Stub", (), {})()
        stub.page, stub._mouse_xy = self._Page(), (600, 300)
        stub._move_mouse = lambda x, y: BaseClaimer._move_mouse(stub, x, y)
        asyncio.run(BaseClaimer._wheel_scroll(stub, 640, 360, -250))
        wheel = [c["params"]["deltaY"] for c in stub.page.sent if c["params"].get("type") == "mouseWheel"]
        assert wheel == [-120, -120]
