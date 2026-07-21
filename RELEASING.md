# Releasing

A PyPI version number can never be reused. Not after a delete, not after a yank — that
exact `name==version` is burned permanently. Every step below exists to catch a problem
*before* it becomes unfixable.

## 1. Prepare

```bash
python -m pytest                       # full suite
python -m pytest -m perf               # wall-clock assertions, on a quiet machine
python -m ruff check .
python -m ruff format --check .
python benchmarks/overhead.py          # do the README's numbers still hold?
```

Bump the version in **`webrtrace/__init__.py` only** — `pyproject.toml` reads it from
there, so the two can never disagree. Then move the `[Unreleased]` heading in
`CHANGELOG.md` to the new version with today's date.

## 2. Build

```bash
python -m pip install --upgrade build twine
rm -rf dist/
python -m build
python -m twine check dist/*
```

`twine check` validates the metadata and the README rendering. A README that fails to
render on PyPI cannot be fixed without a new version number.

Inspect what you are actually shipping:

```bash
python -c "import zipfile; print('\n'.join(zipfile.ZipFile('dist/webrtrace-X.Y.Z-py3-none-any.whl').namelist()))"
```

Expect the modules, `py.typed`, `LICENSE`, and `NOTICE`. Tests, examples, benchmarks, and
`__pycache__` must not be there.

## 3. Verify the artifact, not the repo

This is the step people skip, and it is the one that catches packaging bugs. Everything up
to now has run with the repository on `sys.path`, which hides missing files.

```bash
python -m venv /tmp/verify
/tmp/verify/bin/pip install dist/webrtrace-X.Y.Z-py3-none-any.whl
cd /tmp                                 # somewhere the repo cannot be found
/tmp/verify/bin/python -c "import webrtrace; print(webrtrace.__file__)"
```

The path printed must be inside `site-packages`. Then exercise it for real:

```bash
/tmp/verify/bin/python -m webrtrace some-trace.jsonl
/tmp/verify/bin/python path/to/examples/03_the_silent_failure.py
```

## 4. Test PyPI first

```bash
python -m twine upload --repository testpypi dist/*
```

Install it back from there, into another clean venv:

```bash
python -m venv /tmp/testpypi
/tmp/testpypi/bin/pip install --index-url https://test.pypi.org/simple/ webrtrace
/tmp/testpypi/bin/python -c "from webrtrace import webR_node; print('ok')"
```

Test PyPI burns version numbers too, but only on Test PyPI — which is the entire point of
it existing.

## 5. Publish

```bash
python -m twine upload dist/*
```

Use an API token (`__token__` as the username), scoped to this project once it exists.

## 6. Tag and push

```bash
git tag -a vX.Y.Z -m "webR X.Y.Z"
git push origin main --tags
```

Then create the GitHub release from the tag, pasting that version's CHANGELOG section.

## If something is wrong after publishing

You cannot replace a release. You can only:

- **Yank** it (`pip` stops selecting it for new installs; existing pins keep working). Right
  for a broken build or a serious bug.
- **Publish a patch version.** Right for everything else.

Never delete a release that people may already depend on — deletion breaks their pinned
installs, while a yank does not.
