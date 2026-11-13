import copy
import itertools
import sys

from source import EvidenceError, load, replay


def normal():
    pack = load("data/tiny")
    early = {"commits": {"u": 2}, "reads": {"p1": 1, "p2": 5}}
    at = {"commits": {"u": 3}, "reads": {"p1": 1, "p2": 5}}
    late = {"commits": {"u": 4}, "reads": {"p1": 1, "p2": 5}}
    assert replay(pack, early)["target_versions"] == {
        "a": 2, "b": 2, "c": 1}
    assert replay(pack, at)["missing"] == ["b", "c"]
    assert replay(pack, late)["target_versions"] == {
        "a": 1, "b": 1, "c": 1}
    assert replay(pack, late)["missing"] == ["c"]
    bad = copy.deepcopy(at)
    bad["commits"]["u"] = 5
    try:
        replay(pack, bad)
    except EvidenceError as exc:
        assert str(exc) == "SCHEDULE_WINDOW:u"
    else:
        raise AssertionError("accepted out-of-window commit")
    pack = load("data/conflict")
    # Each archived page is individually achievable.
    for page_id, commit, read in [("p1", 1, 2), ("p2", 6, 4)]:
        one = copy.deepcopy(pack)
        one["pages"] = [p for p in pack["pages"] if p["id"] == page_id]
        assert replay(one, {"commits": {"u": commit},
                            "reads": {page_id: read}})["pages"]
    feasible = 0
    for commit, first, second in itertools.product(
            range(1, 7), range(2, 4), range(4, 6)):
        try:
            replay(pack, {"commits": {"u": commit},
                          "reads": {"p1": first, "p2": second}})
            feasible += 1
        except EvidenceError:
            pass
    assert feasible == 0
    main = load("data/main")
    assert len(main["transactions"]) == 18
    assert sum(len(p["rows"]) for p in main["pages"]) == 16
    print("NORMAL_REGRESSION_OK: target endpoint, exact versions, "
          "atomic rank move, individually feasible but jointly impossible")


if __name__ == "__main__":
    if sys.argv[1:] != ["normal"]:
        raise SystemExit("usage: checks.py normal")
    normal()
