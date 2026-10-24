"""Unity Asset Store weekly giveaway detection.

The asset is free only with a coupon that changes weekly, so a missing coupon must
never yield a half-filled result that the claimer would act on.
"""

import re
from pathlib import Path

import pytest

from src.stores.unity import (UnityClaimer, _package_id, _slug, asset_name_from_title,
                              checkout_blockers, parse_free_asset, parse_total)

# What assetstore.unity.com/publisher-sale served on 2026-08-09.
REAL_TEXT = (
    "PUBLISHER OF THE WEEK Save 50% on assets get a free gift in this week's Publisher Sale. "
    "PUBLISHER ASSET GIVEAWAY Real Materials vol.10 - Patterns "
    "Add this week's featured asset to your cart, then enter the coupon code LEX4ART2026 "
    "at checkout to get it for free. No purchase necessary."
)
REAL_LINK = {"href": "/packages/2d/textures-materials/metals/real-materials-vol-10-patterns-46039",
             "text": "GET YOUR FREE GIFT"}
REAL_STATE = {"text": REAL_TEXT, "giftLinks": [REAL_LINK]}


class TestRealPage:
    def test_reads_the_whole_giveaway(self):
        assert parse_free_asset(REAL_STATE) == {
            "title": "Real Materials vol.10 - Patterns",
            "url": "https://assetstore.unity.com" + REAL_LINK["href"],
            "slug": "real-materials-vol-10-patterns-46039",
            "package_id": "46039",
            "coupon": "LEX4ART2026",
        }

    def test_absolute_link_is_left_alone(self):
        state = dict(REAL_STATE, giftLinks=[dict(REAL_LINK, href="https://assetstore.unity.com/packages/x-1")])
        assert parse_free_asset(state)["url"] == "https://assetstore.unity.com/packages/x-1"

    def test_slug_reading(self):
        assert _slug("/packages/2d/textures/thing-46039") == "thing-46039"
        assert _slug("/packages/2d/textures/thing-46039/") == "thing-46039"
        assert _slug("") == ""

    @pytest.mark.parametrize("href,expected", [
        ("/packages/2d/textures/thing-46039", "46039"),
        ("/packages/2d/textures/thing-46039/", "46039"),
        ("/packages/tools/some-2019-pack-123456", "123456"),
        ("/packages/tools/no-number-here", ""),
        ("", ""),
        # Since 2026-10-01 the gift link is the bare id, which used to give no number at all.
        ("/packages/package/id/250947", "250947"),
        ("/packages/package/id/250947/", "250947"),
    ])
    def test_package_id_is_the_number_entitlements_reports(self, href, expected):
        # The entitlements API answers with productId "46039", never the whole slug.
        assert _package_id(href) == expected

    def test_a_bare_id_link_still_makes_a_full_giveaway(self):
        state = dict(REAL_STATE, giftLinks=[dict(REAL_LINK, href="/packages/package/id/250947")])
        asset = parse_free_asset(state)
        assert asset["package_id"] == "250947" and asset["slug"] == "250947"


class TestTheAssetPageNamesTheAsset:
    """28.09: the sale heading still said PathGrid while its link already led to Magic Time."""

    @pytest.mark.parametrize("title,expected", [
        ("USA Real-World Heightmaps - Vol.1 | 3D Landscapes | Unity Asset Store", "USA Real-World Heightmaps - Vol.1"),
        ("PathGrid | Level Design | Unity Asset Store", "PathGrid"),
        ("Unity Asset Store", ""),
        ("Sign in | Unity ID", ""),
        ("", ""),
        (None, ""),
    ])
    def test_the_name_comes_from_a_store_tab_title_only(self, title, expected):
        assert asset_name_from_title(title) == expected


class TestCouponGuard:
    """No coupon means no giveaway, whatever else the page says."""

    def test_missing_coupon_yields_nothing(self):
        text = REAL_TEXT.replace("the coupon code LEX4ART2026", "a code")
        assert parse_free_asset(dict(REAL_STATE, text=text)) is None

    def test_missing_asset_name_yields_nothing(self):
        text = REAL_TEXT.replace("PUBLISHER ASSET GIVEAWAY", "SOMETHING ELSE")
        assert parse_free_asset(dict(REAL_STATE, text=text)) is None

    def test_missing_link_yields_nothing(self):
        assert parse_free_asset(dict(REAL_STATE, giftLinks=[])) is None

    @pytest.mark.parametrize("code", ["LEX4ART2026", "ABC1234", "SUMMER-2026", "A1B2C3D4E5"])
    def test_coupon_shapes_that_must_be_read(self, code):
        text = REAL_TEXT.replace("LEX4ART2026", code)
        assert parse_free_asset(dict(REAL_STATE, text=text))["coupon"] == code

    def test_lowercase_coupon_wording_still_matches(self):
        text = REAL_TEXT.replace("coupon code LEX4ART2026", "Coupon Code LEX4ART2026")
        assert parse_free_asset(dict(REAL_STATE, text=text))["coupon"] == "LEX4ART2026"


class TestBrokenInput:
    @pytest.mark.parametrize("state", [None, {}, [], "not a dict", {"text": "", "giftLinks": []}])
    def test_unusable_input_yields_nothing(self, state):
        assert parse_free_asset(state) is None

    def test_link_without_href_is_ignored(self):
        assert parse_free_asset(dict(REAL_STATE, giftLinks=[{"text": "GET YOUR FREE GIFT"}])) is None


class TestNotificationVocabulary:
    """Statuses drive the summary filter in main.py, so they may not drift into free text."""

    SOURCE = (Path(__file__).resolve().parent.parent / "src" / "stores" / "unity.py").read_text(encoding="utf-8")
    ALLOWED = re.compile(r"^(claimed|existed|notified|available \(dry run\)|(failed|skipped)(:[a-z-]+)?)$")

    def test_every_status_follows_the_shared_vocabulary(self):
        statuses = set(re.findall(r'"status": "([^"]+)"', self.SOURCE))
        statuses |= set(re.findall(r'status = "([^"]+)"', self.SOURCE))
        assert statuses, "no statuses found, the scan stopped matching"
        unexpected = sorted(s for s in statuses if not self.ALLOWED.match(s))
        assert not unexpected, f"statuses outside the shared vocabulary: {unexpected}"

    def test_vnc_prompts_are_prefixed_with_the_store_name(self):
        titles = re.findall(r'self\._vnc_notice\(\s*"([^"]+)"', self.SOURCE)
        assert titles, "no VNC prompts found, the scan stopped matching"
        assert all(t.startswith("Unity: ") for t in titles), titles

    def test_store_name(self):
        assert UnityClaimer.store_name == "unity"


class TestCheckoutBlockers:
    """Unity refuses the coupon, not just the payment, while its billing form is incomplete."""

    COMPLETE = {
        "exempt": False,
        "regionVisible": False,
        "fields": {
            "sta[country]": "PL", "sta[firstName]": "Ada", "sta[lastName]": "Lovelace",
            "sta[email]": "ada@example.com", "sta[streetAddress]": "1 Main St",
            "sta[postalCode]": "00-001", "sta[locality]": "Warsaw", "sta[vat]": "",
        },
    }

    def test_a_complete_form_blocks_nothing(self):
        assert checkout_blockers(self.COMPLETE) == []

    @pytest.mark.parametrize("name,label", [
        ("sta[country]", "Country"), ("sta[firstName]", "First name"),
        ("sta[lastName]", "Last name"), ("sta[email]", "Email"),
        ("sta[streetAddress]", "Address"), ("sta[postalCode]", "Postal code"),
        ("sta[locality]", "City"),
    ])
    def test_each_empty_field_is_reported_by_its_label(self, name, label):
        state = {**self.COMPLETE, "fields": {**self.COMPLETE["fields"], name: ""}}
        assert checkout_blockers(state) == [label]

    def test_tax_exemption_without_a_number_is_the_state_unity_rejects(self):
        assert checkout_blockers({**self.COMPLETE, "exempt": True}) == ["Tax number"]

    def test_a_real_tax_number_is_left_alone(self):
        fields = {**self.COMPLETE["fields"], "sta[vat]": "PL1234567890"}
        assert checkout_blockers({**self.COMPLETE, "exempt": True, "fields": fields}) == []

    def test_state_province_counts_only_where_unity_shows_it(self):
        # Poland has no such field, the US does.
        assert checkout_blockers({**self.COMPLETE, "regionVisible": True}) == ["State/Province"]
        assert checkout_blockers({**self.COMPLETE, "regionVisible": False}) == []

    @pytest.mark.parametrize("state", [None, {}, {"fields": {}}, "not a dict"])
    def test_unusable_input_blocks_everything(self, state):
        blockers = checkout_blockers(state if isinstance(state, dict) or state is None else {})
        assert "Country" in blockers and "City" in blockers


class TestMoneyGate:
    """The one thing standing between the bot and a real charge: pay only a zero total."""

    def test_a_zero_total_is_the_only_amount_that_allows_paying(self):
        assert parse_total("Order summary Items (1) 17.48€ Discount - 17.48€ To pay now 0.00€") == 0.0

    @pytest.mark.parametrize("text,expected", [
        ("Subtotal 8.74€ Tax 2.01€ To pay now 10.75€", 10.75),
        ("To pay now 10,75 EUR", 10.75),
        ("to pay now $1.99", 1.99),
        ("TO PAY NOW 0.99", 0.99),
    ])
    def test_a_real_price_is_read_as_itself(self, text, expected):
        assert parse_total(text) == expected

    @pytest.mark.parametrize("text", [
        "",
        None,
        "Order summary Items (1) 17.48€ Subtotal 8.74€",
        "Zu zahlen 0,00€",                      # the same checkout in German
        "Do zaplaty teraz 0.00 zl",             # and in Polish
    ])
    def test_anything_unreadable_refuses_to_pay(self, text):
        assert parse_total(text) == -1.0

    def test_an_earlier_zero_on_the_page_cannot_pose_as_the_total(self):
        # Only the number right after the phrase counts, a 0.00 elsewhere must not win.
        text = "Shipping 0.00€ Items (1) 17.48€ To pay now 17.48€"
        assert parse_total(text) == 17.48

    def test_a_faraway_number_is_not_the_total(self):
        # A stray phrase with no amount behind it must not read as free.
        assert parse_total("To pay now, please complete the form below and try again 0.00") == -1.0


class TestTheCheckoutFormIsReadyInEveryCountry:
    """Issue #89: US checkouts have no tax question, and the bot waited for it until it gave up."""

    FILLED = {"sta[country]": "US", "sta[firstName]": "A", "sta[lastName]": "B", "sta[email]": "a@b.c",
              "sta[streetAddress]": "1 Main St", "sta[postalCode]": "94016", "sta[locality]": "SF", "sta[region]": "CA"}

    def _prepare(self, states):
        import asyncio
        from types import SimpleNamespace

        answered = []

        async def checkout_state():
            return states.pop(0) if len(states) > 1 else states[0]

        async def evaluate(script):
            answered.append(script)
            return True

        async def sleep(seconds):
            pass

        async def manual(blockers):
            return False

        stub = SimpleNamespace(_checkout_state=checkout_state, sleep=sleep, _wait_for_manual_setup=manual,
                               page=SimpleNamespace(evaluate=evaluate))
        return asyncio.run(UnityClaimer._prepare_checkout(stub)), answered

    def test_a_us_checkout_without_the_tax_question_goes_on(self):
        ready, answered = self._prepare([{"present": True, "taxAsked": False, "regionVisible": True,
                                          "fields": self.FILLED}])
        assert ready is True and answered == []

    def test_a_tax_question_that_renders_late_is_still_answered(self):
        # EU checkouts start on "exempt: yes", which makes Unity refuse the coupon.
        fields = {**self.FILLED, "sta[vat]": ""}
        ready, answered = self._prepare([
            {"present": True, "taxAsked": False, "fields": fields},
            {"present": True, "taxAsked": True, "exempt": True, "fields": fields},
            {"present": True, "taxAsked": True, "exempt": False, "fields": fields},
        ])
        assert ready is True and len(answered) == 1 and "vatRegistered" in answered[0]

    def test_a_form_that_never_renders_is_not_called_incomplete(self):
        ready, _ = self._prepare([{"present": False}])
        assert ready is None

    def test_a_form_that_never_renders_is_reported_as_failed(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / "unity.py").read_text(encoding="utf-8")
        after = source.split("prepared = await self._prepare_checkout()", 1)[1][:400]
        assert "if prepared is None:" in after and '"failed:checkout"' in after.split("if not prepared:", 1)[0]


class TestOnlyBuyNowStartsTheCheckout:
    """Audit: Add to cart never opens the checkout, it only left the asset in the cart every run."""

    def test_add_to_cart_is_never_pressed(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / "unity.py").read_text(encoding="utf-8")
        start = source.split("async def _start_checkout", 1)[1].split("\n    async def ", 1)[0]
        clicker = start.split('"""')[3]  # the script that picks the button
        assert "/^buy now$/i" in clicker and "add to cart" not in clicker.lower()


class TestEntitlementsCheckReturnsToStoreHost:
    """_owned_ids() calls a relative /api/users/entitlements path, which 404s on pay.unity.com."""

    def test_leaves_the_payment_host_before_reading_entitlements(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "stores" / "unity.py").read_text(encoding="utf-8")
        after_pay = source.split("if not await self._pay_now(", 1)[1].split("if package_id and package_id in await self._owned_ids():", 1)[0]
        assert "url_has_allowed_host(await self._current_url(), STORE_HOST)" in after_pay
        assert "await self.page.get(URL_ORDERS)" in after_pay

