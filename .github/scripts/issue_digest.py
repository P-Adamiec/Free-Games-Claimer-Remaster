"""Daily to-do digest for the owner's Discord, plus the list of issues the labelling agent should check.
Run by .github/workflows/issue-digest.yml as `window`, `pick` or `digest`; standard library only."""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY") or os.environ["GH_REPO"]
OWNER = (os.environ.get("REPO_OWNER") or REPO.split("/")[0]).lower()
WORKFLOW = "issue-digest.yml"
STAMP = "%Y-%m-%dT%H:%M:%SZ"
# shortcut: a flood of spam issues gets labelled 20 a day, raise it if real traffic ever needs more
MAX_ISSUES = 20
# Read by read-issue.sh and edit-issue-labels.sh, which sit in the same folder.
LIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "triage-issues.txt")


def _request(url, data=None):
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "fgc-remaster-issue-digest",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp), resp.headers.get("Link", "")


def get(path, **params):
    url = f"{API}/repos/{REPO}/{path}"
    return _request(url + ("?" + urllib.parse.urlencode(params) if params else ""))[0]


def pages(path, **params):
    """Yield every item of a list endpoint, following the Link header."""
    url = f"{API}/repos/{REPO}/{path}?" + urllib.parse.urlencode({"per_page": 100, **params})
    while url:
        data, link = _request(url)
        yield from data
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = nxt.group(1) if nxt else None


def graphql(query, **variables):
    data = _request(f"{API}/graphql", json.dumps({"query": query, "variables": variables}).encode())[0]
    if data.get("errors"):
        raise RuntimeError(data["errors"][0]["message"])
    return data["data"]


def is_owner(user):
    return (user or {}).get("login", "").lower() == OWNER


def by_others(user):
    login = (user or {}).get("login", "")
    return bool(login) and not is_owner(user) and user.get("type") != "Bot" and not login.endswith("[bot]")


def events_since(since):
    # The repo-wide event list has no `since` filter and comes newest first.
    for ev in pages("issues/events"):
        if ev["created_at"] < since:
            return
        yield ev


# ----- window -----

def window():
    now = datetime.now(timezone.utc)
    hours = os.environ.get("HOURS", "").strip()
    if hours:
        since = now - timedelta(hours=min(max(float(hours), 1), 24 * 30))
    else:
        since = now - timedelta(hours=24)
        try:
            runs = get(f"actions/workflows/{WORKFLOW}/runs", event="schedule", status="success", per_page=1)["workflow_runs"]
            if runs:
                # No upper limit: after an outage the digest covers every missed day rather than dropping some.
                since = datetime.strptime(runs[0]["run_started_at"], STAMP).replace(tzinfo=timezone.utc)
        except urllib.error.HTTPError as exc:
            print(f"No earlier scheduled run found ({exc.code}), using the last 24 hours", file=sys.stderr)
    print(f"since={since.strftime(STAMP)}")


# ----- pick -----

def pick():
    manual = os.environ.get("MANUAL", "")
    if manual.strip():
        picked = {int(n) for n in re.findall(r"\d+", manual)}
    else:
        since = os.environ["SINCE"]
        picked = set()
        for it in pages("issues", state="all", since=since):
            if "pull_request" not in it and (it["created_at"] >= since or (it.get("closed_at") or "") >= since):
                picked.add(it["number"])
        for ev in events_since(since):
            if ev["event"] == "reopened" and "pull_request" not in ev["issue"]:
                picked.add(ev["issue"]["number"])
        for it in pages("issues", state="open"):
            if "pull_request" not in it and not any(l["name"].startswith("type: ") for l in it["labels"]):
                picked.add(it["number"])
    numbers = sorted(picked)[-MAX_ISSUES:]
    with open(LIST_FILE, "w", encoding="utf-8") as fh:
        fh.write("".join(f"{n}\n" for n in numbers))
    print("issues=" + " ".join(map(str, numbers)))
    print("caps=" + json.dumps({"edit-issue-labels.sh": 2 * len(numbers)}))


# ----- digest -----

DISCUSSIONS = """query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    discussions(first: 30, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title url createdAt body closed closedAt author { login }
        comments(last: 30) { nodes { author { login } createdAt url body
          replies(last: 30) { nodes { author { login } createdAt url body } } } }
      }
    }
  }
}"""
# Only an answer settles a thread (a close is handled apart): a label, a rename or a commit naming the issue is no reply.
OWNER_ACTS = {"commented", "reviewed", "reopened"}
KINDS = {"issue": "issue", "pr": "pull request", "discussion": "discussion"}


def md(text):
    """Escape Discord markdown so titles and quotes print as written."""
    return re.sub(r"([\\*_~`|>\[\]])", r"\\\1", text)


def excerpt(body, limit=140):
    # Issue forms put "### Field" headings, ticked boxes and "_No response_" between the answers.
    lines = [l for l in (body or "").splitlines()
             if not l.lstrip().startswith((">", "### ", "- [X] ", "- [x] ", "- [ ] ")) and l.strip() != "_No response_"]
    text = re.sub(r"<img[^>]*>|!\[[^\]]*\]\([^)]*\)", "[image]", " ".join(lines))
    text = re.sub(r"<!--.*?-->|```\w*", " ", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def link(number, url):
    return f"[#{number}]({url})"


def thread(kind, number, title, url, closed, seen, opened, replies, reopens, labels=""):
    """One thread where someone else wrote after the owner's last move, or None when nothing waits."""
    opened = opened if opened and opened[0] > seen else None
    replies = sorted(p for p in replies if p[0] > seen)
    reopens = [p for p in reopens if p[0] > seen]
    if not (opened or replies or reopens):
        return None
    latest = (replies or [opened])[-1]
    parts = []
    if opened:
        parts.append(f"new {KINDS[kind]} by {md(opened[1])}" + (f" · {md(labels)}" if labels else ""))
    if reopens:
        parts.append(f"reopened by {md(reopens[-1][1])}")
    if len(replies) > 1:
        parts.append(f"{len(replies)} new replies from " + ", ".join(dict.fromkeys(md(p[1]) for p in replies)))
    emoji = "🔀" if kind == "pr" else "🗨️" if kind == "discussion" else "🆕" if opened else "🔓" if reopens else "💬"
    head = f"{emoji} **{link(number, latest[3] if latest else url)} {md(excerpt(title, 80))}**" + (" · closed" if closed else "")
    lines = [head] + [f"└ {p}" for p in parts]
    if latest and latest[2]:
        lines.append(f"└ {md(latest[1])}: \"{md(excerpt(latest[2]))}\"")
    return max(p[0] for p in [opened, *replies, *reopens] if p), "\n".join(lines)


def digest():
    since = os.environ["SINCE"]
    found, alerts = [], []

    for it in pages("issues", state="all", since=since):
        seen = it["created_at"] if is_owner(it["user"]) else ""
        replies, reopens = [], []
        for ev in pages(f"issues/{it['number']}/timeline"):
            who, when = ev.get("user") or ev.get("actor"), ev.get("created_at") or ev.get("submitted_at") or ""
            # A close settles the thread whoever closed it; a reply written after it still shows.
            if ev.get("event") == "closed" or (is_owner(who) and ev.get("event") in OWNER_ACTS):
                seen = max(seen, when)
            elif by_others(who) and when >= since:
                if ev.get("event") == "reviewed" or (ev.get("event") == "commented" and ev.get("body")):
                    # A review with only code comments has no text, its verdict stands in.
                    text = ev.get("body") or (ev.get("state") or "reviewed").lower().replace("_", " ")
                    replies.append((when, who["login"], text, ev["html_url"]))
                elif ev.get("event") == "reopened":
                    reopens.append((when, who["login"]))
        new = it["created_at"] >= since and by_others(it["user"])
        opened = (it["created_at"], it["user"]["login"], it["body"], it["html_url"]) if new else None
        labels = ", ".join(l["name"] for l in it["labels"])
        kind = "pr" if "pull_request" in it else "issue"
        found.append(thread(kind, it["number"], it["title"], it["html_url"], it["state"] == "closed", seen, opened, replies, reopens, labels))

    # shortcut: reads the 30 most recently updated discussions, page through them if the forum grows past that
    owner, name = REPO.split("/")
    try:
        discussions = graphql(DISCUSSIONS, owner=owner, name=name)["repository"]["discussions"]["nodes"]
    except (urllib.error.HTTPError, RuntimeError) as exc:
        alerts.append(f"⚠️ **Discussions could not be read** ({md(str(exc))}), check them on GitHub.")
        discussions = []
    for d in discussions:
        # A closed discussion counts as settled, whoever closed it.
        seen = max(d["createdAt"] if is_owner(d["author"]) else "", d["closedAt"] or "")
        replies = []
        for c in d["comments"]["nodes"]:
            for post in [c, *c["replies"]["nodes"]]:
                if is_owner(post["author"]):
                    seen = max(seen, post["createdAt"])
                elif by_others(post["author"]) and post["createdAt"] >= since:
                    replies.append((post["createdAt"], post["author"]["login"], post["body"], post["url"]))
        new = d["createdAt"] >= since and by_others(d["author"])
        opened = (d["createdAt"], d["author"]["login"], d["body"], d["url"]) if new else None
        found.append(thread("discussion", d["number"], d["title"], d["url"], d["closed"], seen, opened, replies, []))

    if os.environ.get("LABEL_OUTCOME") == "failure":
        picked = os.environ.get("LABEL_ISSUES", "").split()
        alerts.append(f"⚠️ **Labelling failed** for {' '.join('#' + n for n in picked)}. Run the workflow again with "
                      f"issues `{' '.join(picked)}`. Log: {os.environ.get('RUN_URL', 'the Actions tab')}")
    waiting = sorted((t for t in found if t), reverse=True)
    body = [text for _, text in waiting] + alerts
    if not body:
        print("Nothing waiting since", since)
        return
    start = datetime.strptime(since, STAMP).strftime("%b %d, %H:%M UTC")
    head = f"📬 **{len(waiting)} thread{'' if len(waiting) == 1 else 's'} waiting for you** · since {start}"
    # The text goes to Discord only: Actions logs of a public repo are public.
    send(head + "\n\n" + "\n\n".join(body))


def send(text):
    hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not hook:
        # Failing keeps the window where it is, so the next run reports these items again.
        sys.exit("DISCORD_WEBHOOK is not set, the digest was not sent")
    # Split between threads, never inside one; Discord takes 2000 characters a message.
    chunks, chunk = [], ""
    for block in text.split("\n\n"):
        if chunk and len(chunk) + len(block) + 2 > 1900:
            chunks.append(chunk)
            chunk = ""
        chunk += block[:1900] + "\n\n"
    chunks.append(chunk)
    for i, part in enumerate(chunks):
        if i:
            # A webhook takes about 5 messages per 2 seconds and 30 a minute; one refusal would fail every later run too.
            time.sleep(2)
        # Flag 4 hides link preview cards, an empty parse list stops every ping.
        payload = {"content": part, "allowed_mentions": {"parse": []}, "flags": 4}
        req = urllib.request.Request(hook, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            # Discord refuses Python's default user agent.
            "User-Agent": f"DiscordBot (https://github.com/{REPO}, 1.0)",
        })
        urllib.request.urlopen(req, timeout=30).close()
    print(f"Sent to Discord in {len(chunks)} message(s)")


if __name__ == "__main__":
    {"window": window, "pick": pick, "digest": digest}[sys.argv[1]]()
