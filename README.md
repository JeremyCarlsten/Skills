# Skills

Claude Code skills I use for code review in an agent-driven workflow.

## Install

Install everything, globally, for Claude Code:

```bash
npx skills@latest add JeremyCarlsten/Skills -g -a claude-code -s '*' -y
```

Or pick one:

```bash
npx skills@latest add JeremyCarlsten/Skills -g -a claude-code -s review-prs -y
```

`-g` installs to your user skills folder (`~/.claude/skills/`), so the skills work in every project. Leave it off to install into the current project only.

Update later with:

```bash
npx skills@latest check
npx skills@latest update
```

This uses the [`skills` CLI](https://github.com/vercel-labs/skills), the same installer Matt Pocock's skills library uses.

## Skills

| Skill | Command | What it does |
|---|---|---|
| [review-outstanding](skills/review-outstanding/SKILL.md) | `/review-outstanding` | Reviews your uncommitted and unpushed changes for standards, spec, pattern breaks, refactoring (KISS, SRP), reuse and architecture, then offers to apply fixes. |
| [review-prs](skills/review-prs/SKILL.md) | `/review-prs owner/repo` | Sweeps a repo's open PRs and drafts big-picture reviews (spec, architecture, security, one-way doors, agent-friendliness, tautological tests, performance) with a Merge Danger call, plus replies to comments in my voice. Drafts land on the PR as my private pending review; nothing is submitted until I approve. Built for `/loop` next to a Ralph loop: waits for CI and settled branches, flags merge-ready and risky PRs, checks fixes, spots overlapping PRs and repeating findings. Stacked-PR aware. |

### review-prs needs

- [GitHub CLI](https://cli.github.com/) installed and logged in (`gh auth login`)
- Python 3 and git
- State and drafts are kept in `~/.review-prs/`, outside your repos.

Common commands:

```text
/loop /review-prs owner/repo            keep watching, stage drafts as pending reviews
/loop /review-prs owner/repo --local    keep drafts on this machine only
/loop /review-prs owner/repo --mine     also review my own PRs
/review-prs owner/repo digest           pending drafts, riskiest first
/review-prs owner/repo show             read the drafts
/review-prs owner/repo stage 42         put a draft on GitHub as my pending review
/review-prs owner/repo post 42          post one (or: post all)
/review-prs owner/repo 567              review one PR now (or paste the PR URL)
/review-prs owner/repo trends           findings that keep repeating, and factory fixes
/review-prs owner/repo overlaps         open PRs touching the same files
/review-prs owner/repo config settle_minutes 30
```

## Repo layout

```text
skills/<name>/SKILL.md     what gets installed (plus any scripts/ and references/)
evals/<name>/              test cases for the skill, not installed
```

## Credits

- `review-outstanding` and `review-prs` build on Matt Pocock's [code-review](https://github.com/mattpocock/skills/tree/main/skills/engineering/code-review) skill (Standards + Spec axes, Fowler smell baseline).
- The Merge Danger section (door, blast radius) follows the [pr](https://github.com/mattpocock/skills/tree/main/skills/engineering/pr) skill, which credits Dex Horthy's [show-me](https://github.com/humanlayer/skills/blob/main/plugins/show-me/skills/show-me/SKILL.md) skill at HumanLayer.
