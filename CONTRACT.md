# 本批分页覆盖核账

## 已有材料与运行
data/main/pack.json 是本批封存输入，包含源端导出的 initial、事务审计 transactions、请求及原始响应 pages，target 是核查时刻。JSON Pointer 是材料位置，例如 /transactions/0/writes/1、/pages/0/rows/0。所有证据都在此文件内，不连接门户。
source.py 提供 load(directory)、snapshot(pack,commits,at)、replay(pack,schedule)。它只判断给定时序，没有覆盖推断器。
python3 checks.py normal 保留正常业务回归。
python3 source.py data/tiny data/tiny/schedule.json 展示实际页内容、目标版本和漏采ID。此见证在 target=3 时提交，missing 应为 ["b","c"]；c 没有被任何页取得，a 的新版已取得，b 只取得旧版。
requirements.txt 提供分析所用 SciPy，milp 可用；不要求开发通用求解器。

## 封存格式与源规则
行统一为 [id,rev,rank,value,deleted]，id 是非空字符串，rev 为正整数，rank/value 为整数，deleted 为布尔值。initial 是时刻0的有效行。ID全集是 initial 与全部 writes 的ID并集。
每个事务含 id、window=[lo,hi]、after=[前驱事务ID]、writes=[行...]。每事务恰提交一次，时刻为闭区间内的整数，同一事务的写入原子生效。前驱严格早于后继；同ID的全部版本通过 after 有序，rev 每次加1，不允许两事务并发覆盖同ID。提交完整替换行，deleted=true 撤回，后续版本可复活；不存在的ID尚未首次写入时也视为不存在。其他事务可交换提交，不能把事务内多行拆开解释。
所有时间使用同一相对整数刻度。每页含 id、window、offset、limit、rows；读取在 window 的某个整数时刻原子发生，已经提交的事务全部生效。提交时刻等于读取或 target 时刻时，提交先发生。无因果关系的同刻事务写不同ID，效果可交换。数组顺序本身不添加提交或读取先后约束，时窗与 after 才是时间证据。
源页从该时刻未删除的完整记录集按 (rank,id) 升序排序，跳过 offset 行、取至多 limit 行，返回整行；rows 是原始归档响应，顺序及所有字段须完全相符，短页也约束当时记录总数。rank相同时按Python字符串顺序比较ID；value不参与排序。offset不随前页内容修正，不能把动态分页当成同一快照。
仅接受能同时解释所有页、所有事务及其时窗的完整时序。核查对象为 target 时刻的有效版本；归档任意页中同 (id,rev) 的整行即算取得该版本，不限制归档发生在 target 前还是后。不同版本的rank/value相同也不能代替目标版本。

## 公开交付接口
python3 analysis.py run INPUT_DIR OUTPUT_DIR
读取 INPUT_DIR/pack.json，重算并写入 OUTPUT_DIR/result.json 与 OUTPUT_DIR/REPORT.md，不改变输入或source.py、checks.py原断言。
python3 analysis.py verify INPUT_DIR OUTPUT_DIR
从材料重新核定版本集合和分类，核对已交结果、逐项调用原 replay 核验见证，并核对报告关键结论；执行小材料穷举互核及联合矛盾验证。不能只检查文件存在或读取缓存结论。核验不通过须非零退出并说明差异。
python3 analysis.py demo INPUT_DIR
重新分析并展示真实覆盖结论及至少一个原 replay 的见证结果；矛盾材料展示冲突。将 .pointwise-runtime.json 的 verify/demo 接到本批这两个入口，run 的输出目录采用 output。
正常结果为 {"status":"ok","rows":[...]}，rows 按ID排序。每项含：
- id：记录ID。
- versions：全部可行时序中该ID在target时刻的互异有效rev，删除或尚未创建合并为null；null在前，其余升序。
- existence："must" 表示全部时序存在，"may" 表示部分时序存在，"never" 表示全部时序不存在。
- coverage："absent" 对应never；其他ID若不存在“有效但未取得”的时序为"complete"；若每个时序均有效但未取得为"definite_gap"；其余存在漏采可能时为"possible_gap"。完整取得允许某些时序不存在，须结合existence阅读。
- witnesses：[{"version":rev或null,"schedule":{"commits":{"事务ID":整数时刻},"reads":{"页ID":整数时刻}}},...]，每个versions值恰有一份完整见证。
- sources：{"initial":[JSON Pointer...],"writes":[JSON Pointer...],"captured":[JSON Pointer...]}，列出该ID的初始行、全部源更正行、归档行的位置；无对应材料用空数组。
没有合法完整时序时，result.json 为 {"status":"conflict","rows":[]}，报告指出矛盾材料位置并解释，不能继续输出局部覆盖结论，不要求最小冲突集。

报告只需说明本批结论、联合时间与排序约束如何排除错误解释、证据位置和验证结果，明确区分可能与确定。每ID每版本见证及联合模型的不可行查询共同支持完整性，不要求理论证明、最少补采或全部时序。
data/tiny 只有一项原子移位更正，可穷举全部整数时刻独立求期望，不复用分析器的联合建模逻辑。data/conflict 中每页单独均可解释，但联合无解；checks.py 已通过逐时刻穷举固定此事实。source.replay 发现无效见证会抛 EvidenceError。所有版本可能性与分类必须由本批输入重算，不按ID硬编码。
