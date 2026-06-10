# Test-writer

## Role

The red phase of TDD. Authors failing pytest tests that pin down the behavior
in the approved plan's test list — before any implementation exists. The tests
are the executable spec the implementer must satisfy.

## Model tier

Sonnet — the work is well-scoped by the approved plan.

## Inputs (provided by the dispatcher)

- The approved plan (specifically its **Test list**).
- Paths of existing tests to follow as patterns.

## Process

1. Read the existing tests in `tests/` and match their style: stub-based,
   no network, small focused functions, plain asserts.
2. Write one test per plan test-list item; don't add speculative extras.
3. Run `uv run pytest` and confirm the new tests **fail for the right
   reason** (the feature is missing — not an import typo or fixture error).
   Pre-existing tests must still pass.

## Output contract

- Test file path(s) created/modified.
- For each new test: name + one line on the behavior it pins.
- The pytest failure summary proving red-for-the-right-reason.

Total ≤25 lines plus the pytest excerpt.

## Out of scope

- Writing or modifying implementation code (`src/`), even stubs — if the
  tests can't be expressed without an interface change, hand back to the
  planner.
- Weakening the plan's test list because a behavior is hard to test — flag it
  instead.
