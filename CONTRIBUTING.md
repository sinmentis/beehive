# Contributing to Beehive

Thanks for your interest in improving Beehive. This guide covers local setup,
running tests, linting, and commit conventions.

## Local setup

Beehive targets Python 3.12. Create a virtual environment and install the
project with its development, AI, and email extras:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,ai,email]"
```

## Running tests

Run a focused subset while iterating on a change:

```bash
.venv/bin/python -m pytest tests/path/to/test_module.py
```

Run the full suite before opening a pull request:

```bash
.venv/bin/python -m pytest
```

## Linting

Beehive uses [Ruff](https://docs.astral.sh/ruff/). Run it before committing:

```bash
.venv/bin/python -m ruff check .
```

## Commit messages

This project follows [Conventional Commits](https://www.conventionalcommits.org/).
Use a type prefix such as `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, or
`chore:`, followed by a concise summary:

```text
feat: add per-channel digest scheduling
```

## Security and credentials

Never include credentials, tokens, connection strings, or other secrets in
issues, pull requests, or commits. If you need to report a security
vulnerability, follow the process in [SECURITY.md](SECURITY.md) instead of
opening a public issue.
