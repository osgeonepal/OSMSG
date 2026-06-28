# Installing osmsg

```bash
# zero-install, one-shot
uvx --from osmsg osmsg --last hour

# pip / pipx / uv
pip install osmsg
uv tool install osmsg

# conda or mamba
conda install -c conda-forge osmsg

# Homebrew (macOS / Linux)
brew install osgeonepal/tap/osmsg

# Docker
docker run --rm -v "$PWD:/work" -w /work ghcr.io/osgeonepal/osmsg:latest --last hour
```

On Windows, grab `osmsg.exe` from the [latest release](https://github.com/osgeonepal/osmsg/releases)
and double-click it to open the desktop app.

---

## Maintainers: publishing each channel

The recipes/specs live in this folder. Bump `version` (and the conda `sha256`) on every release.
None can be fully built from a Linux box; each needs its own environment (noted below).

**conda-forge** (`conda/meta.yaml`) , `noarch: python`; all deps already on conda-forge.
Copy to `conda-forge/staged-recipes` under `recipes/osmsg/`, open a PR. After merge the bot
auto-opens a version-bump PR on each PyPI release. `sha256` is the PyPI sdist hash.

**Homebrew** (`homebrew/osmsg.rb`) , a tap formula (venv + pip-install). Create a tap repo named
`osgeonepal/homebrew-tap` and put the formula at `Formula/osmsg.rb`. Users then run
`brew install osgeonepal/tap/osmsg`, or `brew tap osgeonepal/tap` then `brew install osmsg`.
Homebrew always uses `user/tap/formula` and the tap repo must be named `homebrew-*`, so the tap part
stays until osmsg is accepted into homebrew-core (then it becomes plain `brew install osmsg`).
Validate on macOS: `brew install --build-from-source ./Formula/osmsg.rb`.

**Windows .exe** (`pyinstaller/` + `../.github/workflows/windows-exe.yml`) , PyInstaller one-file
windowed build of the tkinter desktop UI (`osmsg.gui:launch` via `gui_entry.py`), built on a
`windows-latest` GitHub Actions runner. Push a version tag to build it and attach it to the Release;
`workflow_dispatch` builds on demand.
