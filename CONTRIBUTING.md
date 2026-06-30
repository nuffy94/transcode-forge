# Contributing

How changes flow through this repo. Small project, but run like a real one.

## Branching model

```
feature branch  ──PR──▶  dev  ──PR──▶  main
(feat/*, fix/*)        (staging,      (protected,
                        CI-gated)      releasable)
```

- **`main`** — always releasable. Protected: no direct pushes, no force-push, no
  deletion. Every change arrives via a pull request with green CI.
- **`dev`** — integration / staging. Day-to-day work merges here first; CI runs on
  every PR into it. Releases are cut by opening a PR from `dev` → `main`.
- **feature branches** — short-lived, branched from `dev`, named for the change:
  `feat/av1-encoder`, `fix/orphan-job-release`, `chore/ci-foundation`.

## Commits

Conventional Commits — `<type>: <description>`:

`feat` · `fix` · `refactor` · `docs` · `test` · `chore` · `perf` · `ci`

Keep commits **atomic**: each commit should build and pass tests on its own, and
be safe to revert in isolation. Don't bundle an unrelated drive-by fix into a
feature commit — that's what makes a clean rollback possible.

## Before you push

```bash
uv run ruff check src/ tests/      # lint
uv run ruff format src/ tests/     # format (CI enforces --check)
uv run pytest                      # unit + integration
```

## Pull requests

1. Branch from `dev`, make atomic commits.
2. Open a PR into `dev` (use the template). CI's **`test`** job must pass.
3. Squash or rebase merge (history stays linear), then delete the branch.
4. To release: open a PR `dev` → `main`. Same gate.

The `qa-sweep` CI job (axe + screenshots) runs on every PR for visibility but is
not yet a hard merge blocker — treat its output as a signal.

## Branch protection (enforced on `main`)

- Pull request required before merging (0 required approvals — solo project).
- Status check `test` must pass and the branch must be up to date.
- Linear history; no force-pushes; no branch deletion.

To override in a pinch (broken CI, emergency), temporarily relax protection in
**Settings → Branches** — don't make it a habit.
