"""The owner's Discord digest (.github/scripts/issue_digest.py) lists a thread only while someone else has the last word."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "issue_digest.py"


def at(hour):
    return f"2026-10-13T{hour:02d}:00:00Z"


def ev(event, who, hour, body="hi"):
    return {"event": event, "actor": {"login": who}, "user": {"login": who}, "created_at": at(hour),
            "body": body, "html_url": f"https://example/{event}/{hour}"}


def issue(author, hour, *timeline):
    return {"number": 1, "user": {"login": author}, "created_at": at(hour), "title": "A title", "body": "first post",
            "html_url": "https://example/1", "labels": [], "state": "open"}, list(timeline)


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("SINCE", at(0))
    monkeypatch.setenv("DISCORD_WEBHOOK", "https://discord.invalid/hook")
    monkeypatch.delenv("LABEL_OUTCOME", raising=False)
    spec = importlib.util.spec_from_file_location("issue_digest", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def digest(mod, monkeypatch):
    def run(thread, discussions_fail=False):
        sent = []

        def graphql(*_, **__):
            if discussions_fail:
                raise RuntimeError("Resource not accessible by integration")
            return {"repository": {"discussions": {"nodes": []}}}

        class Reply:
            def close(self):
                pass

        def urlopen(req, timeout=None):
            sent.append(json.loads(req.data)["content"])
            return Reply()

        monkeypatch.setattr(mod, "pages", lambda path, **_: [thread[0]] if path == "issues" else thread[1])
        monkeypatch.setattr(mod, "graphql", graphql)
        # Only Discord is faked, so the real send() runs, splitting and all.
        monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
        mod.digest()
        return "".join(sent)

    return run


def test_a_new_issue_nobody_answered_is_listed(digest):
    assert "new issue by stranger" in digest(issue("stranger", 1))


def test_a_form_shows_its_answers_not_its_headings(mod):
    body = ("### Before you report\n\n- [X] I updated to the newest version and the problem still happens.\n\n"
            "### Version\n\nv1.15\n\n### Store or part of the bot\n\nEpic Games\n\n"
            "### What happened\n\nThe bot clicked Get twice.\n\n### When it happens\n\n_No response_")
    assert mod.excerpt(body) == "v1.15 Epic Games The bot clicked Get twice."


def test_an_answered_issue_is_left_out(digest):
    assert digest(issue("stranger", 1, ev("commented", "owner", 2))) == ""


def test_only_what_came_after_the_owners_answer_is_shown(digest):
    text = digest(issue("stranger", 1, ev("commented", "owner", 2), ev("commented", "other", 3, "still broken")))
    assert "still broken" in text and "new issue" not in text


def test_being_mentioned_is_not_an_answer(digest):
    text = digest(issue("stranger", 1, ev("commented", "other", 2, "ping"), ev("mentioned", "owner", 2), ev("subscribed", "owner", 2)))
    assert "ping" in text


@pytest.mark.parametrize("event", ["labeled", "unlabeled", "renamed", "referenced", "cross-referenced"])
def test_touching_a_thread_without_answering_keeps_it_listed(digest, event):
    assert "new issue by stranger" in digest(issue("stranger", 1, ev(event, "owner", 2)))


def test_a_review_without_text_is_listed(digest):
    review = {"event": "reviewed", "user": {"login": "other"}, "submitted_at": at(2), "body": None,
              "state": "CHANGES_REQUESTED", "html_url": "https://example/review"}
    assert "changes requested" in digest(issue("stranger", 1, ev("commented", "owner", 1), review))


def test_a_long_digest_pauses_between_discord_messages(mod, monkeypatch):
    sent, naps = [], []

    class Reply:
        def close(self):
            pass

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout=None: sent.append(req) or Reply())
    monkeypatch.setattr(mod.time, "sleep", naps.append)
    mod.send("\n\n".join("x" * 1000 for _ in range(8)))
    assert len(sent) == 8 and naps == [2] * 7


def test_the_window_reaches_back_to_the_last_good_daily_run_however_old(mod, monkeypatch, capsys):
    # Clamping it would drop the replies of a long outage without a word.
    monkeypatch.delenv("HOURS", raising=False)
    monkeypatch.setattr(mod, "get", lambda *_, **__: {"workflow_runs": [{"run_started_at": "2026-01-01T05:23:00Z"}]})
    mod.window()
    assert capsys.readouterr().out.strip() == "since=2026-01-01T05:23:00Z"


def test_the_agents_list_holds_only_the_picked_issues(mod, monkeypatch, tmp_path, capsys):
    # This file is all the agent may read or relabel, so it must never grow past the pick.
    monkeypatch.setattr(mod, "LIST_FILE", str(tmp_path / "list.txt"))
    monkeypatch.setenv("MANUAL", "76, 82 abc")
    mod.pick()
    assert (tmp_path / "list.txt").read_text() == "76\n82\n"
    monkeypatch.setenv("MANUAL", " ".join(str(n) for n in range(1, 26)))
    mod.pick()
    assert (tmp_path / "list.txt").read_text().split() == [str(n) for n in range(6, 26)]
    assert '{"edit-issue-labels.sh": 40}' in capsys.readouterr().out


def test_a_closed_thread_waits_only_for_replies_after_the_close(digest):
    assert digest(issue("stranger", 1, ev("commented", "stranger", 2, "thanks"), ev("closed", "stranger", 2))) == ""
    assert "still broken" in digest(issue("stranger", 1, ev("closed", "owner", 2), ev("commented", "stranger", 3, "still broken")))


def test_the_digest_text_stays_out_of_the_public_log(digest, capsys):
    # Actions logs of a public repo are public, so the text may only go to Discord.
    assert "first post" in digest(issue("stranger", 1))
    out, err = capsys.readouterr()
    assert "first post" not in out + err


def test_unreadable_discussions_are_reported_not_skipped(digest):
    assert "Discussions could not be read" in digest(issue("stranger", 1, ev("commented", "owner", 2)), discussions_fail=True)
