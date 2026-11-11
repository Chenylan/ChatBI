# ChatBI 离线测评

回答「我现在的模型比修改前提升了没」这一问题的可复现手段：一套**评测集**（自然语言问题 + 黄金 SQL）
加一个**harness**（跑模型 / 离线自检 + 算指标）。

## 文件

- `eval_set.json` — starter 评测集，10 条问题（Spider 风格：`id / difficulty / question / gold_sql`），
  难度从单表聚合 → 过滤 → 分组 → JOIN → HAVING → 聚合+排序取 Top1 梯度上升。
- `run_eval.py` — harness。支持两种模式：
  - `--offline`（默认）：不调 LLM，校验黄金 SQL 可执行、打印参考答案，用于先确认评测集本身是对的。
  - `--live`：真实调用 Agent（`create_agent` → `graph.invoke`），抽取模型实际执行的 SQL 与最终答案并打分。
- `eval_report.json` — `--live` 跑完后自动生成的详细报告。

## 用法

```bash
# 用项目 conda 环境（有 langchain_core）
/opt/anaconda3/envs/nlp/bin/python tests/eval/run_eval.py            # 离线自检
/opt/anaconda3/envs/nlp/bin/python tests/eval/run_eval.py --live     # 真机测评（需 OPENAI_API_KEY，花钱）
/opt/anaconda3/envs/nlp/bin/python tests/eval/run_eval.py --live --limit 3 --model qwen-plus
```

## 指标

| 指标 | 含义 | 权重 |
|------|------|------|
| **Execution Accuracy (EX)** | `pred` 结果集 == `gold` 结果集（列名 + 行多重集合，浮点四舍五入 6 位） | **主指标**，比对比 SQL 文本更鲁棒 |
| 工具调用成功率 | 从图的 ToolMessage 里抽到可执行的 SQL，且重执行不报错 | 诊断指标 |
| 格式合规率 | 最终答案是否含「数据来源/计算过程/查询结果/结论」四段 | 对应防编造 prompt |
| 数值落地（启发式） | 答案中的数字能否追溯到查询结果，识别疑似编造 | 参考，会误报 |

## 怎么用「修改前后对比」

1. 在修改代码前，先跑一次 `--live`，记下 EX（基线）。
2. 做改动。
3. 再跑一次 `--live`，对比 EX / 工具成功率 / 格式合规率的变化。
4. 因为 LLM 有随机性，最好同一条题多跑几次取均值，或固定 `temperature`（项目里已设 0.1）。

## 扩展评测集

往 `eval_set.json` 的 `questions` 数组里加条目即可，字段：
`id`（唯一）、`difficulty`、`question`（自然语言，尽量把期望返回列说清楚）、`gold_sql`。

写黄金 SQL 的小技巧：
- 先想清楚「口径」，再写 SQL，跑一遍确认答案合理。
- 问句里明确要求返回的列（例如「列出商品名和销售总数量」），否则 EX 会因模型少返回列而误判。
- 注意数据稀疏性：`example.db` 里 `ORDER_DETAILS` 只有 4 条落在 2024 年，时间类问题以 2025 年为主。
