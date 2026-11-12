# 数据契约

试飞工程师按控制模式核对累计发电量、可用分钟和风速范围，每个区块采用事先安排的控制模式。

主测量 `energy` 的单位为千瓦时，主体字段是 `flight`，核算维度是 `mode`。`observation_id` 是文件内唯一观测编号，主体允许重复观测。数值只接受有限值；空单元格解析为 None，不能直接当成零。额外列不进入当前核算。

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `observation_id` | str | 必填 |
| `flight` | str | 必填 |
| `mode` | str | 必填 |
| `block` | int | 必填 |
| `minute` | int | 必填 |
| `energy` | float | 必填 |
| `wind` | float | 必填 |
| `switch_minute` | int | 必填 |
| `planned_mode` | str | 必填 |
| `available` | int | 必填 |

时间字段 day、week、tick、hour、minute、cycle、start_day、reported_day、known_at、visible_at、opened、observed 均以各字段名所述的相对时间单位表示整数；不同字段并不必然代表同一种时间。缺少的登记码与浓度等信息保持为空。示例只表达产品数据结构，不能代替实际调查资料。

## 业务字段语义

energy 是该分钟产出电量，单位千瓦时；available=0 表示该分钟停机，产出为零。wind 为米每秒。block 是航次内分配区块，switch_minute 是区块起始分钟，planned_mode 是计划分配，mode 是实际执行。

## switchback 分析中的字段角色

switchback 分析（见 [switchback.md](switchback.md)）以设计文件中的预先分配
为准，本文件里的 `planned_mode` 仅用于逐分钟校验设计文件是否被正确执行。
对比分组始终使用计划分配（ITT），实际 `mode` 只进入执行偏离诊断；
`available=0` 的分钟不产生电量但仍计入计划暴露分钟。`block`、`switch_minute`
与 `minute` 共同决定洗脱段与分析截止点的分钟分类。
