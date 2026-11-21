"""pytest plugin used only at image build time to record, per test:

* which lines of sqlglot/ and tests/ run (a sys.monitoring LINE tracer that
  sees each line once per test: after the first hit a location is disabled
  until the next test starts, so hot loops cost nothing),
* every file the test opens or directory it lists (sys.addaudithook),
* every sqlglot Dialect / Tokenizer / Parser / Generator / Expr subclass it
  instantiates (so class-level config edits can be mapped to their users).

Each test has three contexts "<nodeid>|setup", "|call", "|teardown";
collection-time activity is recorded under "collect|<test file>".
sqlglot's tests farm work out to spawned ProcessPoolExecutor workers, which
no tracer would see, so the executor is replaced by an inline one.

Output: <MAP_OUT>/trace-<worker>.marshal
"""

from __future__ import annotations

import marshal
import os
import sys

import pytest

ROOT = os.path.abspath(os.environ.get("SQLGLOT_ROOT", os.getcwd()))
OUT = os.path.abspath(os.environ.get("MAP_OUT", "map-out"))
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
TRACKED = (os.path.join(ROOT, "sqlglot") + os.sep, os.path.join(ROOT, "tests") + os.sep)
_IMPORT_FILES = ("importlib._bootstrap", "<frozen importlib")

_ctx_names: list[str] = []
_ctx_index: dict[str, int] = {}
_ctx = 0
_hits: set = set()  # (filename, line) seen in the current context
_lines: dict[str, dict[int, list[int]]] = {}
_opens: dict[int, set[str]] = {}
_types: dict[int, set[type]] = {}
_seen_types: set[type] = set()
_active = False
_rel_cache: dict[str, str | None] = {}
_mon = getattr(sys, "monitoring", None)
_TOOL = None


def _ctx_id(name: str) -> int:
    idx = _ctx_index.get(name)
    if idx is None:
        idx = _ctx_index[name] = len(_ctx_names)
        _ctx_names.append(name)
    return idx


def _rel_file(filename: str):
    rel = _rel_cache.get(filename, 0)
    if rel == 0:
        full = os.path.abspath(filename)
        rel = None
        if full.startswith(TRACKED):
            rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
        _rel_cache[filename] = rel
    return rel


def _flush() -> None:
    global _hits, _seen_types
    if _hits:
        hits, _hits = _hits, set()
        for filename, line in hits:
            rel = _rel_file(filename)
            if rel is not None:
                _lines.setdefault(rel, {}).setdefault(line, []).append(_ctx)
    if _seen_types:
        _types.setdefault(_ctx, set()).update(_seen_types)
        _seen_types = set()


def _set_ctx(name: str) -> None:
    global _ctx
    if not _active:
        return
    _flush()
    _ctx = _ctx_id(name)
    _mon.restart_events()


def _on_line(code, line, _DISABLE=getattr(sys, "monitoring", None) and sys.monitoring.DISABLE):
    try:
        _hits.add((code.co_filename, line))
    except AttributeError:  # interpreter shutdown
        pass
    return _DISABLE


def _rel(path) -> str | None:
    try:
        if isinstance(path, int):
            return None
        full = os.path.abspath(os.fsdecode(path))
    except Exception:
        return None
    if not full.startswith(ROOT + os.sep):
        return None
    return os.path.relpath(full, ROOT).replace(os.sep, "/")


def _audit(event: str, args) -> None:
    if not _active:
        return
    if event == "open":
        rel = _rel(args[0])
    elif event in ("os.listdir", "os.scandir", "glob.glob"):
        rel = _rel(args[0] if args and args[0] is not None else ".")
        if rel is not None:
            rel += "/"
    else:
        return
    if rel is None or rel.endswith(".pyc") or "__pycache__" in rel:
        return
    frame = sys._getframe(1)
    while frame is not None:  # the import system's reads are not test inputs
        if frame.f_code.co_filename.startswith(_IMPORT_FILES):
            return
        frame = frame.f_back
    _opens.setdefault(_ctx, set()).add(rel)


def _patch_init(cls) -> None:
    original = cls.__init__

    def __init__(self, *args, **kwargs):
        _seen_types.add(type(self))
        original(self, *args, **kwargs)

    __init__.__wrapped__ = original
    cls.__init__ = __init__


def _install_type_tracking() -> None:
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    for cls in (Dialect, Tokenizer, Parser, Generator, Expr):
        _patch_init(cls)


def _cross(value):
    """What a spawned worker would see: a pickle round trip (sqlglot pickles
    expressions through serde, which must be charged to the test too)."""
    import pickle

    return pickle.loads(pickle.dumps(value))


class InlineExecutor:
    """Stand-in for ProcessPoolExecutor that runs each task immediately in
    this process, so its lines, opens and types are charged to the test."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer is not None:
            initializer(*_cross(tuple(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        from concurrent.futures import Future

        future = Future()
        try:
            args, kwargs = _cross((args, kwargs))
            future.set_result(_cross(fn(*args, **kwargs)))
        except BaseException as error:  # noqa: BLE001 - mirror executor semantics
            future.set_exception(error)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_inline_executor() -> None:
    import concurrent.futures
    import concurrent.futures.process as process

    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    process.ProcessPoolExecutor = InlineExecutor


def pytest_configure(config):
    global _active, _TOOL, _ctx
    if not hasattr(config, "workerinput") and getattr(config.option, "numprocesses", None):
        return  # xdist controller: the workers record
    if _mon is None:
        raise RuntimeError("mapper_plugin needs Python 3.12+ (sys.monitoring)")
    _install_inline_executor()
    for tool in (_mon.COVERAGE_ID, 3, 4):
        if _mon.get_tool(tool) is None:
            _TOOL = tool
            break
    _mon.use_tool_id(_TOOL, "blast-radius-mapper")
    _mon.register_callback(_TOOL, _mon.events.LINE, _on_line)
    _active = True
    _ctx = _ctx_id("collect|")
    _mon.set_events(_TOOL, _mon.events.LINE)
    sys.addaudithook(_audit)
    _install_type_tracking()


@pytest.hookimpl(hookwrapper=True)
def pytest_collect_file(file_path, parent):
    previous = _ctx_names[_ctx] if _active else ""
    _set_ctx(f"collect|{_rel(str(file_path))}")
    yield
    _set_ctx(previous)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    previous = _ctx_names[_ctx] if _active else ""
    path = getattr(collector, "path", None)
    if path is not None and str(path).endswith(".py"):
        _set_ctx(f"collect|{_rel(str(path))}")
    yield
    _set_ctx(previous)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _set_ctx(f"{item.nodeid}|setup")
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _set_ctx(f"{item.nodeid}|call")
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _set_ctx(f"{item.nodeid}|teardown")
    yield
    _set_ctx("between|")


def _names(types: set[type]) -> list[str]:
    names = set()
    for typ in types:
        for klass in typ.__mro__:
            module = getattr(klass, "__module__", "") or ""
            if module.startswith("sqlglot"):
                names.add(f"{module}:{klass.__qualname__}")
    return sorted(names)


def pytest_unconfigure(config):
    global _active
    if not _active:
        return
    _set_ctx("end|")
    _active = False
    _mon.set_events(_TOOL, 0)
    _mon.free_tool_id(_TOOL)
    os.makedirs(OUT, exist_ok=True)
    result = {
        "contexts": _ctx_names,
        "lines": _lines,
        "opens": {i: sorted(paths) for i, paths in _opens.items()},
        "types": {i: _names(types) for i, types in _types.items()},
    }
    path = os.path.join(OUT, f"trace-{WORKER}.marshal")
    with open(path + ".tmp", "wb") as handle:
        marshal.dump(result, handle)
    os.replace(path + ".tmp", path)
