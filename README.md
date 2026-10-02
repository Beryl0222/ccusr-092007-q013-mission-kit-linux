# 跨境义诊器材交接

海外医疗行动跟踪便携筛查设备、无菌耗材、校准和返程结存。

`fixtures/equipment_handoff.json` 保存一条经过脱敏的业务样例（schema v2），
源代码只定义读取这份样例所需的最小合同。后续模块应保持既有标识和时间
含义，新增状态必须说明迁移方式。

## 模块

- `contracts`：登记头最小合同（record_id / domain / occurred_at 等），
  对 v1、v2 样例都可读；
- `manifest`：行动器材清单——箱件与封签、耗材批次（报关单号、批号、
  效期、储运温湿度）、设备（序列号、校准、软件版本）、人员资质、
  放行软件清单；
- `ledger`：事件溯源台账。拆箱、分装、领用、消耗、借用、报损全部
  保持数量守恒；`event_id` 幂等去重，离线扫码补传与航段重复通知
  不会制造额外库存；事件按 UTC 瞬间排序，晚到的补传事件落到正确
  位置，因缺少前因而暂缓的事件自动重试；
- `devices`：设备启用核查（校准、软件版本、操作者资质）；涉及设备
  完整性的失败只隔离该设备，不影响其他物品与诊室；
- `handover`：留置资产的双语交接——条款须覆盖清单声明的全部语言，
  有培训记录且捐赠方、接收方均确认责任后才算完成；交接、返运、
  销毁都留下带内容指纹的凭证；
- `reports`：随时回答某件物品在哪里、是否还能使用、由谁接管；
  `closing_balance` 按分装谱系复算结存并校验守恒，输出只含数量
  与去向，不含 `usage_ref` 等患者诊疗详情；
- `store`：JSONL 事件日志，追加写入、全量重放。

## 状态与迁移

schema_version 1 → 2：新增 `mission` 清单块。v1 文件只有登记头，
`load_record` 行为不变；`load_manifest` 要求 v2，否则抛出
`ManifestError`。状态机为 v2 新增概念，v1 无状态数据，无需迁移。

批次在账状态：`sealed`（箱内封存）→ `stock`（临时库房）→
`issued`（领用到诊室）/ `borrowed`（借出）；`quarantined`（隔离，
解除后回到原状态）、`damaged`（报损留存待处置）。终态：`consumed`
（消耗，须登记脱敏用途凭证）、`destroyed`、`handed_over`、
`shipped_back`、`written_off`（遗失核销）。设备状态：`in_case` →
`warehouse` → `room`，异常时 `quarantined`，终态 `handed_over` /
`shipped_back` / `destroyed`。后续新增状态值须在此登记含义与迁移
路径。

时间约定：所有事件时间必须携带时区偏移（ISO 8601），朴素时间在
入帐前拒绝；排序一律按 UTC 瞬间，同一瞬间按到达先后。

## 本地检查

运行 `python -m unittest discover -s tests`。
