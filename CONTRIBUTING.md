# Contributing

Thanks for your interest in envdoctor.

## Ground rules

- **Standard library only.** No new runtime or test dependencies.
  Tests run with `python -m unittest discover -s tests -v` on Python 3.9+.
- **One problem, well solved.** New features must serve the core mission:
  catching env-var drift between code, `.env.example`, and `.env`.
- **Every new scan pattern needs a test** in `tests/test_envdoctor.py`,
  including a negative case where appropriate.
- **Never print secret values.** Any output change must preserve the
  names-only guarantee (`test_values_never_printed` must keep passing).

## Workflow

1. Fork and create a feature branch.
2. Add tests first, then the implementation.
3. Run the full suite: `python -m unittest discover -s tests -v`.
4. Self-check the repo: `python envdoctor.py --root . --no-env --exclude tests`
   (test fixtures intentionally contain sample env references, so `tests/`
   is excluded; the repo ships its own `.env.example` for this).
5. Open a PR describing the problem, the change, and the test evidence.

## Release checklist (maintainers)

1. Update `__version__`, `CHANGELOG.md`, and README examples if behavior changed.
2. Full test suite green on Windows, macOS, and Linux (see CI).
3. Security review per `SECURITY.md` guarantees.
