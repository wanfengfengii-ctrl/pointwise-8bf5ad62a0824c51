# 本批分页覆盖核账报告

- 输入材料：`data/main/pack.json`（initial 18 行、transactions 18 项、pages 6 页、target=60）；材料位置一律用 JSON Pointer 标注。
- 方法：事务提交时刻与页读取时刻为同一整数刻度上的变量，受闭区间时窗与 after 严格偏序约束；每页归档行必须等于该读取时刻未删除记录按 (rank,id) 升序的 [offset, offset+limit) 切片（短页同时约束当时记录总数）。先枚举每页全部可解释的已提交下集，再合并为差分约束区域（Floyd-Warshall 闭包）做联合可行性判定；不逐页拼接快照。

## 结论：存在合法完整时序，逐 ID 核定如下

status: ok

### 联合约束规模

- 逐页可解释下集数：/pages/0（p1）= 54、/pages/1（p2）= 19、/pages/2（p3）= 9、/pages/3（p4）= 25、/pages/4（p5）= 26、/pages/5（p6）= 48。
- 联合可行组合数: 12096（同时满足全部时窗、after 偏序与全部页切片的模式组合；每个组合都是一个差分约束可行区域）。
- 短页约束：`/pages/5` 仅 1 行 < limit 3 ⇒ 其读取时刻未删除记录总数 ≤ 16。
- 各页读取时窗互不重叠且先后错位，rank 更正使行跨页移位（如 r06 由 /pages/0 的 rank 5 移至 /pages/3 的 rank 145），必须同一次合法源演进同时解释全部页，不能逐页拼接。

### 逐 ID 结论（核查时刻 target=60）

| ID | 全部可能有效版本 | 存在性 | 覆盖判定 | 材料位置（initial / writes / captured） |
| --- | --- | --- | --- | --- |
| r01 | [1] | must | complete | 初始:`/initial/0` ｜ 更正:无 ｜ 归档:`/pages/0/rows/1` |
| r02 | [2, 3] | must | possible_gap | 初始:`/initial/1` ｜ 更正:`/transactions/2/writes/1`,`/transactions/8/writes/1`,`/transactions/16/writes/1` ｜ 归档:`/pages/0/rows/2`,`/pages/3/rows/0` |
| r03 | [2, 3, 4] | must | possible_gap | 初始:`/initial/2` ｜ 更正:`/transactions/0/writes/0`,`/transactions/6/writes/0`,`/transactions/12/writes/0` ｜ 归档:`/pages/2/rows/2` |
| r04 | [1] | must | complete | 初始:`/initial/3` ｜ 更正:无 ｜ 归档:`/pages/1/rows/1` |
| r05 | [2, 3] | must | definite_gap | 初始:`/initial/4` ｜ 更正:`/transactions/3/writes/1`,`/transactions/9/writes/1`,`/transactions/17/writes/1` ｜ 归档:无 |
| r06 | [2, 3] | must | complete | 初始:`/initial/5` ｜ 更正:`/transactions/1/writes/0`,`/transactions/7/writes/0`,`/transactions/13/writes/0` ｜ 归档:`/pages/0/rows/0`,`/pages/3/rows/2` |
| r07 | [1] | must | complete | 初始:`/initial/6` ｜ 更正:无 ｜ 归档:`/pages/2/rows/0` |
| r08 | [null, 2, 4] | may | possible_gap | 初始:`/initial/7` ｜ 更正:`/transactions/4/writes/1`,`/transactions/10/writes/1`,`/transactions/12/writes/1` ｜ 归档:`/pages/4/rows/0` |
| r09 | [null, 2] | may | possible_gap | 初始:`/initial/8` ｜ 更正:`/transactions/2/writes/0`,`/transactions/8/writes/0`,`/transactions/14/writes/0` ｜ 归档:无 |
| r10 | [1] | must | definite_gap | 初始:`/initial/9` ｜ 更正:无 ｜ 归档:无 |
| r11 | [1, 2] | must | definite_gap | 初始:`/initial/10` ｜ 更正:`/transactions/5/writes/1`,`/transactions/11/writes/1`,`/transactions/14/writes/1` ｜ 归档:`/pages/4/rows/2` |
| r12 | [2, 3] | must | definite_gap | 初始:`/initial/11` ｜ 更正:`/transactions/3/writes/0`,`/transactions/9/writes/0`,`/transactions/15/writes/0` ｜ 归档:无 |
| r13 | [1] | must | complete | 初始:`/initial/12` ｜ 更正:无 ｜ 归档:`/pages/3/rows/1` |
| r14 | [null, 2] | may | complete | 初始:`/initial/13` ｜ 更正:`/transactions/0/writes/1`,`/transactions/6/writes/1`,`/transactions/13/writes/1` ｜ 归档:`/pages/1/rows/0` |
| r15 | [2, 3] | must | possible_gap | 初始:`/initial/14` ｜ 更正:`/transactions/4/writes/0`,`/transactions/10/writes/0`,`/transactions/16/writes/0` ｜ 归档:`/pages/1/rows/2` |
| r16 | [1] | must | complete | 初始:`/initial/15` ｜ 更正:无 ｜ 归档:`/pages/4/rows/1` |
| r17 | [null, 3] | may | possible_gap | 初始:`/initial/16` ｜ 更正:`/transactions/1/writes/1`,`/transactions/7/writes/1`,`/transactions/15/writes/1` ｜ 归档:`/pages/5/rows/0` |
| r18 | [1, 2] | must | possible_gap | 初始:`/initial/17` ｜ 更正:`/transactions/5/writes/0`,`/transactions/11/writes/0`,`/transactions/17/writes/0` ｜ 归档:`/pages/2/rows/1` |

- 存在性：必然存在（must）14 项；可能存在（may）4 项：r08, r09, r14, r17；从不存在（never）0 项。
- 覆盖：完整取得（complete）7 项：r01, r04, r06, r07, r13, r14, r16；确定漏采（definite_gap，每个时序均有效但未取得）4 项：r05, r10, r11, r12；可能漏采（possible_gap）7 项：r02, r03, r08, r09, r15, r17, r18。
- complete 允许某些时序不存在（须结合存在性阅读）：如 r14 在部分时序中 target 时刻已删除（rev3，`/transactions/6/writes/1` deleted=true），但所有存在时序中其版本必为 rev2 且已归档（`/pages/1/rows/0`）。

### 联合时间与排序约束如何排除错误解释

- 版本排除共 22 项：纯时窗/因果排除 20 项，联合分页约束排除 2 项；全部经联合区域闭包判定，并由 scipy.optimize.milp 独立编码复核一致。
  - r02 停留初始版（rev1）：首次写入事务 t03（`/transactions/2`）时窗 [1, 44] 的上界不晚于 target=60，提交必然先生效，初始版不可能保留到 target。
  - r02 rev4：该版本的写入事务 t17（`/transactions/16`）时窗 [84, 125] 的下界晚于 target=60，不可能在 target 前（含）提交。
  - r03 停留初始版（rev1）：首次写入事务 t01（`/transactions/0`）时窗 [1, 30] 的上界不晚于 target=60，提交必然先生效，初始版不可能保留到 target。
  - r05 停留初始版（rev1）：首次写入事务 t04（`/transactions/3`）时窗 [6, 50] 的上界不晚于 target=60，提交必然先生效，初始版不可能保留到 target。
  - r05 rev4：该版本的写入事务 t18（`/transactions/17`）时窗 [90, 125] 的下界晚于 target=60，不可能在 target 前（含）提交。
  - r06 停留初始版（rev1）：首次写入事务 t02（`/transactions/1`）时窗 [1, 38] 的上界不晚于 target=60，提交必然先生效，初始版不可能保留到 target。
  - r06 rev4（删除态）：该版本的写入事务 t14（`/transactions/13`）时窗 [66, 110] 的下界晚于 target=60，不可能在 target 前（含）提交。
  - r08 停留初始版（rev1）：首次写入事务 t05（`/transactions/4`）时窗 [12, 56] 的上界不晚于 target=60，提交必然先生效，初始版不可能保留到 target。
  - r09 停留初始版（rev1）：首次写入事务 t03（`/transactions/2`）时窗 [1, 44] 的上界不晚于 target=60，提交必然先生效，初始版不可能保留到 target。
  - r09 rev4：该版本的写入事务 t15（`/transactions/14`）时窗 [72, 116] 的下界晚于 target=60，不可能在 target 前（含）提交。
  - r11 rev4：该版本的写入事务 t15（`/transactions/14`）时窗 [72, 116] 的下界晚于 target=60，不可能在 target 前（含）提交。
  - r12 停留初始版（rev1）：首次写入事务 t04（`/transactions/3`）时窗 [6, 50] 的上界不晚于 target=60，提交必然先生效，初始版不可能保留到 target。
  - r12 rev4：该版本的写入事务 t16（`/transactions/15`）时窗 [78, 122] 的下界晚于 target=60，不可能在 target 前（含）提交。
  - r14 停留初始版（rev1）：首次写入事务 t01（`/transactions/0`）时窗 [1, 30] 的上界不晚于 target=60，提交必然先生效，初始版不可能保留到 target。
  - r14 rev4：该版本的写入事务 t14（`/transactions/13`）时窗 [66, 110] 的下界晚于 target=60，不可能在 target 前（含）提交。
  - r15 停留初始版（rev1）：首次写入事务 t05（`/transactions/4`）时窗 [12, 56] 的上界不晚于 target=60，提交必然先生效，初始版不可能保留到 target。
  - r15 rev4（删除态）：该版本的写入事务 t17（`/transactions/16`）时窗 [84, 125] 的下界晚于 target=60，不可能在 target 前（含）提交。
  - r17 停留初始版（rev1）：首次写入事务 t02（`/transactions/1`）时窗 [1, 38] 的上界不晚于 target=60，提交必然先生效，初始版不可能保留到 target。
  - r17 rev4：该版本的写入事务 t16（`/transactions/15`）时窗 [78, 122] 的下界晚于 target=60，不可能在 target 前（含）提交。
  - r18 rev4：该版本的写入事务 t18（`/transactions/17`）时窗 [90, 125] 的下界晚于 target=60，不可能在 target 前（含）提交。
  - r11 rev3：需要事务 t12（`/transactions/11`）在 target=60 前提交，但全部联合可行组合中其提交时刻被页切片与 after 链约束在 [70, 97]，恒晚于 target。
  - r18 rev3：需要事务 t12（`/transactions/11`）在 target=60 前提交，但全部联合可行组合中其提交时刻被页切片与 after 链约束在 [70, 97]，恒晚于 target。
- 旧版不算取得新版：归档行按 (id,rev) 整行认定。例如 r11 归档仅取得 rev3（`/pages/4/rows/2`），而其 target 版本只可能为 1 或 2，均未被任何页取得 ⇒ 确定漏采；r17 归档取得 rev4（`/pages/5/rows/0`），但 rev4 不可能在 target 时刻有效（见上），其可能版本 rev3 未归档 ⇒ 可能漏采。

### 见证

- 每 ID 每可能版本各一份完整时序见证，共 32 份，全部通过 source.replay 重放核验（提交/读取时刻落在各自闭区间时窗内，全部页切片逐字段相符，target 版本与结论一致）。
- 另抽样 126 个联合可行组合提取具体时序，全部通过 source.replay。

### 机器可核对结论

- r01: versions=[1] existence=must coverage=complete
- r02: versions=[2, 3] existence=must coverage=possible_gap
- r03: versions=[2, 3, 4] existence=must coverage=possible_gap
- r04: versions=[1] existence=must coverage=complete
- r05: versions=[2, 3] existence=must coverage=definite_gap
- r06: versions=[2, 3] existence=must coverage=complete
- r07: versions=[1] existence=must coverage=complete
- r08: versions=[null, 2, 4] existence=may coverage=possible_gap
- r09: versions=[null, 2] existence=may coverage=possible_gap
- r10: versions=[1] existence=must coverage=definite_gap
- r11: versions=[1, 2] existence=must coverage=definite_gap
- r12: versions=[2, 3] existence=must coverage=definite_gap
- r13: versions=[1] existence=must coverage=complete
- r14: versions=[null, 2] existence=may coverage=complete
- r15: versions=[2, 3] existence=must coverage=possible_gap
- r16: versions=[1] existence=must coverage=complete
- r17: versions=[null, 3] existence=may coverage=possible_gap
- r18: versions=[1, 2] existence=must coverage=possible_gap

## 验证结果
- 联合模型查询：可行 32 项、排除 22 项，区域闭包与独立 MILP 编码全部一致。
- 见证 32 份、抽样联合时序 126 份：source.replay 全部通过。
- 基础断言 checks.py normal：通过。
- 小材料穷举互核（data/tiny，合法时序 3 个逐一枚举）：一致。
- 联合矛盾验证（data/conflict，逐页可解释、联合穷举 0 个合法时序）：确认。
