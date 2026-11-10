#!/usr/bin/env python3
"""Helper for the review-prs skill. Wraps the `gh` CLI so every pass is consistent.

All state lives under ~/.review-prs/<owner>__<repo>/ (override with REVIEW_PRS_HOME),
so the skill works the same from any directory and never touches your working tree.

  state.json        what has been reviewed (head SHA) and which comments were handled
  drafts/pr-N.json  reviews and replies waiting for your OK
  clone/            a partial clone used to read code at the PR's commit
  wt/pr-N/          a worktree checked out at the PR head

Subcommands
  scan  REPO [--include-own]    list open PRs that need attention this pass
  fetch REPO N                  pull PR details, diff, comments and a worktree
  mark  REPO N --sha S [--comments ID ...]   record what this pass handled
  show  REPO [N]                print pending drafts in readable form
  stage REPO N|all [--dry-run]  put drafts on GitHub as your pending (unsubmitted) review
  post  REPO N|all [--dry-run] [--only review|replies]   post approved drafts (submits staged ones)
  digest REPO                  one line per pending draft: door, blast radius, score
  path  REPO                    print the state directory

Stdlib only. Requires `gh` (authenticated) and `git`.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BOT_RE = re.compile(r"(\[bot\]$|^dependabot|^renovate|-bot$)", re.I)


# ---------------------------------------------------------------- helpers
def run(cmd, check=True, cwd=None, input_text=None):
    p = subprocess.run(cmd, cwd=cwd, input=input_text, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout


def gh_json(args):
    out = run(["gh"] + args)
    return json.loads(out) if out.strip() else None


def gh_paged(path):
    """GET a list endpoint with pagination; returns a flat list."""
    out = run(["gh", "api", path, "--paginate", "--jq", ".[]"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_bot(user):
    if not user:
        return True
    if user.get("type") == "Bot" or user.get("is_bot"):
        return True
    return bool(BOT_RE.search(user.get("login", "")))


def home(repo):
    base = Path(os.environ.get("REVIEW_PRS_HOME", Path.home() / ".review-prs"))
    d = base / repo.replace("/", "__")
    (d / "drafts").mkdir(parents=True, exist_ok=True)
    return d


def load_state(repo):
    f = home(repo) / "state.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"repo": repo, "me": None, "prs": {}}


def save_state(repo, state):
    (home(repo) / "state.json").write_text(json.dumps(state, indent=2))


def me(state):
    if not state.get("me"):
        state["me"] = run(["gh", "api", "user", "--jq", ".login"]).strip()
    return state["me"]


def draft_file(repo, n):
    return home(repo) / "drafts" / f"pr-{n}.json"


def load_draft(repo, n):
    f = draft_file(repo, n)
    return json.loads(f.read_text()) if f.exists() else None


# ---------------------------------------------------------------- comments
def comments_needing_reply(pr_author, my_login, issue_comments, review_comments, seen):
    """Return comments that are waiting on *me*.

    Review-comment threads: the newest comment is by someone else (not a bot), and
    I'm part of the thread, or it's my PR, or I'm @-mentioned.
    Issue (conversation) comments: by someone else, newer than anything handled,
    and it's my PR or I'm @-mentioned or it comes after my own last comment.
    """
    mention = re.compile(rf"@{re.escape(my_login)}\b", re.I)
    mine_pr = pr_author.lower() == my_login.lower()
    out = []

    threads = {}
    for c in review_comments:
        root = c.get("in_reply_to_id") or c["id"]
        threads.setdefault(root, []).append(c)
    for root, cs in threads.items():
        cs.sort(key=lambda c: c["created_at"])
        last = cs[-1]
        who = last["user"]["login"]
        if who.lower() == my_login.lower() or is_bot(last["user"]) or last["id"] in seen:
            continue
        in_thread = any(c["user"]["login"].lower() == my_login.lower() for c in cs)
        if in_thread or mine_pr or mention.search(last.get("body") or ""):
            out.append({
                "kind": "review_comment",
                "id": last["id"],
                "reply_to": root,
                "author": who,
                "path": last.get("path"),
                "line": last.get("line") or last.get("original_line"),
                "body": last.get("body"),
                "diff_hunk": cs[0].get("diff_hunk"),
                "thread": [{"author": c["user"]["login"], "body": c.get("body")} for c in cs],
                "url": last.get("html_url"),
            })

    ics = sorted(issue_comments, key=lambda c: c["created_at"])
    my_last = max((c["created_at"] for c in ics if c["user"]["login"].lower() == my_login.lower()), default=None)
    for c in ics:
        who = c["user"]["login"]
        if who.lower() == my_login.lower() or is_bot(c["user"]) or c["id"] in seen:
            continue
        after_me = my_last is not None and c["created_at"] > my_last
        if mine_pr or after_me or mention.search(c.get("body") or ""):
            out.append({
                "kind": "issue_comment",
                "id": c["id"],
                "author": who,
                "body": c.get("body"),
                "url": c.get("html_url"),
            })
    return out


# ---------------------------------------------------------------- stacks
def patch_id(diff_text):
    """Content fingerprint of a diff that ignores line offsets, so a rebase that
    doesn't change the PR's own changes keeps the same id."""
    if not diff_text.strip():
        return None
    out = run(["git", "patch-id", "--stable"], input_text=diff_text, check=False).split()
    return out[0] if out else None


def stack_map(prs):
    """A PR is stacked on another when its base branch is that PR's head branch."""
    by_head = {p["headRefName"]: p for p in prs}
    info = {}
    for p in prs:
        parent = by_head.get(p["baseRefName"])
        kids = [q["number"] for q in prs if q["baseRefName"] == p["headRefName"]]
        if parent or kids:
            chain, cur = [], p
            while cur and cur["number"] not in chain:
                chain.insert(0, cur["number"])
                cur = by_head.get(cur["baseRefName"])
            info[p["number"]] = {"parent": parent["number"] if parent else None, "children": kids,
                                 "below": chain[:-1], "position": len(chain)}
    return info


# ---------------------------------------------------------------- scan
def cmd_scan(a):
    repo = a.repo
    state = load_state(repo)
    login = me(state)
    prs = gh_json(["pr", "list", "-R", repo, "--state", "open", "--limit", "200", "--json",
                   "number,title,author,isDraft,headRefOid,baseRefName,headRefName,updatedAt,url"]) or []
    stacks = stack_map(prs)
    open_nums = {str(p["number"]) for p in prs}
    result = []
    for p in prs:
        n = p["number"]
        author = p["author"]["login"]
        entry = {"number": n, "title": p["title"], "author": author, "url": p["url"],
                 "head_sha": p["headRefOid"], "actions": [], "skip_reason": None}
        if n in stacks:
            entry["stack"] = stacks[n]
        if p["isDraft"] and not a.include_drafts:
            entry["skip_reason"] = "draft"
            result.append(entry)
            continue
        if is_bot(p["author"]) and not a.include_bots:
            entry["skip_reason"] = "bot author"
            result.append(entry)
            continue
        st = state["prs"].get(str(n), {})
        own = author.lower() == login.lower()

        if not own or a.include_own:
            prev = st.get("reviewed_sha")
            if prev is None:
                entry["actions"].append("review")
            elif prev != p["headRefOid"]:
                # Restacked or rebased with the same changes? Then there's nothing to re-review.
                pid = st.get("patch_id")
                if pid and patch_id(run(["gh", "pr", "diff", str(n), "-R", repo])) == pid:
                    st["reviewed_sha"] = p["headRefOid"]
                    state["prs"][str(n)] = st
                    entry["rebased_only"] = True
                else:
                    entry["actions"].append("re-review")
                    entry["previous_sha"] = prev

        seen = set(st.get("seen_comment_ids", []))
        ic = gh_paged(f"repos/{repo}/issues/{n}/comments")
        rc = gh_paged(f"repos/{repo}/pulls/{n}/comments")
        waiting = comments_needing_reply(author, login, ic, rc, seen)
        if waiting:
            entry["actions"].append("reply")
            entry["comments_waiting"] = len(waiting)

        d = load_draft(repo, n)
        if d and d.get("status") == "staged":
            d = reconcile_staged(repo, n, d)
            entry["staged_on_github"] = d.get("status") == "staged"
        if d and d.get("status") in ("pending", "staged"):
            entry["pending_draft"] = True
            # A pending draft for this same head and no new comments: nothing to do.
            same = d.get("head_sha") == p["headRefOid"] or entry.get("rebased_only")
            if same and "reply" not in entry["actions"] and "re-review" not in entry["actions"]:
                entry["actions"] = []
                entry["skip_reason"] = "draft already waiting for you"
        if not entry["actions"] and not entry["skip_reason"]:
            entry["skip_reason"] = ("rebased, same changes" if entry.get("rebased_only")
                                    else "own PR, no new comments" if own else "nothing new since last pass")
        result.append(entry)

    # tidy up worktrees for PRs that closed
    wt = home(repo) / "wt"
    if wt.exists():
        for d in wt.iterdir():
            num = d.name.replace("pr-", "")
            if num not in open_nums:
                run(["git", "-C", str(home(repo) / "clone"), "worktree", "remove", "--force", str(d)], check=False)
    print(json.dumps({"repo": repo, "me": login, "scanned_at": now(), "prs": result}, indent=2))
    save_state(repo, state)


# ---------------------------------------------------------------- fetch
def ensure_clone(repo):
    clone = home(repo) / "clone"
    if not (clone / ".git").exists() and not (clone / "HEAD").exists():
        run(["gh", "repo", "clone", repo, str(clone), "--", "--filter=blob:none", "--no-checkout", "-q"])
    return clone


def cmd_fetch(a):
    repo, n = a.repo, a.number
    state = load_state(repo)
    login = me(state)
    work = home(repo) / "work" / f"pr-{n}"
    work.mkdir(parents=True, exist_ok=True)

    pr = gh_json(["pr", "view", str(n), "-R", repo, "--json",
                  "number,title,body,author,url,baseRefName,headRefName,headRefOid,baseRefOid,"
                  "files,closingIssuesReferences,labels,isDraft"])
    (work / "pr.json").write_text(json.dumps(pr, indent=2))
    (work / "diff.patch").write_text(run(["gh", "pr", "diff", str(n), "-R", repo]))

    ic = gh_paged(f"repos/{repo}/issues/{n}/comments")
    rc = gh_paged(f"repos/{repo}/pulls/{n}/comments")
    rv = gh_paged(f"repos/{repo}/pulls/{n}/reviews")
    (work / "comments.json").write_text(json.dumps({"issue_comments": ic, "review_comments": rc, "reviews": rv}, indent=2))

    st = state["prs"].get(str(n), {})
    waiting = comments_needing_reply(pr["author"]["login"], login, ic, rc, set(st.get("seen_comment_ids", [])))
    (work / "waiting.json").write_text(json.dumps(waiting, indent=2))

    # linked issues (the spec)
    issues = []
    for ref in pr.get("closingIssuesReferences") or []:
        num = ref.get("number")
        if num:
            issues.append(gh_json(["issue", "view", str(num), "-R", repo, "--json", "number,title,body,url"]))
    (work / "issues.json").write_text(json.dumps(issues, indent=2))

    # stack context: the layers below and above this one
    stack = None
    allprs = gh_json(["pr", "list", "-R", repo, "--state", "open", "--limit", "200", "--json",
                      "number,title,body,headRefName,baseRefName,headRefOid,isDraft"]) or []
    sm = stack_map(allprs)
    if n in sm:
        sdir = work / "stack"; sdir.mkdir(exist_ok=True)
        layers = []
        def above(x):
            out = []
            for c in sm.get(x, {}).get("children", []):
                out += [c] + above(c)
            return out
        for num in sm[n]["below"] + [n] + above(n):
            q = next(x for x in allprs if x["number"] == num)
            rel = "this" if num == n else ("below" if num in sm[n]["below"] else "above")
            layer = {"number": num, "relation": rel, "title": q["title"], "base": q["baseRefName"],
                     "head": q["headRefName"], "body": q["body"]}
            if num != n:
                f = sdir / f"pr-{num}.diff"
                f.write_text(run(["gh", "pr", "diff", str(num), "-R", repo], check=False))
                layer["diff"] = str(f)
            layers.append(layer)
        stack = {**sm[n], "layers": layers}
        (work / "stack.json").write_text(json.dumps(stack, indent=2))

    # code at the PR head, in a worktree that is ours alone
    worktree = None
    incremental = None
    if not a.no_checkout:
        clone = ensure_clone(repo)
        run(["git", "-C", str(clone), "fetch", "-q", "origin",
             f"+refs/pull/{n}/head:refs/remotes/pr/{n}", pr["baseRefName"]])
        wt = home(repo) / "wt" / f"pr-{n}"
        if wt.exists():
            run(["git", "-C", str(wt), "checkout", "-q", "--detach", pr["headRefOid"]])
        else:
            wt.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "-C", str(clone), "worktree", "add", "-q", "--detach", str(wt), pr["headRefOid"]])
        worktree = str(wt)
        prev = st.get("reviewed_sha")
        if prev and prev != pr["headRefOid"]:
            ok = subprocess.run(["git", "-C", str(clone), "merge-base", "--is-ancestor", prev, pr["headRefOid"]],
                                capture_output=True).returncode == 0
            if ok:
                (work / "incremental.patch").write_text(run(["git", "-C", str(clone), "diff", prev, pr["headRefOid"]]))
                incremental = str(work / "incremental.patch")

    prev_draft = draft_file(repo, n)
    print(json.dumps({
        "pr": n, "title": pr["title"], "author": pr["author"]["login"], "head_sha": pr["headRefOid"],
        "own_pr": pr["author"]["login"].lower() == login.lower(),
        "work_dir": str(work), "worktree": worktree,
        "files": {k: str(work / k) for k in ["pr.json", "diff.patch", "comments.json", "waiting.json", "issues.json"]},
        "incremental_patch": incremental,
        "previous_reviewed_sha": st.get("reviewed_sha"),
        "previous_draft": str(prev_draft) if prev_draft.exists() else None,
        "comments_waiting": len(waiting),
        "stack": ({k: stack[k] for k in ("parent", "children", "below", "position")} | {"file": str(work / "stack.json")}) if stack else None,
        "write_draft_to": str(draft_file(repo, n)),
    }, indent=2))
    save_state(repo, state)


# ---------------------------------------------------------------- mark
def cmd_mark(a):
    state = load_state(a.repo)
    st = state["prs"].setdefault(str(a.number), {})
    if a.sha:
        st["reviewed_sha"] = a.sha
        diff = home(a.repo) / "work" / f"pr-{a.number}" / "diff.patch"
        if diff.exists():
            st["patch_id"] = patch_id(diff.read_text())
    seen = set(st.get("seen_comment_ids", []))
    seen.update(int(c) for c in a.comments or [])
    st["seen_comment_ids"] = sorted(seen)
    st["last_pass"] = now()
    save_state(a.repo, state)
    print(json.dumps({"pr": a.number, **st}, indent=2))


# ---------------------------------------------------------------- show
DOORS = {"one-way", "two-way"}


def merge_danger_block(a):
    """The Merge Danger block, in the same shape as the /pr skill's PR body."""
    if not a:
        return ""
    lines = [f"**Door:** {a['door'].capitalize()}"]
    if a.get("door_note"):
        lines.append(a["door_note"].strip())
    lines += ["", f"**Blast Radius:** {a['blast_radius'].capitalize()}"]
    if a.get("blast_note"):
        lines.append(a["blast_note"].strip())
    lines += ["", f"**Rubberstampability:** {a['rubberstamp']}/5. {a.get('rubberstamp_reason', '').strip()}".rstrip()]
    return "\n".join(lines)


def review_body(d):
    r = d.get("review") or {}
    body = (r.get("body") or "").strip()
    block = merge_danger_block(d.get("assessment"))
    return f"{body}\n\n{block}".strip() if block else body


def assessment_problems(d):
    a = d.get("assessment")
    if not d.get("review"):
        return []
    if not a:
        return ["review has no assessment (door, blast radius, rubberstampability)"]
    out = []
    if a.get("door") not in DOORS:
        out.append("assessment.door must be 'one-way' or 'two-way'")
    br = (a.get("blast_radius") or "").strip()
    if not br or " " in br:
        out.append("assessment.blast_radius must be one word")
    rs = a.get("rubberstamp")
    if not isinstance(rs, int) or not 1 <= rs <= 5:
        out.append("assessment.rubberstamp must be an integer 1-5")
    elif a.get("door") == "one-way" and rs > 3:
        out.append("a one-way door caps rubberstampability at 3")
    return out


def render(d):
    lines = [f"## PR #{d['pr']} — {d.get('title', '')}  [{d.get('status')}]",
             f"head {d.get('head_sha', '')[:8]}  ·  drafted {d.get('created_at', '')}"]
    if d.get("status") == "staged":
        lines.append(f"On GitHub as your pending review (only you can see it): {d.get('staged_url', '')}")
    lines.append("")
    r = d.get("review")
    if r:
        lines += ["### Review (posted as a plain comment review)", "", review_body(d), ""]
        for c in r.get("comments", []):
            rng = f"{c['start_line']}-{c['line']}" if c.get("start_line") else str(c["line"])
            lines += [f"**{c['path']}:{rng}**", "", c["body"].strip(), ""]
    for rep in d.get("replies", []):
        lines += [f"### Reply to @{rep.get('to', '?')} ({rep['kind']})",
                  f"> {(rep.get('context') or '').strip()[:300]}", "", rep["body"].strip(), ""]
    if d.get("notes_for_me"):
        lines += ["### Notes for you (not posted)", "", d["notes_for_me"].strip(), ""]
    for p in voice_problems(d) + assessment_problems(d):
        lines.append(f"WARNING: {p} (post will refuse this draft)")
    return "\n".join(lines)


def cmd_show(a):
    drafts = sorted((home(a.repo) / "drafts").glob("pr-*.json"), key=lambda p: int(p.stem[3:]))
    shown = 0
    for f in drafts:
        d = json.loads(f.read_text())
        if a.number and d["pr"] != a.number:
            continue
        if not a.number and d.get("status") not in ("pending", "staged"):
            continue
        print(render(d)); print("\n---\n"); shown += 1
    if not shown:
        print("No pending drafts.")


# ---------------------------------------------------------------- post
DASH_RE = re.compile(r"—| – ")


def voice_problems(d):
    """Hard rules from the voice guide that a script can check."""
    texts = []
    r = d.get("review")
    if r:
        texts.append(("review summary", review_body(d)))
        texts += [(f"comment on {c.get('path')}:{c.get('line')}", c.get("body", "")) for c in r.get("comments", [])]
    texts += [(f"reply to @{x.get('to', '?')}", x.get("body", "")) for x in d.get("replies", []) if not x.get("posted")]
    return [f"{where} has an em dash" for where, t in texts if DASH_RE.search(t or "")]


def reanchor(repo, n, d, head):
    """After a restack, check the review can move to the new head untouched.
    Returns None if it can, otherwise the reason it can't."""
    st = load_state(repo)["prs"].get(str(n), {})
    if not st.get("patch_id") or patch_id(run(["gh", "pr", "diff", str(n), "-R", repo])) != st["patch_id"]:
        return "the PR's changes are different"
    clone = home(repo) / "clone"
    if not clone.exists():
        return "no local clone to compare lines"
    run(["git", "-C", str(clone), "fetch", "-q", "origin", f"+refs/pull/{n}/head:refs/remotes/pr/{n}"], check=False)
    for c in (d.get("review") or {}).get("comments", []):
        def lines(sha):
            txt = run(["git", "-C", str(clone), "show", f"{sha}:{c['path']}"], check=False).splitlines()
            lo = c.get("start_line") or c["line"]
            return txt[lo - 1:c["line"]]
        if lines(d["head_sha"]) != lines(head):
            return f"lines moved in {c['path']}"
    return None


def resolve_commit(repo, n, d, done):
    """The commit a review should attach to, or None if the PR moved too much."""
    head = gh_json(["pr", "view", str(n), "-R", repo, "--json", "headRefOid"])["headRefOid"]
    if head == d["head_sha"]:
        return head
    moved = reanchor(repo, n, d, head)
    if moved:
        done.append(f"review NOT sent: PR moved from {d['head_sha'][:8]} to {head[:8]} ({moved}); re-run the review")
        return None
    done.append(f"PR was rebased to {head[:8]} with the same changes; using the new head")
    return head


def review_payload(d, commit, event):
    r = d.get("review") or {}
    payload = {"commit_id": commit, "body": review_body(d),
               "comments": [{k: v for k, v in c.items() if k in ("path", "line", "start_line", "side", "start_side", "body")}
                            for c in r.get("comments", [])]}
    for c in payload["comments"]:
        c.setdefault("side", "RIGHT")
    if event:
        payload["event"] = event
    return payload


def my_pending_review(repo, n, login):
    for rv in gh_paged(f"repos/{repo}/pulls/{n}/reviews"):
        if rv.get("state") == "PENDING" and (rv.get("user") or {}).get("login", "").lower() == login.lower():
            return rv
    return None


def thread_id_for(repo, n, root_comment_id):
    owner, name = repo.split("/", 1)
    q = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){"
         "reviewThreads(first:100){nodes{id comments(first:1){nodes{databaseId}}}}}}}")
    data = gh_json(["api", "graphql", "-f", f"query={q}", "-F", f"o={owner}", "-F", f"r={name}", "-F", f"n={n}"])
    for t in data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
        first = t["comments"]["nodes"]
        if first and first[0]["databaseId"] == root_comment_id:
            return t["id"]
    return None


def reconcile_staged(repo, n, d):
    """A staged draft's review lives on GitHub as PENDING until Jeremy submits or discards it."""
    rid = d.get("staged_review_id")
    p = subprocess.run(["gh", "api", f"repos/{repo}/pulls/{n}/reviews/{rid}"], capture_output=True, text=True)
    if p.returncode != 0:
        d["status"] = "discarded"
    else:
        state = json.loads(p.stdout).get("state")
        if state != "PENDING":
            # Submitted on GitHub. Anything that couldn't be staged (conversation replies) still waits.
            if d.get("review"):
                d["review"]["posted"] = True
            for x in d.get("replies", []):
                if x.get("staged"):
                    x["posted"] = True
            left = [x for x in d.get("replies", []) if not x.get("posted")]
            d["status"] = "pending" if left else "posted"
            if not left:
                d["posted_at"] = now()
    if d["status"] != "staged":
        draft_file(repo, n).write_text(json.dumps(d, indent=2))
        st = load_state(repo)
        st["prs"].get(str(n), {}).pop("staged_review_id", None)
        save_state(repo, st)
    return d


def stage_one(repo, n, dry):
    d = load_draft(repo, n)
    if not d:
        return f"PR #{n}: no draft"
    if d.get("status") not in ("pending", "staged"):
        return f"PR #{n}: draft is {d.get('status')}, nothing to stage"
    problems = voice_problems(d) + assessment_problems(d)
    if problems:
        return f"PR #{n}: NOT staged, fix the draft first: " + "; ".join(problems)
    thread_replies = [x for x in d.get("replies", []) if x["kind"] == "review_comment" and not x.get("posted")]
    convo_replies = [x for x in d.get("replies", []) if x["kind"] == "issue_comment" and not x.get("posted")]
    if not d.get("review") and not thread_replies:
        return f"PR #{n}: nothing that can wait on GitHub" + (f"; {len(convo_replies)} conversation replies stay local, use post" if convo_replies else "")
    done = []
    commit = resolve_commit(repo, n, d, done)
    if not commit:
        return f"PR #{n}: " + "; ".join(done)

    state = load_state(repo)
    login = me(state)
    st = state["prs"].setdefault(str(n), {})
    ours = st.get("staged_review_id")
    existing = my_pending_review(repo, n, login)
    if existing and existing["id"] != ours:
        return (f"PR #{n}: NOT staged, you already have your own pending review on this PR on GitHub. "
                f"GitHub allows one per person, so submit or discard it first.")
    if dry:
        if existing:
            done.append(f"DRY RUN: delete old staged review {existing['id']}")
        done.append(f"DRY RUN: create pending review with {len((d.get('review') or {}).get('comments', []))} comments"
                    + (f" and {len(thread_replies)} thread replies" if thread_replies else ""))
        return f"PR #{n}: " + "; ".join(done)
    if existing:
        run(["gh", "api", "-X", "DELETE", f"repos/{repo}/pulls/{n}/reviews/{existing['id']}"])
        done.append("replaced the earlier staged review")

    rv = json.loads(run(["gh", "api", "-X", "POST", f"repos/{repo}/pulls/{n}/reviews", "--input", "-"],
                        input_text=json.dumps(review_payload(d, commit, None))))
    for rep in thread_replies:
        tid = thread_id_for(repo, n, rep["reply_to"])
        if not tid:
            done.append(f"reply to @{rep.get('to', '?')} kept local (thread not found)")
            continue
        m = ("mutation($rv:ID!,$t:ID!,$b:String!){addPullRequestReviewThreadReply("
             "input:{pullRequestReviewId:$rv,pullRequestReviewThreadId:$t,body:$b}){comment{id}}}")
        run(["gh", "api", "graphql", "-f", f"query={m}", "-f", f"rv={rv['node_id']}", "-f", f"t={tid}", "-f", f"b={rep['body']}"])
        rep["staged"] = True
    d.update(status="staged", staged_review_id=rv["id"], staged_url=rv.get("html_url"), staged_at=now(), staged_commit=commit)
    st["staged_review_id"] = rv["id"]
    save_state(repo, state)
    draft_file(repo, n).write_text(json.dumps(d, indent=2))
    n_c = len((d.get("review") or {}).get("comments", []))
    n_r = sum(1 for x in thread_replies if x.get("staged"))
    done.append(f"waiting on GitHub as your pending review ({n_c} comments, {n_r} thread replies): {rv.get('html_url', '')}")
    if convo_replies:
        done.append(f"{len(convo_replies)} conversation replies stay local (GitHub can't hold those as pending); use post")
    return f"PR #{n}: " + "; ".join(done)


def cmd_stage(a):
    nums = ([json.loads(f.read_text())["pr"] for f in (home(a.repo) / "drafts").glob("pr-*.json")
             if json.loads(f.read_text()).get("status") == "pending"]
            if a.target == "all" else [int(x) for x in a.target.split(",")])
    for n in sorted(nums):
        print(stage_one(a.repo, n, a.dry_run))


def post_one(repo, n, dry, only):
    d = load_draft(repo, n)
    if not d:
        return f"PR #{n}: no draft"
    if d.get("status") == "staged":
        d = reconcile_staged(repo, n, d)
    if d.get("status") not in ("pending", "staged"):
        return f"PR #{n}: draft is {d.get('status')}, nothing to post"
    problems = voice_problems(d) + assessment_problems(d)
    if problems and d.get("status") == "pending":
        return f"PR #{n}: NOT posted, fix the draft first: " + "; ".join(problems)
    done = []
    calls = []
    r = d.get("review")
    if d.get("status") == "staged" and only in (None, "review"):
        # Submit the pending review exactly as it now stands on GitHub, including any edits made there.
        calls.append(("staged", ["gh", "api", "-X", "POST", f"repos/{repo}/pulls/{n}/reviews/{d['staged_review_id']}/events",
                                 "--input", "-"], json.dumps({"event": "COMMENT"})))
        r = None
    if r and not r.get("posted") and only in (None, "review"):
        commit = resolve_commit(repo, n, d, done)
        if commit:
            payload = review_payload(d, commit, "COMMENT")
            calls.append(("review", ["gh", "api", "-X", "POST", f"repos/{repo}/pulls/{n}/reviews", "--input", "-"], json.dumps(payload)))
    if only in (None, "replies"):
        for rep in d.get("replies", []):
            if rep.get("posted") or rep.get("staged"):
                continue
            if rep["kind"] == "review_comment":
                cmd = ["gh", "api", "-X", "POST", f"repos/{repo}/pulls/{n}/comments/{rep['reply_to']}/replies", "--input", "-"]
            else:
                cmd = ["gh", "api", "-X", "POST", f"repos/{repo}/issues/{n}/comments", "--input", "-"]
            calls.append((rep, cmd, json.dumps({"body": rep["body"]})))
    for what, cmd, body in calls:
        if dry:
            done.append(f"DRY RUN: {' '.join(cmd)}  <<< {body[:200]}")
            continue
        run(cmd, input_text=body)
        if what == "review":
            r["posted"] = True
            done.append("review posted")
        elif what == "staged":
            for x in d.get("replies", []):
                if x.get("staged"):
                    x["posted"] = True
            d["review_submitted"] = True
            done.append("pending review submitted")
        else:
            what["posted"] = True
            done.append(f"reply to @{what.get('to', '?')} posted")
    if not dry:
        review_ok = (not r or r.get("posted")) if d.get("status") != "staged" else d.get("review_submitted")
        replies_ok = all(x.get("posted") for x in d.get("replies", []))
        if review_ok and replies_ok:
            d["status"] = "posted"
            load = load_state(repo); load["prs"].get(str(n), {}).pop("staged_review_id", None); save_state(repo, load)
            d["posted_at"] = now()
        draft_file(repo, n).write_text(json.dumps(d, indent=2))
    return f"PR #{n}: " + ("; ".join(done) if done else "nothing to post")


def cmd_post(a):
    if a.target == "all":
        nums = [json.loads(f.read_text())["pr"] for f in (home(a.repo) / "drafts").glob("pr-*.json")
                if json.loads(f.read_text()).get("status") in ("pending", "staged")]
    else:
        nums = [int(x) for x in a.target.split(",")]
    for n in sorted(nums):
        print(post_one(a.repo, n, a.dry_run, a.only))


def cmd_digest(a):
    rows = []
    for f in sorted((home(a.repo) / "drafts").glob("pr-*.json"), key=lambda p: int(p.stem[3:])):
        d = json.loads(f.read_text())
        if d.get("status") not in ("pending", "staged"):
            continue
        asm = d.get("assessment") or {}
        r = d.get("review") or {}
        cs = r.get("comments", [])
        nits = sum(1 for c in cs if (c.get("body") or "").lstrip().lower().startswith("nit"))
        parts = []
        if asm:
            parts.append(f"{asm.get('door', '?')} · {asm.get('blast_radius', '?')} · {asm.get('rubberstamp', '?')}/5")
        if r:
            parts.append(f"{len(cs) - nits} must-fix, {nits} nits")
        if d.get("replies"):
            parts.append(f"{len(d['replies'])} replies")
        if d.get("status") == "staged":
            parts.append("on GitHub, not submitted")
        rows.append((asm.get("rubberstamp", 9), f"  #{d['pr']:<5} {d.get('title', '')[:48]:<48}  " + "  ·  ".join(parts)))
    if not rows:
        print("No pending drafts."); return
    print("Pending drafts, riskiest first")
    for _, line in sorted(rows):
        print(line)

    # stacks: bottom to top, with the stack's worst door and lowest score
    prs = gh_json(["pr", "list", "-R", a.repo, "--state", "open", "--limit", "200", "--json",
                   "number,headRefName,baseRefName"]) or []
    sm = stack_map(prs)
    tops = [n for n, i in sm.items() if not i["children"]]
    shown = set()
    for top in sorted(tops):
        chain = sm[top]["below"] + [top]
        if len(chain) < 2 or tuple(chain) in shown:
            continue
        shown.add(tuple(chain))
        asms = [(x, (load_draft(a.repo, x) or {}).get("assessment") or {}) for x in chain]
        one_way = [f"#{x}" for x, m in asms if m.get("door") == "one-way"]
        scores = [m["rubberstamp"] for _, m in asms if isinstance(m.get("rubberstamp"), int)]
        bits = [" -> ".join(f"#{x}" for x in chain)]
        bits.append(f"one-way at {', '.join(one_way)}" if one_way else "all two-way so far")
        if scores:
            bits.append(f"lowest {min(scores)}/5")
        missing = [f"#{x}" for x, m in asms if not m]
        if missing:
            bits.append(f"not scored: {', '.join(missing)}")
        print("Stack  " + "  ·  ".join(bits))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("repo"); s.add_argument("--include-drafts", action="store_true"); s.add_argument("--include-bots", action="store_true"); s.add_argument("--include-own", action="store_true", help="also review PRs you opened")
    s = sub.add_parser("fetch"); s.add_argument("repo"); s.add_argument("number", type=int); s.add_argument("--no-checkout", action="store_true")
    s = sub.add_parser("mark"); s.add_argument("repo"); s.add_argument("number", type=int); s.add_argument("--sha"); s.add_argument("--comments", nargs="*")
    s = sub.add_parser("show"); s.add_argument("repo"); s.add_argument("number", type=int, nargs="?")
    s = sub.add_parser("post"); s.add_argument("repo"); s.add_argument("target", help="PR number, comma list, or 'all'")
    s.add_argument("--dry-run", action="store_true"); s.add_argument("--only", choices=["review", "replies"])
    s = sub.add_parser("stage"); s.add_argument("repo"); s.add_argument("target", help="PR number, comma list, or 'all'")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("path"); s.add_argument("repo")
    s = sub.add_parser("digest"); s.add_argument("repo")
    a = ap.parse_args()
    if a.cmd == "path":
        print(home(a.repo)); return
    {"scan": cmd_scan, "fetch": cmd_fetch, "mark": cmd_mark, "show": cmd_show, "post": cmd_post, "digest": cmd_digest, "stage": cmd_stage}[a.cmd](a)


if __name__ == "__main__":
    main()
