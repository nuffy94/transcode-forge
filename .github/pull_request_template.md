## Summary

<!-- What does this PR change, and why? One or two sentences. -->

## Type of change

- [ ] feat — new feature
- [ ] fix — bug fix
- [ ] refactor — no behavior change
- [ ] docs / test / chore / perf / ci

## How verified

<!-- Commands run + observed result. e.g. `uv run pytest` → 412 passed.
     For deploy/infra changes, what you ran against a real target. -->

## Checklist

- [ ] `uv run ruff check src/ tests/` clean
- [ ] `uv run ruff format src/ tests/` applied
- [ ] `uv run pytest` passes
- [ ] Commits are atomic (each one builds + tests green; revertable on its own)
- [ ] New schema change → new numbered migration (released migrations are immutable)
- [ ] Docs / DECISIONS.md updated if a decision or interface changed
