"""Build-time: merge the mapper outputs into one compact marshal file.

usage: python build_map.py <sqlglot_root> <mapper_out_dir> <out.marshal>

Bitsets are Python ints whose bit i is test i of map["tests"].
"""

from __future__ import annotations

import ast
import glob
import json
import marshal
import os
import re
import sys

root = os.path.abspath(sys.argv[1])
mdir = os.path.abspath(sys.argv[2])
out = sys.argv[3]

RUN_PHASES = ("setup", "call", "teardown")
_param = re.compile(r"\[.*$")


def rel(path: str) -> str:
    return os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")


def split_ctx(ctx: str):
    nodeid, _, phase = ctx.rpartition("|")
    return nodeid, phase


# ---------------------------------------------------------------- test index
trace_files = sorted(glob.glob(os.path.join(mdir, "trace-*.marshal")))
if not trace_files:
    sys.exit("no mapper output found")
traces = [marshal.load(open(p, "rb")) for p in trace_files]
nodeids = set()
for trace in traces:
    for ctx in trace["contexts"]:
        nodeid, phase = split_ctx(ctx)
        if phase in RUN_PHASES and nodeid:
            nodeids.add(nodeid)

tests = sorted(nodeids)
bit = {nodeid: 1 << i for i, nodeid in enumerate(tests)}
group_bits: dict[str, int] = {}
file_bits: dict[str, int] = {}
for nodeid, b in bit.items():
    base = _param.sub("", nodeid)
    group = base.rsplit("::", 1)[0]
    group_bits[group] = group_bits.get(group, 0) | b
    tfile = base.split("::", 1)[0]
    file_bits[tfile] = file_bits.get(tfile, 0) | b

_ctx_cache: dict[str, int] = {}


def ctx_bits(ctx: str) -> int:
    """Tests a recorded context stands for (0 for contexts outside tests)."""
    cached = _ctx_cache.get(ctx)
    if cached is not None:
        return cached
    nodeid, phase = split_ctx(ctx)
    value = 0
    if phase == "call":
        value = bit.get(nodeid, 0)
    elif phase in ("setup", "teardown"):
        # setUpClass / class fixtures run inside the first test's setup and
        # the last test's teardown: charge them to the whole class (or module).
        value = group_bits.get(_param.sub("", nodeid).rsplit("::", 1)[0], 0) | bit.get(nodeid, 0)
    elif nodeid == "collect" and phase:
        value = file_bits.get(phase, 0)
    _ctx_cache[ctx] = value
    return value


_intern: dict[int, int] = {}


def intern(value: int) -> int:
    return _intern.setdefault(value, value)


# ---------------------------------------------------------------- coverage
lines: dict[str, dict[int, int]] = {}
for trace in traces:
    ctx_values = [ctx_bits(ctx) for ctx in trace["contexts"]]
    for frel, per_line in trace["lines"].items():
        per_file = lines.setdefault(frel, {})
        for lineno, ctx_ids in per_line.items():
            value = 0
            for ctx_id in ctx_ids:
                value |= ctx_values[ctx_id]
            if value:
                per_file[lineno] = per_file.get(lineno, 0) | value
for frel, per_file in lines.items():
    for lineno in per_file:
        per_file[lineno] = intern(per_file[lineno])
file_any = {}
for frel, per_file in lines.items():
    value = 0
    for v in per_file.values():
        value |= v
    file_any[frel] = intern(value)

# ---------------------------------------------------------------- opens / types
opens: dict[str, int] = {}
types: dict[str, int] = {}
for trace in traces:
    names = trace["contexts"]
    for section, target in (("opens", opens), ("types", types)):
        for ctx_id, items in trace[section].items():
            value = ctx_bits(names[ctx_id])
            if not value:
                continue
            for item in items:
                if section == "opens" and ("__pycache__" in item or item.startswith(".")):
                    continue
                target[item] = target.get(item, 0) | value
opens = {k: intern(v) for k, v in opens.items()}
types = {k: intern(v) for k, v in types.items()}

# ---------------------------------------------------------------- import pass
imp = json.load(open(os.path.join(mdir, "import.json"), encoding="utf-8"))

# ---------------------------------------------------------------- units index
py_files = sorted(
    rel(p)
    for d in ("sqlglot", "tests")
    for p in glob.glob(os.path.join(root, d, "**", "*.py"), recursive=True)
)


def start_of(node) -> int:
    decorators = getattr(node, "decorator_list", None) or []
    return min([node.lineno] + [d.lineno for d in decorators])


def collect_units(body, prefix, units):
    for node in body:
        start, end = start_of(node), node.end_lineno
        indent = node.col_offset
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.append((start, end, "f", prefix + node.name, indent))
        elif isinstance(node, ast.ClassDef):
            units.append((start, end, "c", prefix + node.name, indent))
            collect_units(node.body, prefix + node.name + ".", units)
        else:
            units.append((start, end, "s", prefix.rstrip("."), indent))
            for sub in ast.walk(node):
                if isinstance(sub, ast.Lambda):
                    units.append((sub.lineno, sub.end_lineno, "l", prefix.rstrip("."), indent))


units: dict[str, list] = {}
module_of_file: dict[str, str] = {}
file_of_module: dict[str, str] = {}
trees: dict[str, ast.Module] = {}
for frel in py_files:
    try:
        source = open(os.path.join(root, frel), encoding="utf-8").read()
        tree = ast.parse(source)
    except Exception as error:
        print(f"skip {frel}: {error!r}", file=sys.stderr)
        continue
    trees[frel] = tree
    found: list = []
    collect_units(tree.body, "", found)
    found.sort(key=lambda u: (u[0], -u[1]))
    units[frel] = found
    mod = frel[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    module_of_file[frel] = mod
    file_of_module[mod] = frel


# ---------------------------------------------------------------- import graph
def is_type_checking(test) -> bool:
    return "TYPE_CHECKING" in ast.unparse(test)


def top_level_imports(tree, package: str):
    found = []

    def visit(body):
        for node in body:
            if isinstance(node, ast.Import):
                found.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".")
                    parts = parts[: len(parts) - node.level + 1]
                    base = ".".join(parts + ([base] if base else []))
                found.append(base)
                found.extend(f"{base}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.If):
                if not is_type_checking(node.test):
                    visit(node.body)
                    visit(node.orelse)
            elif isinstance(node, ast.Try):
                visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)

    visit(tree.body)
    return found


importers: dict[str, list[str]] = {}
for frel, tree in trees.items():
    mod = module_of_file[frel]
    package = mod if frel.endswith("__init__.py") else mod.rpartition(".")[0]
    targets = set()
    for name in top_level_imports(tree, package):
        # importing a.b.c also runs a/__init__ and a/b/__init__
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            target = file_of_module.get(".".join(parts[:i]))
            if target and target != frel:
                targets.add(target)
    for target in targets:
        importers.setdefault(target, []).append(frel)

result = {
    "tests": tests,
    "lines": lines,
    "file_any": file_any,
    "opens": opens,
    "types": types,
    "import_lines": imp["import_lines"],
    "eager": [file_of_module[m] for m in imp["eager"] if m in file_of_module],
    "dialects": imp["dialects"],
    "units": units,
    "importers": importers,
}
with open(out, "wb") as handle:
    marshal.dump(result, handle)
os.chmod(out, 0o644)
print(
    f"map: {len(tests)} tests, {len(lines)} covered files, {len(_intern)} distinct sets, "
    f"{len(opens)} opened paths, {len(types)} classes, {os.path.getsize(out) / 1e6:.1f} MB",
    file=sys.stderr,
)
