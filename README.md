# 跨境义诊器材交接

海外眼科行动跟踪便携筛查设备、无菌耗材、校准与返程结存的后端内核。

`fixtures/equipment_handoff.json` 保存一条脱敏的业务样例（v1 元数据信封）。
业务状态不直接写库，而是由 **append-only 事件流**重放得到：扫码动作先暂存、
经守恒与合规校验后才追加落盘，任何被拒绝的动作不产生副作用。

## 回答三个问题

任意时刻从事件重放出 `Inventory` 投影后：

- `where_is(ref)` —— 某箱件/设备/耗材在哪里、什么状态、谁持有；
- `usable(ref, as_of=...)` —— 是否还能使用（效期、隔离、校准、软件版本），不可用给出原因；
- `custodian(ref)` —— 由谁接管（留置资产返回双语交接的责任确认凭证）。

行动结束用 `reconcile()` 复算结存，输出**只有聚合数量**，不含就诊代号等任何患者信息。

## 数量守恒

每行库存恒满足：

```
registered = available + issued + quarantined
           + consumed + written_off + retained + shipped_back + destroyed
           + subdivided
```

- 拆箱/分装：仅在行间平移（`subdivided` 对应转入直接子行的登记量）；
- 领用/借用/归还：`available ⇄ issued` 状态平移；
- 消耗：必须先领用，且必须带脱敏就诊代号 `ENC-XXXX` 或认可的非临床用途
  （`training` 等），防止"开封未记用途"；
- 报损/销毁/返运/留置：终态处置，分别强制见证人、销毁凭证、运单+报关单号、
  双语交接包。

## 同步与去重

- 所有 `occurred_at` 必须带时区偏移，落库统一为 UTC，本地时间另存于
  `local_occurred_at`，跨时区补传不会打乱重放顺序；
- 扫码离线补传携带 `client_event_id`，重放整批缓存幂等丢弃，**不重复扣库存**；
- 航段通知按业务身份计算语义指纹（`notice_fingerprint`），多网关、多时区
  重复推送只更新一次位置，**不产生库存**。

## 设备启用核对

`enable_device()` 同时核对：校准有效期、软件版本与设备要求一致、操作者具备
设备要求且未过期的资质。任一失败：

- 不写"已启用"事件（诊疗未发生）；
- 只隔离该设备及其登记附件（精准隔离），同诊室其他设备、耗材照常轮转；
- 重新校准/整改后用 `release_hold()` 解除即可再启用。

温湿度越限（`env_reading`）只隔离同地点的冷链物品与在场设备。

## 留置资产门槛

设备留给当地团队前，`asset_retained` 要求事件流中已齐备：

1. 中英双语交接文档（`zh` + `en`，均带文档引用）；
2. 培训验收记录（培训人 + 受训人）；
3. 接管责任确认（责任人 + 签署凭证）。

缺一即抛 `HandoffIncomplete`，设备仍归行动方负责，状态不变。

## 事件类型（v2）

`manifest_registered`（报关身份/批号效期）、`seal_verified`、`box_opened`、
`subpack_created`、`items_issued`、`consumable_used`、`items_returned`、
`equipment_checked_out` / `equipment_returned`、`env_reading`、
`item_quarantined` / `item_released`、`calibration_recorded`、
`software_verified`、`operator_registered`、`device_session_started/ended`、
`handover_docs_recorded`、`training_completed`、`responsibility_accepted`、
`asset_retained`、`item_written_off`、`items_destroyed`、`return_shipment`、
`transport_notice`。

## 合同版本迁移

- **v1**：仅元数据信封（当前样例）。
- **v2**：新增独立事件流；既有标识与时间含义逐字段保留，`events` 初始为空。
  用 `migrate_envelope(payload)` 迁移；事件本身每条带 `schema_version`，
  新增状态必须以追加新事件类型的方式扩展，不改写历史事件。

## 最小示例

```python
from mission_kit import EventStore, Inventory, MissionService

svc = MissionService(EventStore("ledger.jsonl"), Inventory())
svc.register_manifest(container_id="BOX-1", seal_id="SEAL-1", customs_ref="CUS-1",
                      contents=[...], devices=[...],
                      actor="logistics", occurred_at="2026-09-20T09:00:00+08:00")
...
print(svc.inventory.where_is("DEV-SCAN-01"))
print(svc.inventory.reconcile())
```

## 本地检查

运行 `python -m unittest discover -s tests`。
