"""Build-time helper: record which sqlglot lines run at import time, which
modules `import sqlglot` loads eagerly, and every Dialect's component classes.

usage: python import_pass.py <sqlglot_root> <out.json>
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import sys

import coverage

root = os.path.abspath(sys.argv[1])
out = sys.argv[2]
sys.path.insert(0, root)

cov = coverage.Coverage(data_file=None, source=[os.path.join(root, "sqlglot")], config_file=False)
cov.start()
import sqlglot  # noqa: E402

eager = sorted(name for name in sys.modules if name == "sqlglot" or name.startswith("sqlglot."))
failed = []
for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if info.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(info.name)
    except Exception as error:  # pragma: no cover - diagnostics only
        failed.append(f"{info.name}: {error!r}")
cov.stop()

data = cov.get_data()


def rel(path: str) -> str:
    return os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")


import_lines = {rel(f): sorted(data.lines(f) or []) for f in data.measured_files()}

from sqlglot.dialects.dialect import Dialect  # noqa: E402


def names(klass) -> list[str]:
    return [
        f"{k.__module__}:{k.__qualname__}"
        for k in klass.__mro__
        if (getattr(k, "__module__", "") or "").startswith("sqlglot")
    ]


closure = {}
stack = [Dialect]
seen = set()
while stack:
    klass = stack.pop()
    if klass in seen:
        continue
    seen.add(klass)
    stack.extend(klass.__subclasses__())
    parts = set(names(klass))
    for attr in ("tokenizer_class", "jsonpath_tokenizer_class", "parser_class", "generator_class"):
        component = getattr(klass, attr, None)
        if isinstance(component, type):
            parts.update(names(component))
    closure[f"{klass.__module__}:{klass.__qualname__}"] = sorted(parts)

json.dump(
    {"import_lines": import_lines, "eager": eager, "failed": failed, "dialects": closure},
    open(out, "w", encoding="utf-8"),
)
print(f"import pass: {len(import_lines)} files, {len(eager)} eager modules, "
      f"{len(closure)} dialects, {len(failed)} failed imports", file=sys.stderr)
for line in failed:
    print("  import failed:", line, file=sys.stderr)
