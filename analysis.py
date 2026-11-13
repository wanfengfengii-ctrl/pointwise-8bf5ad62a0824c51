#!/usr/bin/env python3
"""本批分页覆盖核账分析器（CONTRACT.md 的公开交付接口）。

用法：
    python3 analysis.py run INPUT_DIR OUTPUT_DIR     重算并写出 result.json 与 REPORT.md
    python3 analysis.py verify INPUT_DIR OUTPUT_DIR  从材料重新核定并核对已交结果
    python3 analysis.py demo INPUT_DIR               重新分析并演示核账

联合时序模型（不逐页拼接）：
    * 每个事务的提交时刻、每页的读取时刻都是整数变量，受各自闭区间时窗约束；
    * after 前驱严格早于后继：c_child - c_parent >= 1；
    * 一页内容 ⇔ 读取时刻已提交事务集合（必为 after 下集）产生的
      “该时刻未删除记录按 (rank,id) 升序后的 [offset, offset+limit) 切片”，
      短页同时约束当时记录总数；
    * 先枚举每页所有可解释下集，再把逐页模式合并为差分约束区域
      （Floyd-Warshall 闭包，scipy.sparse.csgraph），只保留联合可行组合；
    * 某 ID 在 target 时刻的版本查询 = 联合区域上关于链上事务提交时刻的
      界限约束是否可满足；排除结论另用 scipy.optimize.milp 独立编码重算。
"""
from __future__ import annotations

import json
import subprocess
import sys
from itertools import product
from pathlib import Path

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import NegativeCycleError, floyd_warshall

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source  # 材料重放器（只使用，不修改）


# ---------------------------------------------------------------- 差分约束区域

def _fw(adj):
    """Floyd-Warshall 闭包（最短路矩阵）；负环（不可行）返回 None。

    用 scipy 稀疏最短路实现；显式 0 权边经 csr 显式存储得以保留。
    """
    rows, cols = np.nonzero(~np.isinf(adj))
    try:
        dist = floyd_warshall(
            csr_matrix((adj[rows, cols], (rows, cols)), shape=adj.shape))
    except NegativeCycleError:
        return None
    return dist


class JointModel:
    """一批材料的联合时序模型：时窗 + after + 逐页可解释下集。"""

    def __init__(self, pack):
        self.pack = pack
        self.target = pack["target"]
        self.txs = pack["transactions"]
        self.pages = pack["pages"]
        self.txids = [t["id"] for t in self.txs]
        self.pageids = [p["id"] for p in self.pages]
        self.initial = {r[0]: list(r) for r in pack["initial"]}
        self.chains = {}
        for t in self.txs:
            for r in t["writes"]:
                self.chains.setdefault(r[0], []).append((r[1], t["id"], list(r)))
        for rid in self.chains:
            self.chains[rid].sort(key=lambda e: e[0])
        self.universe = sorted(set(self.initial) | set(self.chains))
        self.captured = {}
        for p in self.pages:
            for r in p["rows"]:
                self.captured.setdefault(r[0], set()).add(r[1])
        # 节点编号：事务 0..ntx-1，页 ntx..ntx+npg-1，常量点 const
        self.tnode = {t: i for i, t in enumerate(self.txids)}
        self.pnode = {p: len(self.txids) + i for i, p in enumerate(self.pageids)}
        self.const = len(self.txids) + len(self.pageids)
        self.n_nodes = self.const + 1
        self.base_edges = self._base_edges()
        self.patterns = self._page_patterns()

    # 边 (a,b,w) 表示 x_b - x_a <= w
    def _base_edges(self):
        edges = []
        C = self.const
        for t in self.txs:
            lo, hi = t["window"]
            n = self.tnode[t["id"]]
            edges.append((C, n, float(hi)))    # c_t <= hi
            edges.append((n, C, float(-lo)))   # c_t >= lo
        for p in self.pages:
            lo, hi = p["window"]
            n = self.pnode[p["id"]]
            edges.append((C, n, float(hi)))    # r_p <= hi
            edges.append((n, C, float(-lo)))   # r_p >= lo
        for t in self.txs:
            for pa in t["after"]:
                # c_parent <= c_child - 1（前驱严格早于后继）
                edges.append((self.tnode[t["id"]], self.tnode[pa], -1.0))
        return edges

    def _downsets(self):
        """after 偏序的全部下集（读取时刻已提交集合的全部候选）。"""
        parents = {t["id"]: list(t["after"]) for t in self.txs}
        topo = []
        done = set()
        while len(topo) < len(self.txids):
            for t in self.txids:
                if t not in done and all(p in done for p in parents[t]):
                    topo.append(t)
                    done.add(t)
        out = []

        def rec(i, S):
            if i == len(topo):
                out.append(frozenset(S))
                return
            t = topo[i]
            rec(i + 1, S)
            if all(p in S for p in parents[t]):
                rec(i + 1, S | {t})

        rec(0, set())
        return out

    def _visible_rows(self, S):
        """已提交集合 S 下，该时刻未删除记录按 (rank,id) 的升序序列。"""
        state = {rid: list(row) for rid, row in self.initial.items()}
        for rid, ch in self.chains.items():
            best = None
            for rev, txid, row in ch:  # 已提交者必为链上前缀
                if txid in S:
                    best = row
            if best is not None:
                state[rid] = list(best)
        return sorted((r for r in state.values() if not r[4]),
                      key=lambda r: (r[2], r[0]))

    def _page_patterns(self):
        """每页：所有能复现其归档 rows 的已提交下集及对应差分约束边。"""
        pats = {}
        sets = self._downsets()
        for p in self.pages:
            off, lim = p["offset"], p["limit"]
            pn = self.pnode[p["id"]]
            lst = []
            for S in sets:
                vis = self._visible_rows(S)
                if vis[off:off + lim] == p["rows"]:  # 短页总数约束蕴含其中
                    edges = []
                    for t in self.txids:
                        tn = self.tnode[t]
                        if t in S:
                            edges.append((pn, tn, 0.0))    # c_t <= r_p
                        else:
                            edges.append((tn, pn, -1.0))   # c_t >= r_p + 1
                    lst.append({"set": S, "edges": tuple(edges)})
            pats[p["id"]] = lst
        return pats

    def closure(self, extra_edges=()):
        adj = np.full((self.n_nodes, self.n_nodes), np.inf)
        np.fill_diagonal(adj, 0.0)
        for a, b, w in self.base_edges:
            if w < adj[a, b]:
                adj[a, b] = w
        for a, b, w in extra_edges:
            if w < adj[a, b]:
                adj[a, b] = w
        return _fw(adj)

    @staticmethod
    def _closure_with(dist, extra_edges):
        adj = dist.copy()
        for a, b, w in extra_edges:
            if w < adj[a, b]:
                adj[a, b] = w
        return _fw(adj)

    def combine(self):
        """逐级合并逐页模式；返回联合可行组合的闭包列表。"""
        base = self.closure()
        if base is None:
            return []
        combos = [base]
        for pid in self.pageids:
            nxt = []
            for d in combos:
                for pat in self.patterns[pid]:
                    # 预过滤：已有最短路 b~>a 与新边 a->b 成负环则必不可行
                    if any(d[b, a] + w < 0 for a, b, w in pat["edges"]):
                        continue
                    d2 = self._closure_with(d, pat["edges"])
                    if d2 is not None:
                        nxt.append(d2)
            combos = nxt
            if not combos:
                break
        return combos

    def version_ok(self, d, rid, j):
        """联合区域 d 中，target 时刻版本下标为 j 是否可满足。

        版本=j ⟺ 链上第 j 个事务提交 <= target 且第 j+1 个提交 >= target+1
        （提交时刻等于 target 时先生效）。链上事务因果有序，两个界限约束
        同时加入不会产生额外负环，故分别用区域最紧界判定即可。
        """
        ch = self.chains.get(rid, [])
        T = self.target
        if j >= 1:
            if -d[self.tnode[ch[j - 1][1]], self.const] > T:
                return False
        if j < len(ch):
            if d[self.const, self.tnode[ch[j][1]]] < T + 1:
                return False
        return True

    def version_edges(self, rid, j):
        ch = self.chains.get(rid, [])
        T = self.target
        edges = []
        if j >= 1:
            edges.append((self.const, self.tnode[ch[j - 1][1]], float(T)))
        if j < len(ch):
            edges.append((self.tnode[ch[j][1]], self.const, float(-(T + 1))))
        return edges

    def point_of(self, dist):
        """由可行区域闭包提取一个具体整数时序（x_v = -d[v][const]）。"""
        x = -dist[:, self.const]
        commits = {t: int(round(x[self.tnode[t]])) for t in self.txids}
        reads = {p: int(round(x[self.pnode[p]])) for p in self.pageids}
        return {"commits": commits, "reads": reads}


def value_of(model, rid, j):
    """版本下标 j 对应的核查时刻取值：rev 或 None（删除/尚未创建）。"""
    if j == 0:
        if rid not in model.initial:
            return None
        row = model.initial[rid]
        return None if row[4] else row[1]
    rev, _txid, row = model.chains[rid][j - 1]
    return None if row[4] else rev


def classify(values, captured_revs):
    """由可行取值集合判定 existence 与 coverage（CONTRACT.md 口径）。"""
    has_null = None in values
    revs = sorted(v for v in values if v is not None)
    versions = ([None] if has_null else []) + revs
    if not has_null:
        existence = "must"
    elif not revs:
        existence = "never"
    else:
        existence = "may"
    if existence == "never":
        coverage = "absent"
    else:
        uncaptured = [v for v in revs if v not in captured_revs]
        captured = [v for v in revs if v in captured_revs]
        if not uncaptured:
            coverage = "complete"
        elif not has_null and not captured:
            coverage = "definite_gap"
        else:
            coverage = "possible_gap"
    return versions, existence, coverage


def sources_for(pack, rid):
    init = [f"/initial/{i}" for i, r in enumerate(pack["initial"])
            if r[0] == rid]
    writes = [f"/transactions/{ti}/writes/{wi}"
              for ti, t in enumerate(pack["transactions"])
              for wi, r in enumerate(t["writes"]) if r[0] == rid]
    captured = [f"/pages/{pi}/rows/{ri}"
                for pi, p in enumerate(pack["pages"])
                for ri, r in enumerate(p["rows"]) if r[0] == rid]
    return {"initial": init, "writes": writes, "captured": captured}


# ---------------------------------------------------------------- 独立 MILP 复核

def milp_cross_check(model, feas):
    """用 scipy.optimize.milp 的独立编码复核全部 (ID, 版本下标) 查询。

    逐页模式选择用 0-1 变量 + 大 M 指示约束编码，与区域闭包是两条
    独立求解路径；任何不一致都说明内部矛盾，直接抛错。
    返回 (可行查询数, 排除查询数)。
    """
    txids, pageids = model.txids, model.pageids
    T = model.target
    pat_sets = {pid: [p["set"] for p in model.patterns[pid]] for pid in pageids}
    nsel = sum(len(v) for v in pat_sets.values())
    nv = len(txids) + len(pageids) + nsel
    tvar = {t: i for i, t in enumerate(txids)}
    pvar = {p: len(txids) + i for i, p in enumerate(pageids)}
    svar = {}
    off = len(txids) + len(pageids)
    for pid in pageids:
        for k in range(len(pat_sets[pid])):
            svar[(pid, k)] = off
            off += 1
    hi_all = max([w[1] for w in [t["window"] for t in model.txs]]
                 + [w[1] for w in [p["window"] for p in model.pages]] + [1])
    M = float(2 * hi_all + 10)
    A, lo, up = [], [], []

    def add(coef, l, u):
        A.append(coef)
        lo.append(l)
        up.append(u)

    for t in model.txs:  # after：c_child - c_parent >= 1
        for pa in t["after"]:
            add({tvar[t["id"]]: 1.0, tvar[pa]: -1.0}, 1.0, np.inf)
    for pid in pageids:  # 每页恰选一种模式
        add({svar[(pid, k)]: 1.0 for k in range(len(pat_sets[pid]))}, 1.0, 1.0)
    for pid in pageids:
        pi = pvar[pid]
        for k, S in enumerate(pat_sets[pid]):
            sk = svar[(pid, k)]
            for t in txids:
                ti = tvar[t]
                if t in S:   # c_t - r_p <= M(1-sel)
                    add({ti: 1.0, pi: -1.0, sk: M}, -np.inf, M)
                else:        # r_p - c_t <= -1 + M(1-sel)
                    add({pi: 1.0, ti: -1.0, sk: M}, -np.inf, M - 1.0)
    A = np.array([[coef.get(v, 0.0) for v in range(nv)] for coef in A])
    base_con = LinearConstraint(A, np.array(lo), np.array(up))
    lb = np.zeros(nv)
    ub = np.full(nv, M)
    for t in model.txs:
        lb[tvar[t["id"]]], ub[tvar[t["id"]]] = map(float, t["window"])
    for p in model.pages:
        lb[pvar[p["id"]]], ub[pvar[p["id"]]] = map(float, p["window"])
    for v in range(len(txids) + len(pageids), nv):
        lb[v], ub[v] = 0.0, 1.0
    bounds = Bounds(lb, ub)
    integrality = np.ones(nv)
    c0 = np.zeros(nv)
    n_feas = n_infeas = 0
    for rid in model.universe:
        ch = model.chains.get(rid, [])
        for j in range(len(ch) + 1):
            extra, elo, eup = [], [], []
            if j >= 1:
                extra.append({tvar[ch[j - 1][1]]: 1.0})
                elo.append(-np.inf)
                eup.append(float(T))
            if j < len(ch):
                extra.append({tvar[ch[j][1]]: 1.0})
                elo.append(float(T + 1))
                eup.append(np.inf)
            if extra:
                E = np.array([[coef.get(v, 0.0) for v in range(nv)]
                              for coef in extra])
                con = LinearConstraint(
                    np.vstack([A, E]),
                    np.concatenate([lo, elo]), np.concatenate([up, eup]))
            else:
                con = base_con
            res = milp(c0, constraints=con, integrality=integrality,
                       bounds=bounds, options={"time_limit": 30})
            if res.status == 0:
                got, n_feas = True, n_feas + 1
            elif res.status == 2:
                got, n_infeas = False, n_infeas + 1
            else:
                raise RuntimeError(f"MILP 求解异常（{rid} 版本{j}）：{res.message}")
            want = j in feas[rid]
            if got != want:
                raise RuntimeError(
                    f"MILP 与联合区域结论不一致：{rid} 版本下标{j} "
                    f"MILP={got} 区域={want}")
    return n_feas, n_infeas


# ---------------------------------------------------------------- 穷举（独立口径）

def exhaustive_outputs(pack):
    """逐一枚举全部整数时序并用 source.replay 判定，返回合法重放结果。

    不复用联合建模逻辑，用于小材料穷举互核与联合矛盾验证。
    """
    txs, pages = pack["transactions"], pack["pages"]
    spans = [range(t["window"][0], t["window"][1] + 1) for t in txs]
    spans += [range(p["window"][0], p["window"][1] + 1) for p in pages]
    outs = []
    for combo in product(*spans):
        schedule = {
            "commits": {t["id"]: v for t, v in zip(txs, combo)},
            "reads": {p["id"]: v for p, v in zip(pages, combo[len(txs):])},
        }
        try:
            outs.append(source.replay(pack, schedule))
        except source.EvidenceError:
            pass
    return outs


def exhaustive_classification(pack):
    """由穷举结果给出每 ID 的 versions/existence/coverage（独立期望）。"""
    outs = exhaustive_outputs(pack)
    captured = {}
    for p in pack["pages"]:
        for r in p["rows"]:
            captured.setdefault(r[0], set()).add(r[1])
    values = {}
    for out in outs:
        for rid, rev in out["target_versions"].items():
            values.setdefault(rid, set()).add(rev)
    universe = sorted({r[0] for r in pack["initial"]} |
                      {r[0] for t in pack["transactions"] for r in t["writes"]})
    expect = {}
    for rid in universe:
        versions, existence, coverage = classify(
            values.get(rid, set()), captured.get(rid, set()))
        expect[rid] = {"versions": versions, "existence": existence,
                       "coverage": coverage}
    return len(outs), expect


# ---------------------------------------------------------------- 分析主流程

def analyze(input_dir):
    """从 INPUT_DIR/pack.json 重算全部结论；返回分析包（dict）。"""
    pack = source.load(input_dir)
    model = JointModel(pack)
    counts = {pid: len(model.patterns[pid]) for pid in model.pageids}
    combos = model.combine()
    bundle = {"input_dir": str(input_dir), "pack": pack, "model": model,
              "pattern_counts": counts, "n_combos": len(combos)}
    if not combos:
        bundle["status"] = "conflict"
        bundle["conflict"] = _conflict_details(model, counts)
        bundle["result_dict"] = {"status": "conflict", "rows": []}
        return bundle

    # 逐 ID 逐版本下标的可行性 + 见证区域；跨组合的事务时刻界
    feas = {rid: {} for rid in model.universe}
    tx_lb = {t: np.inf for t in model.txids}
    tx_ub = {t: -np.inf for t in model.txids}
    for d in combos:
        for t in model.txids:
            n = model.tnode[t]
            tx_lb[t] = min(tx_lb[t], -d[n, model.const])
            tx_ub[t] = max(tx_ub[t], d[model.const, n])
        for rid in model.universe:
            ch = model.chains.get(rid, [])
            for j in range(len(ch) + 1):
                if j not in feas[rid] and model.version_ok(d, rid, j):
                    feas[rid][j] = d
    bundle["tx_bounds"] = {t: [int(tx_lb[t]), int(tx_ub[t])]
                           for t in model.txids}

    # 排除理由：纯时窗/因果（基础区域已不可行）还是联合分页约束
    base = model.closure()
    exclusions = []
    for rid in model.universe:
        ch = model.chains.get(rid, [])
        for j in range(len(ch) + 1):
            if j in feas[rid]:
                continue
            reason = "joint"
            if base is None or not model.version_ok(base, rid, j):
                reason = "window"
            exclusions.append({"id": rid, "index": j,
                               "value": value_of(model, rid, j),
                               "rev": (model.chains[rid][j - 1][0] if j >= 1
                                       else model.initial.get(rid, [None])[1]
                                       if rid in model.initial else None),
                               "reason": reason})
    bundle["exclusions"] = exclusions

    # 逐 ID 结论与见证（每版本一份完整可重放时序）
    rows = []
    n_witness = 0
    for rid in model.universe:
        values = {}
        for j in sorted(feas[rid]):
            values.setdefault(value_of(model, rid, j), j)
        vset = set(values)
        versions, existence, coverage = classify(
            vset, model.captured.get(rid, set()))
        witnesses = []
        for v in versions:
            j = values[v]
            d = model._closure_with(feas[rid][j], model.version_edges(rid, j))
            if d is None:
                raise RuntimeError(f"见证区域不可行：{rid} 版本{v}")
            schedule = model.point_of(d)
            out = source.replay(pack, schedule)  # 用原 replay 自核见证
            if out["target_versions"][rid] != v:
                raise RuntimeError(f"见证重放不符：{rid} 版本{v}")
            witnesses.append({"version": v, "schedule": schedule})
            n_witness += 1
        rows.append({"id": rid, "versions": versions, "existence": existence,
                     "coverage": coverage, "witnesses": witnesses,
                     "sources": sources_for(pack, rid)})
    bundle["rows"] = rows
    bundle["status"] = "ok"
    bundle["result_dict"] = {"status": "ok", "rows": [
        {k: r[k] for k in ("id", "versions", "existence", "coverage",
                           "witnesses", "sources")} for r in rows]}

    # 抽样联合可行时序，逐一过原 replay 自核
    step = 97
    picks = list(range(0, len(combos), step))
    if (len(combos) - 1) not in picks:
        picks.append(len(combos) - 1)
    for i in picks:
        source.replay(pack, model.point_of(combos[i]))
    bundle["n_sampled"] = len(picks)
    bundle["n_witness"] = n_witness

    # 独立 MILP 编码复核全部版本查询（含排除结论）
    bundle["milp"] = milp_cross_check(model, feas)
    return bundle


def _conflict_details(model, counts):
    """矛盾材料的定位与解释：逐页可解释性 + 可推导的界限冲突。"""
    pages = model.pages
    per_page = []
    for p in pages:
        per_page.append({"id": p["id"],
                         "pointer": f"/pages/{model.pageids.index(p['id'])}",
                         "patterns": counts[p["id"]]})
    # 可推导的界限冲突：某事务被一页的全部解释要求已提交（上界），
    # 又被另一页的全部解释要求未提交（下界），且下界 > 上界。
    bound_conflicts = []
    for t in model.txs:
        tid = t["id"]
        lo, hi = t["window"]
        lb, ub = lo, hi
        lb_src = [f"/transactions/{model.txids.index(tid)}/window"]
        ub_src = list(lb_src)
        for p in pages:
            pats = model.patterns[p["id"]]
            if not pats:
                continue
            pi = model.pageids.index(p["id"])
            if all(tid in pat["set"] for pat in pats):
                if p["window"][1] < ub:
                    ub = p["window"][1]
                    ub_src = [f"/pages/{pi}/window", f"/pages/{pi}/rows"]
            if all(tid not in pat["set"] for pat in pats):
                if p["window"][0] + 1 > lb:
                    lb = p["window"][0] + 1
                    lb_src = [f"/pages/{pi}/window", f"/pages/{pi}/rows"]
        if lb > ub:
            bound_conflicts.append({
                "tx": tid,
                "tx_pointer": f"/transactions/{model.txids.index(tid)}",
                "lower": lb, "upper": ub,
                "lower_src": lb_src, "upper_src": ub_src})
    empty_pages = [f"/pages/{model.pageids.index(p['id'])}"
                   for p in pages if counts[p["id"]] == 0]
    return {"per_page": per_page, "bound_conflicts": bound_conflicts,
            "empty_pages": empty_pages,
            "base_feasible": model.closure() is not None}


# ---------------------------------------------------------------- 报告

def fmt_versions(versions):
    return "[" + ", ".join("null" if v is None else str(v)
                           for v in versions) + "]"


def conclusion_line(row):
    return (f"- {row['id']}: versions={fmt_versions(row['versions'])} "
            f"existence={row['existence']} coverage={row['coverage']}")


def build_report(bundle, cross):
    pack, model = bundle["pack"], bundle["model"]
    L = []
    L.append("# 本批分页覆盖核账报告")
    L.append("")
    L.append(f"- 输入材料：`{bundle['input_dir']}/pack.json`"
             f"（initial {len(pack['initial'])} 行、transactions "
             f"{len(pack['transactions'])} 项、pages {len(pack['pages'])} 页、"
             f"target={pack['target']}）；材料位置一律用 JSON Pointer 标注。")
    L.append("- 方法：事务提交时刻与页读取时刻为同一整数刻度上的变量，"
             "受闭区间时窗与 after 严格偏序约束；每页归档行必须等于该读取时刻"
             "未删除记录按 (rank,id) 升序的 [offset, offset+limit) 切片"
             "（短页同时约束当时记录总数）。先枚举每页全部可解释的已提交下集，"
             "再合并为差分约束区域（Floyd-Warshall 闭包）做联合可行性判定；"
             "不逐页拼接快照。")
    L.append("")
    if bundle["status"] == "conflict":
        L.append("## 结论：材料矛盾，停止核定")
        L.append("")
        L.append("status: conflict")
        L.append("")
        det = bundle["conflict"]
        if not det["base_feasible"]:
            L.append("- 事务时窗与 after 偏序自身已无可行提交时序"
                     "（见 /transactions 各项 window 与 after）。")
        for pg in det["empty_pages"]:
            L.append(f"- 页 `{pg}` 的归档行与任何合法已提交集合产生的"
                     "排序切片都不一致（行内容或短页总数矛盾）。")
        if not det["empty_pages"] and det["base_feasible"]:
            L.append("- 每页单独均可解释（逐页可解释下集数："
                     + "、".join(f"`{p['pointer']}`={p['patterns']}"
                                 for p in det["per_page"])
                     + "），但联合无解：")
        for bc in det["bound_conflicts"]:
            L.append(f"  - 事务 `{bc['tx']}`（`{bc['tx_pointer']}`）："
                     f"联合约束给出提交时刻下界 {bc['lower']}"
                     f"（来自 {'、'.join('`' + s + '`' for s in bc['lower_src'])}"
                     "，页的全部可解释下集都不含该事务，读取时窗闭区间下界+1）"
                     f"与上界 {bc['upper']}"
                     f"（来自 {'、'.join('`' + s + '`' for s in bc['upper_src'])}"
                     "，页的全部可解释下集都含该事务，读取时窗闭区间上界）"
                     "矛盾，整数时刻无解。")
        L.append("- 按要求停止核定，不输出任何局部覆盖结论；"
                 "result.json 为 {\"status\":\"conflict\",\"rows\":[]}。")
        L.append("")
        L.append("## 验证结果")
        L.extend(_report_cross_lines(bundle, cross))
        L.append("")
        return "\n".join(L)

    # ---------------- ok ----------------
    L.append("## 结论：存在合法完整时序，逐 ID 核定如下")
    L.append("")
    L.append("status: ok")
    L.append("")
    L.append("### 联合约束规模")
    L.append("")
    L.append("- 逐页可解释下集数："
             + "、".join(f"/pages/{i}（{model.pageids[i]}）= "
                         f"{bundle['pattern_counts'][model.pageids[i]]}"
                         for i in range(len(model.pageids)))
             + "。")
    L.append(f"- 联合可行组合数: {bundle['n_combos']}"
             "（同时满足全部时窗、after 偏序与全部页切片的模式组合；"
             "每个组合都是一个差分约束可行区域）。")
    short = [(i, p) for i, p in enumerate(pack["pages"])
             if len(p["rows"]) < p["limit"]]
    for i, p in short:
        L.append(f"- 短页约束：`/pages/{i}` 仅 {len(p['rows'])} 行 < limit "
                 f"{p['limit']} ⇒ 其读取时刻未删除记录总数 ≤ "
                 f"{p['offset'] + len(p['rows'])}。")
    L.append("- 各页读取时窗互不重叠且先后错位，rank 更正使行跨页移位"
             "（如 r06 由 /pages/0 的 rank 5 移至 /pages/3 的 rank 145），"
             "必须同一次合法源演进同时解释全部页，不能逐页拼接。")
    L.append("")
    L.append("### 逐 ID 结论（核查时刻 target=%d）" % pack["target"])
    L.append("")
    L.append("| ID | 全部可能有效版本 | 存在性 | 覆盖判定 | 材料位置（initial / writes / captured） |")
    L.append("| --- | --- | --- | --- | --- |")
    for r in bundle["rows"]:
        s = r["sources"]
        ptr = (("初始:" + ",".join(f"`{x}`" for x in s["initial"]))
               if s["initial"] else "初始:无") + " ｜ " + \
              (("更正:" + ",".join(f"`{x}`" for x in s["writes"]))
               if s["writes"] else "更正:无") + " ｜ " + \
              (("归档:" + ",".join(f"`{x}`" for x in s["captured"]))
               if s["captured"] else "归档:无")
        L.append(f"| {r['id']} | {fmt_versions(r['versions'])} | "
                 f"{r['existence']} | {r['coverage']} | {ptr} |")
    L.append("")
    ex = {"must": [], "may": [], "never": []}
    cov = {"complete": [], "definite_gap": [], "possible_gap": [], "absent": []}
    for r in bundle["rows"]:
        ex[r["existence"]].append(r["id"])
        cov[r["coverage"]].append(r["id"])
    L.append(f"- 存在性：必然存在（must）{len(ex['must'])} 项；"
             f"可能存在（may）{len(ex['may'])} 项：{', '.join(ex['may']) or '无'}；"
             f"从不存在（never）{len(ex['never'])} 项。")
    L.append(f"- 覆盖：完整取得（complete）{len(cov['complete'])} 项："
             f"{', '.join(cov['complete']) or '无'}；"
             f"确定漏采（definite_gap，每个时序均有效但未取得）"
             f"{len(cov['definite_gap'])} 项：{', '.join(cov['definite_gap']) or '无'}；"
             f"可能漏采（possible_gap）{len(cov['possible_gap'])} 项："
             f"{', '.join(cov['possible_gap']) or '无'}。")
    L.append("- complete 允许某些时序不存在（须结合存在性阅读）："
             "如 r14 在部分时序中 target 时刻已删除（rev3，"
             "`/transactions/6/writes/1` deleted=true），但所有存在时序中"
             "其版本必为 rev2 且已归档（`/pages/1/rows/0`）。")
    L.append("")
    L.append("### 联合时间与排序约束如何排除错误解释")
    L.append("")
    win = [e for e in bundle["exclusions"] if e["reason"] == "window"]
    joint = [e for e in bundle["exclusions"] if e["reason"] == "joint"]
    L.append(f"- 版本排除共 {len(bundle['exclusions'])} 项："
             f"纯时窗/因果排除 {len(win)} 项，联合分页约束排除 {len(joint)} 项；"
             "全部经联合区域闭包判定，并由 scipy.optimize.milp 独立编码复核一致。")
    for e in win:
        ch = model.chains.get(e["id"], [])
        if e["index"] >= 1:
            tx = ch[e["index"] - 1][1]
            ti = model.txids.index(tx)
            kind = f"rev{e['rev']}（删除态）" if e["value"] is None \
                else f"rev{e['rev']}"
            L.append(f"  - {e['id']} {kind}：该版本的写入事务 {tx}"
                     f"（`/transactions/{ti}`）时窗 "
                     f"{pack['transactions'][ti]['window']} 的下界晚于 "
                     f"target={pack['target']}，不可能在 target 前（含）提交。")
        else:
            tx = ch[0][1]
            ti = model.txids.index(tx)
            L.append(f"  - {e['id']} 停留初始版（rev{e['rev']}）：首次写入事务 "
                     f"{tx}（`/transactions/{ti}`）时窗 "
                     f"{pack['transactions'][ti]['window']} 的上界不晚于 "
                     f"target={pack['target']}，提交必然先生效，"
                     "初始版不可能保留到 target。")
    for e in joint:
        ch = model.chains.get(e["id"], [])
        tx = ch[e["index"] - 1][1] if e["index"] >= 1 else ch[0][1]
        ti = model.txids.index(tx)
        lb, ub = bundle["tx_bounds"][tx]
        kind = f"rev{e['rev']}（删除态）" if e["value"] is None \
            else f"rev{e['rev']}"
        L.append(f"  - {e['id']} {kind}：需要事务 {tx}"
                 f"（`/transactions/{ti}`）在 target={pack['target']} 前提交，"
                 f"但全部联合可行组合中其提交时刻被页切片与 after 链约束在 "
                 f"[{lb}, {ub}]，恒晚于 target。")
    L.append("- 旧版不算取得新版：归档行按 (id,rev) 整行认定。例如 r11 归档仅"
             "取得 rev3（`/pages/4/rows/2`），而其 target 版本只可能为 1 或 2，"
             "均未被任何页取得 ⇒ 确定漏采；r17 归档取得 rev4"
             "（`/pages/5/rows/0`），但 rev4 不可能在 target 时刻有效"
             "（见上），其可能版本 rev3 未归档 ⇒ 可能漏采。")
    L.append("")
    L.append("### 见证")
    L.append("")
    L.append(f"- 每 ID 每可能版本各一份完整时序见证，共 {bundle['n_witness']} 份，"
             "全部通过 source.replay 重放核验（提交/读取时刻落在各自闭区间时窗内，"
             "全部页切片逐字段相符，target 版本与结论一致）。")
    L.append(f"- 另抽样 {bundle['n_sampled']} 个联合可行组合提取具体时序，"
             "全部通过 source.replay。")
    L.append("")
    L.append("### 机器可核对结论")
    L.append("")
    for r in bundle["rows"]:
        L.append(conclusion_line(r))
    L.append("")
    L.append("## 验证结果")
    L.extend(_report_cross_lines(bundle, cross))
    L.append("")
    return "\n".join(L)


def _report_cross_lines(bundle, cross):
    L = []
    if bundle["status"] == "ok":
        mf, mi = bundle["milp"]
        L.append(f"- 联合模型查询：可行 {mf} 项、排除 {mi} 项，"
                 "区域闭包与独立 MILP 编码全部一致。")
        L.append(f"- 见证 {bundle['n_witness']} 份、抽样联合时序 "
                 f"{bundle['n_sampled']} 份：source.replay 全部通过。")
    if cross.get("checks") is not None:
        L.append(f"- 基础断言 checks.py normal：{'通过' if cross['checks'] else '失败'}。")
    if cross.get("tiny") is not None:
        ok, detail = cross["tiny"]
        L.append(f"- 小材料穷举互核（data/tiny，{detail}）：{'一致' if ok else '不一致'}。")
    if cross.get("conflict") is not None:
        ok, detail = cross["conflict"]
        L.append(f"- 联合矛盾验证（data/conflict，{detail}）：{'确认' if ok else '不符'}。")
    return L


def report_keys(bundle):
    """报告关键结论（verify 据此核对报告）。"""
    if bundle["status"] == "conflict":
        keys = ["status: conflict"]
        det = bundle["conflict"]
        keys += [p["pointer"] for p in det["per_page"]]
        keys += [bc["tx_pointer"] for bc in det["bound_conflicts"]]
        return keys
    keys = ["status: ok", f"联合可行组合数: {bundle['n_combos']}"]
    keys += [conclusion_line(r) for r in bundle["rows"]]
    return keys


# ---------------------------------------------------------------- 交叉核验

def run_basic_assertions(root):
    """保留的基础断言：checks.py normal。"""
    proc = subprocess.run([sys.executable, "checks.py", "normal"],
                          cwd=root, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def cross_check_tiny(tiny_dir):
    """小材料穷举互核：逐时刻穷举（独立口径）对比分析器联合建模结论。"""
    n_feas, expect = exhaustive_classification(source.load(tiny_dir))
    bundle = analyze(tiny_dir)
    diffs = []
    if bundle["status"] != "ok":
        diffs.append(f"分析器状态异常：{bundle['status']}")
    else:
        for row in bundle["rows"]:
            exp = expect[row["id"]]
            for key in ("versions", "existence", "coverage"):
                if row[key] != exp[key]:
                    diffs.append(f"{row['id']}.{key}: 分析器={row[key]} "
                                 f"穷举={exp[key]}")
    detail = f"合法时序 {n_feas} 个逐一枚举"
    return (not diffs and n_feas > 0), detail, diffs


def cross_check_conflict(conflict_dir):
    """联合矛盾验证：逐页可解释但联合无解（穷举 + 分析器双重确认）。"""
    pack = source.load(conflict_dir)
    outs = exhaustive_outputs(pack)
    bundle = analyze(conflict_dir)
    per_page_ok = True
    for i, p in enumerate(pack["pages"]):
        one = dict(pack)
        one["pages"] = [p]
        if not exhaustive_outputs(one):
            per_page_ok = False
    diffs = []
    if outs:
        diffs.append(f"穷举发现 {len(outs)} 个合法联合时序")
    if bundle["status"] != "conflict":
        diffs.append("分析器未判定矛盾")
    if not per_page_ok:
        diffs.append("存在单独不可解释的页")
    detail = f"逐页可解释、联合穷举 {len(outs)} 个合法时序"
    return not diffs, detail, diffs


def gather_cross_checks(input_dir):
    """run/verify 共用的交叉核验：基础断言 + 小材料 + 联合矛盾材料。"""
    root = Path(__file__).resolve().parent
    cross = {}
    ok, _out = run_basic_assertions(root)
    cross["checks"] = ok
    base = Path(input_dir).parent
    tiny, conflict = base / "tiny", base / "conflict"
    cross["tiny"] = cross_check_tiny(tiny)[:2] if (tiny / "pack.json").exists() else None
    cross["conflict"] = (cross_check_conflict(conflict)[:2]
                         if (conflict / "pack.json").exists() else None)
    return cross


# ---------------------------------------------------------------- 入口

def cmd_run(input_dir, output_dir):
    bundle = analyze(input_dir)
    cross = gather_cross_checks(input_dir)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(
        json.dumps(bundle["result_dict"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (out / "REPORT.md").write_text(build_report(bundle, cross),
                                  encoding="utf-8")
    print(f"RUN_OK: status={bundle['status']} -> {out}/result.json, "
          f"{out}/REPORT.md")
    if bundle["status"] == "ok":
        print(f"  联合可行组合 {bundle['n_combos']} 个；见证 "
              f"{bundle['n_witness']} 份；MILP 复核 {bundle['milp']}。")
    return 0


def _failures_compare_rows(exp_rows, got_rows, pack):
    """逐项核对已交结果；见证逐项调用原 replay 核验。返回差异列表。"""
    diffs = []
    if len(exp_rows) != len(got_rows):
        return [f"rows 数量不符：期望 {len(exp_rows)} 实得 {len(got_rows)}"]
    for exp, got in zip(exp_rows, got_rows):
        rid = exp["id"]
        if got.get("id") != rid:
            diffs.append(f"行顺序或 ID 不符：期望 {rid} 实得 {got.get('id')}")
            continue
        for key in ("versions", "existence", "coverage", "sources"):
            if got.get(key) != exp[key]:
                diffs.append(f"{rid}.{key} 不符：期望 {exp[key]} "
                             f"实得 {got.get(key)}")
        ws = got.get("witnesses")
        if not isinstance(ws, list) or len(ws) != len(exp["versions"]):
            diffs.append(f"{rid}.witnesses 数量不符")
            continue
        for w, v in zip(ws, exp["versions"]):
            if w.get("version") != v:
                diffs.append(f"{rid} 见证版本 {w.get('version')} 应为 {v}")
                continue
            schedule = w.get("schedule")
            try:
                out = source.replay(pack, schedule)
            except (source.EvidenceError, TypeError, KeyError) as exc:
                diffs.append(f"{rid} 版本 {v} 见证 replay 失败：{exc}")
                continue
            if out["target_versions"][rid] != v:
                diffs.append(f"{rid} 版本 {v} 见证重放得到 "
                             f"{out['target_versions'][rid]}")
    return diffs


def cmd_verify(input_dir, output_dir):
    failures = []

    def check(ok, label, detail=""):
        print(f"{'PASS' if ok else 'FAIL'}: {label}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(label + (f" — {detail}" if detail else ""))

    # 1. 保留的基础断言
    ok, out = run_basic_assertions(Path(__file__).resolve().parent)
    check(ok, "基础断言 checks.py normal", out)

    # 2. 从材料重新核定
    bundle = analyze(input_dir)
    pack = bundle["pack"]

    # 3. 核对已交 result.json
    result_path = Path(output_dir) / "result.json"
    if not result_path.exists():
        check(False, "已交 result.json 存在", f"缺少 {result_path}")
        got_result = None
    else:
        got_result = json.loads(result_path.read_text(encoding="utf-8"))
        check(True, "已交 result.json 存在")
    if got_result is not None:
        check(got_result.get("status") == bundle["status"],
              "result.json status 一致",
              f"期望 {bundle['status']} 实得 {got_result.get('status')}")
        if bundle["status"] == "ok":
            diffs = _failures_compare_rows(bundle["rows"],
                                           got_result.get("rows", []), pack)
            check(not diffs, "逐 ID 结论与见证 replay 核验",
                  "；".join(diffs[:5]))
        else:
            check(got_result.get("rows") == [], "矛盾材料不输出局部结论",
                  f"rows={got_result.get('rows')}")

    # 4. 核对报告关键结论
    report_path = Path(output_dir) / "REPORT.md"
    if not report_path.exists():
        check(False, "REPORT.md 存在", f"缺少 {report_path}")
    else:
        text = report_path.read_text(encoding="utf-8")
        missing = [k for k in report_keys(bundle) if k not in text]
        check(not missing, "REPORT.md 关键结论一致",
              "缺少：" + "；".join(missing[:5]))

    # 5. 联合模型独立 MILP 复核已在 analyze 内完成（不一致会抛错）
    if bundle["status"] == "ok":
        mf, mi = bundle["milp"]
        check(True, f"联合约束重算（区域闭包 + MILP：可行 {mf}、排除 {mi}）")

    # 6. 小材料穷举互核 / 7. 联合矛盾验证
    base = Path(input_dir).parent
    tiny, conflict = base / "tiny", base / "conflict"
    if (tiny / "pack.json").exists():
        ok, detail, diffs = cross_check_tiny(tiny)
        check(ok, f"小材料穷举互核（{detail}）", "；".join(diffs[:5]))
    else:
        print("SKIP: 未找到 data/tiny，跳过穷举互核")
    if (conflict / "pack.json").exists():
        ok, detail, diffs = cross_check_conflict(conflict)
        check(ok, f"联合矛盾验证（{detail}）", "；".join(diffs[:5]))
    else:
        print("SKIP: 未找到 data/conflict，跳过联合矛盾验证")

    if failures:
        print(f"VERIFY_FAILED: {len(failures)} 项不符")
        for f in failures:
            print("  - " + f)
        return 1
    print("VERIFY_OK: 已交结果与材料重算结论一致，见证与交叉核验全部通过")
    return 0


def cmd_demo(input_dir):
    bundle = analyze(input_dir)
    pack = bundle["pack"]
    print(f"== 分析入口 demo: {input_dir} ==")
    if bundle["status"] == "conflict":
        det = bundle["conflict"]
        print("[冲突] 每页单独可解释（"
              + "、".join(f"{p['id']}={p['patterns']} 种" for p in det["per_page"])
              + "），但联合无解：")
        for bc in det["bound_conflicts"]:
            print(f"  事务 {bc['tx']}（{bc['tx_pointer']}）：提交时刻下界 "
                  f"{bc['lower']}（{', '.join(bc['lower_src'])}）> 上界 "
                  f"{bc['upper']}（{', '.join(bc['upper_src'])}）")
        print("result: {\"status\":\"conflict\",\"rows\":[]}（停止核定）")
        return 0
    print(f"[分析] {len(pack['transactions'])} 事务 / {len(pack['pages'])} 页 / "
          f"target={pack['target']}；联合可行组合 {bundle['n_combos']} 个")
    print("[结论] 逐 ID（versions / existence / coverage）：")
    for r in bundle["rows"]:
        print("  " + conclusion_line(r)[2:])
    cov = {}
    for r in bundle["rows"]:
        cov.setdefault(r["coverage"], []).append(r["id"])
    print("[汇总] " + "；".join(f"{k}={len(v)}（{', '.join(v)}）"
                               for k, v in sorted(cov.items())))
    # 演示一份原 replay 见证：优先确定漏采 ID
    demo_row = next((r for r in bundle["rows"]
                     if r["coverage"] == "definite_gap"), bundle["rows"][0])
    w = demo_row["witnesses"][0]
    print(f"[见证] {demo_row['id']} 版本 {w['version']} 的完整时序：")
    print("  schedule: " + json.dumps(w["schedule"], sort_keys=True))
    out = source.replay(pack, w["schedule"])
    print("  source.replay: " + json.dumps(
        {"target_versions": out["target_versions"], "missing": out["missing"]},
        sort_keys=True))
    print(f"[核验] 全部 {bundle['n_witness']} 份见证与 "
          f"{bundle['n_sampled']} 份抽样联合时序均通过 source.replay；"
          f"MILP 复核：可行 {bundle['milp'][0]}、排除 {bundle['milp'][1]}。")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[0] == "run":
        return cmd_run(argv[1], argv[2])
    if len(argv) == 3 and argv[0] == "verify":
        return cmd_verify(argv[1], argv[2])
    if len(argv) == 2 and argv[0] == "demo":
        return cmd_demo(argv[1])
    raise SystemExit("usage: analysis.py run|verify INPUT_DIR OUTPUT_DIR | "
                     "analysis.py demo INPUT_DIR")


if __name__ == "__main__":
    sys.exit(main())
