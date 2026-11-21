# Blast Radius — coverage-guided test selector for sqlglot

Submission for the RWX Blast Radius track of the Builders buildathon
(<https://www.joinbuilders.org/buildathon/tracks/blast-radius>). The exact
scoring rules are in [SCORING.md](SCORING.md), and the organisers' original
starter README is summarised at the end.

For each patch in `/data/commits/*.patch` (a diff against sqlglot
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`), `selector.py` prints the tests
that could fail and splits them across up to 8 workers.

## How it works

### Build time (`Dockerfile`, `tools/`)

1. Installs `requirements.lock` (the labelling environment) and downloads
   sqlglot at the pinned commit to `/opt/sqlglot`.
2. Runs the whole sqlglot suite once with `tools/mapper_plugin.py` loaded
   (`pytest -n 4 --dist loadfile`). For each test, the plugin records:
   * **Lines executed** in `sqlglot/` and `tests/`. A `sys.monitoring` LINE
     tracer counts each line once per test and then disables that
     location until the next test starts, so overhead is about 20%.
     coverage.py with per-test contexts was more than 5× slower.
   * **Files opened and directories listed** (`sys.addaudithook`). This is
     how fixture `.sql` files map to the tests that read them.
   * **sqlglot classes instantiated**, recorded on Dialect, Tokenizer,
     Parser, Generator and Expr subclasses together with their MRO.
     Class-level config edits map to the tests that use that class.
   * `ProcessPoolExecutor` is swapped for an inline executor during mapping.
     The optimizer and executor tests otherwise run their work in spawned
     processes, which no tracer sees.
   * `setUpClass` and other class fixtures run inside the first test's setup,
     so setup and teardown lines are charged to the whole class.
3. `tools/import_pass.py` records which lines run at import time, which
   modules `import sqlglot` loads eagerly, and each Dialect's component
   classes.
4. `tools/build_map.py` merges everything into `/app/map.marshal`. Test
   sets are stored as int bitsets, and the file is about 3 MB.

### Run time (`selector.py`)

The patch is applied in memory to the original source. The selector
re-parses only the smallest enclosing unit (a function, or a module- or
class-level statement), using a unit index precomputed at build time, so
it never runs `ast.parse` on a whole 10k-line file. Each change is then
classified:

| Change | Tests selected |
| --- | --- |
| AST unchanged (comments, whitespace) | none |
| Inside a function body | tests that executed the innermost changed statement (or the neighbouring statements, for an insertion) |
| Function signature or decorators | tests that executed any line of the function |
| Lambda body inside a class-level table | tests that executed the lambda |
| Module- or class-level statement (runs at import) | tests that instantiated the class, or any Dialect built from it (MRO), plus tests that execute code referencing the names it binds |
| Function body that runs at import time (metaclass, class-body helper) | the above, applied to the function's users |
| Changed import, syntax error, deleted module | every test that imports the module (all tests for eagerly imported modules) |
| Test module, module- or class-level | the whole test file and the test files that import it |
| Data files (fixtures) | tests that opened the file or listed its directory |

Anything it cannot reason about falls back to selecting more. Tests that
are missing from the map always run.

### Worker packing (`pack.py`)

For each k from 1 to 8, the packer runs LPT and then a move/swap local
search, and picks the k that minimises `max_load + 2 s × k`. Small selections
therefore stay on one worker.

## Local verification without Docker

```sh
python -m venv venv && venv/bin/pip install -r requirements.lock
git clone https://github.com/tobymao/sqlglot && git -C sqlglot checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
cd sqlglot && SKIP_INTEGRATION=0 MAP_OUT=../mapout SQLGLOT_ROOT=$PWD PYTHONPATH=../blast-radius/tools:. \
  ../venv/bin/python -m pytest -p mapper_plugin -p no:cacheprovider -q -n 2 --dist loadfile tests/ ; cd ..
venv/bin/python blast-radius/tools/import_pass.py sqlglot mapout/import.json
venv/bin/python blast-radius/tools/build_map.py sqlglot mapout map.marshal
venv/bin/python blast-radius/tools/local_eval.py --map map.marshal --sqlglot sqlglot
```

`make sample` from the starter kit still works on a machine with Docker.

## Starter kit

`score.py`, `sample/`, `SCORING.md`, `Makefile` and `requirements.lock`
come unchanged from the organisers' `blast-radius-sample.tar.gz`.
