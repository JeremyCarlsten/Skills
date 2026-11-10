---
name: review-prs
description: Sweep a GitHub repo's open pull requests and draft big-picture reviews (spec contradictions, architecture, security, one-way doors, things that make future agent work harder, tautological tests, serious performance) with a Merge Danger call (door, blast radius, rubberstampability 1-5), stacked-PR aware, plus replies to comments waiting on the user in their plain-spoken voice. Drafts wait as the user's private pending review on GitHub; nothing is submitted until they approve. Built to run under /loop alongside a Ralph loop (waits for CI and settled branches, flags merge-ready and risky PRs, checks earlier comments were fixed, spots overlapping PRs and repeating findings). Can also review a single PR by number or URL. Use for /review-prs, "review the open PRs on X", "check my PRs", "answer the comments on my PRs", or posting approved PR drafts.
---

# Review PRs

Go through the open PRs on one GitHub repo, review the ones that changed, and draft answers to comments that are waiting on the user. Everything is drafted locally; the user approves before anything goes on GitHub, because it all goes out under their name.

Invocation:
- `/review-prs owner/repo` — one pass.
- `/review-prs owner/repo 567`, `/review-prs owner/repo#567` or a PR URL — review that one PR now. Pass `--pr 567` to `scan` (or the URL as REPO). A PR asked for by number skips the CI and settle checks and is reviewed again even if nothing changed.
- `/review-prs owner/repo --mine` — also review PRs the user opened ("include my PRs" means the same). Pass `--include-own` to `scan`. Under `/loop`, the flag rides along in the loop prompt, so `/loop /review-prs owner/repo --mine` keeps it on.
- `/loop /review-prs owner/repo` — keep watching (see "Loop mode").
- `/review-prs owner/repo --local` — keep drafts on this machine only; don't stage them on GitHub.
- `/review-prs owner/repo stage 42` · `stage all` — put drafts on GitHub as the user's pending review (see "Staging on GitHub").
- `/review-prs owner/repo post 42` · `post 42,57` · `post all` — post approved drafts, or submit staged ones.
- `/review-prs owner/repo approve 42` · `approve 42,57` (or "approve and post 42") — same as post, but submits the review as an approval. Only by PR number, never `all`.
- `/review-prs owner/repo show` — print pending drafts.
- `/review-prs owner/repo trends` — findings that keep repeating across PRs, and what to change in the factory to stop them.
- `/review-prs owner/repo overlaps` — open PRs that touch the same files.
- `/review-prs owner/repo config settle_minutes 30` · `config review_red true` — change how patient a pass is.

If no repo is given, use the current directory's GitHub remote (`gh repo view --json nameWithOwner -q .nameWithOwner`). If that fails, ask.

## The helper script

`scripts/prs.py` (in this skill's folder) does the GitHub and git plumbing so every pass behaves the same. Call it with the absolute path to this skill's directory: `python3 <skill-dir>/scripts/prs.py ...` (`python` or `py` on Windows if `python3` isn't found).

Where things live (`prs.py path REPO` prints both):
- **Data** (state, drafts, findings, fetched PR files, factory notes) goes in a `.skills-local` folder (shared with any other skill that keeps local data, one subfolder each). Run from inside a clone of the repo, that's `<repo root>/.skills-local/review-prs/`; from anywhere else, `~/.skills-local/review-prs/<owner>__<repo>/`. Run passes from the same place each time (ideally inside the clone) so they share state. The first run adds `.skills-local/` to the user's global gitignore (the file `core.excludesFile` points to, or git's default `~/.config/git/ignore`) and says so; nothing in `.skills-local` is ever committed. Data from older locations (`~/.review-prs/`, `.jeremy/`) is moved over once, automatically.
- **Code checkouts** (a partial clone and per-PR worktrees) go in `~/.skills-local/cache/<owner>__<repo>/`, outside every project, so test runners and linters in the user's repo never pick them up. It never touches the user's own checkout.

| Command | What it does |
|---|---|
| `scan REPO [--pr N]` | Lists open PRs and what each needs: `review`, `re-review`, `reply`, or a `skip_reason`. Holds reviews while CI is running or red, or the branch is still getting commits (see "Keeping up with a Ralph loop"). Marks stacked PRs and spots rebase-only changes. Skips drafts and bot PRs. Skips reviewing the user's own PRs unless `--include-own` is passed, but always surfaces comments waiting on them there. |
| `fetch REPO N` | Writes `pr.json`, `diff.patch`, `comments.json`, `waiting.json` (comments needing a reply), `issues.json` (linked issues), `my_open_threads.json` (the user's unresolved review threads, for checking fixes), `overlaps.json` (other open PRs touching the same files) and an `incremental.patch` if this is a re-review. Checks the PR head out in a worktree. Prints the paths, including `write_draft_to`. |
| `mark REPO N --sha SHA --comments ID ...` | Records that the head SHA was reviewed and those comment IDs were handled, so the next pass skips them. Also logs the review's labelled comments for `trends`. |
| `show REPO [N]` | Prints pending drafts in readable form. |
| `report REPO [N]` | The user's local read on each reviewed PR: summary, private take and the Merge Danger call. The take and the call are never posted. |
| `digest REPO` | 🚨 PRs scoring 1-2, ✅ merge-ready PRs, then one line per pending draft (riskiest first), one line per stack, and overlapping PRs. |
| `overlaps REPO [N]` | Open PRs that change the same files, not counting PRs in the same stack. |
| `trends REPO [--days 30] [--min-prs 3]` | ❌ and 💡 findings grouped by category, where the same category shows up across several PRs. |
| `config REPO [KEY [VALUE]]` | Shows or sets `settle_minutes` (default 15) and `review_red` (default false). |
| `stage REPO N\|all [--dry-run]` | Creates the review on GitHub as the user's pending (unsubmitted) review, with thread replies attached. Only the user can see it until they submit it. |
| `post REPO N\|all [--dry-run] [--only review\|replies]` | Posts approved drafts, or submits a staged review. Refuses to post a review if the PR moved since it was drafted. |
| `approve REPO N[,N] [--dry-run]` | Same as `post`, but the review goes in as an approval (with the summary as its comment, if there is one). |

Run `gh auth status` once at the start. If it fails, tell the user to run `gh auth login` and stop.

## One pass

1. `scan REPO`. Work through PRs that have actions, oldest first. Don't announce skipped PRs one by one; they go in the digest.
2. Work bottom-up through stacks (see "Stacked PRs"). For each PR: `fetch REPO N`, then do the review (below) and/or the replies, write the draft, then `mark`, then `stage` it on GitHub unless the user passed `--local`.
3. End with the digest (below). If a PR scored 1 or 2 this pass, also send an alert (see "Alerts").

Keep each PR self-contained: read what `fetch` wrote, work in that PR's worktree, finish its draft, then move on. If there are more than ~4 PRs to review and you can spawn subagents, hand each PR to its own subagent with this skill's path, the repo, the PR number and the fetch output, and have it write the draft file and return a two-line summary. That keeps one big PR from crowding out the rest.

## Reviewing a PR

This review is about the big picture. The code is written by agents in a software factory (Matt Pocock's skills flow: spec, tickets, implement, review), so the line-level stuff mostly takes care of itself. What a person reviewing the PR has to catch is the stuff agents are bad at seeing: decisions that are hard to undo, drift from the spec and the architecture, security holes, and changes that will make the next agent's job harder.

The PR description and linked issues (`issues.json`) are the spec. The worktree is the codebase as it will be after merge.

**Before judging, learn the code around the change.** Read the repo's standards and agent docs (`CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, `docs/adr`, `docs/agents`) and `GLOSSARY.md` if there is one; use its domain terms in comments. Map the layers and which way imports flow. For each changed file, open 2 or 3 existing siblings of the same kind. Grep for existing helpers that do what new code does.

### Big-picture lenses, most important first

1. **Spec contradictions.** Does it do something the spec rules out, or skip something it requires? Quote the spec line. Scope creep counts when it adds surface area nobody asked for.
2. **Architecture.** Layer skipping, dependency direction reversed, cross-module reach into internals, business logic in the wrong layer, a second way of doing something the codebase already does one way (a new HTTP client, state library, error style, data-access pattern). Point at the existing code or doc that sets the boundary.
3. **Security.** Unvalidated input reaching queries, shell, file paths or HTML; authz checks missing or done client-side; secrets in code, logs or the client bundle; PII in logs; overly broad tokens, CORS or permissions; unsafe deserialization; new dependencies with known problems.
4. **Direction and doors.** Does this commit us to a path? A one-way door is expensive to reverse once merged and used: database schema and migrations, persisted data formats, public or cross-team API contracts, auth/identity model, new infrastructure or vendors, event/message schemas, anything other services or stored data will depend on. A two-way door can be changed later by editing code. Call out one-way doors even when the choice looks right, because they deserve a deliberate yes.
5. **Future agentic work.** Agents learn the codebase by copying what's there and trust tests as their feedback loop. Flag what makes that worse: a new pattern that conflicts with the existing one (the next agent will copy whichever it sees first), hidden coupling or implicit behavior (magic strings, global state, side effects in getters), giant files or functions an agent can't hold in context, missing types at boundaries, conventions that only live in someone's head, stale `CLAUDE.md`/docs after this change, flaky or slow tests that break the loop.
6. **Tautological tests.** Tests that can't fail: they assert on the mock they just configured, re-implement the code under test to compute the expected value, snapshot everything with no meaningful assertion, `expect(true)`, catch-and-ignore, or cover only the happy path of risky logic. In an agent-written codebase these are worse than no tests, because they give the loop a green light it didn't earn. Say what the test should assert instead.
7. **Serious performance.** Only the kind that bites: N+1 queries, unbounded queries or loops over user data, missing pagination, sync or blocking I/O in a hot path, O(n^2) over collections that grow, loading whole tables or files into memory, render storms. Say what input size makes it hurt.
8. **Correctness bugs that matter.** Wrong behavior, broken edge cases, error paths that swallow failures. The ones that would cause an incident or bad data, not style.

### Comment labels

Every inline comment starts with exactly one emoji so people and agents know at a glance how much it matters:

| Emoji | Meaning | Use it for |
|---|---|---|
| ❌ | Blocking | Must be fixed before merge: the big-picture findings above that cause real harm. |
| 💡 | Suggestion | Worth doing, not blocking: a better approach, a reuse opportunity, a simpler shape. |
| ❓ | Question | You need an answer before you can judge it, or you might be missing something. |
| ⛏️ | Nitpick | Small polish: naming, dead code, minor KISS/SRP cleanups. `references/smells.md` has the heuristics. |

Then a space, then the comment: `❌ This queries invoices once per customer...`. Every ❌ and 💡 comment also gets a `category` field in the draft (not in the text): `spec`, `architecture`, `security`, `one-way-door`, `agent-friction`, `tautological-test`, `performance`, `correctness`, `simplify`, `reuse`, `naming` or `other`. That's what `trends` groups by. Use the emoji characters, not GitHub shortcodes, so they read the same in the terminal. Cap nitpicks at 5, and if there's nothing big, don't pad with ⛏️ to look busy. `stage` and `post` refuse a comment that doesn't start with one of the four.

### Ground rules

- Review the PR's change, not the whole repo.
- The repo's own consistent conventions beat textbook advice.
- Skip anything a linter, formatter or type checker in the repo already enforces.
- Every finding points at a line in the diff, plus the existing code, doc or spec it's measured against. No evidence, no comment.
- Don't repeat what another reviewer (a person or a bot like CodeRabbit or Copilot) already said in `comments.json`. If their comment raises one of your findings, add it to the draft's `agree` list instead: that puts a 👍 on their comment, which flags it as worth looking into. Only agree with comments you checked and actually agree with, and only inline review comments or conversation comments (GitHub can't react to a review summary; mention those in `private_take`).
- If the PR is fine, say so in one line.

### Comments agents can act on

Agents will pick up these comments and make the fixes, but people read them too. So every ❌ comment (and any 💡 an agent could act on) has two parts: the comment itself in Jeremy's voice (one to three sentences), then a collapsed block with the precise fix:

```markdown
❌ This queries invoices once per customer, so the dashboard does 500 queries for a big account. I'd load them in one query keyed by customer id.

<details><summary>Fix for an agent</summary>

- In `src/services/dashboard.ts:42`, replace the per-customer `findByCustomer` loop with one `findByCustomerIds(ids)` call.
- Add `findByCustomerIds` to `src/repositories/invoiceRepository.ts`, matching the style of `findByIds` there.
- Verify: the test in `dashboard.test.ts` should assert the repository is called once for 3 customers.
</details>
```

The fix block can be terse and technical; it isn't held to the voice guide's sentence count, but it still uses no em dashes. ⛏️ and ❓ comments don't need a fix block.

**The user's own PRs** (`own_pr` is true, only with `--mine`): same lenses and Merge Danger call. GitHub lets you post a comment review on your own PR, so it posts the same way. Write the comments as notes to the factory and to the user rather than to a teammate: what to change and why, still in the voice guide.

**Re-reviews** (`incremental.patch` exists): review what changed since the last pass and update the assessment. If the branch was force-pushed, review the whole diff again but don't re-raise points already made.

**Checking fixes.** `my_open_threads.json` lists the user's unresolved review threads on the PR. On every re-review, go through the ❌ and 💡 ones and check the current code (the worktree and `incremental.patch`), not the thread's `outdated` flag, which only means the lines moved:
- **Fixed:** draft a reply like "Fixed in a1b2c3d." (the short SHA of the commit that fixed it) with `"resolve": true` and the thread's `thread_id`. The thread gets resolved when the reply is posted or the staged review is submitted. Only do this when you saw the fix yourself.
- **Partly fixed:** a one-sentence reply saying what's still missing, no `resolve`.
- **Not touched:** no reply. List it in `private_take` ("❌ on src/a.ts:4 still open") so the user knows the loop skipped it, and keep it in mind for the score.

**Other PRs in flight.** `overlaps.json` lists open PRs, outside this PR's stack, that change the same files. With a lot of agents working at once, check them for: the same thing being built twice, two competing patterns for one job (the next agent will copy whichever it sees), and changes that will conflict when both merge. If it's real, raise it as a ❓ or ❌ comment naming the other PR ("#571 adds its own `clampRadius` too. Should one of these go?"); if it's only a likely merge conflict, put it in `private_take`.

## Stacked PRs

A PR is part of a stack when its base branch is another open PR's head branch. This works the same with Graphite, ghstack, git-town, spr or plain branches, because it only looks at base and head branches. `scan` adds a `stack` entry (`parent`, `children`, `below`, `position`) and `fetch` writes `stack.json`, listing every layer below and above with its title, body and diff.

What changes when reviewing a layer:

- **Review the layer, read the stack.** `diff.patch` is already just this layer (against its parent's branch), so review that. But read the layers above before calling something missing: if the test, the endpoint or the cleanup lands in #43, it's not a finding on #42. At most, note it in `notes_for_me` if the order looks risky.
- **Each layer has to stand on its own** if the stack merges one layer at a time. Flag a layer that leaves main broken or half-wired when merged alone: it calls code that only exists higher up, a migration lands before the code that writes the new column safely, or a feature flag is missing so a half-built feature ships.
- **Spec across the stack.** Layers often share one parent issue. Check this layer against its slice of the spec (its PR body), and check that the stack as a whole doesn't contradict the issue.
- **Merge Danger is per layer**, about what this layer's own changes do. If a layer depends on a one-way door further down, say so in `door_note` ("Depends on the migration in #41") rather than marking this layer one-way too. The digest adds a stack line showing where the one-way doors are and the lowest score in the stack.
- **Don't repeat findings across layers.** If the problem lives in a lower layer, comment there, not on every layer that inherits it.
- **Review bottom-up** within a pass, so findings on lower layers are known when you look at the ones above.

**Restacks.** When a lower layer changes, every layer above gets rebased and gets a new head SHA even though its own changes didn't change. `scan` catches this by comparing the layer's diff fingerprint (`git patch-id`) and marks it `rebased_only` instead of asking for a re-review, so restacks don't trigger a wave of duplicate reviews. If a draft is pending when that happens, `stage` and `post` move the review onto the new head automatically, as long as the commented lines are identical; otherwise it asks for a re-review.

## Merge Danger

Every review comes with a Merge Danger call, in the same shape as the `/pr` skill's PR body (Matt Pocock's skills library, credited to Dex Horthy's show-me skill), so author and reviewer speak the same language. Put it in the draft's `assessment`. It's for the user, not the PR: it's shown locally (`report`, `show`, the digest) and never posted to GitHub. The PR body already has the author's door and blast radius; if yours differs, say so plainly in the posted summary or a ❓ comment ("This adds a migration, so I don't think it's reversible by revert") without using the Merge Danger labels or a score.

**Check the author's claim first.** PRs from the factory usually carry their own `## Merge Danger` section and an `## Evidence` section in the body (`pr.json`). Read them. If your call differs from theirs (they said two-way, you see a migration), say so in the review body, because a wrong self-assessment is exactly what lets a risky PR get rubber-stamped. If the Evidence is a test that's tautological, say that too.

**Door.** `one-way` or `two-way`. You can walk back through a two-way door by reverting; you can't walk back through a one-way door. One-way: destructive or hard-to-reverse actions such as schema changes and migrations, deleting or rewriting stored data, persisted data formats, public or cross-team API contracts, auth/identity changes, new infrastructure or vendor commitments, events other systems consume, anything released to users that can't be pulled back. A PR that's cheap to roll back is lower risk. Add `door_note` (one sentence) when the call isn't obvious or the PR body got it wrong.

**Blast Radius.** One word for how far the effects reach if this goes wrong. Consider all of it: breakages for consumers of an API or package, layout shift, mobile responsiveness, data correctness, performance under load, other teams. Common words: `contained` (one function or screen), `feature`, `app-wide`, `consumers` (other services, packages or teams), `data` (stored data can end up wrong), `security`. Use another single word if it fits better. Add `blast_note` with the likely ramifications when it's bigger than `contained`.

**Rubberstampability.** 1 to 5: how safe is it for a human to approve this after a skim? It's the answer to "can I trust the factory on this one?"

| Score | Meaning | Typical shape |
|---|---|---|
| 5 | Stamp it | Two-way door, contained, matches the spec, real tests (not tautological) cover the change, no ❌ comments. |
| 4 | Skim it | Two-way, small radius, only 💡, ❓ or ⛏️ comments, evidence is real. |
| 3 | Read it | Something needs a person's eyes: a ❌ comment, weak or tautological tests on the logic that matters, a radius beyond one feature, or any one-way door. |
| 2 | Review closely | A one-way door with open questions, or an architecture, spec or serious performance problem. |
| 1 | Don't merge as is | A security hole, a data-loss or bad-data risk, a contradiction of the spec, or a one-way door that looks wrong. |

Caps: a one-way door is never above 3, because it deserves a deliberate yes (the script enforces this). A ❌ for security or a spec contradiction is never above 2. `rubberstamp_reason` is one plain sentence naming what drove the score.

## Replying to comments

`waiting.json` lists comments that are waiting on the user: threads they're in, comments on their own PRs, @-mentions, and conversation comments after their last one. For each:

- Read the whole thread and the code it's on. Answer the actual question.
- If the commenter is right, say so and say what will change. If you disagree, say why once and leave room.
- If it needs something only the user can decide — scope, priorities, commitments, anything about people — don't decide it. Leave it out of `replies` and put it in `notes_for_me`.
- Only say something was changed, fixed or tested if you verified it (the commit is in the PR, the incremental diff shows it, or you ran the test). Otherwise phrase it as what will happen ("I'll switch it to `formatCurrency`") and let the user edit before posting.

## Voice

Read `references/voice.md` before writing anything that will be posted, and follow it. It's Jeremy's own guide, and it wins over any phrasing habit of yours. Reviews and replies go out as him, so they must sound like him: lead with the answer, one to three sentences, plain words, the practical reason, no em dashes. The finding labels above (Correctness, SRP, etc.) are for your thinking. They don't belong in the comment text. Run the checklist at the end of `voice.md` on every draft before writing the file.

## The draft file

Write one JSON file per PR at the `write_draft_to` path. Replace any older draft for that PR.

```json
{
  "repo": "owner/repo",
  "pr": 42,
  "title": "Add invoices endpoints",
  "head_sha": "<head SHA from fetch>",
  "created_at": "<ISO time>",
  "status": "pending",
  "review": {
    "body": "Posted as the review summary: one to three sentences in the user's voice on the overall take and the one or two things that matter most. No door, blast radius or score; the PR body already has its own Merge Danger section.",
    "private_take": "Local only, never posted: blunt notes for the user on how well the PR is written and whether the PR's own Merge Danger call holds up.",
    "comments": [
      { "path": "src/routes/invoices.js", "line": 9, "category": "spec", "body": "❌ ..." },
      { "path": "src/routes/invoices.js", "start_line": 15, "line": 26, "body": "⛏️ ..." }
    ]
  },
  "assessment": {
    "door": "one-way",
    "door_note": "Adds a NOT NULL column with a backfill, so rolling back means another migration.",
    "blast_radius": "data",
    "blast_note": "Existing invoices get a default due date that may be wrong.",
    "rubberstamp": 2,
    "rubberstamp_reason": "The migration is a one-way door and the backfill default isn't in the spec."
  },
  "replies": [
    { "kind": "review_comment", "reply_to": 123456, "to": "alice", "context": "what they said, short", "body": "..." },
    { "kind": "issue_comment", "to": "bob", "context": "...", "body": "..." },
    { "kind": "review_comment", "reply_to": 654321, "thread_id": "PRRT_abc", "resolve": true, "to": "alice", "context": "my earlier ❌ on the N+1 query", "body": "Fixed in a1b2c3d." }
  ],
  "agree": [
    { "kind": "review_comment", "id": 777001, "author": "coderabbitai", "path": "src/dashboard.ts", "line": 42,
      "why": "Same N+1 query I found.", "category": "performance", "severity": "❌" }
  ],
  "notes_for_me": "Things for the user only: decisions to make, low-confidence findings, anything you left out and why."
}
```

- `review` and `assessment` are `null` when there's no review this pass (own PR, or only replies needed). Every review needs an assessment; `stage` and `post` refuse one without it.
- Inline comment `line` numbers are lines in the PR head version of the file (RIGHT side) and must fall inside a diff hunk, or GitHub rejects the review. Check against `diff.patch`. If a point is about code outside the diff, put it in the review body instead.
- For `review_comment` replies, `reply_to` is the thread's root comment ID (`reply_to` in `waiting.json`).
- `agree` entries use the other comment's numeric `id` from `comments.json` (`review_comments` → `review_comment`, `issue_comments` → `issue_comment`). `severity` is the label you'd have given it (❌ or 💡) and `category` is one of the comment categories, so agreed findings still count in `trends`.
- What goes to GitHub: the short text summary (`review.body`), the inline comments and the thread replies. What stays local: `private_take` and the whole Merge Danger call. The PR body already carries its own door and blast radius, and rubberstampability is for the user only. `stage` and `post` refuse a summary or comment that mentions door, blast radius or rubberstampability.
- Reviews post as a plain comment review. Approve only when the user names the PR (`approve 42`, "approve and post 42"); never request changes unless they ask.

Then: `mark REPO N --sha <head_sha> --comments <every id from waiting.json you handled or deliberately skipped>`.

## The digest

End every pass with your local read on each PR reviewed this pass: run `report REPO N` for each and print it (the posted summary, your private take on how well the PR is written, Door, Blast Radius, Rubberstampability, notes). The private take and Merge Danger call never go on GitHub. Then the digest: run `digest REPO` for the pending-drafts table (riskiest first) and put your one-line notes on what's new this pass above it:

```
review-prs · owner/repo · 14:32
New this pass
  #42  drafted review, PR body says two-way but it adds a migration
  #57  drafted 2 replies on your PR
  #38  re-review: 2 of 3 ❌ fixed and resolved, 1 still open
🚨 Needs you
  #42    Add invoices endpoints        2/5  The migration is a one-way door and the backfill isn't in the spec.
✅ Merge-ready (5/5, no ❌, CI green, reviewed at current head)
  #61    Field card spacing
💬 Summary only, no inline comments
  #61    Field card spacing  (5/5)
         "Looks good to me. Spacing matches the other cards."
         -> approve 61  ·  post 61 (comment only)
Pending drafts, riskiest first
  #42    Add invoices endpoints        one-way · data · 2/5  ·  ❌2 💡1 ⛏️1 👍1  ·  1 replies
  #61    Field card spacing            two-way · contained · 5/5  ·  no comments
  #57    Fix login redirect                                        ·  2 replies
Stack  #41 -> #42 -> #43  ·  one-way at #41  ·  lowest 2/5
Overlapping PRs (same files, different stacks)
  #44 <-> #49  2 shared: src/pocket/radius.ts, src/pocket/types.ts
Held: 2 waiting for CI, 1 CI failing, 3 still settling
Repeating: tautological-test in 4 PRs this month (`trends` for details)
Skipped: 3 drafts, 1 bot, 4 with nothing new, 2 rebased only
Next: read the staged ones on GitHub (Files changed tab), `show` for the rest, `post 42` or `post all` to send.
```

Build the Held line from the `skip_reason`s in `scan`. For the Repeating line, run `trends REPO --days 30` each pass (it's a local file read) and mention any category at 3+ PRs. If nothing needed doing and nothing is urgent or merge-ready, one line: `review-prs · owner/repo · nothing new.`

## Keeping up with a Ralph loop

A Ralph loop opens and updates PRs faster than anyone can read them, so a pass is choosy about what it spends time on:

- **Wait for CI and for the branch to settle.** `scan` holds a review (not replies) while CI is pending, while CI is red (the loop will usually fix it, so reviewing now is wasted work), and until the branch has had no new commits for `settle_minutes` (default 15). Held PRs are picked up on a later pass. Change with `config REPO settle_minutes 30` or `config REPO review_red true`. A PR the user asks for by number is never held.
- **Merge-ready list.** The digest's ✅ section lists PRs reviewed at their current head with a 5/5, no ❌ and green CI: the ones the user can approve and move on from.
- **Alerts.** See below.
- **Fix the factory, not just the PR.** See "Trends".

## Alerts

When a pass drafts a review scoring 1 or 2, the user should hear about it without waiting for their next look. If a push-notification tool is available (for example `PushNotification`), send one short line per PR: `#42 Add invoices endpoints: 2/5, one-way migration not in the spec`. Otherwise the 🚨 section at the top of the digest is the alert. Don't alert on the same PR and head twice.

## Trends

Every reviewed comment is logged (`findings.jsonl` in the data folder). `trends` groups ❌ and 💡 findings by category and shows the ones that keep coming back across PRs. When the same problem shows up in three or more PRs, the fix belongs in the factory: a rule in `CLAUDE.md`, a line in the relevant skill (`/implement`, `/to-tickets`), a lint rule or a test helper.

When the user asks for trends (or a "factory report"), run `trends REPO`, read the examples, look at the repo's current `CLAUDE.md` and agent docs, and write a short report to `factory-notes.md` in the data folder: for each repeating category, what keeps going wrong (with PR links), and the exact text of the rule or change you'd add and where. Show it to the user. Don't edit the repo's docs or skills yourself; that's the user's call.

## Staging on GitHub

By default, after a pass writes a draft, run `stage REPO N` for it. That puts the review on the PR as the user's **pending review**: the short summary, the inline comments and the replies to review threads show up on the PR's Files changed tab, marked Pending, and nobody but the user can see them. The user reads them in context, edits or deletes anything right there, and submits with "Finish your review" (or says `post 42`, which submits it as a plain comment review). The draft's `staged_url` links to it.

Things to know:
- **👍 reactions can't be pending.** They go on when the user runs `post`/`approve`, or on the next pass after they submit the staged review on GitHub.
- **Summary-only reviews aren't staged.** When a review has a summary but no inline comments, a pending review would just hide one sentence behind "Finish your review". It stays local and shows up in the digest's 💬 section with the summary text, so the user can say `approve 42` or `post 42` in one step.
- **Conversation replies can't be staged.** GitHub can only hold inline comments and review-thread replies as pending. Replies to top-level conversation comments stay local; `show` lists them and `post` sends them.
- **One pending review per person per PR.** If the user already has their own pending review going on a PR, `stage` refuses rather than touching it, and says so. Leave that draft local and mention it in the digest.
- **Re-staging replaces.** After a re-review, `stage` deletes the earlier pending review it made and creates the new one. It never deletes a pending review it didn't make.
- **It notices what the user did.** On the next `scan`, a staged review that was submitted is marked posted (leftover conversation replies stay pending), and one the user discarded is marked discarded and not staged again.
- Skip staging if the user passed `--local` or said they want drafts kept local.

## Posting

Only when the user asks (`post 42`, `post all`, `approve 42`, "send the review on 42", "approve and post 42"). First run `post ... --dry-run` and check it's what you expect, then run it for real and report what went out. For a staged draft, `post` submits the pending review as it now stands on GitHub, including the user's edits there. For a local draft, it posts the draft file as written, including any edits the user made to it. If a review is refused because the PR moved, offer to re-review. GitHub doesn't allow approving your own PR; if `approve` is refused for that, say so and offer `post` instead. Never submit or post during a loop pass on your own; staging is as far as a pass goes.

## Loop mode

Under `/loop`, each firing is one pass. The state file makes passes cheap: PRs with no new commits and no new comments are skipped without fetching. Each pass:
- Never submits or posts. It drafts, then stages new drafts as pending reviews on GitHub (unless `--local`), which only the user can see.
- Doesn't re-draft a PR whose pending draft already covers the current head, unless new comments came in.
- Keeps the digest short, and leaves out anything that was already in the previous digest and hasn't changed (the 🚨 and ✅ lists always show).
- Holds reviews for PRs that are mid-CI, red or still getting commits.

If the loop is self-paced (`/loop` with no interval), wait about 20–30 minutes between passes; PRs don't move faster than that, and a quiet pass costs little. If the user gave an interval (`/loop 30m ...`), that's the pacing.
