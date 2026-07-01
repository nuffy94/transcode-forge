# Git, for Mason

A plain-English reference for working with git on this repo. You drive the
everyday flow yourself; reach for Claude on the gnarly stuff (bottom of this file).

## The mental model (what the words mean)

- **Repo** — the whole project + the full history of every change ever made.
- **Branch** — a parallel copy you can change safely. `main` = the official,
  released version; a *feature branch* = a short-lived workspace for one change.
- **Commit** — one saved snapshot of a change, with a message.
- **Staging area** — the "box" you put changes into before sealing a commit.
  `git add` fills the box; `git commit` seals it.
- **Push / pull** — upload your commits to GitHub / download GitHub's latest.
- **Pull request (PR)** — "please merge these changes into `main`," plus the
  review and the automated checks.
- **CI check** — robots that run automatically on a PR (here: `test`, `qa-sweep`).
- **Merge** — accept the PR; the change becomes part of `main`.
- **.gitignore** — a list of files git deliberately ignores (secrets, local notes,
  build junk). They live on your disk but never get committed or pushed. *This is
  why editing `docs/DECISIONS.md` showed nothing in `git status` — it's gitignored.*

## The everyday loop (your 10 steps)

Every change you make follows these beats:

```
git checkout main             # 1. get onto main...
git pull                      #    ...and make it current
git checkout -b docs/thing    # 2. branch off it. -b = CREATE. It branches from
                              #    wherever you're standing, so do step 1 first.
# ...make the change...       # 3.
git status                    # 4. see what changed (git diff for edits to
                              #    existing files; NEW files show only in status)
git add <file>                # 5. stage it (put it in the commit box)
git commit -m "type: message" # 6. seal the snapshot
git push -u origin docs/thing # 7. send the branch to GitHub (-u = first time only)
gh pr create --base main --fill   # 8. open the PR
gh pr checks <number>         # 9. wait until `test` = pass
gh pr merge <n> --squash --delete-branch   # 10. merge + delete the branch
```

Commit message prefixes (Conventional Commits): `feat:` `fix:` `docs:` `test:`
`chore:` `refactor:` `perf:` `ci:`.

## This repo's rules

- **Branch off `main`, PR back into `main`.** (`dev` becomes a staging middle-tier
  later, once there's a release pipeline — until then, straight to `main`.)
- `main` is **protected**: no direct commits or pushes. The `test` check must pass
  and the PR flow is required — the tooling *enforces* the discipline so you can't
  forget it.
- **`test`** (lint + format + ~400 unit tests) is the **required** gate.
  **`qa-sweep`** (real-browser accessibility + screenshots) runs for info only.
- Feature branches are **throwaway** — delete after merge (`--delete-branch`).
  `main` and `dev` live forever; never delete those.

## Seeing what changed: `git status` vs `git diff`

Two different questions, two different commands — this trips up everyone at first:

- **`git status` = the dashboard.** *"What branch am I on, and which files are
  changed / staged / untracked?"* It lists file **names** and their **state**. Run
  it constantly — any time you're unsure what's going on.
- **`git diff` = the detail.** *"Show me the actual line-by-line changes."* Removed
  lines are red and start with `-`; added lines are green and start with `+`; each
  changed region is a "hunk" headed by `@@ ... @@`.

The catch that causes confusion: **`git diff` with no arguments only shows
*unstaged* edits to files git *already tracks*.** So a diff can come up empty for
three different reasons — and `git status` tells you which:

| If `git diff` is empty... | ...the reason is | `git status` shows it as |
|---|---|---|
| you made a **brand-new file** | git doesn't track it yet | **"Untracked files"** |
| you already ran **`git add`** | the change is now **staged** | **"Changes to be committed"** (green) — see it with `git diff --staged` |
| the file is **gitignored** | git deliberately ignores it | *nothing at all* |

**Rule of thumb:** `status` to see *what* changed (names + state); `diff` to see the
*actual lines*. When a diff is surprisingly empty, run `status` — it tells you why.

## Handy habits & gotchas

- **`!` prefix** runs a command in your Claude session and shows the output in
  chat — that's how you run these day-to-day.
- **When a command is quiet, verify.** `git status` is your dashboard: "where am I,
  what's changed, what's staged?" Run it whenever unsure.
- **`gh pr checks` "failing" while pending is normal** — it exits non-zero until
  everything's green. Just re-run it.
- **Pull before you branch, always** — free insurance against a stale start point.
- Undo buttons: `git restore --staged <file>` unstages; `git checkout -- <file>`
  discards uncommitted edits to a file. Ask Claude first if unsure.

## When to hand it to Claude

Do the everyday loop yourself. Reach for Claude on the messy 10%:
- merge conflicts,
- rebases or rewriting history,
- "I did something weird and I'm not sure what state I'm in,"
- anything where you'd otherwise be guessing.
