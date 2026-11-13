import json
import sys
from pathlib import Path


class EvidenceError(ValueError):
    pass


def integer(value):
    return type(value) is int


def row_ok(row):
    return (isinstance(row, list) and len(row) == 5
            and isinstance(row[0], str) and bool(row[0])
            and integer(row[1]) and row[1] > 0
            and integer(row[2]) and integer(row[3])
            and type(row[4]) is bool)


def window_ok(window):
    return (isinstance(window, list) and len(window) == 2
            and all(integer(x) and x >= 1 for x in window)
            and window[0] <= window[1])


def validate(pack):
    if not integer(pack["target"]) or pack["target"] < 0:
        raise EvidenceError("BAD_TARGET")
    initial = pack["initial"]
    if not all(row_ok(r) and not r[4] for r in initial):
        raise EvidenceError("BAD_INITIAL")
    if len({r[0] for r in initial}) != len(initial):
        raise EvidenceError("DUPLICATE_INITIAL")
    txs = pack["transactions"]
    ids = [x["id"] for x in txs]
    if (not all(isinstance(x, str) and x for x in ids)
            or len(set(ids)) != len(ids)):
        raise EvidenceError("BAD_TRANSACTION_ID")
    by_id = {x["id"]: x for x in txs}
    done = set()
    ordered = []
    while len(done) < len(txs):
        ready = [x for x in txs if x["id"] not in done
                 and set(x["after"]) <= done]
        if not ready:
            raise EvidenceError("BAD_DEPENDENCY")
        for tx in sorted(ready, key=lambda x: x["id"]):
            if not window_ok(tx["window"]):
                raise EvidenceError("BAD_COMMIT_WINDOW")
            writes = tx["writes"]
            if (not writes or not all(row_ok(r) for r in writes)
                    or len({r[0] for r in writes}) != len(writes)):
                raise EvidenceError("BAD_WRITES")
            ordered.append(tx)
            done.add(tx["id"])
    # Every pair of writes to an ID must be causally ordered by revision.
    ancestors = {}
    versions = {r[0]: (r[1], None) for r in initial}
    for tx in ordered:
        parents = set(tx["after"])
        ancestors[tx["id"]] = parents.union(
            *(ancestors[p] for p in parents))
        for row in tx["writes"]:
            old_rev, old_tx = versions.get(row[0], (0, None))
            if (row[1] != old_rev + 1
                    or (old_tx is not None
                        and old_tx not in ancestors[tx["id"]])):
                raise EvidenceError("BAD_VERSION_CHAIN")
            versions[row[0]] = (row[1], tx["id"])
    page_ids = []
    for page in pack["pages"]:
        page_ids.append(page["id"])
        if (not isinstance(page["id"], str) or not page["id"]
                or not window_ok(page["window"])
                or not integer(page["offset"]) or page["offset"] < 0
                or not integer(page["limit"]) or page["limit"] <= 0
                or not all(row_ok(r) and not r[4] for r in page["rows"])
                or len(page["rows"]) > page["limit"]):
            raise EvidenceError("BAD_PAGE")
    if len(set(page_ids)) != len(page_ids):
        raise EvidenceError("DUPLICATE_PAGE")
    return pack


def load(directory):
    return validate(json.loads((Path(directory) / "pack.json").read_text(
        encoding="utf-8")))


def _times(items, values, kind):
    if set(values) != {x["id"] for x in items}:
        raise EvidenceError("SCHEDULE_KEYS:" + kind)
    for item in items:
        value = values[item["id"]]
        if (not integer(value)
                or not item["window"][0] <= value <= item["window"][1]):
            raise EvidenceError("SCHEDULE_WINDOW:" + item["id"])


def snapshot(pack, commits, at):
    state = {r[0]: list(r) for r in pack["initial"]}
    for tx in sorted(pack["transactions"],
                     key=lambda x: (commits[x["id"]], x["id"])):
        if commits[tx["id"]] <= at:
            state.update((r[0], list(r)) for r in tx["writes"])
    return state


def visible(state):
    return sorted((r for r in state.values() if not r[4]),
                  key=lambda r: (r[2], r[0]))


def replay(pack, schedule):
    validate(pack)
    commits, reads = schedule["commits"], schedule["reads"]
    _times(pack["transactions"], commits, "commits")
    _times(pack["pages"], reads, "reads")
    for tx in pack["transactions"]:
        for parent in tx["after"]:
            if commits[parent] >= commits[tx["id"]]:
                raise EvidenceError("CAUSAL_ORDER:" + tx["id"])
    pages = {}
    for page in pack["pages"]:
        rows = visible(snapshot(pack, commits, reads[page["id"]]))
        actual = rows[page["offset"]:page["offset"] + page["limit"]]
        if actual != page["rows"]:
            raise EvidenceError("PAGE_MISMATCH:" + page["id"])
        pages[page["id"]] = actual
    target = snapshot(pack, commits, pack["target"])
    universe = {r[0] for r in pack["initial"]}
    universe.update(r[0] for tx in pack["transactions"] for r in tx["writes"])
    captured = {(r[0], r[1]) for p in pack["pages"] for r in p["rows"]}
    versions = {rid: (target[rid][1]
                     if rid in target and not target[rid][4] else None)
                for rid in sorted(universe)}
    missing = [rid for rid, rev in versions.items()
               if rev is not None and (rid, rev) not in captured]
    return {"pages": pages, "target_versions": versions,
            "missing": missing}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        raise SystemExit("usage: source.py INPUT_DIR SCHEDULE.json")
    pack = load(argv[0])
    schedule = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(replay(pack, schedule), ensure_ascii=False,
                     sort_keys=True))


if __name__ == "__main__":
    main()
