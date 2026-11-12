# flight-switchback

试飞工程师按控制模式核对累计发电量、可用分钟和风速范围，每个区块采用事先安排的控制模式。

## 使用

运行需要 Python 3.11 或更新版本，没有第三方运行依赖。数据在本地处理，不请求外部接口。

```bash
python3 -m analysis_app summary
python3 -m analysis_app summary --input data/observations.csv --output output/summary.json
python3 -m analysis_app switchback --design design/switchback_design.json
python3 -m analysis_app demo
```

标准输出是完整 JSON，输出文件使用临时文件原子替换；失败退出码为 2，不覆盖既有结果。空数据集保留零计数与空分组，缺失数值不进入数值分母。数据指纹由按 observation_id 排序后的解析记录计算。

隔离模式（`python3 -I`，忽略 cwd 与 PYTHONPATH）下使用脚本入口：

```bash
python3 -I scripts/run_analysis.py switchback --design design/switchback_design.json
python3 -I scripts/run_analysis.py demo
```

## switchback 实验分析

`switchback` 子命令在既有电量核算之上做切换实验的意向处理分析。区块分配、
洗脱分钟数、最小有效区块数、分析截止分钟全部由设计文件显式给定
（`design/switchback_design.json`），方法与处置规则见
[docs/switchback.md](docs/switchback.md)。要点：

- 主分析按 `planned_mode` 做 ITT，实际 `mode` 只用于执行偏离诊断；
  `available=0` 的分钟照常计入计划暴露，不剔除低产时段。
- 切换后洗脱段与跨截止区块按固定规则处置，全部分钟在 `exposure_audit`
  中有去向，电量与分钟数双向守恒。
- 随机化检验只在航次级整体翻转模式分配（保留航次内区块结构，不打散分钟）；
  小样本精确枚举 2^K，大样本用有种子蒙特卡洛并披露次数，报告与电量口径
  一致的差值、随机化区间、p 值与有效分配数。
- 洗脱窗口敏感性、风况失衡与执行偏离诊断随结果输出，触发阈值时结论降级为
  仅描述；同配置输出可逐字节重放。
- `demo` 用合成航次在一次本地命令内验证零效应、明显残留、缺失区块与
  不同航次长度场景。

## 验证与容器

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q analysis_app
docker build -t flight-switchback .
docker run --rm flight-switchback
```

容器内也可执行 `python -m unittest discover -s tests -v`。镜像构建阶段取得 Python 镜像，产品启动、测试和演示均无需下载。小规模样例的完整单元测试和 summary 可以连续执行，不需要等待定时任务。

## 代码与数据

- `analysis_app/io.py`：有类型的 CSV 读取、完整性校验及内容指纹。
- `analysis_app/schema.py`：字段类型、主体与聚合维度。
- `analysis_app/domain.py`：当前业务核算函数。
- `analysis_app/summary.py`：确定性的分组与数值口径。
- `analysis_app/design.py`：预先确定的设计参数加载、校验与数据一致性核对。
- `analysis_app/blocks.py`：分钟分类（洗脱/截止/停机保留）、区块聚合与暴露审计。
- `analysis_app/randomization.py`：航次级符号翻转的枚举/蒙特卡洛随机化检验与区间。
- `analysis_app/inference.py`：ITT 分析编排、风况与偏离诊断、降级规则、洗脱敏感性。
- `analysis_app/synthetic.py`：可重放的合成航次生成器与验证场景集。
- `analysis_app/cli.py`：本地命令及原子输出。
- `scripts/run_analysis.py`：`python3 -I` 隔离模式入口。
- `docs/data.md`：数据契约；`docs/switchback.md`：switchback 方法说明。

演示数据全部为本地合成，不对应现实机构、设备或人员；不能用来推断真实业务效果。
