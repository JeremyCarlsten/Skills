---
name: review-prs
description: Sweep a GitHub repo's open pull requests and draft big-picture reviews (spec contradictions, architecture, security, one-way doors, things that make future agent work harder, tautological tests, serious performance) with a Merge Danger call (door, blast radius, rubberstampability 1-5), stacked-PR aware, plus replies to comments waiting on the user in their plain-spoken voice. Drafts wait as the user's private pending review on GitHub; nothing is submitted until they approve. Built to run under /loop. Use for /review-prs, "review the open PRs on X", "check my PRs", "answer the comments on my PRs", or posting approved PR drafts.
---

# Review PRs

Go through the open PRs on one GitHub repo, review the ones that changed, and draft answers to comments that are waiting on the user. Everything is drafted locally; the user approves before anything goes on GitHub, because it all goes out under their name.

Invocation:
- `/review-prs owner/repo` — one pass.
- `/review-prs owner/repo --mine` — also review PRs the user opened ("include my PRs" means the same). Pass `--include-own` to `scan`. Under `/loop`, the flag rides along in the loop prompt, so `/loop /review-prs owner/repo --mine` keeps it on.
- `/loop /review-prs owner/repo` — keep watching (see "Loop mode").
- `/review-prs owner/repo --local` — keep drafts on this machine only; don't stage them on GitHub.
- `/review-prs owner/repo stage 42` · `stage all` — put drafts on GitHub as the user's pending review (see "Staging on GitHub").
- `/review-prs owner/repo post 42` · `post 42,57` · `post all` — post approved drafts, or submit staged ones.
- `/review-prs owner/repo show` — print pending drafts.

If no repo is given, use the current directory's GitHub remote (`gh repo view --json nameWithOwner -q .nameWithOwner`). If that fails, ask.

## The helper script

`scripts/prs.py` (in this skill's folder) does the GitHub and git plumbing so every pass behaves the same. Call it with the absolute path to this skill's directory: `python3 <skill-dir>/scripts/prs.py ...` (`python` or `py` on Windows if `python3` isn't found). It keeps all state in `~/.review-prs/<owner>__<repo>/` — its own partial clone and worktrees — so it never touches the user's checkout and works from any terminal.

| Command | What it does |
|---|---|
| `scan REPO` | Lists open PRs and what each needs: `review`, `re-review`, `reply`, or a `skip_reason`. Marks stacked PRs and spots rebase-only changes. Skips drafts and bot PRs. Skips reviewing the user's own PRs unless `--include-own` is passed, but always surfaces comments waiting on them there. |
| `fetch REPO N` | Writes `pr.json`, `diff.patch`, `comments.json`, `waiting.json` (comments needing a reply), `issues.json` (linked issues), and an `incremental.patch` if this is a re-review. Checks the PR head out in a worktree. Prints the paths, including `write_draft_to`. |
| `mark REPO N --sha SHA --comments ID ...` | Records that the head SHA was reviewed and those comment IDs were handled, so the next pass skips them. |
| `show REPO [N]` | Prints pending drafts in readable form. |
| `digest REPO` | One line per pending draft with door, blast radius and score, riskiest first, then one line per stack. |
| `stage REPO N\|all [--dry-run]` | Creates the review on GitHub as the user's pending (unsubmitted) review, with thread replies attached. Only the user can see it until they submit it. |
| `post REPO N\|all [--dry-run] [--only review\|replies]` | Posts approved drafts, or submits a staged review. Refuses to post a review if the PR moved since it was drafted. |

Run `gh auth status` once at the start. If it fails, tell the user to run `gh auth login` and stop.

## One pass

1. `scan REPO`. Work through PRs that have actions, oldest first. Don't announce skipped PRs one by one; they go in the digest.
2. Work bottom-up through stacks (see "Stacked PRs"). For each PR: `fetch REPO N`, then do the review (below) and/or the replies, write the draft, then `mark`, then `stage` it on GitHub unless the user passed `--local`.
3. End with the digest (below).

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

### Nits (optional, capped)

After the big items, you may add up to 5 small comments: simplification (KISS), a function doing several jobs (SRP), an existing helper that should be reused, naming, dead code. `references/smells.md` has the heuristics. Start each with `nit:` so nobody mistakes it for a blocker. If there's nothing big, don't pad with nits to look busy.

### Ground rules

- Review the PR's change, not the whole repo.
- The repo's own consistent conventions beat textbook advice.
- Skip anything a linter, formatter or type checker in the repo already enforces.
- Every finding points at a line in the diff, plus the existing code, doc or spec it's measured against. No evidence, no comment.
- Don't repeat what another reviewer already said in `comments.json`.
- If the PR is fine, say so in one line.

### Comments agents can act on

Agents will pick up these comments and make the fixes, but people read them too. So every must-fix inline comment has two parts: the comment itself in Jeremy's voice (one to three sentences), then a collapsed block with the precise fix:

```markdown
This queries invoices once per customer, so the dashboard does 500 queries for a big account. I'd load them in one query keyed by customer id.

<details><summary>Fix for an agent</summary>

- In `src/services/dashboard.ts:42`, replace the per-customer `findByCustomer` loop with one `findByCustomerIds(ids)` call.
- Add `findByCustomerIds` to `src/repositories/invoiceRepository.ts`, matching the style of `findByIds` there.
- Verify: the test in `dashboard.test.ts` should assert the repository is called once for 3 customers.
</details>
```

The fix block can be terse and technical; it isn't held to the voice guide's sentence count, but it still uses no em dashes. Nits don't need a fix block.

**The user's own PRs** (`own_pr` is true, only with `--mine`): same lenses and Merge Danger call. GitHub lets you post a comment review on your own PR, so it posts the same way. Write the comments as notes to the factory and to the user rather than to a teammate: what to change and why, still in the voice guide.

**Re-reviews** (`incremental.patch` exists): review what changed since the last pass, check whether earlier findings were addressed, and update the assessment. If the branch was force-pushed, review the whole diff again but don't re-raise points already made.

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

Every review ends with a Merge Danger call, in the same shape as the `/pr` skill's PR body (Matt Pocock's skills library, credited to Dex Horthy's show-me skill), so author and reviewer speak the same language. Put it in the draft's `assessment`; the script appends it to the review body when posting.

**Check the author's claim first.** PRs from the factory usually carry their own `## Merge Danger` section and an `## Evidence` section in the body (`pr.json`). Read them. If your call differs from theirs (they said two-way, you see a migration), say so in the review body, because a wrong self-assessment is exactly what lets a risky PR get rubber-stamped. If the Evidence is a test that's tautological, say that too.

**Door.** `one-way` or `two-way`. You can walk back through a two-way door by reverting; you can't walk back through a one-way door. One-way: destructive or hard-to-reverse actions such as schema changes and migrations, deleting or rewriting stored data, persisted data formats, public or cross-team API contracts, auth/identity changes, new infrastructure or vendor commitments, events other systems consume, anything released to users that can't be pulled back. A PR that's cheap to roll back is lower risk. Add `door_note` (one sentence) when the call isn't obvious or the PR body got it wrong.

**Blast Radius.** One word for how far the effects reach if this goes wrong. Consider all of it: breakages for consumers of an API or package, layout shift, mobile responsiveness, data correctness, performance under load, other teams. Common words: `contained` (one function or screen), `feature`, `app-wide`, `consumers` (other services, packages or teams), `data` (stored data can end up wrong), `security`. Use another single word if it fits better. Add `blast_note` with the likely ramifications when it's bigger than `contained`.

**Rubberstampability.** 1 to 5: how safe is it for a human to approve this after a skim? It's the answer to "can I trust the factory on this one?"

| Score | Meaning | Typical shape |
|---|---|---|
| 5 | Stamp it | Two-way door, contained, matches the spec, real tests (not tautological) cover the change, no must-fix findings. |
| 4 | Skim it | Two-way, small radius, only nits or one minor must-fix, evidence is real. |
| 3 | Read it | Something needs a person's eyes: a real must-fix, weak or tautological tests on the logic that matters, a radius beyond one feature, or any one-way door. |
| 2 | Review closely | A one-way door with open questions, or an architecture, spec or serious performance problem. |
| 1 | Don't merge as is | A security hole, a data-loss or bad-data risk, a contradiction of the spec, or a one-way door that looks wrong. |

Caps: a one-way door is never above 3, because it deserves a deliberate yes (the script enforces this). A security or spec-contradiction must-fix is never above 2. `rubberstamp_reason` is one plain sentence naming what drove the score.

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
    "body": "One to three sentences: the overall take, the one or two things that matter most, and whether the PR's own Merge Danger call holds up. No Merge Danger block here; the script adds it.",
    "comments": [
      { "path": "src/routes/invoices.js", "line": 9, "body": "..." },
      { "path": "src/routes/invoices.js", "start_line": 15, "line": 26, "body": "..." }
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
    { "kind": "issue_comment", "to": "bob", "context": "...", "body": "..." }
  ],
  "notes_for_me": "Things for the user only: decisions to make, low-confidence findings, anything you left out and why."
}
```

- `review` and `assessment` are `null` when there's no review this pass (own PR, or only replies needed). Every review needs an assessment; `stage` and `post` refuse one without it.
- Inline comment `line` numbers are lines in the PR head version of the file (RIGHT side) and must fall inside a diff hunk, or GitHub rejects the review. Check against `diff.patch`. If a point is about code outside the diff, put it in the review body instead.
- For `review_comment` replies, `reply_to` is the thread's root comment ID (`reply_to` in `waiting.json`).
- Reviews post as a plain comment review — never approve or request changes unless the user asks.

Then: `mark REPO N --sha <head_sha> --comments <every id from waiting.json you handled or deliberately skipped>`.

## The digest

End every pass with a short digest in the terminal. Run `digest REPO` for the pending-drafts table (riskiest first) and put your one-line notes on what's new this pass above it:

```
review-prs · owner/repo · 14:32
New this pass
  #42  drafted review, PR body says two-way but it adds a migration
  #57  drafted 2 replies on your PR
Pending drafts, riskiest first
  #42    Add invoices endpoints        one-way · data · 2/5  ·  2 must-fix, 1 nits  ·  1 replies
  #61    Field card spacing            two-way · contained · 5/5  ·  0 must-fix, 0 nits
  #57    Fix login redirect                                        ·  2 replies
Stack  #41 -> #42 -> #43  ·  one-way at #41  ·  lowest 2/5
Skipped: 3 drafts, 1 bot, 4 with nothing new, 2 rebased only
Next: read the staged ones on GitHub (Files changed tab), `show` for the rest, `post 42` or `post all` to send.
```

If nothing needed doing, one line: `review-prs · owner/repo · nothing new.`

## Staging on GitHub

By default, after a pass writes a draft, run `stage REPO N` for it. That puts the review on the PR as the user's **pending review**: the summary, the inline comments and the replies to review threads all show up on the PR's Files changed tab, marked Pending, and nobody but the user can see them. The user reads them in context, edits or deletes anything right there, and submits with "Finish your review" (or says `post 42`, which submits it as a plain comment review). The draft's `staged_url` links to it.

Things to know:
- **Conversation replies can't be staged.** GitHub can only hold inline comments and review-thread replies as pending. Replies to top-level conversation comments stay local; `show` lists them and `post` sends them.
- **One pending review per person per PR.** If the user already has their own pending review going on a PR, `stage` refuses rather than touching it, and says so. Leave that draft local and mention it in the digest.
- **Re-staging replaces.** After a re-review, `stage` deletes the earlier pending review it made and creates the new one. It never deletes a pending review it didn't make.
- **It notices what the user did.** On the next `scan`, a staged review that was submitted is marked posted (leftover conversation replies stay pending), and one the user discarded is marked discarded and not staged again.
- Skip staging if the user passed `--local` or said they want drafts kept local.

## Posting

Only when the user asks (`post 42`, `post all`, "send the review on 42"). First run `post ... --dry-run` and check it's what you expect, then run it for real and report what went out. For a staged draft, `post` submits the pending review as it now stands on GitHub, including the user's edits there. For a local draft, it posts the draft file as written, including any edits the user made to it. If a review is refused because the PR moved, offer to re-review. Never submit or post during a loop pass on your own; staging is as far as a pass goes.

## Loop mode

Under `/loop`, each firing is one pass. The state file makes passes cheap: PRs with no new commits and no new comments are skipped without fetching. Each pass:
- Never submits or posts. It drafts, then stages new drafts as pending reviews on GitHub (unless `--local`), which only the user can see.
- Doesn't re-draft a PR whose pending draft already covers the current head, unless new comments came in.
- Keeps the digest short, and leaves out anything that was already in the previous digest and hasn't changed.

If the loop is self-paced (`/loop` with no interval), wait about 20–30 minutes between passes; PRs don't move faster than that, and a quiet pass costs little. If the user gave an interval (`/loop 30m ...`), that's the pacing.
