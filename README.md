# cancer_policy_graph

服务政策证据处理：文档条款、术语关系、版本差异、解释冲突及证据导出。当前入口是 `cancer_policy_graph.cli`，它使用本地 JSON 资料和 SQLite 状态；目录 `cancer_policy_graph` 存放业务模块，`tests/policy_graph` 验证既有业务行为。

## 环境

使用 Python 3.11，全部依赖来自标准库。`requirements.txt` 为空，不需要下载数据、模型或第三方包。数据库在运行时创建，可以删除临时数据库重新开始。

## 检查

```bash
python3 -m unittest discover -s tests/policy_graph -v
python3 -m unittest discover -s tests/analysis_context -v
python3 -m compileall -q cancer_policy_graph tests scripts
```

## 运行

```bash
python3 -m cancer_policy_graph.cli  fixtures/demo.json --db /tmp/cancer_policy_graph-demo.sqlite3
python3 -m cancer_policy_graph.impact fixtures/policy_impact.json --output /tmp/policy-impact
python3 scripts/check_project.py
```

批量组合影响分析（`cancer_policy_graph.impact`）把基准与多个候选组合应用到同一份人群快照上，
不决定个人待遇、不作诊断。输出四个机器可读文件：`manifest.json`（名册/解释/资金三个冻结版本、
假设与免责声明）、`joint_results.json`（联合覆盖的确定/可能人数、预算区间、支付方口径、
导致区间变宽的条款与人群）、`interactions.json`（leave-one-out 边际、成对交互、支付方转移、
相对基准的新增受益人，均可回查居民匿名键与条款证据）、`unrecognized.json`（不识别条款、
未生效或已撤回条款、未裁决条款、属性未知/过期、被拒绝的组合及原因）。支付上限、互斥与
补充抵扣按声明的业务关系计算，与遍历顺序无关；无法解释的组合会被拒绝并记录原因，不阻断
其他可行组合。

`check_project.py` 串行执行测试、编译和真实业务入口，将本次业务输出完整打印到终端；状态文件使用独立临时目录，重复运行不沿用上次状态。

批量分析资料位于 `fixtures/policy_impact.json`，字段含义见 `docs/data.md`。它与当前单笔流程的输入格式不同，不能直接交给原来的入口处理。
