"""[fork:webui] Every setting the bot reads is on the settings page, or deliberately left off.

Catches an upstream merge that adds a setting the web interface does not know yet (as LG_EMAIL did in v1.13).
"""

from src.core.config import env_setting_kinds
from webui.app import schema

# Not on the settings page, on purpose: set by Docker or webui/bots.sh, or only useful while debugging.
NOT_IN_WEBUI = {
    "BROWSER_DIR", "SCREENSHOTS_DIR",          # paths inside the container
    "SHOW", "WIDTH", "HEIGHT",                 # the bot's screen, fixed per bot
    "NOVNC_PORT", "VNC_IP", "VNC_URL",         # the screen goes through the web interface
    "TZ",                                      # the container's clock, a Docker setting
    "RUN_ONCE", "RESET_DB_GAMES", "DEBUG_LIBS",  # one-off or debugging switches
    "GP_UNKNOWN_STORES",                       # reserved upstream, does nothing yet
}


def test_every_setting_is_on_the_page_or_left_off_on_purpose():
    missing = sorted(set(env_setting_kinds()) - set(schema.MANAGED_KEYS) - NOT_IN_WEBUI)
    assert not missing, f"Nieuwe instellingen zonder plek in webui/app/schema.py: {missing}"


def test_the_left_off_list_holds_only_settings_that_exist():
    assert not NOT_IN_WEBUI - set(env_setting_kinds())
