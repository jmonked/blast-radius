"""Run selector.py on the sample set without Docker and score it.

usage: python tools/local_eval.py --map MAP --sqlglot SQLGLOT_DIR [--data sample]

Prints the scorer's report, the selector's CPU seconds (user+system of the
child process) and, per commit, selected/failing tests and any misses.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

parser = argparse.ArgumentParser()
parser.add_argument("--map", required=True)
parser.add_argument("--sqlglot", required=True)
parser.add_argument("--data", default=str(HERE / "sample"))
parser.add_argument("--quiet", action="store_true")
args = parser.parse_args()

env = dict(os.environ, BR_DATA=args.data, BR_MAP=args.map, BR_SQLGLOT=args.sqlglot)
out_path = HERE / "output.jsonl"
err_path = HERE / "selector.log"
start = time.perf_counter()
with open(out_path, "wb") as out, open(err_path, "wb") as err:
    # process_time() at exit = user+system CPU of the selector (startup excluded)
    code = ("import runpy, sys, time; sys.argv = [sys.argv[1]]; "
            "runpy.run_path(sys.argv[0], run_name='__main__'); "
            "sys.stderr.write('CPU %.6f\\n' % time.process_time())")
    proc = subprocess.Popen([sys.executable, "-I", "-c", code, str(HERE / "selector.py")],
                            stdout=out, stderr=err, env=env)
    proc.wait()
cpu = None
for line in open(err_path, encoding="utf-8"):
    if line.startswith("CPU "):
        cpu = float(line.split()[1])
wall = time.perf_counter() - start
if proc.returncode:
    print(open(err_path, encoding="utf-8").read()[-3000:])
    sys.exit(f"selector exited {proc.returncode}")

data = Path(args.data)
tests = json.loads((data / "tests.json").read_text())["tests"]
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
answers = json.loads((data / "answers.json").read_text())["commits"]
total_selected_s = 0
for line in out_path.read_text().splitlines():
    row = json.loads(line)
    chosen = {n for w in row["workers"] for n in w}
    failing = set(answers[row["commit"]]["failing"])
    missed = failing - chosen
    cost = (max(sum(dur[n] for n in w) for w in row["workers"]) + 2000 * len(row["workers"])) if row["workers"] else 0
    if not args.quiet or missed:
        print(f"{row['commit']} {answers[row['commit']]['kind']:8s} selected={len(chosen):5d} "
              f"failing={len(failing):3d} workers={len(row['workers'])} cost={cost / 1000:7.3f}s"
              + (f"  MISSED {sorted(missed)}" if missed else ""))
print()
subprocess.run([sys.executable, str(HERE / "score.py"), args.data, str(data / "answers.json"), str(out_path)]
               + (["--cpu-seconds", f"{cpu:.3f}"] if cpu is not None else []))
print(f"wall {wall:.2f}s, selector CPU {cpu if cpu is None else round(cpu, 3)}s (local machine)")
