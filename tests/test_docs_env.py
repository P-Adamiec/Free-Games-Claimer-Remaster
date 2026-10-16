"""Every setting must be documented in all three places.

`VNC_LOGIN_TIMEOUT` lived in config.py and the README table but never reached
.env.example; that silent gap is what these tests exist to catch.
"""

import re
from pathlib import Path

import pytest
import yaml

from src.core.config import env_setting_kinds

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

# Handled by Docker, not by config.py: the image tag, TurboVNC's own password and the user the entrypoint switches to.
DOCKER_ONLY = {"FGC_TAG", "VNC_PASSWORD", "PUID", "PGID"}
# [fork:webui] read by the webui container and webui/bots.sh, documented in webui/README.md
DOCKER_ONLY |= {"WEBUI_ADMIN_USER", "WEBUI_ADMIN_PASSWORD", "WEBUI_PORT", "WEBUI_BIND", "WEBUI_PUBLIC_URL",
                "WEBUI_TRUSTED_PROXIES", "WEBUI_COOKIE_SECURE", "WEBUI_SECRET_KEY", "WEBUI_TIMEZONE",
                "FGC_RUN_TIMEOUT_MINUTES", "FGC_WORKERS"}


def _config_vars() -> list[str]:
    # config.py scans itself for this, so the runtime settings guard and these tests agree.
    return sorted(env_setting_kinds())


def _env_example_vars() -> set[str]:
    return set(re.findall(r"^#?\s*([A-Z_0-9]+)=", ENV_EXAMPLE, re.M))


def _readme_vars() -> set[str]:
    return set(re.findall(r"^\|\s*`([A-Z_0-9]+)`", README, re.M))


class TestEverySettingIsDocumented:
    def test_the_scan_still_finds_settings(self):
        # Guards the regex itself: a rename in config.py must not empty this list.
        assert len(_config_vars()) > 60

    @pytest.mark.parametrize("name", _config_vars())
    def test_it_is_in_env_example(self, name):
        assert name in _env_example_vars(), f"{name} is missing from .env.example"

    @pytest.mark.parametrize("name", _config_vars())
    def test_it_is_in_the_readme_table(self, name):
        assert name in _readme_vars(), f"{name} is missing from the README config table"


class TestNothingDocumentedThatDoesNotExist:
    @pytest.mark.parametrize("source,names", [
        (".env.example", _env_example_vars()),
        ("README.md", _readme_vars()),
    ])
    def test_no_unknown_settings(self, source, names):
        unknown = sorted(names - set(_config_vars()) - DOCKER_ONLY)
        assert not unknown, f"{source} documents settings the code does not read: {unknown}"


class TestArchitectureTree:
    """The README file tree is a map, an unlisted module makes it a wrong one."""

    TREE = README.split("## Architecture", 1)[-1].split("###", 1)[0]

    @pytest.mark.parametrize("module", sorted(p.name for p in (ROOT / "src" / "core").glob("*.py")
                                              if p.name != "__init__.py"))
    def test_every_core_module_is_listed(self, module):
        assert module in self.TREE, f"src/core/{module} is missing from the README tree"

    @pytest.mark.parametrize("module", sorted(p.name for p in (ROOT / "src" / "stores").glob("*.py")
                                              if p.name != "__init__.py"))
    def test_every_store_module_is_listed(self, module):
        assert module in self.TREE, f"src/stores/{module} is missing from the README tree"


class TestIssueForms:
    """A form that is not valid YAML drops out of GitHub's chooser, and with blank issues off nobody could report."""

    FOLDER = ROOT / ".github" / "ISSUE_TEMPLATE"
    FORMS = sorted(p for p in FOLDER.glob("*.yml") if p.name != "config.yml")
    LABELS = {"type: bug", "type: feature", "type: question", "type: docs"}

    def test_the_three_forms_are_there(self):
        assert [p.name for p in self.FORMS] == ["bug_report.yml", "feature_request.yml", "other.yml"]

    @pytest.mark.parametrize("path", FORMS, ids=lambda p: p.name)
    def test_every_form_reads_and_asks_for_the_newest_version(self, path):
        form = yaml.safe_load(path.read_text(encoding="utf-8"))
        ids = [field["id"] for field in form["body"] if field["type"] != "markdown"]
        assert form["name"] and form["description"] and len(ids) == len(set(ids))
        assert set(form.get("labels", [])) <= self.LABELS
        # On the newest version the log is masked and the fix may already be there.
        newest = next(field for field in form["body"] if field.get("id") == "newest")
        assert newest["attributes"]["options"][0]["required"] is True

    def test_blank_issues_stay_off(self):
        assert yaml.safe_load((self.FOLDER / "config.yml").read_text(encoding="utf-8"))["blank_issues_enabled"] is False
