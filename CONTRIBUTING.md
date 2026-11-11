# Contributing to shipit

Thanks for helping! shipit is small on purpose: Python stdlib only, no runtime dependencies. Please keep it that way.

## Setup

```bash
git clone https://github.com/desnarong/shipit.git
cd shipit
python -m unittest discover -s tests          # fast unit tests, no Docker needed
SHIPIT_E2E=1 python -m unittest tests.test_e2e -v   # real Docker end-to-end tests (a few minutes)
```

Requires Python 3.11+, git, and Docker for the e2e tests.

## Making a change

1. Open an issue first for anything bigger than a small fix, so we agree on the direction.
2. Branch from `main`, keep the change focused, and add or update tests.
3. Run the unit tests (and the e2e tests if you touched `deploy.py`, `proxy.py` or `detect.py`).
4. Open a pull request and fill in the template.

## Good first contributions

- **A new language template** in `shipit/detect.py` (add a rule + a test in `tests/test_shipit.py`).
- Better start-command inference for a framework.
- Docs, examples, and bug reports with logs.

## Guidelines

- No new runtime dependencies. Standard library only.
- Never put secrets in logs, URLs or files other than the state dir. Webhook input is untrusted.
- Tests that call Docker belong in `tests/test_e2e.py`; everything else must run without Docker.

By contributing you agree your work is released under the [MIT License](LICENSE).
