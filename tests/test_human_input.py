"""Typing and clicking like a hand: the keys a keyboard would send, and a mouse click only on what can be clicked."""

import asyncio
import logging

import pytest

import src.core.claimer as claimer_module
from src.core.claimer import BaseClaimer, key_for


class _Page:
    def __init__(self, boxes=None):
        self.sent, self.boxes = [], list(boxes or [])

    async def send(self, command):
        self.sent.append(next(command)["params"])

    async def evaluate(self, expression):
        return self.boxes.pop(0) if self.boxes else '{"reason": "missing"}'


class _Hand:
    logger = logging.getLogger("test")
    _bring_into_view = BaseClaimer._bring_into_view
    _read_box = BaseClaimer._read_box
    _type_like_a_hand = BaseClaimer._type_like_a_hand
    _modifier_key = BaseClaimer._modifier_key
    _press_key = BaseClaimer._press_key
    _press_enter = BaseClaimer._press_enter
    _select_all = BaseClaimer._select_all
    _click_like_a_hand = BaseClaimer._click_like_a_hand

    def __init__(self, boxes=None):
        self.page, self.clicks, self.wheels = _Page(boxes), [], []

    async def _human_click(self, x, y, w=0, h=0):
        self.clicks.append((x, y))

    async def _wheel_scroll(self, x, y, dy):
        self.wheels.append(dy)

    async def _page_wheel_point(self):
        return (640, 360)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(claimer_module.asyncio, "sleep", no_sleep)


class TestKeys:
    @pytest.mark.parametrize("char,expected", [
        ("a", ("KeyA", 65, False)), ("Q", ("KeyQ", 81, True)), ("7", ("Digit7", 55, False)),
        ("@", ("Digit2", 50, True)), ("+", ("Equal", 187, True)), (".", ("Period", 190, False)),
        (" ", ("Space", 32, False)), ("_", ("Minus", 189, True)), ("ł", ("", 0, False)),
    ])
    def test_a_us_keyboard_key_for_each_character(self, char, expected):
        assert key_for(char) == expected


class TestTyping:
    def test_a_letter_with_no_key_is_put_in_like_an_input_method_does(self):
        # Probe 7.11 in Chrome: "ł" arrived as a key press with code "" and keyCode 0, which no keyboard sends.
        hand = _Hand()
        asyncio.run(hand._type_like_a_hand("Zł日"))
        assert not [p for p in hand.page.sent if p.get("type") in ("keyDown", "keyUp") and not p.get("code")]
        assert {"text": "ł"} in hand.page.sent and {"text": "日"} in hand.page.sent
        # Shift from the capital is let go before the inserted letter.
        shift = [i for i, p in enumerate(hand.page.sent) if p.get("key") == "Shift"]
        assert len(shift) == 2 and shift[1] < hand.page.sent.index({"text": "ł"})

    def test_every_character_is_a_key_down_and_up_with_shift_as_a_key_of_its_own(self):
        hand = _Hand()
        asyncio.run(hand._type_like_a_hand("Ab@1"))
        assert [(p["type"], p["key"], p.get("modifiers")) for p in hand.page.sent] == [
            ("keyDown", "Shift", 8), ("keyDown", "A", 8), ("keyUp", "A", 8), ("keyUp", "Shift", 0),
            ("keyDown", "b", 0), ("keyUp", "b", 0),
            ("keyDown", "Shift", 8), ("keyDown", "@", 8), ("keyUp", "@", 8), ("keyUp", "Shift", 0),
            ("keyDown", "1", 0), ("keyUp", "1", 0)]
        typed = [p for p in hand.page.sent if p["type"] == "keyDown" and p["key"] != "Shift"]
        assert "".join(p["text"] for p in typed) == "Ab@1"
        assert all("text" not in p for p in hand.page.sent if p["type"] == "keyUp" or p["key"] == "Shift")

    def test_shift_stays_down_across_capitals_in_a_row(self):
        hand = _Hand()
        asyncio.run(hand._type_like_a_hand("AB"))
        assert [p["key"] for p in hand.page.sent] == ["Shift", "A", "A", "B", "B", "Shift"]

    def test_enter_is_a_key_with_its_text(self):
        hand = _Hand()
        asyncio.run(hand._press_enter())
        assert [(p["type"], p["key"]) for p in hand.page.sent] == [("keyDown", "Enter"), ("keyUp", "Enter")]
        assert hand.page.sent[0]["text"] == "\r"

    def test_select_all_is_control_held_around_a_without_text(self):
        hand = _Hand()
        asyncio.run(hand._select_all())
        assert [(p["type"], p["key"], p["modifiers"]) for p in hand.page.sent] == [
            ("keyDown", "Control", 2), ("keyDown", "a", 2), ("keyUp", "a", 2), ("keyUp", "Control", 0)]
        assert hand.page.sent[1]["commands"] == ["selectAll"] and "text" not in hand.page.sent[1]


class TestClicking:
    def test_a_clickable_element_gets_a_mouse_click_at_the_checked_spot(self):
        hand = _Hand(['{"x": 120, "y": 340, "key": "BUTTON|Search"}'])
        assert asyncio.run(hand._click_like_a_hand("() => document.body", "Search")) is True
        assert hand.clicks == [(120, 340)]

    def test_an_offscreen_element_is_wheeled_into_view_first(self):
        hand = _Hand(['{"reason": "offscreen", "dy": 900}', '{"x": 50, "y": 60, "key": "A|Card"}'])
        assert asyncio.run(hand._click_like_a_hand("() => null", "Card")) is True
        assert hand.wheels == [900] and hand.clicks == [(50, 60)]

    @pytest.mark.parametrize("box", ['{"reason": "missing"}', '{"reason": "covered"}', '{"reason": "disabled"}'])
    def test_nothing_is_clicked_when_it_cannot_be(self, box):
        hand = _Hand([box])
        assert asyncio.run(hand._click_like_a_hand("() => null", "Card")) is False and hand.clicks == []
