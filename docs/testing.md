# Testing

```sh
uv sync --extra dev
uv run pytest
```

The offline suite needs no network and no key, and it is what CI runs on
Windows, macOS and Linux on every push and pull request.

For the browser and image tests as well, after a one-off
`uv run --with playwright playwright install chromium`:

```sh
uv run --with playwright --with pillow --with zxing-cpp pytest
```

## Captures

**The capture-based tests are skipped on a fresh clone, and that is expected.**
A capture is a verbatim transcript of whatever was said in front of the
microphone, so none are committed here. Everything else runs without them.

To turn those tests on, put your own recordings in `tests/fixtures/` under the
names `tests/captures.py` documents, either by recording one:

```sh
uv run python tools/probe_soniox.py --file talk.m4a --out tests/fixtures/run1.jsonl
```

or by copying a live response log out of `logs/` after a session. Both files
carry real speech, which is why `tests/fixtures/*.jsonl`, `logs/`,
`recordings/` and `transcripts/` are all gitignored.

## What the suite will not let you do

Two guards exist because they have each fired on real work:

- `tests/conftest.py` redirects the operator's own files and directories for
  every test, and fails the run at the end if anything wrote to the real
  `config/`, `logs/` or `transcripts/`. Those files are gitignored and have no
  history to recover from.
- `tests/test_house_style.py` reads every committed markdown and Python file
  and rejects an em dash in our own prose. Use a comma, a period, a colon,
  parentheses or a spaced hyphen.

`tests/test_brand.py` also refuses any committed image other than
`docs/demo.gif`, which is the one in [the README](../README.md).
