# Contributing to notepaDB

Thanks for considering a contribution. This project is small and maintained
in spare time, so please keep PRs focused — small, reviewable changes land
faster than large ones.

## Reporting bugs

Open an [issue](https://github.com/DrorMaor/notepadb/issues) and include:

- What you did, what you expected, and what happened instead.
- Which database engine you were connected to (SQL Server / PostgreSQL /
  MySQL / Oracle / SQLite), if relevant.
- Your OS and Python version.
- Steps to reproduce, if you can find them.

**Never include real connection strings, hostnames, credentials, or query
history from your own databases in an issue, PR, or screenshot.**

## Suggesting features

Open an issue describing the use case before writing code — it's easier to
agree on an approach up front than to rework a finished PR.

## Development setup

```bash
git clone https://github.com/DrorMaor/notepadb.git
cd notepadb
pip install -r requirements.txt
py app.py
```

There's no test suite yet; manual verification against the engine(s) your
change touches is expected. SQLite is the easiest engine to test against
locally since it needs no server.

## Pull requests

1. Fork the repo and create a branch for your change.
2. Keep the change scoped to one thing — avoid bundling unrelated fixes or
   reformatting with a feature/bugfix.
3. Match the existing code style (no linter/formatter is enforced yet).
4. Describe what the PR does and why in the description, not just what
   changed.

By submitting a PR, you agree your contribution is licensed under the
project's [GPL-2.0-only license](LICENSE).

## Cutting a release

Releases are built automatically by
[`.github/workflows/release.yml`](.github/workflows/release.yml): pushing a
tag matching `v*.*.*` builds `notepadb.exe` with PyInstaller and attaches it
to a GitHub Release.

```bash
git tag v0.1.0
git push origin v0.1.0
```

Bump `__version__` in `app.py` and add a `CHANGELOG.md` entry before tagging.

## Adding a new database backend

Backends live in `db_backends.py` and implement a small common interface
(`connect`, `disconnect`, `test`, `list_schema`, `execute`, `label`). Subclass
`DbBackend`, implement the abstract methods, and register the new class in
`BACKENDS`. See the existing backends for the expected shape.
