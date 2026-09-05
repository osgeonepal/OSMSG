#!/usr/bin/env bash
# Install the built wheel into a clean environment (no source tree on sys.path) and import every osmsg
# submodule plus run the console entrypoint. A file dropped from the wheel (e.g. excluded by a .gitignore
# pattern) fails here instead of shipping to PyPI. Run after `uv build`, before `uv publish`.
set -euo pipefail

wheel=$(ls dist/*.whl)
echo "Verifying wheel: ${wheel}"
uv venv /tmp/wheelcheck
uv pip install --python /tmp/wheelcheck "${wheel}"
/tmp/wheelcheck/bin/osmsg --version
/tmp/wheelcheck/bin/python - <<'PY'
import importlib
import pkgutil

import osmsg

modules = [m.name for m in pkgutil.walk_packages(osmsg.__path__, "osmsg.")]
for name in modules:
    importlib.import_module(name)
print(f"imported {len(modules)} osmsg submodules from the wheel")
PY
