# Smell & principle baseline

Heuristics for the Refactoring and Reuse axes. Every one is a **judgement call**: label findings as "possible X", and drop them when the repo consistently does otherwise (the repo overrides the baseline) or when tooling already enforces the rule.

## Fowler smells (Refactoring, ch. 3) — symptom → usual fix

| Smell | Symptom | Fix |
|---|---|---|
| Mysterious Name | You can't tell what it does from its name | Rename. If no honest name comes to mind, the design is murky — say that. |
| Duplicated Code | Same shape in 2+ places (in the diff, or diff vs existing code) | Extract the shared shape, or call the existing one |
| Long Function | Has to be scrolled; sections separated by comments | Extract functions named after what the comments say |
| Long Parameter List | 4+ params, or params always passed together | Parameter object, or pass the thing the params came from |
| Feature Envy | Method mostly reads another object's data | Move it onto the data it envies |
| Data Clumps | Same 3+ fields travel together | Bundle into one type |
| Primitive Obsession | Strings/numbers standing in for a domain concept (ids, money, status codes) | Give the concept a small type or enum |
| Repeated Switches | Same switch/if-chain on the same discriminator in several places | One map or polymorphism shared by all sites |
| Shotgun Surgery | One logical change forces edits across many files | Gather what changes together into one module |
| Divergent Change | One module changes for unrelated reasons | Split so each changes for one reason |
| Speculative Generality | Hooks, params, abstractions, or config with one (or zero) users | Inline/delete until a real second need appears |
| Message Chains | `a.b().c().d()` | Hide the walk behind one method |
| Middle Man | A class/function that only delegates | Call the target directly |
| Refused Bequest | Subclass ignores/overrides most of what it inherits | Composition instead of inheritance |
| Flag Argument | Boolean param that switches behavior | Two functions, or an explicit enum |
| Dead Code | Unused vars, unreachable branches, commented-out code | Delete it |

## Principles

**KISS.** Prefer the most direct code that solves the actual problem. Flags: clever one-liners that need a comment to decode, nested ternaries, generic frameworks built for one caller, reflection/metaprogramming where a plain call works, configuration for things that never vary. The fix is usually *less* code.

**Single Responsibility.** A function, component, or module should have one reason to change. Flags: a function that fetches, transforms, and renders; a component that owns data loading, business rules, and layout; a name with "And" in it (or that would need one to be honest); a file whose imports span unrelated domains. The fix is to split along the seams, matching how the codebase already splits things (e.g. if the repo puts data loading in hooks, the split goes there).

**Clean code.** Intention-revealing names; small functions at one level of abstraction; early returns over deep nesting; no magic numbers/strings where the repo has constants; comments explain *why*, not *what*; errors handled the way neighbors handle them.

**DRY — with restraint.** Duplication of *knowledge* (a business rule, a regex, an endpoint URL, a validation) is worth removing even at two copies. Duplication of incidental *shape* (two loops that happen to look alike) usually isn't until the third copy. Wrong abstractions cost more than duplication — when unsure, say "consider" rather than "should fix".

## Extraction checklist (Reuse axis)

Before recommending an extraction, confirm:
1. Does something in the codebase already do this? Search `utils/`, `lib/`, `shared/`, `common/`, `helpers/`, `hooks/`, and grep for distinctive literals (regexes, URLs, error messages, key names). If yes → "use existing `path:symbol`", which beats creating anything new.
2. Are there ≥2 real call sites (in the diff or diff + existing)? One call site → don't extract.
3. Do the copies represent the same *concept* (will they change together)? If they'll diverge, leave them.
4. Where would it live? Name the directory based on where the repo keeps similar helpers.
