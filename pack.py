"""Assign tests to CI workers to minimise max(worker load) + worker_cost_ms * k."""

from __future__ import annotations

import bisect, heapq, json, random, time
from pathlib import Path

WORKER_COST_MS = 2000
MAX_WORKERS = 8
_INF = float("inf")

def cost(workers: list[list[int]], dur: dict[int, int], worker_cost_ms: int = 2000) -> int:
    """Score a packing; empty workers are dropped, as the harness does."""
    loads = [sum(dur[n] for n in group) for group in workers if group]
    return 0 if not loads else max(loads) + worker_cost_ms * len(loads)

def _lpt(order: list[tuple[int, int]], k: int) -> tuple[list[list[int]], list[int]]:
    """Longest test first onto the least loaded worker (ties: smaller index)."""
    workers: list[list[int]] = [[] for _ in range(k)]
    loads = [0] * k
    heap = [(0, i) for i in range(k)]
    heapq.heapify(heap)
    for num, d in order:
        load, i = heapq.heappop(heap)
        workers[i].append(num); loads[i] = load = load + d
        heapq.heappush(heap, (load, i))
    return workers, loads

def _improve(workers: list[list[int]], loads: list[int], dur: dict[int, int], lower_bound: int,
             max_iter: int = 30) -> None:
    """Move/swap one item off the most loaded worker while the max load shrinks."""
    k = len(workers)
    neg = None  # per worker: negated durations, ascending (durations descending)
    for _ in range(max_iter):
        m = max(loads)
        if m <= lower_bound:
            return
        neg = neg or [[-dur[n] for n in group] for group in workers]
        mi = loads.index(m)
        second = max(loads[:mi] + loads[mi + 1:]) if k > 1 else -1
        if second >= m:  # another worker pins the max; one move cannot help
            return
        li = min((i for i in range(k) if i != mi), key=loads.__getitem__); lmin = loads[li]
        best_nm, best, wm, dm = m, None, workers[mi], neg[mi]
        # Move: the ideal duration is near (m - lmin) / 2.
        pos = bisect.bisect_left(dm, -((m - lmin) // 2))
        for ia in range(max(0, pos - 3), min(len(wm), pos + 4)):
            d = -dm[ia]
            if 0 < d and lmin + d < m:
                cand = max(second, m - d, lmin + d)
                if cand < best_nm:
                    best_nm, best = cand, ("move", ia, d, li)
        # Swap: any pair a > b with 0 < a - b < m - lj lowers the max.
        for j in range(k):
            if j == mi:
                continue
            lj, wj, dj = loads[j], workers[j], neg[j]
            span = m - lj
            target = span // 2
            a_pos = {0, 1, 2, bisect.bisect_left(dm, -target),
                     bisect.bisect_left(dm, -(target + span // 4))}
            for ia in sorted(p for p in a_pos if p < len(wm)):
                a = -dm[ia]
                if a <= 0:
                    continue
                pb = bisect.bisect_left(dj, -(a - target))
                for ib in range(max(0, pb - 1), min(len(wj), pb + 2)):
                    b = -dj[ib]
                    if not 0 < a - b < span:
                        continue
                    cand = max(second, m - a + b, lj - b + a)
                    if cand < best_nm:
                        best_nm, best = cand, ("swap", ia, ib, a, b, j)
        if best is None:
            return
        if best[0] == "move":
            _, ia, d, target = best
            num = wm.pop(ia); dm.pop(ia)
            at = bisect.bisect_left(neg[target], -d)
            workers[target].insert(at, num)
            neg[target].insert(at, -d)
            loads[mi], loads[target] = m - d, lmin + d
        else:
            _, ia, ib, a, b, target = best
            num_a, num_b = wm.pop(ia), workers[target].pop(ib)
            dm.pop(ia), neg[target].pop(ib)
            at = bisect.bisect_left(neg[mi], -b)
            workers[mi].insert(at, num_b)
            neg[mi].insert(at, -b)
            at = bisect.bisect_left(neg[target], -a)
            workers[target].insert(at, num_a)
            neg[target].insert(at, -a)
            loads[mi], loads[target] = m - a + b, loads[target] - b + a

def pack(items: list[tuple[int, int]], max_workers: int = 8,
         worker_cost_ms: int = 2000) -> list[list[int]]:
    """Return non-empty groups of test numbers minimising the cost."""
    if not items:
        return []
    dur = {num: d for num, d in items}
    n = len(dur)
    kcap = min(max_workers, MAX_WORKERS, n)
    if kcap <= 0:
        return []
    order = sorted(items, key=lambda t: (-t[1], t[0]))
    total, max_d = sum(dur.values()), max(dur.values())
    bound = [max(max_d, -(-total // k)) + worker_cost_ms * k for k in range(1, kcap + 1)]
    tail = [_INF] * (kcap + 1)  # tail[i] = min bound cost over k >= i
    for i in range(kcap - 1, -1, -1):
        tail[i] = min(bound[i], tail[i + 1])

    def build(k: int) -> tuple[list[list[int]], int]:
        workers, loads = _lpt(order, k)
        if k > 1:
            _improve(workers, loads, dur, max(max_d, -(-total // k)))
        return workers, max(loads) + worker_cost_ms * k

    # Seed with the k whose lower bound is smallest (ties: fewer workers).
    k0 = min(range(kcap), key=lambda i: (bound[i], i)) + 1
    workers, best_cost = build(k0)
    best_k, best_workers = k0, [list(group) for group in workers]
    for i in range(kcap):
        k = i + 1
        if (k != k0 and bound[i] <= best_cost
                and (bound[i] < best_cost or k < best_k)):  # bound permits a win
            workers, c = build(k)
            if c < best_cost or (c == best_cost and k < best_k):
                best_cost, best_k, best_workers = c, k, [list(group) for group in workers]
        # Nothing left can beat the best, and ties at larger k are worse.
        if i + 1 >= best_k and tail[i] >= best_cost:
            break
    return best_workers

def _brute(items: list[tuple[int, int]], worker_cost_ms: int = WORKER_COST_MS) -> int:
    """Exact optimum by enumerating every partition (tiny inputs only)."""
    loads, best = [], _INF

    def rec(i: int) -> None:
        nonlocal best
        if i == len(items):
            best = min(best, max(loads) + worker_cost_ms * len(loads))
            return
        d = items[i][1]
        for w in range(len(loads)):
            loads[w] += d; rec(i + 1); loads[w] -= d
        if len(loads) < min(MAX_WORKERS, len(items)):
            loads.append(d); rec(i + 1); loads.pop()

    rec(0)
    return best

def _self_test() -> None:
    rng = random.Random(20240607)
    def make(n: int) -> list[tuple[int, int]]:
        nums = rng.sample(range(10_000), n)
        ds = [rng.choice([0, rng.randint(0, 12), rng.randint(0, 3000)]) for _ in range(n)]
        return list(zip(nums, ds))

    for _ in range(500):
        n = rng.randint(0, 80)
        items, cap = make(n), rng.randint(1, 8)
        out = pack(items, cap)
        if n == 0:
            assert out == [] and cost(out, {}) == 0
        else:
            dur = dict(items)
            flat = [x for group in out for x in group]
            assert sorted(flat) == sorted(dur), "test numbers not covered exactly once"
            assert all(out) and len(out) <= min(cap, 8), "empty or too many workers"
            lw, _ = _lpt(sorted(items, key=lambda t: (-t[1], t[0])), len(out))
            assert cost(out, dur) <= cost(lw, dur), "worse than plain LPT for the chosen k"
    print("random validation: 500 instances OK")

    gaps = []
    for _ in range(400):
        items = make(rng.randint(1, 8))
        opt = _brute(items)
        gaps.append((cost(pack(items, 8), dict(items)) - opt) / opt if opt else 0.0)
    print(f"brute-force optimum: {len(gaps)} tiny instances, "
          f"average gap {100.0 * sum(gaps) / len(gaps):.3f}%, worst {100.0 * max(gaps):.3f}%")

    path = Path(__file__).resolve().parent / "sample" / "tests.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))["tests"][:1620]
    except (OSError, ValueError, KeyError) as exc:
        print(f"sample timing skipped: {exc}")
        return
    items = [(t["number"], int(t["seconds"] * 1000 + 0.5)) for t in raw]
    pack(items)  # warm up
    times = []
    for _ in range(5):
        start = time.perf_counter(); out = pack(items)
        times.append((time.perf_counter() - start) * 1000.0)
    dur = dict(items)
    assert sorted(x for group in out for x in group) == sorted(dur)
    print(f"sample: {len(items)} tests, chosen k={len(out)}, cost={cost(out, dur)}, "
          f"time={min(times):.2f} ms")

if __name__ == "__main__":
    _self_test()
