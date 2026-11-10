#!/usr/bin/env python3
"""Helper for the review-prs skill. Wraps the `gh` CLI so every pass is consistent.

Data (JSON) lives in a .skills-local folder, which is git-ignored globally:
  - run from inside a clone of the repo: <repo root>/.skills-local/review-prs/
  - anywhere else:                       ~/.skills-local/review-prs/<owner>__<repo>/
  (override with REVIEW_PRS_HOME)

  state.json        what has been reviewed (head SHA) and which comments were handled
  drafts/pr-N.json  reviews and replies waiting for your OK
  findings.jsonl    every labelled comment, for trends
  work/pr-N/        what fetch pulled for each PR

Code checkouts live outside any repo, in ~/.skills-local/cache/<owner>__<repo>/ (override
with REVIEW_PRS_CACHE), so test runners and linters in your project never see them:
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
  report REPO [N]              your local read on each PR: overall take + Merge Danger (never posted)
  overlaps REPO [N]            open PRs that touch the same files (not counting their own stack)
  trends REPO [--days D]       findings that keep repeating across PRs (fix the factory, not the PR)
  config REPO [KEY [VALUE]]    show or set settle_minutes, review_red
  path  REPO                    print the state directory

REPO can also be a PR URL (https://github.com/owner/repo/pull/123); scan then looks at that PR only.

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
    p = subprocess.run(cmd, cwd=cwd, input=input_text, capture_output=True, text=True, encoding="utf-8", errors="replace")
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


_HOMES = {}


def ensure_global_ignore():
    """Make sure `.skills-local/` is in the user's global gitignore. Idempotent."""
    path = run(["git", "config", "--global", "--get", "core.excludesFile"], check=False).strip()
    if path:
        f = Path(os.path.expanduser(path))
    else:
        # git reads this file by default when core.excludesFile isn't set
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        f = Path(xdg) / "git" / "ignore"
    text = f.read_text(encoding="utf-8") if f.exists() else ""
    if not re.search(r"^/?\.skills-local/?\s*$", text, re.M):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".skills-local/\n", encoding="utf-8")
        print(f"review-prs: added .skills-local/ to your global gitignore ({f})", file=sys.stderr)
    return f


def repo_root_for(repo):
    """The current directory's git root, if it's a clone of `repo`."""
    root = run(["git", "rev-parse", "--show-toplevel"], check=False).strip()
    if not root:
        return None
    remotes = run(["git", "-C", root, "remote", "-v"], check=False).lower()
    return Path(root) if re.search(rf"[/:]{re.escape(repo.lower())}(\.git)?\s", remotes) else None


def home(repo):
    """Where this repo's JSON lives. Inside the repo's own .skills-local folder when run from a clone."""
    if repo in _HOMES:
        return _HOMES[repo]
    if os.environ.get("REVIEW_PRS_HOME"):
        d = Path(os.environ["REVIEW_PRS_HOME"]) / repo.replace("/", "__")
    else:
        root = repo_root_for(repo)
        if root:
            ensure_global_ignore()
            # belt and braces: also ignore it in this clone, in case a repo rule un-ignores it
            if subprocess.run(["git", "-C", str(root), "check-ignore", "-q", ".skills-local/x"]).returncode != 0:
                excl = root / ".git" / "info" / "exclude"
                if excl.parent.exists():
                    excl.write_text((excl.read_text(encoding="utf-8") if excl.exists() else "") + "\n.skills-local/\n", encoding="utf-8")
            d = root / ".skills-local" / "review-prs"
        else:
            d = Path.home() / ".skills-local" / "review-prs" / repo.replace("/", "__")
    (d / "drafts").mkdir(parents=True, exist_ok=True)
    migrate_old_state(repo, d)
    _HOMES[repo] = d
    return d


def cache(repo):
    """Where clones and worktrees live: never inside a project, so tools there don't pick them up."""
    base = Path(os.environ.get("REVIEW_PRS_CACHE", Path.home() / ".skills-local" / "cache"))
    d = base / repo.replace("/", "__")
    d.mkdir(parents=True, exist_ok=True)
    return d


def migrate_old_state(repo, d):
    """One-time move of JSON from older locations (~/.review-prs, .jeremy)."""
    slug = repo.replace("/", "__")
    candidates = [Path.home() / ".review-prs" / slug, Path.home() / ".jeremy" / "review-prs" / slug]
    if d.parent.name == ".skills-local":
        candidates.insert(0, d.parent.parent / ".jeremy" / "review-prs")
    old = next((c for c in candidates if (c / "state.json").exists()), None)
    if old is None or (d / "state.json").exists():
        return
    import shutil
    for name in ("state.json", "findings.jsonl", "factory-notes.md"):
        if (old / name).exists():
            shutil.copy2(old / name, d / name)
    for sub in ("drafts", "work"):
        if (old / sub).exists():
            shutil.copytree(old / sub, d / sub, dirs_exist_ok=True)
    old.rename(old.with_name(old.name + ".migrated"))  # so it's only picked up once
    print(f"review-prs: moved your drafts and state from {old} to {d}", file=sys.stderr)


def load_state(repo):
    f = home(repo) / "state.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {"repo": repo, "me": None, "prs": {}}


def save_state(repo, state):
    (home(repo) / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def me(state):
    if not state.get("me"):
        state["me"] = run(["gh", "api", "user", "--jq", ".login"]).strip()
    return state["me"]


def draft_file(repo, n):
    return home(repo) / "drafts" / f"pr-{n}.json"


def load_draft(repo, n):
    f = draft_file(repo, n)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


DEFAULTS = {"settle_minutes": 15, "review_red": False}
CATEGORIES = {"spec", "architecture", "security", "one-way-door", "agent-friction", "tautological-test",
              "performance", "correctness", "simplify", "reuse", "naming", "other"}


def config(state):
    return {**DEFAULTS, **state.get("config", {})}


def pr_signals(repo):
    """CI state and last commit time for every open PR, in one paged GraphQL query."""
    owner, name = repo.split("/", 1)
    q = ("query($o:String!,$r:String!,$c:String){repository(owner:$o,name:$r){pullRequests(states:OPEN,first:100,after:$c){"
         "pageInfo{hasNextPage endCursor} nodes{number commits(last:1){nodes{commit{committedDate statusCheckRollup{state}}}}}}}}")
    out, cursor = {}, None
    while True:
        args = ["api", "graphql", "-f", f"query={q}", "-F", f"o={owner}", "-F", f"r={name}"]
        if cursor:
            args += ["-f", f"c={cursor}"]
        data = gh_json(args)
        prs = data["data"]["repository"]["pullRequests"]
        for node in prs["nodes"]:
            commits = node["commits"]["nodes"]
            c = commits[0]["commit"] if commits else {}
            state = ((c.get("statusCheckRollup") or {}).get("state") or "").upper()
            ci = {"SUCCESS": "passing", "PENDING": "pending", "EXPECTED": "pending",
                  "FAILURE": "failing", "ERROR": "failing"}.get(state, "none")
            out[node["number"]] = {"ci": ci, "last_commit": c.get("committedDate")}
        if not prs["pageInfo"]["hasNextPage"]:
            return out
        cursor = prs["pageInfo"]["endCursor"]


def minutes_since(iso):
    if not iso:
        return None
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def parse_target(repo):
    """Accept owner/repo or a PR URL. Returns (repo, pr_number_or_None)."""
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", repo)
    if m:
        return m.group(1), int(m.group(2))
    m = re.match(r"^([^/\s]+/[^/\s#]+)#(\d+)$", repo)
    if m:
        return m.group(1), int(m.group(2))
    return repo, None


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
    cfg = config(state)
    signals = pr_signals(repo)
    only = getattr(a, "pr", None)
    if only and str(only) not in open_nums:
        sys.exit(f"PR #{only} is not open on {repo}")
    result = []
    for p in prs:
        if only and p["number"] != only:
            continue
        n = p["number"]
        author = p["author"]["login"]
        entry = {"number": n, "title": p["title"], "author": author, "url": p["url"],
                 "head_sha": p["headRefOid"], "actions": [], "skip_reason": None}
        if n in stacks:
            entry["stack"] = stacks[n]
        sig = signals.get(n, {})
        entry["ci"] = sig.get("ci", "none")
        st0 = state["prs"].setdefault(str(n), {})
        st0["ci"] = entry["ci"]
        if p["isDraft"] and not a.include_drafts and not only:
            entry["skip_reason"] = "draft"
            result.append(entry)
            continue
        if is_bot(p["author"]) and not a.include_bots and not only:
            entry["skip_reason"] = "bot author"
            result.append(entry)
            continue
        st = state["prs"].get(str(n), {})
        own = author.lower() == login.lower()

        if not own or a.include_own or only:
            prev = st.get("reviewed_sha")
            if only and prev == p["headRefOid"]:
                entry["actions"].append("review")  # asked for by number: review again even if nothing changed
                entry["forced"] = True
            elif prev is None:
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

        # Ralph loops push a lot: wait for CI and for the branch to go quiet before reviewing.
        wants_review = any(x in entry["actions"] for x in ("review", "re-review"))
        if wants_review and not only:
            quiet = minutes_since(sig.get("last_commit"))
            hold = None
            if entry["ci"] == "pending":
                hold = "waiting for CI"
            elif entry["ci"] == "failing" and not cfg["review_red"]:
                hold = "CI failing, waiting for the loop to fix it"
            elif quiet is not None and quiet < cfg["settle_minutes"]:
                hold = f"still settling (last commit {int(quiet)}m ago)"
            if hold:
                entry["actions"] = [x for x in entry["actions"] if x not in ("review", "re-review")]
                entry["held"] = hold

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
            if same and "reply" not in entry["actions"] and "re-review" not in entry["actions"] and not entry.get("forced"):
                entry["actions"] = []
                entry["skip_reason"] = "draft already waiting for you"
        if not entry["actions"] and not entry["skip_reason"]:
            entry["skip_reason"] = (entry["held"] if entry.get("held")
                                    else "rebased, same changes" if entry.get("rebased_only")
                                    else "own PR, no new comments" if own else "nothing new since last pass")
        result.append(entry)

    # tidy up worktrees for PRs that closed
    wt = cache(repo) / "wt"
    if wt.exists():
        for d in wt.iterdir():
            num = d.name.replace("pr-", "")
            if num not in open_nums:
                run(["git", "-C", str(cache(repo) / "clone"), "worktree", "remove", "--force", str(d)], check=False)
    print(json.dumps({"repo": repo, "me": login, "scanned_at": now(), "config": cfg, "prs": result}, indent=2))
    save_state(repo, state)


# ---------------------------------------------------------------- overlaps
def overlap_pairs(prs):
    """Pairs of open PRs that change the same files, ignoring PRs in the same stack."""
    sm = stack_map(prs)
    def lineage(n):
        info = sm.get(n)
        if not info:
            return {n}
        out = {n, *info["below"]}
        todo = list(info["children"])
        while todo:
            c = todo.pop(); out.add(c); todo += sm.get(c, {}).get("children", [])
        return out
    files = {p["number"]: {f["path"] for f in (p.get("files") or [])} for p in prs}
    titles = {p["number"]: p["title"] for p in prs}
    pairs = []
    nums = sorted(files)
    for i, x in enumerate(nums):
        for y in nums[i + 1:]:
            if y in lineage(x):
                continue
            shared = sorted(files[x] & files[y])
            if shared:
                pairs.append({"a": x, "b": y, "a_title": titles[x], "b_title": titles[y], "shared": shared})
    return sorted(pairs, key=lambda p: -len(p["shared"]))


def open_prs_with_files(repo):
    return gh_json(["pr", "list", "-R", repo, "--state", "open", "--limit", "200", "--json",
                    "number,title,body,headRefName,baseRefName,files"]) or []


def cmd_overlaps(a):
    pairs = overlap_pairs(open_prs_with_files(a.repo))
    if a.number:
        pairs = [p for p in pairs if a.number in (p["a"], p["b"])]
    if not pairs:
        print("No open PRs touch the same files."); return
    for p in pairs:
        more = f" (+{len(p['shared']) - 4} more)" if len(p["shared"]) > 4 else ""
        print(f"#{p['a']} {p['a_title'][:40]}  <->  #{p['b']} {p['b_title'][:40]}\n    {', '.join(p['shared'][:4])}{more}")


# ---------------------------------------------------------------- fetch
def ensure_clone(repo):
    clone = cache(repo) / "clone"
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
    (work / "pr.json").write_text(json.dumps(pr, indent=2), encoding="utf-8")
    (work / "diff.patch").write_text(run(["gh", "pr", "diff", str(n), "-R", repo]), encoding="utf-8")

    ic = gh_paged(f"repos/{repo}/issues/{n}/comments")
    rc = gh_paged(f"repos/{repo}/pulls/{n}/comments")
    rv = gh_paged(f"repos/{repo}/pulls/{n}/reviews")
    (work / "comments.json").write_text(json.dumps({"issue_comments": ic, "review_comments": rc, "reviews": rv}, indent=2), encoding="utf-8")

    st = state["prs"].get(str(n), {})
    waiting = comments_needing_reply(pr["author"]["login"], login, ic, rc, set(st.get("seen_comment_ids", [])))
    (work / "waiting.json").write_text(json.dumps(waiting, indent=2), encoding="utf-8")

    # linked issues (the spec)
    issues = []
    for ref in pr.get("closingIssuesReferences") or []:
        num = ref.get("number")
        if num:
            issues.append(gh_json(["issue", "view", str(num), "-R", repo, "--json", "number,title,body,url"]))
    (work / "issues.json").write_text(json.dumps(issues, indent=2), encoding="utf-8")

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
                f.write_text(run(["gh", "pr", "diff", str(num), "-R", repo], check=False), encoding="utf-8")
                layer["diff"] = str(f)
            layers.append(layer)
        stack = {**sm[n], "layers": layers}
        (work / "stack.json").write_text(json.dumps(stack, indent=2), encoding="utf-8")

    # my unresolved review threads on this PR, so a re-review can check whether they were fixed
    owner, name = repo.split("/", 1)
    q = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){"
         "reviewThreads(first:100){nodes{id isResolved isOutdated path line "
         "comments(first:50){nodes{databaseId body createdAt author{login}}}}}}}}")
    data = gh_json(["api", "graphql", "-f", f"query={q}", "-F", f"o={owner}", "-F", f"r={name}", "-F", f"n={n}"])
    open_threads = []
    for t in data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
        cs = t["comments"]["nodes"]
        if t["isResolved"] or not cs or (cs[0].get("author") or {}).get("login", "").lower() != login.lower():
            continue
        open_threads.append({"thread_id": t["id"], "reply_to": cs[0]["databaseId"], "path": t["path"], "line": t["line"],
                             "outdated": t["isOutdated"], "label": label_of(cs[0]["body"]), "body": cs[0]["body"],
                             "replies": [{"author": (c.get("author") or {}).get("login"), "body": c["body"]} for c in cs[1:]]})
    (work / "my_open_threads.json").write_text(json.dumps(open_threads, indent=2), encoding="utf-8")

    # other open PRs touching the same files (outside this PR's stack)
    overlaps = [p for p in overlap_pairs(open_prs_with_files(repo)) if n in (p["a"], p["b"])]
    (work / "overlaps.json").write_text(json.dumps(overlaps, indent=2), encoding="utf-8")

    # code at the PR head, in a worktree that is ours alone
    worktree = None
    incremental = None
    if not a.no_checkout:
        clone = ensure_clone(repo)
        run(["git", "-C", str(clone), "fetch", "-q", "origin",
             f"+refs/pull/{n}/head:refs/remotes/pr/{n}", pr["baseRefName"]])
        wt = cache(repo) / "wt" / f"pr-{n}"
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
                (work / "incremental.patch").write_text(run(["git", "-C", str(clone), "diff", prev, pr["headRefOid"]]), encoding="utf-8")
                incremental = str(work / "incremental.patch")

    prev_draft = draft_file(repo, n)
    print(json.dumps({
        "pr": n, "title": pr["title"], "author": pr["author"]["login"], "head_sha": pr["headRefOid"],
        "own_pr": pr["author"]["login"].lower() == login.lower(),
        "work_dir": str(work), "worktree": worktree,
        "files": {k: str(work / k) for k in ["pr.json", "diff.patch", "comments.json", "waiting.json", "issues.json",
                                              "my_open_threads.json", "overlaps.json"]},
        "my_open_threads": len(open_threads),
        "overlapping_prs": [p["b"] if p["a"] == n else p["a"] for p in overlaps],
        "incremental_patch": incremental,
        "previous_reviewed_sha": st.get("reviewed_sha"),
        "previous_draft": str(prev_draft) if prev_draft.exists() else None,
        "comments_waiting": len(waiting),
        "stack": ({k: stack[k] for k in ("parent", "children", "below", "position")} | {"file": str(work / "stack.json")}) if stack else None,
        "write_draft_to": str(draft_file(repo, n)),
    }, indent=2))
    save_state(repo, state)


# ---------------------------------------------------------------- findings log
def log_findings(repo, n, sha):
    """Append this review's labelled comments to findings.jsonl, so trends can spot repeats."""
    d = load_draft(repo, n)
    if not d or d.get("head_sha") != sha or not d.get("review"):
        return
    f = home(repo) / "findings.jsonl"
    seen = set()
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                x = json.loads(line); seen.add((x["pr"], x["sha"], x["path"], x["line"]))
            except (ValueError, KeyError):
                pass
    with f.open("a", encoding="utf-8") as out:
        for c in d["review"].get("comments", []):
            key = (n, sha, c.get("path"), c.get("line"))
            if key in seen:
                continue
            first = re.split(r"(?<=[.?!])\s", re.sub(r"^\W+\s*", "", (c.get("body") or "").split("<details>")[0].strip()), maxsplit=1)[0]
            out.write(json.dumps({"date": now(), "pr": n, "sha": sha, "path": c.get("path"), "line": c.get("line"),
                                  "label": label_of(c.get("body")), "category": c.get("category", "other"),
                                  "text": first[:200]}, ensure_ascii=False) + "\n")


def cmd_trends(a):
    f = home(a.repo) / "findings.jsonl"
    if not f.exists():
        print("No findings logged yet."); return
    cutoff = datetime.now(timezone.utc).timestamp() - a.days * 86400
    groups = {}
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            x = json.loads(line)
        except ValueError:
            continue
        if datetime.fromisoformat(x["date"]).timestamp() < cutoff or x.get("label") not in ("❌", "💡"):
            continue
        groups.setdefault(x["category"], []).append(x)
    rows = []
    for cat, xs in groups.items():
        prs = sorted({x["pr"] for x in xs})
        if len(prs) >= a.min_prs:
            rows.append((len(prs), cat, xs, prs))
    if not rows:
        print(f"Nothing has repeated across {a.min_prs}+ PRs in the last {a.days} days."); return
    print(f"Repeating findings, last {a.days} days (❌ and 💡 only)\n")
    for count, cat, xs, prs in sorted(rows, reverse=True):
        blocking = sum(1 for x in xs if x["label"] == "❌")
        print(f"## {cat}: {count} PRs ({blocking} blocking)  " + " ".join(f"#{p}" for p in prs))
        for x in xs[:5]:
            print(f"  #{x['pr']} {x['path']}:{x['line']}  {x['label']} {x['text']}")
        print()


# ---------------------------------------------------------------- mark
def cmd_mark(a):
    state = load_state(a.repo)
    st = state["prs"].setdefault(str(a.number), {})
    if a.sha:
        st["reviewed_sha"] = a.sha
        diff = home(a.repo) / "work" / f"pr-{a.number}" / "diff.patch"
        if diff.exists():
            st["patch_id"] = patch_id(diff.read_text(encoding="utf-8"))
        log_findings(a.repo, a.number, a.sha)
    seen = set(st.get("seen_comment_ids", []))
    seen.update(int(c) for c in a.comments or [])
    st["seen_comment_ids"] = sorted(seen)
    st["last_pass"] = now()
    save_state(a.repo, state)
    print(json.dumps({"pr": a.number, **st}, indent=2))


# ---------------------------------------------------------------- show
DOORS = {"one-way", "two-way"}
LABELS = {"❌": "blocking", "💡": "suggestion", "❓": "question", "⛏": "nitpick"}  # ⛏ may carry U+FE0F


def label_of(body):
    b = (body or "").lstrip()
    for e in LABELS:
        if b.startswith(e):
            return e
    return None


def label_counts(d):
    cs = (d.get("review") or {}).get("comments", [])
    counts = {e: 0 for e in LABELS}
    for c in cs:
        e = label_of(c.get("body"))
        if e:
            counts[e] += 1
    shown = " ".join(f"{'⛏️' if e == '⛏' else e}{n}" for e, n in counts.items() if n)
    return shown or "no comments"


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


def local_report(d):
    """Jeremy's read: the posted summary, the private take and the Merge Danger call.
    The Merge Danger call and private take are never posted to GitHub."""
    r = d.get("review") or {}
    parts = []
    if (r.get("body") or "").strip():
        parts.append("Summary (posted): " + r["body"].strip())
    if (r.get("private_take") or "").strip():
        parts.append("Private take: " + r["private_take"].strip())
    block = merge_danger_block(d.get("assessment"))
    if block:
        parts.append(block)
    return "\n\n".join(parts)


def has_inline(d):
    """Something in the review that goes on GitHub: inline comments or a summary."""
    r = d.get("review") or {}
    return bool(r.get("comments") or (r.get("body") or "").strip())


LEAK_RE = re.compile(r"rubber\s*stamp|\bdoor\b\s*:|blast\s+radius|one-way door|two-way door", re.I)


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
        lines += ["### Your read on this PR (local only, not posted)", "", local_report(d), ""]
        lines += ["### Inline comments (these go on the PR)" if r.get("comments") else "### No inline comments", ""]
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
        d = json.loads(f.read_text(encoding="utf-8"))
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
        texts.append(("review summary", r.get("body", "")))
        texts += [(f"comment on {c.get('path')}:{c.get('line')}", c.get("body", "")) for c in r.get("comments", [])]
    texts += [(f"reply to @{x.get('to', '?')}", x.get("body", "")) for x in d.get("replies", []) if not x.get("posted")]
    out = [f"{where} has an em dash" for where, t in texts if DASH_RE.search(t or "")]
    out += [f"{where} mentions the Merge Danger call (door, blast radius or rubberstampability stay local)"
            for where, t in texts if LEAK_RE.search(t or "")]
    for c in (r or {}).get("comments", []):
        lab = label_of(c.get("body"))
        if not lab:
            out.append(f"comment on {c.get('path')}:{c.get('line')} doesn't start with ❌ 💡 ❓ or ⛏️")
        elif lab in ("❌", "💡") and c.get("category") not in CATEGORIES:
            out.append(f"comment on {c.get('path')}:{c.get('line')} needs a category ({', '.join(sorted(CATEGORIES))})")
    return out


def reanchor(repo, n, d, head):
    """After a restack, check the review can move to the new head untouched.
    Returns None if it can, otherwise the reason it can't."""
    st = load_state(repo)["prs"].get(str(n), {})
    if not st.get("patch_id") or patch_id(run(["gh", "pr", "diff", str(n), "-R", repo])) != st["patch_id"]:
        return "the PR's changes are different"
    clone = cache(repo) / "clone"
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
    payload = {"commit_id": commit, **({"body": r["body"].strip()} if (r.get("body") or "").strip() else {}),
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


def resolve_threads(repo, d, done):
    """Resolve review threads whose fix was verified (reply has resolve: true and was posted)."""
    m = "mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{id}}}"
    for x in d.get("replies", []):
        if x.get("resolve") and x.get("thread_id") and x.get("posted") and not x.get("resolved"):
            res = subprocess.run(["gh", "api", "graphql", "-f", f"query={m}", "-f", f"t={x['thread_id']}"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode == 0:
                x["resolved"] = True
                done.append(f"resolved thread on {x.get('path', 'the PR')}")


def reconcile_staged(repo, n, d):
    """A staged draft's review lives on GitHub as PENDING until Jeremy submits or discards it."""
    rid = d.get("staged_review_id")
    p = subprocess.run(["gh", "api", f"repos/{repo}/pulls/{n}/reviews/{rid}"], capture_output=True, text=True, encoding="utf-8", errors="replace")
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
            resolve_threads(repo, d, [])
            left = [x for x in d.get("replies", []) if not x.get("posted")]
            d["status"] = "pending" if left else "posted"
            if not left:
                d["posted_at"] = now()
    if d["status"] != "staged":
        draft_file(repo, n).write_text(json.dumps(d, indent=2), encoding="utf-8")
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
    if not has_inline(d) and not thread_replies:
        return f"PR #{n}: no inline comments or thread replies to put on GitHub (your read stays local, see report)" + (f"; {len(convo_replies)} conversation replies stay local, use post" if convo_replies else "")
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
    draft_file(repo, n).write_text(json.dumps(d, indent=2), encoding="utf-8")
    n_c = len((d.get("review") or {}).get("comments", []))
    n_r = sum(1 for x in thread_replies if x.get("staged"))
    done.append(f"waiting on GitHub as your pending review ({n_c} comments, {n_r} thread replies): {rv.get('html_url', '')}")
    if convo_replies:
        done.append(f"{len(convo_replies)} conversation replies stay local (GitHub can't hold those as pending); use post")
    return f"PR #{n}: " + "; ".join(done)


def cmd_stage(a):
    nums = ([json.loads(f.read_text(encoding="utf-8"))["pr"] for f in (home(a.repo) / "drafts").glob("pr-*.json")
             if json.loads(f.read_text(encoding="utf-8")).get("status") == "pending"]
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
    if r and not r.get("posted") and not has_inline(d):
        r["posted"] = True  # the summary is local only; nothing to send
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
        res = subprocess.run(cmd, input=body, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            if what == "review":
                # GitHub may insist on a review summary. We don't post one, so send the
                # inline comments one by one instead (single comments need no summary).
                pl = json.loads(body)
                for c in pl["comments"]:
                    run(["gh", "api", "-X", "POST", f"repos/{repo}/pulls/{n}/comments", "--input", "-"],
                        input_text=json.dumps({"commit_id": pl["commit_id"], **c}))
            elif what == "staged":
                done.append(f"couldn't submit the pending review from here ({res.stderr.strip()[:120]}); "
                            f"open {d.get('staged_url', 'the PR')} and click Finish your review")
                continue
            else:
                sys.exit(f"command failed: {' '.join(cmd)}\n{res.stderr.strip()}")
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
        resolve_threads(repo, d, done)
        review_ok = (not r or r.get("posted")) if d.get("status") != "staged" else d.get("review_submitted")
        replies_ok = all(x.get("posted") for x in d.get("replies", []))
        if review_ok and replies_ok:
            d["status"] = "posted"
            load = load_state(repo); load["prs"].get(str(n), {}).pop("staged_review_id", None); save_state(repo, load)
            d["posted_at"] = now()
        draft_file(repo, n).write_text(json.dumps(d, indent=2), encoding="utf-8")
    return f"PR #{n}: " + ("; ".join(done) if done else "nothing to post")


def cmd_post(a):
    if a.target == "all":
        nums = [json.loads(f.read_text(encoding="utf-8"))["pr"] for f in (home(a.repo) / "drafts").glob("pr-*.json")
                if json.loads(f.read_text(encoding="utf-8")).get("status") in ("pending", "staged")]
    else:
        nums = [int(x) for x in a.target.split(",")]
    for n in sorted(nums):
        print(post_one(a.repo, n, a.dry_run, a.only))


def cmd_digest(a):
    rows = []
    for f in sorted((home(a.repo) / "drafts").glob("pr-*.json"), key=lambda p: int(p.stem[3:])):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("status") not in ("pending", "staged"):
            continue
        asm = d.get("assessment") or {}
        r = d.get("review") or {}
        cs = r.get("comments", [])
        parts = []
        if asm:
            parts.append(f"{asm.get('door', '?')} · {asm.get('blast_radius', '?')} · {asm.get('rubberstamp', '?')}/5")
        if r:
            parts.append(label_counts(d))
        if d.get("replies"):
            parts.append(f"{len(d['replies'])} replies")
        if d.get("status") == "staged":
            parts.append("on GitHub, not submitted")
        rows.append((asm.get("rubberstamp", 9), f"  #{d['pr']:<5} {d.get('title', '')[:48]:<48}  " + "  ·  ".join(parts)))
    # Needs you now: low scores. Merge-ready: reviewed at the current head, 5/5, no ❌, CI green.
    prs_now = gh_json(["pr", "list", "-R", a.repo, "--state", "open", "--limit", "200", "--json",
                       "number,title,headRefOid,headRefName,baseRefName"]) or []
    heads = {p["number"]: p["headRefOid"] for p in prs_now}
    signals = pr_signals(a.repo)
    urgent, ready = [], []
    for f in (home(a.repo) / "drafts").glob("pr-*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        n, asm = d["pr"], d.get("assessment") or {}
        if n not in heads or d.get("status") == "discarded" or not d.get("review"):
            continue
        blockers = sum(1 for c in d["review"].get("comments", []) if label_of(c.get("body")) == "❌")
        current = d.get("head_sha") == heads[n]
        if d.get("status") in ("pending", "staged") and isinstance(asm.get("rubberstamp"), int) and asm["rubberstamp"] <= 2:
            urgent.append(f"  #{n:<5} {d.get('title', '')[:48]:<48}  {asm['rubberstamp']}/5  {asm.get('rubberstamp_reason', '')[:70]}")
        if current and asm.get("rubberstamp") == 5 and not blockers and signals.get(n, {}).get("ci") == "passing":
            ready.append(f"  #{n:<5} {d.get('title', '')[:48]}")
    if urgent:
        print("🚨 Needs you")
        print("\n".join(sorted(urgent)) + "\n")
    if ready:
        print("✅ Merge-ready (5/5, no ❌, CI green, reviewed at current head)")
        print("\n".join(sorted(ready)) + "\n")
    if not rows:
        print("No pending drafts.")
    else:
        print("Pending drafts, riskiest first")
        for _, line in sorted(rows):
            print(line)

    # stacks: bottom to top, with the stack's worst door and lowest score
    sm = stack_map(prs_now)
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

    pairs = overlap_pairs(open_prs_with_files(a.repo))
    if pairs:
        print("\nOverlapping PRs (same files, different stacks)")
        for p in pairs[:5]:
            print(f"  #{p['a']} <-> #{p['b']}  {len(p['shared'])} shared: {', '.join(p['shared'][:3])}")
        if len(pairs) > 5:
            print(f"  ... {len(pairs) - 5} more, see overlaps")


def cmd_report(a):
    """Jeremy's local read: overall take + Merge Danger for each draft, riskiest first."""
    items = []
    for f in (home(a.repo) / "drafts").glob("pr-*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        if a.number and d["pr"] != a.number:
            continue
        if not a.number and d.get("status") not in ("pending", "staged"):
            continue
        if not d.get("review"):
            continue
        items.append(((d.get("assessment") or {}).get("rubberstamp", 9), d))
    if not items:
        print("No reviews to report."); return
    for _, d in sorted(items, key=lambda x: (x[0], x[1]["pr"])):
        cs = d["review"].get("comments", [])
        where = "pending on GitHub" if d.get("status") == "staged" else d.get("status")
        print(f"## #{d['pr']} {d.get('title', '')}  ({label_counts(d)}; {where})\n")
        print(local_report(d))
        if d.get("notes_for_me"):
            print(f"\nNotes: {d['notes_for_me'].strip()}")
        print("\n---\n")


def cmd_config(a):
    state = load_state(a.repo)
    cfg = state.setdefault("config", {})
    if a.key:
        if a.key not in DEFAULTS:
            sys.exit(f"unknown setting {a.key}; known: {', '.join(DEFAULTS)}")
        if a.value is not None:
            v = a.value
            cfg[a.key] = int(v) if isinstance(DEFAULTS[a.key], int) and not isinstance(DEFAULTS[a.key], bool) \
                else v.lower() in ("1", "true", "yes", "on") if isinstance(DEFAULTS[a.key], bool) else v
            save_state(a.repo, state)
    print(json.dumps(config(state), indent=2))


def main():
    # Windows consoles and pipes default to a legacy code page; drafts carry emoji.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan"); s.add_argument("repo"); s.add_argument("--include-drafts", action="store_true"); s.add_argument("--include-bots", action="store_true"); s.add_argument("--include-own", action="store_true", help="also review PRs you opened")
    s.add_argument("--pr", type=int, help="look at this one PR only; reviews it now even if CI is red or it was already reviewed")
    s = sub.add_parser("fetch"); s.add_argument("repo"); s.add_argument("number", type=int); s.add_argument("--no-checkout", action="store_true")
    s = sub.add_parser("mark"); s.add_argument("repo"); s.add_argument("number", type=int); s.add_argument("--sha"); s.add_argument("--comments", nargs="*")
    s = sub.add_parser("show"); s.add_argument("repo"); s.add_argument("number", type=int, nargs="?")
    s = sub.add_parser("post"); s.add_argument("repo"); s.add_argument("target", help="PR number, comma list, or 'all'")
    s.add_argument("--dry-run", action="store_true"); s.add_argument("--only", choices=["review", "replies"])
    s = sub.add_parser("stage"); s.add_argument("repo"); s.add_argument("target", help="PR number, comma list, or 'all'")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("report"); s.add_argument("repo"); s.add_argument("number", type=int, nargs="?")
    s = sub.add_parser("overlaps"); s.add_argument("repo"); s.add_argument("number", type=int, nargs="?")
    s = sub.add_parser("trends"); s.add_argument("repo"); s.add_argument("--days", type=int, default=30); s.add_argument("--min-prs", type=int, default=3)
    s = sub.add_parser("config"); s.add_argument("repo"); s.add_argument("key", nargs="?"); s.add_argument("value", nargs="?")
    s = sub.add_parser("path"); s.add_argument("repo")
    s = sub.add_parser("digest"); s.add_argument("repo")
    a = ap.parse_args()
    a.repo, url_pr = parse_target(a.repo)
    if url_pr:
        if a.cmd == "scan":
            a.pr = url_pr
        elif hasattr(a, "number") and a.number is None:
            a.number = url_pr
    if a.cmd == "path":
        print(f"data:  {home(a.repo)}\ncache: {cache(a.repo)}"); return
    {"scan": cmd_scan, "fetch": cmd_fetch, "mark": cmd_mark, "show": cmd_show, "post": cmd_post, "digest": cmd_digest, "stage": cmd_stage, "report": cmd_report,
     "overlaps": cmd_overlaps, "trends": cmd_trends, "config": cmd_config}[a.cmd](a)


if __name__ == "__main__":
    main()
