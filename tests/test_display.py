"""The virtual screen the browser draws on.

TurboVNC's screen was started once and never watched again, so when it died Chrome
stopped starting and noVNC went quiet at the same time, with nothing in the log saying
why (issue #52). These are the pure parts of the fix; the recovery itself is verified
against a real container.
"""

from pathlib import Path

import pytest

from src.core import display

ROOT = Path(__file__).resolve().parent.parent
CLAIMER = (ROOT / "src" / "core" / "claimer.py").read_text(encoding="utf-8")
ENTRYPOINT = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
START_VNC = (ROOT / "start-vnc.sh").read_text(encoding="utf-8")


class TestReadingTheDisplaySetting:

    @pytest.mark.parametrize("value,expected", [
        (":1", 1),
        (":0", 0),
        ("host:2.0", 2),
        (":1.0", 1),
        ("", 1),
        ("nonsense", 1),
    ])
    def test_the_screen_number_is_read_or_assumed(self, value, expected):
        assert display.display_number(value) == expected

    def test_the_socket_follows_the_number(self):
        assert display.socket_path(3).as_posix().endswith("/tmp/.X11-unix/X3")


class TestOutsideTheContainerNothingHappens:
    """A Windows dev run has no screen to manage, and must not be told its screen is broken."""

    def test_a_machine_without_the_script_manages_no_screen(self, monkeypatch, tmp_path):
        monkeypatch.setattr(display, "VNC_SCRIPT", tmp_path / "start-vnc.sh")
        assert display.screen_is_managed() is False

    def test_restarting_there_is_refused_quietly(self, monkeypatch, tmp_path):
        monkeypatch.setattr(display, "VNC_SCRIPT", tmp_path / "start-vnc.sh")
        assert display.restart_screen() is False

    def test_a_script_that_will_not_run_is_not_a_crash(self, monkeypatch, tmp_path):
        script = tmp_path / "start-vnc.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(display, "VNC_SCRIPT", script)
        monkeypatch.setattr(display.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no bash")))
        assert display.restart_screen() is False


class TestTheStateLine:

    def test_it_names_the_screen_and_both_ports(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":1")
        monkeypatch.setenv("VNC_PORT", "5900")
        monkeypatch.setenv("NOVNC_PORT", "7080")
        monkeypatch.setattr(display, "xvnc_running", lambda: False)
        monkeypatch.setattr(display, "port_is_open", lambda port, host="127.0.0.1": False)
        state = display.screen_state()
        assert state.startswith(":1 socket=")
        assert "Xvnc=False" in state and "vnc:5900=False" in state and "novnc:7080=False" in state


class TestTheBotActsOnIt:

    def test_only_a_run_that_needs_a_window_checks_the_screen(self):
        assert "if not headless and screen_is_managed() and not screen_is_alive():" in CLAIMER

    def test_a_screen_that_will_not_come_back_stops_the_attempt(self):
        branch = CLAIMER.split("screen_is_managed() and not screen_is_alive():", 1)[1].split("\n\n", 1)[0]
        assert "restart_screen()" in branch
        assert "raise RuntimeError" in branch
        assert "TurboVNC.log" in branch

    def test_the_diagnostics_line_answers_the_nas_questions(self):
        line = CLAIMER.split("Chrome would not start.", 1)[1].split(")\n", 1)[0]
        assert "screen %s" in line and "user %s" in line and "memory %s" in line


class TestTheScriptsSayWhatHappened:

    def test_the_entrypoint_uses_the_shared_script(self):
        assert "/fgc/start-vnc.sh" in ENTRYPOINT

    def test_the_entrypoint_names_the_user_and_the_data_folder(self):
        # A NAS often runs the app as someone who can write neither, which looks like two faults.
        assert "id -u" in ENTRYPOINT and "data folder is" in ENTRYPOINT

    def test_a_user_id_the_image_does_not_know_is_survivable(self):
        # Seen live: without the fallback, set -e ended the script on the lookup and printed nothing at all.
        assert '|| user_name=""' in ENTRYPOINT

    def test_a_failed_screen_is_no_longer_silent(self):
        assert "-xstartup /usr/bin/ratpoison > /dev/null 2>&1" not in START_VNC
        assert "started=$(" in START_VNC
        assert "TurboVNC.log" in START_VNC

    def test_a_failed_web_viewer_warns_instead_of_stopping_the_run(self):
        tail = START_VNC.split("websockify -D", 1)[1]
        assert "did not come up" in tail
        assert "exit 1" not in tail


class TestTheScriptReachesTheImage:

    def test_it_is_not_excluded_from_the_build(self):
        assert "*.sh" not in (ROOT / ".dockerignore").read_text(encoding="utf-8")

    def test_it_is_made_executable_with_the_others(self):
        assert "chmod +x ./*.sh" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
