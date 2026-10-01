# Contributing

Thanks for helping. smackcicd is small on purpose; contributions that keep it
that way are the easiest to accept.

## Ground rules

- **No runtime dependencies.** A build machine should keep building without a
  package index. The standard library only; test and lint tools are fine as dev
  dependencies.
- **Python 3.11+**, Windows and Linux. Code that touches processes, paths or
  services must work on both, or say clearly that it is OS-specific.
- **Explain the why in comments** when code exists because of a trap, such as a
  Windows quirk or an engine version difference. The next person will be
  tempted to "simplify" it.
- **Nothing project-specific.** No hard-coded hosts, paths, project names or
  engine locations. If something must be configurable, add it to
  `DEFAULTS` in `config.py`, to the template, and to `docs/configuration.md`.
  A test checks that every setting is documented.

## Development

```bash
git clone <this repository> && cd smackcicd
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check .
```

The tests need git on PATH and no Unreal install: `test_pipeline.py` drives
the real pipeline against a throwaway repository and a fake engine whose
`RunUAT` is a small Python script.

To try the daemon without touching a real project, point a scratch home at a
throwaway workspace:

```bash
smackcicd --home ./scratch init --repo https://github.com/you/some-repo.git --no-clone --yes
smackcicd --home ./scratch watch
```

## Adding a platform

1. Add a `PlatformInfo` entry in `platforms.py`: Unreal's platform name, archive
   subfolders, package markers, tag aliases, and the hosts that can build it.
2. Add `[platforms.<Name>]` defaults in `config.py`, the template, and
   `docs/configuration.md`.
3. Add any platform-specific `BuildCookRun` flags in
   `engines.build_cook_run_args`.
4. Add an artifact-layout test in `tests/test_artifacts.py`.

## Adding a forge

Implement `forge.base.Forge`: tags, commit status, releases and assets,
normalised as described in its docstring. Register it in `forge/__init__.py`,
and teach `detect_kind` and `default_api_url` to recognise it. Test against
the mock server in `tests/test_forge.py`.

## Pull requests

Keep them focused, include tests, and update the docs and `CHANGELOG.md`. By
contributing you agree your work is licensed under the GPL-3.0-or-later, the
project's license.
