"""One run, then the container stops.

Someone scheduling the container from outside set SCHEDULER_HOURS=0 and found the bot
sat there forever with nothing scheduled (issue #51). Both that case and the explicit
RUN_ONCE switch have to end the process instead of idling.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
# From the --once shortcut to the line that would keep the process alive.
TAIL = MAIN.split('if "--once" in sys.argv', 1)[1].split("scheduler.start()", 1)[0]


class TestTheSetting:
    def test_run_once_is_read_and_off_by_default(self):
        config = (ROOT / "src" / "core" / "config.py").read_text(encoding="utf-8")
        assert re.search(r'run_once: bool = _bool\("RUN_ONCE", default=False\)', config)

    def test_it_is_documented_where_people_look(self):
        assert "RUN_ONCE" in (ROOT / ".env.example").read_text(encoding="utf-8")
        assert "`RUN_ONCE`" in (ROOT / "README.md").read_text(encoding="utf-8")


class TestNothingScheduledMeansStop:
    def test_an_empty_schedule_is_recognised(self):
        assert "cfg.scheduler_hours <= 0 and not fixed_times" in TAIL

    def test_both_cases_end_the_process(self):
        # The claiming run happens, then the function returns instead of reaching the scheduler.
        branch = TAIL.split("if cfg.run_once or nothing_scheduled:", 1)[1]
        assert "await run_claimers()" in branch
        assert "return" in branch

    def test_a_configuration_that_does_nothing_says_so(self):
        branch = TAIL.split("if cfg.run_once or nothing_scheduled:", 1)[1]
        assert "RUN_ON_STARTUP=false" in branch

    def test_the_restart_policy_is_spelled_out(self):
        # Without restart: "no" the container comes straight back and runs again.
        assert 'restart: "no"' in TAIL

    def test_the_scheduler_is_never_reached_in_that_case(self):
        assert TAIL.index("if cfg.run_once or nothing_scheduled:") < TAIL.index("AsyncIOScheduler")


class TestTheNormalPathIsUntouched:
    def test_an_interval_still_starts_the_scheduler(self):
        assert "if cfg.scheduler_hours > 0:" in MAIN and "scheduler.start()" in MAIN

    def test_fixed_times_alone_still_keep_it_running(self):
        # SCHEDULER_HOURS=0 plus fixed times is a real schedule, so it must not stop.
        assert "and not fixed_times" in TAIL
