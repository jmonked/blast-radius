"""Blast Radius selector.

For every /data/commits/<id>.patch print one JSON line naming the tests that
could fail and how to split them over up to 8 workers.

The decision uses a map built at image build time from the pinned sqlglot
commit (see tools/): per-test line coverage, files each test opens, sqlglot
classes each test instantiates, lines that run at import time, and an index
of every module/class-level statement.  A patch is applied to the original
source in memory, only the smallest enclosing unit is re-parsed, and the
edit is classified:

* no AST change (comments, whitespace)          -> nothing
* change inside a function / lambda body         -> tests covering the
  innermost changed statement
* change to module / class level code            -> tests that use the class
  (instantiated it or a dialect built from it) or reference the names bound
* syntax error / import breakage                 -> everything importing it
* data files (fixtures)                          -> tests that opened them

Anything the selector cannot reason about falls back to running more tests.
Logs go to stderr only.
"""

from __future__ import annotations

import ast
import copy
import json
import marshal
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
HERE = Path(__file__).resolve().parent
MAP_PATH = Path(os.environ.get("BR_MAP", HERE / "map.marshal"))
SRC = Path(os.environ.get("BR_SQLGLOT", "/opt/sqlglot"))
MAX_WORKERS = 8
WORKER_COST_MS = 2000
NAME_HIT_LIMIT = 4000
MAX_DEPTH = 3

sys.path.insert(0, str(HERE))


def log(*args) -> None:
    print(*args, file=sys.stderr)


# --------------------------------------------------------------------------- patch parsing
class FileDiff:
    __slots__ = ("old", "new", "edits")

    def __init__(self, old, new):
        self.old = old
        self.new = new
        self.edits = []  # (a, b, new_lines): old lines [a, b) replaced by new_lines


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip(path: str):
    path = path.strip().split("\t")[0]
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_patch(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            parts = line.split(" ")
            old = parts[2][2:] if len(parts) > 3 else None
            new = parts[3][2:] if len(parts) > 3 else None
            current = FileDiff(old, new)
            files.append(current)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            if current is None or current.edits:
                current = FileDiff(None, None)
                files.append(current)
            current.old = _strip(line[4:])
            current.new = _strip(lines[i + 1][4:])
            i += 2
            continue
        if line.startswith("deleted file mode") and current is not None:
            current.new = None
        elif line.startswith("new file mode") and current is not None:
            current.old = None
        elif line.startswith("rename from ") and current is not None:
            current.old = line[len("rename from "):]
        elif line.startswith("rename to ") and current is not None:
            current.new = line[len("rename to "):]
        m = _HUNK.match(line)
        if m and current is not None:
            old_line = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            if old_count == 0:
                old_line += 1  # "-k,0" means insertion after line k
            i += 1
            removed_left, added_left = old_count, new_count
            edit_a = None
            edit_new: list[str] = []
            edit_removed = 0
            while i < n and (removed_left > 0 or added_left > 0):
                body = lines[i]
                tag = body[:1]
                if tag == "\\":
                    i += 1
                    continue
                if tag == "-":
                    if edit_a is None:
                        edit_a = old_line
                    edit_removed += 1
                    old_line += 1
                    removed_left -= 1
                elif tag == "+":
                    if edit_a is None:
                        edit_a = old_line
                    edit_new.append(body[1:] + "\n")
                    added_left -= 1
                else:  # context (an empty line is a context line of "")
                    if edit_a is not None:
                        current.edits.append((edit_a, edit_a + edit_removed, edit_new))
                        edit_a, edit_new, edit_removed = None, [], 0
                    old_line += 1
                    removed_left -= 1
                    added_left -= 1
                i += 1
            if edit_a is not None:
                current.edits.append((edit_a, edit_a + edit_removed, edit_new))
            continue
        i += 1
    return files


# --------------------------------------------------------------------------- helpers
def bits_to_indices(value: int) -> list[int]:
    out = []
    s = bin(value)[:1:-1]
    find = s.find
    i = find("1")
    while i != -1:
        out.append(i)
        i = find("1", i + 1)
    return out


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def parse_snippet(text: str):
    """Parse a statement-level snippet that may be indented. Returns (tree, offset)."""
    first = next((l for l in text.splitlines() if l.strip()), "")
    if first[:1] in (" ", "\t"):
        return ast.parse("if 1:\n" + text), 1
    return ast.parse(text), 0


def snippet_body(tree, offset):
    return tree.body[0].body if offset else tree.body


def bound_names(node) -> set[str]:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Store):
            names.add(sub.attr)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(sub.name)
    return names


class _MaskLambdas(ast.NodeTransformer):
    def visit_Lambda(self, node):
        return ast.Constant(value="<lambda>")


def _node_start(node) -> int:
    decorators = getattr(node, "decorator_list", None) or []
    return min([node.lineno] + [d.lineno for d in decorators])


_BLOCK_FIELDS = ("body", "orelse", "finalbody")
_HEADER_ONLY = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)


def _blocks(stmt):
    for field in _BLOCK_FIELDS:
        block = getattr(stmt, field, None)
        if block and isinstance(block, list) and isinstance(block[0], ast.stmt):
            yield block
    for handler in getattr(stmt, "handlers", None) or []:
        yield handler.body
    for case in getattr(stmt, "cases", None) or []:
        yield case.body


def relevant_ranges(body, lo: int, hi: int):
    """Line ranges (snippet coordinates) whose execution signals that the
    statements covering lines lo..hi run."""
    overlapping = [s for s in body if _node_start(s) <= hi and s.end_lineno >= lo]
    if len(overlapping) == 1:
        stmt = overlapping[0]
        start = _node_start(stmt)
        if start <= lo and hi <= stmt.end_lineno:
            for block in _blocks(stmt):
                if block[0].lineno <= lo and hi <= block[-1].end_lineno:
                    inner = relevant_ranges(block, lo, hi)
                    if inner:
                        return inner
            if isinstance(stmt, _HEADER_ONLY):
                first_block = min(b[0].lineno for b in _blocks(stmt))
                if hi < first_block:
                    return [(start, first_block - 1)]
            return [(start, stmt.end_lineno)]
    return [(_node_start(s), s.end_lineno) for s in overlapping]


# --------------------------------------------------------------------------- selector
class Selector:
    def __init__(self, tests_json: dict, map_data: dict | None):
        self.tests = tests_json["tests"]
        self.dur = {t["number"]: int((t["seconds"] * 1000) + 0.5) for t in self.tests}
        self.all_numbers = [t["number"] for t in self.tests]
        self.by_file: dict[str, list[int]] = {}
        for t in self.tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        self.map = map_data
        self.src_cache: dict[str, list[str]] = {}
        self.text_cache: dict[str, str] = {}
        self.dialect_inv = None
        if map_data is None:
            return
        number_by_id = {t["id"]: t["number"] for t in self.tests}
        self.bit_number = [number_by_id.get(nodeid) for nodeid in map_data["tests"]]
        known = {n for n in self.bit_number if n is not None}
        # Tests absent from the map are always run.
        self.always = [n for n in self.all_numbers if n not in known]
        if self.always:
            log(f"{len(self.always)} tests are not in the map; they run on every commit")
        self.ALL = (1 << len(map_data["tests"])) - 1
        self.lines = map_data["lines"]
        self.units = map_data["units"]
        self.import_lines = {k: set(v) for k, v in map_data["import_lines"].items()}
        self.eager = set(map_data["eager"])
        self.file_bit_cache: dict[str, int] = {}

    # ----------------------------------------------------------- sources
    def source_lines(self, path: str) -> list[str]:
        cached = self.src_cache.get(path)
        if cached is None:
            try:
                text = (SRC / path).read_text(encoding="utf-8")
            except OSError:
                text = ""
            cached = text.splitlines(keepends=True)
            self.src_cache[path] = cached
        return cached

    def source_text(self, path: str) -> str:
        cached = self.text_cache.get(path)
        if cached is None:
            cached = "".join(self.source_lines(path))
            self.text_cache[path] = cached
        return cached

    # ----------------------------------------------------------- bitset queries
    def cov_range(self, path: str, start: int, end: int) -> int:
        per_file = self.lines.get(path)
        if not per_file:
            return 0
        value = 0
        if end - start > 4 * len(per_file):
            for lineno, bits in per_file.items():
                if start <= lineno <= end:
                    value |= bits
            return value
        get = per_file.get
        for lineno in range(start, end + 1):
            bits = get(lineno)
            if bits:
                value |= bits
        return value

    def test_file_bits(self, test_file: str) -> int:
        cached = self.file_bit_cache.get(test_file)
        if cached is None:
            cached = 0
            prefix = test_file + "::"
            for i, nodeid in enumerate(self.map["tests"]):
                if nodeid.startswith(prefix):
                    cached |= 1 << i
            self.file_bit_cache[test_file] = cached
        return cached

    def module_name(self, path: str) -> str:
        mod = path[:-3].replace("/", ".")
        return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod

    def uses_class(self, cname: str) -> int:
        """Tests that instantiated cname (or a subclass), or used a dialect built from it."""
        types = self.map["types"]
        value = types.get(cname, 0)
        if self.dialect_inv is None:
            inv: dict[str, list[str]] = {}
            for dialect, parts in self.map["dialects"].items():
                for part in parts:
                    inv.setdefault(part, []).append(dialect)
            self.dialect_inv = inv
        for dialect in self.dialect_inv.get(cname, ()):
            value |= types.get(dialect, 0)
        return value

    def class_tracked(self, cname: str) -> bool:
        if self.dialect_inv is None:
            self.uses_class("")
        return cname in self.map["types"] or cname in self.dialect_inv

    def innermost_unit(self, path: str, lineno: int, kinds="fsc"):
        best = None
        for unit in self.units.get(path, ()):
            if unit[0] > lineno:
                break
            if unit[1] >= lineno and unit[2] in kinds:
                best = unit
        return best

    def unit_has_import_lines(self, path: str, start: int, end: int) -> bool:
        imp = self.import_lines.get(path)
        if not imp:
            return False
        return any(start <= l <= end for l in imp)

    # ----------------------------------------------------------- rules
    def tests_of_test_module(self, path: str, seen=None) -> int:
        """All tests in a test module plus test modules that import it."""
        seen = seen if seen is not None else set()
        if path in seen:
            return 0
        seen.add(path)
        value = self.test_file_bits(path)
        for importer in self.map["importers"].get(path, ()):
            if importer.startswith("tests/"):
                value |= self.tests_of_test_module(importer, seen)
        return value

    def import_failure(self, path: str, seen=None) -> int:
        """Tests that break if `path` cannot be imported."""
        if path in self.eager or path.endswith("__init__.py") and path.startswith("sqlglot/"):
            return self.ALL
        if path.startswith("tests/"):
            return self.tests_of_test_module(path)
        seen = seen if seen is not None else set()
        if path in seen:
            return 0
        seen.add(path)
        mod = self.module_name(path)
        value = self.map["file_any"].get(path, 0)
        for unit in self.units.get(path, ()):
            if unit[2] == "c":
                value |= self.uses_class(f"{mod}:{unit[3]}")
        for importer in self.map["importers"].get(path, ()):
            value |= self.import_failure(importer, seen)
        return value

    def class_change(self, path: str, qual: str, names: set[str], depth: int = 0) -> int:
        if path.startswith("tests/"):
            return self.tests_of_test_module(path)
        mod = self.module_name(path)
        value = 0
        parts = qual.split(".")
        untracked = []
        for i in range(len(parts), 0, -1):
            cname = f"{mod}:{'.'.join(parts[:i])}"
            value |= self.uses_class(cname)
            if not self.class_tracked(cname):
                untracked.append(parts[i - 1])
        span = self.innermost_class_span(path, qual)
        if span:
            value |= self.cov_range(path, span[0], span[1])
        if untracked:
            value |= self.name_users(set(untracked) | names, depth + 1)
        return value

    def innermost_class_span(self, path: str, qual: str):
        for unit in self.units.get(path, ()):
            if unit[2] == "c" and unit[3] == qual:
                return unit[0], unit[1]
        return None

    def module_change(self, path: str, names: set[str], depth: int = 0) -> int:
        if path.startswith("tests/"):
            return self.tests_of_test_module(path)
        if not names:
            return self.import_failure(path)
        return self.name_users(names, depth + 1)

    def name_users(self, names: set[str], depth: int) -> int:
        """Tests that use any of `names`, found by scanning sqlglot and test sources."""
        names = {n for n in names if n and n != "_"}
        if not names:
            return 0
        if depth > MAX_DEPTH:
            return self.ALL
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in sorted(names)) + r")\b")
        value = 0
        hits = 0
        follow: dict[str, set[str]] = {}
        for path in self.units:
            text = self.source_text(path)
            if not pattern.search(text):
                continue
            line_starts = None
            for match in pattern.finditer(text):
                hits += 1
                if hits > NAME_HIT_LIMIT:
                    return self.ALL
                if line_starts is None:
                    line_starts = [0]
                    pos = text.find("\n")
                    while pos != -1:
                        line_starts.append(pos + 1)
                        pos = text.find("\n", pos + 1)
                lineno = _bisect(line_starts, match.start())
                unit = self.innermost_unit(path, lineno)
                if unit is None:
                    continue
                start, end, kind, qual = unit[0], unit[1], unit[2], unit[3]
                if path.startswith("tests/"):
                    if kind == "f":
                        value |= self.cov_range(path, start, end)
                    else:
                        value |= self.tests_of_test_module(path)
                    continue
                if kind == "f":
                    value |= self.cov_range(path, start, end)
                    if self.unit_has_import_lines(path, start, end):
                        follow.setdefault(path, set()).add(qual.split(".")[-1])
                elif kind == "c" or (kind == "s" and qual):
                    value |= self.class_change(path, qual, set(), depth)
                else:  # module level statement: whatever it binds is affected too
                    try:
                        tree, _ = parse_snippet("".join(self.source_lines(path)[start - 1 : end]))
                        follow.setdefault(path, set()).update(bound_names(tree) - names)
                    except SyntaxError:
                        value |= self.import_failure(path)
        extra = set()
        for found in follow.values():
            extra |= found
        if extra:
            value |= self.name_users(extra, depth + 1)
        return value

    # ----------------------------------------------------------- per file analysis
    def analyze(self, diff: FileDiff) -> int:
        path = diff.new or diff.old
        if diff.old is None:  # new file
            if path.endswith(".py"):
                log(f"  new python file {path}: nothing imports it")
                return 0
            return self.data_file(path)
        if diff.new is None or diff.new != diff.old:  # deleted / renamed
            if diff.old.endswith(".py") and diff.old in self.units:
                return self.import_failure(diff.old)
            return self.data_file(diff.old)
        if path.endswith(".py") and path in self.units:
            return self.python_file(path, diff.edits)
        return self.data_file(path)

    def data_file(self, path: str) -> int:
        opens = self.map["opens"]
        value = opens.get(path, 0)
        parent = path
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in ("tests", "tests/fixtures", "sqlglot"):
                break
            value |= opens.get(parent + "/", 0)
        if not value and path.startswith("tests/"):
            base = path.rsplit("/", 1)[-1]
            stem = base.rsplit(".", 1)[0]
            pattern = re.compile(re.escape(stem))
            for test_file in self.units:
                if test_file.startswith("tests/") and pattern.search(self.source_text(test_file)):
                    value |= self.tests_of_test_module(test_file)
        log(f"  data file {path}: {bin(value).count('1')} tests")
        return value

    def python_file(self, path: str, edits) -> int:
        lines = self.source_lines(path)
        groups: dict = {}
        for edit in edits:
            unit = self.containing_unit(path, lines, edit)
            groups.setdefault(unit, []).append(edit)
        value = 0
        for unit, unit_edits in groups.items():
            value |= self.unit_change(path, lines, unit, unit_edits)
        return value

    def containing_unit(self, path: str, lines: list[str], edit):
        a, b, new_lines = edit
        best = None
        new_indent = min((indent_of(l) for l in new_lines if l.strip()), default=None)
        for unit in self.units.get(path, ()):
            start, end, kind = unit[0], unit[1], unit[2]
            if start > max(a, b - 1):
                break
            if kind == "l":
                continue
            if b > a:
                inside = start <= a and b - 1 <= end
            else:
                inside = start < a <= end
                if not inside and a > end and kind in "fc" and new_indent is not None:
                    gap = lines[end : a - 1]
                    inside = new_indent > unit[4] and all(is_blank_or_comment(l) for l in gap)
            if inside:
                best = unit
        return best

    def apply(self, lines: list[str], start: int, end: int, edits) -> str:
        out = []
        ordered = sorted(edits)
        k = 0
        lineno = start
        while lineno <= end:
            while k < len(ordered) and ordered[k][0] == lineno:
                a, b, new_lines = ordered[k]
                out.extend(new_lines)
                k += 1
                if b > a:
                    lineno = b - 1
                    break
            else:
                out.append(lines[lineno - 1] if lineno - 1 < len(lines) else "")
            lineno += 1
        while k < len(ordered):  # appended after the unit
            out.extend(ordered[k][2])
            k += 1
        return "".join(out)

    def unit_change(self, path: str, lines: list[str], unit, edits) -> int:
        if unit is None or unit[2] == "c":
            return self.gap_change(path, lines, unit, edits)
        start, end, kind, qual = unit[0], unit[1], unit[2], unit[3]
        old_text = "".join(lines[start - 1 : end])
        new_text = self.apply(lines, start, end, edits)
        try:
            old_tree, off = parse_snippet(old_text)
        except SyntaxError:
            log(f"  cannot parse original unit {path}:{start}; whole module")
            return self.import_failure(path)
        try:
            new_tree, new_off = parse_snippet(new_text)
        except SyntaxError:
            log(f"  syntax error in {path}:{start}")
            return self.import_failure(path)
        if ast.dump(old_tree) == ast.dump(new_tree):
            log(f"  {path}:{start} no semantic change")
            return 0
        old_body = snippet_body(old_tree, off)
        new_body = snippet_body(new_tree, new_off)
        base = start - 1 - off  # snippet line + base = file line
        if kind == "f":
            return self.function_change(path, unit, old_body, new_body, edits, base)
        return self.statement_change(path, unit, old_body, new_body, base)

    def function_change(self, path, unit, old_body, new_body, edits, base) -> int:
        start, end, _, qual = unit[0], unit[1], unit[2], unit[3]
        if len(old_body) != 1 or len(new_body) != 1:
            return self.class_or_module_change(path, qual, {qual.split(".")[-1]})
        old_f, new_f = old_body[0], new_body[0]
        if not isinstance(new_f, type(old_f)):
            return self.class_or_module_change(path, qual, {qual.split(".")[-1]})
        value = 0
        header_old, header_new = _header(old_f), _header(new_f)
        body_lo = old_f.body[0].lineno + base
        whole_body = False
        if header_old != header_new:
            whole_body = True
            if old_f.name != new_f.name:
                value |= self.class_or_module_change(path, qual, {old_f.name})
        ranges = []
        for a, b, _ in edits:
            lo, hi = (a, b - 1) if b > a else (a - 1, a)
            if lo < body_lo:
                if b > a or a <= body_lo - 1:
                    # touches the def line, decorators or signature
                    whole_body = True
                    if b == a:
                        ranges += relevant_ranges(old_f.body, body_lo - base, body_lo - base)
                    continue
            if b > a:
                ranges += relevant_ranges(old_f.body, lo - base, hi - base)
            else:
                for probe in (a - 1, a):
                    if body_lo <= probe <= end:
                        ranges += relevant_ranges(old_f.body, probe - base, probe - base)
        if whole_body:
            value |= self.cov_range(path, body_lo, end)
        for lo, hi in ranges:
            value |= self.cov_range(path, lo + base, hi + base)
        touched = [(lo + base, hi + base) for lo, hi in ranges] or [(start, end)]
        if whole_body:
            touched.append((start, end))
        if any(self.unit_has_import_lines(path, lo, hi) for lo, hi in touched if lo >= body_lo) or (
            whole_body and self.unit_has_import_lines(path, body_lo, end)
        ):
            # this function runs while modules are imported (metaclass, class-body helper)
            log(f"  {path}:{qual} runs at import time; following its users")
            value |= self.name_users({old_f.name}, 1)
        if path.startswith("tests/") and whole_body:
            value |= self.test_file_bits(path) if not qual.count(".") else 0
        return value

    def statement_change(self, path, unit, old_body, new_body, base) -> int:
        start, end, _, qual = unit[0], unit[1], unit[2], unit[3]
        masker = _MaskLambdas()
        old_masked = [ast.dump(masker.visit(copy.deepcopy(s))) for s in old_body]
        new_masked = [ast.dump(masker.visit(copy.deepcopy(s))) for s in new_body]
        if old_masked == new_masked:
            old_l = [n for s in old_body for n in ast.walk(s) if isinstance(n, ast.Lambda)]
            new_l = [n for s in new_body for n in ast.walk(s) if isinstance(n, ast.Lambda)]
            if len(old_l) == len(new_l):
                value = 0
                for o, nw in zip(old_l, new_l):
                    if ast.dump(o) != ast.dump(nw):
                        value |= self.cov_range(path, o.lineno + base, o.end_lineno + base)
                log(f"  {path}:{start} lambda body change")
                return value
        names = set()
        for s in old_body + new_body:
            names |= bound_names(s)
        if any(isinstance(s, (ast.Import, ast.ImportFrom)) for s in old_body + new_body):
            # a broken import makes the whole module unimportable
            return self.import_failure(path) | self.class_or_module_change(path, qual, names)
        log(f"  {path}:{start} import-time change in {qual or '<module>'} binding {sorted(names)[:5]}")
        return self.class_or_module_change(path, qual, names)

    def class_or_module_change(self, path, qual, names) -> int:
        container = qual.rsplit(".", 1)[0] if "." in qual else ""
        unit = self.innermost_class_span(path, container) if container else None
        if container and unit:
            return self.class_change(path, container, names)
        if qual and self.innermost_class_span(path, qual):
            return self.class_change(path, qual, names)
        return self.module_change(path, names)

    def gap_change(self, path, lines, unit, edits) -> int:
        """Edits between statements of a module or class body (or in a class header)."""
        value = 0
        for a, b, new_lines in edits:
            removed = lines[a - 1 : b - 1]
            if unit is not None:
                members = [u for u in self.units[path] if u[0] > unit[0] and u[1] <= unit[1]]
                header_end = (members[0][0] - 1) if members else unit[1]
                if a <= header_end:
                    log(f"  {path}:{a} class header change {unit[3]}")
                    value |= self.class_change(path, unit[3], {unit[3].split(".")[-1]})
                    continue
            if all(is_blank_or_comment(l) for l in removed + new_lines):
                continue
            try:
                tree, _ = parse_snippet("".join(new_lines) or "pass\n")
            except SyntaxError:
                log(f"  {path}:{a} unparsable gap edit")
                value |= self.import_failure(path)
                continue
            names = bound_names(tree)
            if unit is not None:
                value |= self.class_change(path, unit[3], names)
            else:
                value |= self.module_change(path, names)
        return value

    # ----------------------------------------------------------- commit
    def select(self, patch_text: str) -> list[int]:
        if self.map is None:
            return list(self.all_numbers)
        try:
            diffs = parse_patch(patch_text)
        except Exception as error:  # pragma: no cover - defensive
            log(f"  patch parse failed: {error!r}")
            return list(self.all_numbers)
        if not diffs:
            return []
        value = 0
        for diff in diffs:
            try:
                value |= self.analyze(diff)
            except Exception as error:  # pragma: no cover - defensive
                log(f"  analysis failed for {diff.new or diff.old}: {error!r}; running everything")
                return list(self.all_numbers)
            if value == self.ALL:
                break
        numbers = {self.bit_number[i] for i in bits_to_indices(value)}
        numbers.discard(None)
        numbers.update(self.always)
        return sorted(numbers)


def _header(func) -> str:
    parts = [func.name, ast.dump(func.args)] if hasattr(func, "args") else [ast.dump(func)]
    parts += [ast.dump(d) for d in getattr(func, "decorator_list", ())]
    returns = getattr(func, "returns", None)
    parts.append(ast.dump(returns) if returns is not None else "")
    return "|".join(parts)


def _bisect(starts: list[int], pos: int) -> int:
    lo, hi = 0, len(starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if starts[mid] <= pos:
            lo = mid + 1
        else:
            hi = mid
    return lo  # 1-based line number


# --------------------------------------------------------------------------- packing
def split_workers(numbers: list[int], dur: dict[int, int]) -> list[list[int]]:
    if not numbers:
        return []
    try:
        from pack import pack

        return pack([(n, dur[n]) for n in numbers], MAX_WORKERS, WORKER_COST_MS)
    except ImportError:
        pass
    best = None
    items = sorted(numbers, key=lambda n: (-dur[n], n))
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        loads = [0] * k
        workers: list[list[int]] = [[] for _ in range(k)]
        for n in items:
            i = loads.index(min(loads))
            workers[i].append(n)
            loads[i] += dur[n]
        cost = max(loads) + WORKER_COST_MS * k
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best[1]


def main() -> None:
    tests_json = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))
    try:
        with open(MAP_PATH, "rb") as handle:
            map_data = marshal.load(handle)
    except Exception as error:
        log(f"map unavailable ({error!r}); running every test")
        map_data = None
    selector = Selector(tests_json, map_data)
    out = []
    for patch in sorted((DATA / "commits").glob("*.patch")):
        commit = patch.stem
        log(f"{commit}:")
        try:
            numbers = selector.select(patch.read_text(encoding="utf-8", errors="replace"))
        except Exception as error:  # pragma: no cover - defensive
            log(f"  failed: {error!r}; running everything")
            numbers = list(selector.all_numbers)
        workers = split_workers(numbers, selector.dur)
        log(f"  -> {len(numbers)} tests on {len(workers)} workers")
        out.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


if __name__ == "__main__":
    main()
