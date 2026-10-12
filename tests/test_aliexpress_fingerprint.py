"""The AliExpress phone keeps the Chrome version the browser really runs, in every place it states it.

Read live on 2026-10-12: the cached phone said Chrome 148 while the image ran Chrome 155.
The brand lists below are exactly what browserforge stored for 148 and what a real Chrome 155 reported.
"""

import json
from pathlib import Path

import pytest

from src.stores.aliexpress import chrome_grease_brands, with_chrome_version

ROOT = Path(__file__).resolve().parent.parent

OLD_FULL = "148.0.7778.96"
OLD_UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36"
B148 = [{"brand": "Chromium", "version": "148"}, {"brand": "Google Chrome", "version": "148"},
        {"brand": "Not/A)Brand", "version": "99"}]
F148 = [{"brand": "Chromium", "version": OLD_FULL}, {"brand": "Google Chrome", "version": OLD_FULL},
        {"brand": "Not/A)Brand", "version": "99.0.0.0"}]
NEW_FULL = "155.0.8059.39"
B155 = [{"brand": "Google Chrome", "version": "155"}, {"brand": "Chromium", "version": "155"},
        {"brand": "Not(A:Brand", "version": "24"}]


def _phone(extra_js=""):
    """A bundle shaped like the cached one: the script embeds the same values as JSON, headers included."""
    device = {"navigator": {"userAgent": OLD_UA, "appVersion": OLD_UA[8:], "userAgentData": {
        "brands": B148, "platformVersion": "17.0.0", "uaFullVersion": OLD_FULL, "fullVersionList": F148,
        "model": "SM-X"}}, "headers": {"sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
                                       "User-Agent": OLD_UA}}
    return {"ua": OLD_UA, "platform": "Linux armv81", "inject_js": f"(() => {{ const fp = {json.dumps(device)}; {extra_js} }})()",
            "ua_metadata": {"brands": B148, "fullVersionList": F148, "uaFullVersion": OLD_FULL, "model": "SM-X",
                            "platform": "Android", "mobile": True}}


class TestChromesOwnBrandRule:
    def test_it_gives_what_browserforge_stored_for_148(self):
        assert chrome_grease_brands(148, OLD_FULL) == (B148, F148)

    def test_it_gives_what_a_real_chrome_155_reported(self):
        brands, full = chrome_grease_brands(155, NEW_FULL)
        assert brands == B155
        assert [b["version"] for b in full] == [NEW_FULL, NEW_FULL, "24.0.0.0"]


class TestMovingThePhone:
    def test_every_place_gets_the_new_version(self):
        moved = with_chrome_version(_phone(), NEW_FULL)
        assert "Chrome/155.0.0.0 Mobile" in moved["ua"]
        assert moved["ua_metadata"]["brands"] == B155 and moved["ua_metadata"]["uaFullVersion"] == NEW_FULL
        assert "148" not in moved["inject_js"] and "148" not in moved["ua"]
        assert '\\"Google Chrome\\";v=\\"155\\"' in moved["inject_js"]

    def test_the_device_itself_stays(self):
        moved = with_chrome_version(_phone(), NEW_FULL)
        assert moved["ua_metadata"]["model"] == "SM-X" and moved["platform"] == "Linux armv81"
        assert '"model": "SM-X"' in moved["inject_js"]

    @pytest.mark.parametrize("full", [OLD_FULL, "", "155", None, "155.0.8059"])
    def test_nothing_changes_without_a_new_full_version(self, full):
        phone = _phone()
        assert with_chrome_version(phone, full) is phone

    def test_a_half_moved_phone_is_never_shipped(self):
        # An old version in a form the move does not know keeps the whole old bundle.
        phone = _phone(extra_js='const odd = "Chrome/148.1.2.3";')
        assert with_chrome_version(phone, NEW_FULL) is phone

    def test_moving_twice_changes_nothing(self):
        moved = with_chrome_version(_phone(), NEW_FULL)
        assert with_chrome_version(moved, NEW_FULL) is moved


class TestTheBotUsesIt:
    SOURCE = (ROOT / "src" / "stores" / "aliexpress.py").read_text(encoding="utf-8")

    def test_a_cached_phone_is_moved_and_saved(self):
        load = self.SOURCE.split("def _load_or_make_fingerprint", 1)[1].split("\n    def ", 1)[0]
        assert "return self._match_chrome(data, cache_path)" in load
        assert "data = with_chrome_version(data, chrome_version())" in load
        match = self.SOURCE.split("def _match_chrome", 1)[1].split("\n    def ", 1)[0]
        assert "cache_path.write_text(json.dumps(moved)" in match
