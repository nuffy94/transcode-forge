---
name: shape
description: Before proposing code for any fix, find the version where the bug cannot happen. Use on every fix, bug, patch, guard, sync, flag, or "make it stop doing X" request, before any code is written. Also use to grade an existing change or a ledger entry.
---

# Shape before rule

A **shape** fix changes the structure so the bug has no place to happen: one rule, enforced at one boundary, and everything else stays unchanged. A **rule** fix leaves the structure alone and adds a check, a flag, a sync, or a special case at each place the bug can reach. Rules multiply; shapes delete.

Reference (verified 2026-09-06, luthermonson/linode-tui PR #12): a CLI token was being written to disk. Rule version: a "temporary mode" flag that every save site must consult, and legitimate saves get lost. Shape version: the token becomes a synthetic account named `__cli__`, every reader works unchanged, and only `Save()` knows it is ephemeral and marshals a scrubbed copy. One rule, one place, at the memory/disk boundary.

## Procedure

Answer these five in order, in writing, before any code:

0. **What is the evidence, and what must keep working?** Name the repro or trace that separates the old behavior from the new. The invariant has two halves: the bad state that must not exist, and the valid behavior the fix must preserve. (R-020 lesson, 2026-09-12: deleting the poison-report exception and keeping the global claim fence scores well on rule count and deletion, and brings back the two-day stall.)
1. **What is the version where this cannot happen?** Not "where we catch it." Where the state that causes it does not exist.
2. **Where is the single boundary?** Name the one function, table, type, endpoint, or protocol that would own the rule. A boundary can be a protocol that spans components (database, API, worker); if it does, name every path that reaches it, because one helper is not a boundary when another caller can go around it. If you cannot name one place, you have not found the shape yet.
3. **How many rules does each version add?** A rule is anything a future maintainer must remember: a flag, a check at a call site, a second copy of state kept in step, a config knob, an "on X also do Y." Count them for the shape version and the rule version.
4. **What gets deleted?** A shape fix usually removes a special case. If the proposal only adds, say so; that is the smell.

## Output (fixed format)

```
Bug: <one sentence>
Evidence: <the repro or trace that separates old from new>
Keeps working: <the valid behavior the fix must preserve>
Boundary: <the one place, or "not found">
Shape: <one sentence of what changes> (rules added: N, deleted: M)
Rule:  <one sentence of the patch version> (rules added: N)
Recommend: shape | rule, because <cost, blast radius, freeze, or new API needed>
In plain words: <two sentences for someone who does not read code: what a
future person must remember with each version>
```

Then stop and let Mason pick. He picks by the rule count and by whether something got deleted; he does not need to read the code.

## Smells that mean you are holding a rule

- A flag whose meaning is "do not do the normal thing this time."
- The same check added at several call sites.
- A second copy of state that has to be kept in step with the first.
- A new config setting to make a bug go away.
- "Also update X when Y happens" added to a growing list.

## When the rule is the right call

Sometimes the shape needs a new API, a schema change, or crosses a release freeze. Ship the rule, but the PR body must open with `Rule: <what it does>. The shape would be <one sentence>, not taken because <reason>.`
