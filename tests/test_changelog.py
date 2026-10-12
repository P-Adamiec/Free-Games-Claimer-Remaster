"""CHANGELOG.md follows Keep a Changelog 1.1.0, https://keepachangelog.com/en/1.1.0/.

One choice of the owner's differs: instead of [Unreleased], the version being worked on sits on top without a date.
"""

import re
from pathlib import Path

import pytest

from src.version import __version__

TEXT = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
REPO = "https://github.com/P-Adamiec/Free-Games-Claimer-Remaster"
TYPES = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]
HEADING = re.compile(r"^## \[(\d+\.\d+)\](?: - (\d{4}-\d{2}-\d{2}))?$")
LINK = re.compile(r"^\[([^\]]+)\]: (\S+)$", re.M)

HEADER, *_SECTIONS = re.split(r"(?m)^(?=## )", TEXT)
SECTIONS = {part.split("\n", 1)[0]: LINK.sub("", part.split("\n", 1)[1]) for part in _SECTIONS}
NAMES = [HEADING.match(h).group(1) if HEADING.match(h) else h for h in SECTIONS]
DATED = [HEADING.match(h).group(1) for h in SECTIONS if HEADING.match(h) and HEADING.match(h).group(2)]


def _number(name: str) -> tuple:
    return tuple(int(part) for part in name.split("."))


class TestTheFile:
    def test_the_header_names_the_format_and_the_versioning(self):
        assert "[Keep a Changelog](https://keepachangelog.com/en/1.1.0/)" in HEADER
        assert "Semantic Versioning" in HEADER

    @pytest.mark.parametrize("heading", list(SECTIONS))
    def test_every_version_heading_has_the_standard_form(self, heading):
        assert HEADING.match(heading), f"{heading!r} is not '## [X.Y] - YYYY-MM-DD' or '## [X.Y]'"

    @pytest.mark.parametrize("heading", list(SECTIONS)[1:])
    def test_only_the_version_on_top_may_lack_a_date(self, heading):
        assert HEADING.match(heading).group(2), f"{heading!r} is released, so it needs its date"

    def test_the_version_being_worked_on_is_the_next_one(self):
        top = HEADING.match(next(iter(SECTIONS)))
        if not top.group(2):
            assert _number(top.group(1)) > _number(__version__), f"{top.group(1)} is not newer than {__version__}"

    def test_the_newest_version_comes_first(self):
        assert NAMES == sorted(NAMES, key=_number, reverse=True) and len(NAMES) == len(set(NAMES))
        dates = [HEADING.match(h).group(2) for h in SECTIONS if HEADING.match(h) and HEADING.match(h).group(2)]
        assert dates == sorted(dates, reverse=True)

    def test_the_newest_release_is_the_running_version(self):
        # version.py moves to the new number only on release day, with the date.
        assert DATED[0] == __version__

    def test_unreleased_is_gone(self):
        assert "[Unreleased]" not in TEXT and "[unreleased]:" not in TEXT

    @pytest.mark.parametrize("name", NAMES)
    def test_every_version_is_linkable(self, name):
        links = {label.lower(): url for label, url in LINK.findall(TEXT)}
        assert name.lower() in links, f"no '[{name}]: …' link at the bottom"
        assert links[name.lower()].startswith(REPO + "/")

    @pytest.mark.parametrize("heading", list(SECTIONS))
    def test_the_link_says_whether_the_version_is_out(self, heading):
        # Release day: the dated version compares up to its own tag, the version in progress up to dev.
        name, date = HEADING.match(heading).groups()
        url = {label.lower(): link for label, link in LINK.findall(TEXT)}[name.lower()]
        if date:
            assert url.endswith(f"...v{name}") or url.endswith(f"/releases/tag/v{name}"), url
        else:
            assert url.endswith("...dev"), url

    def test_no_em_dash(self):
        assert "\u2014" not in TEXT


class TestEveryVersion:
    @pytest.mark.parametrize("heading", list(SECTIONS))
    def test_only_the_six_types_in_their_order(self, heading):
        types = re.findall(r"(?m)^### (.+)$", SECTIONS[heading])
        assert all(t in TYPES for t in types), f"{heading}: {types}"
        assert types == sorted(types, key=TYPES.index) and len(types) == len(set(types)), f"{heading}: {types}"

    @pytest.mark.parametrize("heading", list(SECTIONS))
    def test_no_type_is_left_empty(self, heading):
        for name, body in re.findall(r"(?ms)^### (.+?)$(.*?)(?=^### |\Z)", SECTIONS[heading]):
            assert body.strip(), f"{heading}: '### {name}' has no entries"
