# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。

## M7 衔接规则中心

- rule_id：`MCT|机场|到达航站楼|出发航站楼|生效日` 的稳定自然键，同键只能发布一次，内容不可变。
- 规则选择：按机场+航站楼对精确匹配，取 `effective_date <= 连接日(到达 UTC 日期)` 的最新 active 版本；withdrawn 与无匹配统一报 RULE_EXPIRED。
- snapshot_id：整单规则集合的内容哈希（与顺序无关），出票/改签把单内全部连接绑定同一快照；哈希相同复用快照行。
- 连接凭据：每个连接留存 wait_minutes、rule_id、required_minutes、decision、revision；票号由 idempotency_key 派生，重试返回同一票号且不重复出票。
- 改签：变动连接（航站楼对或两端时刻变化）按当前 active 规则重验并产生新修订；未变连接凭据原样保留；任何连接选不到有效规则则整单回滚。
- 撤销：仅 status 置 withdrawn 并记 withdrawn_at，不删除任何规则、快照或票凭据；旧票回读永远反映出票承诺。
- 边界：M7 不做当地时刻换算，只接收带偏移的 ISO 8601 时刻；等待时长复用 M5 `waiting_minutes`，判定复用 M5 `connection_result`（上限 720 分钟）。

