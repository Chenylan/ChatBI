#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ChatBI 离线测评 harness。

两种运行模式：
  --offline（默认）：不调用 LLM，仅校验评测集、用黄金 SQL 计算参考答案并自检，输出统计。
  --live           ：真实调用 Agent（需 OPENAI_API_KEY），对每条问题跑图，抽取 pred_sql 与
                     最终答案，计算 Execution Accuracy / 工具调用成功率 / 格式合规 / 数值落地。

用法：
  python tests/eval/run_eval.py                     # 离线自检（不花钱、不需要 key）
  python tests/eval/run_eval.py --live              # 真机测评（花钱，需要 OPENAI_API_KEY）
  python tests/eval/run_eval.py --live --limit 3    # 只跑前 3 条
  python tests/eval/run_eval.py --live --model qwen-plus

指标说明：
  Execution Accuracy (EX)：pred 结果集 == gold 结果集（列名 + 行多重集合，浮点四舍五入到 6 位）。
                          这是 Text-to-SQL 最核心的指标，比看 SQL 文本是否一致更鲁棒。
  工具调用成功率           ：从图的 ToolMessage 中成功抽到可执行的 SQL。
  格式合规                 ：最终答案是否包含「数据来源/计算过程/查询结果/结论」四段。
  数值落地（启发式）       ：最终答案中的数字是否能追溯到查询结果，防止编造。仅供参考，会误报。
"""
import argparse
import json
import os
import re
import sqlite3
import sys

# 项目根目录加入 sys.path，保证 `import agent` / `from tools...` 可用
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EVAL_SET_PATH = os.path.join(os.path.dirname(__file__), "eval_set.json")
DB_PATH = os.path.join(ROOT, "tools", "example.db")


# --------------------------------------------------------------------------- #
# 结果集规范化与比较
# --------------------------------------------------------------------------- #
def _round(v, nd=6):
    try:
        f = float(v)
        if f == int(f):
            return int(f)
        return round(f, nd)
    except (TypeError, ValueError):
        return v


def _canonical(columns, rows):
    """把 (columns, rows) 规整成可比较的多重集合：按行排序、列名小写、浮点四舍五入。"""
    cols = tuple((c or "").lower() for c in columns)
    norm_rows = sorted(tuple(_round(x) for x in r) for r in rows)
    return (cols, norm_rows)


def _exec_sql(sql, db_path=DB_PATH):
    """用原生 sqlite3 执行 SQL（绕过只读守卫，纯本地评测用），返回 (columns, rows)。"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(sql)
        if cur.description is None:
            return None, None  # 写语句或非查询
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return columns, rows
    finally:
        conn.close()


def _result_sets_equal(pred_columns, pred_rows, gold_columns, gold_rows):
    return _canonical(pred_columns, pred_rows) == _canonical(gold_columns, gold_rows)


# --------------------------------------------------------------------------- #
# 从图的 messages 中抽取 pred_sql / 最终答案
# --------------------------------------------------------------------------- #
def _recursive_find_query(obj, depth=0):
    """在（可能被 tool_executor 包一层的）ToolMessage 内容里，递归找含 query 的结果字典。"""
    if depth > 4 or obj is None:
        return None
    if isinstance(obj, dict):
        if isinstance(obj.get("query"), str):
            return obj["query"]
        for v in obj.values():
            got = _recursive_find_query(v, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _recursive_find_query(v, depth + 1)
            if got:
                return got
    return None


def _extract_pred_sql(messages):
    """从图状态取「最后一条」由 execute_sqlite_query 实际执行的 SQL。"""
    from langchain_core.messages import ToolMessage
    sqls = []
    for m in messages:
        if isinstance(m, ToolMessage):
            content = m.content
            obj = content
            if isinstance(content, str):
                try:
                    obj = json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    continue
            q = _recursive_find_query(obj)
            if q:
                sqls.append(q)
    return sqls[-1] if sqls else None


def _extract_final_answer(result):
    """取图最终状态里最后一条 AIMessage 的 content（即跑完工具后的最终回答）。"""
    from langchain_core.messages import AIMessage
    if result and isinstance(result, dict):
        for m in reversed(result.get("messages", [])):
            if isinstance(m, AIMessage) and m.content:
                return m.content if isinstance(m.content, str) else str(m.content)
    return ""


# --------------------------------------------------------------------------- #
# 指标计算
# --------------------------------------------------------------------------- #
FORMAT_MARKERS = ("数据来源", "计算过程", "查询结果", "结论")


def _format_compliance(final_answer):
    """最终答案是否包含四段式防编造格式。"""
    if not final_answer:
        return False
    return all(marker in final_answer for marker in FORMAT_MARKERS)


def _numeric_grounding_violations(final_answer, pred_rows, gold_rows):
    """启发式：最终答案里的数字能否追溯到查询结果（无法追溯的记为疑似编造）。

    会误报（年份、序号、百分比等），只作参考，不参与主指标。
    跳过：小整数(<=20)、年份(1900-2100)、以及能四舍五入匹配到 pred/gold 结果里的数。
    """
    pool = set()
    for rows in (pred_rows or [], gold_rows or []):
        for r in rows:
            for x in r:
                try:
                    pool.add(round(float(x), 2))
                except (TypeError, ValueError):
                    pass

    text = final_answer or ""
    viol = []
    for tok in re.findall(r"-?\d+(?:\.\d+)?", text):
        f = float(tok)
        if f.is_integer() and abs(f) <= 20:      # 小计数/序号
            continue
        if 1900 <= f <= 2100 and f.is_integer():  # 年份
            continue
        if round(f, 2) in pool:
            continue
        viol.append(tok)
    return viol


# --------------------------------------------------------------------------- #
# 离线自检
# --------------------------------------------------------------------------- #
def run_offline():
    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        data = json.load(f)
    questions = data["questions"]

    print(f"离线自检：{len(questions)} 条问题，DB = {data['meta']['db']}\n")
    ok = 0
    for q in questions:
        columns, rows = _exec_sql(q["gold_sql"])
        if columns is None or rows is None:
            print(f"[{q['id']}] FAIL  gold_sql 不可执行：{q['gold_sql']}")
            continue
        ok += 1
        preview = str(rows[:5]) + (" ..." if len(rows) > 5 else "")
        print(f"[{q['id']}][{q['difficulty']:11s}] {q['question']}")
        print(f"  SQL: {q['gold_sql']}")
        print(f"  ANS: {preview}  (共 {len(rows)} 行)")
    print(f"\n离线自检通过 {ok}/{len(questions)} 条。")
    print("确认黄金 SQL 全部可执行、答案合理后，用 `--live` 跑真机测评。")


# --------------------------------------------------------------------------- #
# 真机测评
# --------------------------------------------------------------------------- #
def run_live(model, limit):
    # 延迟导入 agent：import 会初始化 MCP client 与 tools_charts 里的 LLM，只有 --live 才需要。
    from langchain_core.messages import HumanMessage
    from langchain_core.callbacks.base import BaseCallbackHandler
    from agent import MessagesState, create_agent

    class _NoopCallback(BaseCallbackHandler):
        pass

    if not os.getenv("OPENAI_API_KEY"):
        print("[ERROR] 未设置 OPENAI_API_KEY，无法跑 --live。可先 `--offline` 自检。")
        return

    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        questions = json.load(f)["questions"]
    if limit:
        questions = questions[:limit]

    graph = create_agent(_NoopCallback(), model, memory_prefix="")

    results = []
    for i, q in enumerate(questions, 1):
        qid = q["id"]
        print(f"\n=== [{i}/{len(questions)}] {qid} [{q['difficulty']}] {q['question']} ===")
        try:
            state = MessagesState(messages=[HumanMessage(content=q["question"])])
            result = graph.invoke(
                state,
                config={"configurable": {"thread_id": f"eval-{qid}"}, "recursion_limit": 100},
            )
        except Exception as e:
            print(f"  [ERROR] agent 执行失败: {e}")
            results.append({"id": qid, "error": str(e), "ex": False, "tool_success": False,
                            "format_compliance": False})
            continue

        pred_sql = _extract_pred_sql(result["messages"])
        final_answer = _extract_final_answer(result)

        # 1) 工具调用成功 + 重执行 pred_sql
        tool_success = False
        pred_columns = pred_rows = None
        if pred_sql:
            try:
                pred_columns, pred_rows = _exec_sql(pred_sql)
                tool_success = pred_columns is not None
            except sqlite3.Error as e:
                print(f"  [WARN] pred_sql 执行失败: {e}")

        # 2) Execution Accuracy：pred 结果集 vs gold 结果集
        gold_columns, gold_rows = _exec_sql(q["gold_sql"])
        ex = bool(tool_success) and _result_sets_equal(
            pred_columns, pred_rows, gold_columns, gold_rows)

        # 3) 格式合规
        fmt = _format_compliance(final_answer)

        # 4) 数值落地（启发式）
        viol = _numeric_grounding_violations(final_answer, pred_rows, gold_rows)

        print(f"  pred_sql: {pred_sql or '(未抽取到)'}")
        print(f"  最终答案: {final_answer[:120]}{'...' if len(final_answer) > 120 else ''}")
        print(f"  EX={ex}  工具成功={tool_success}  格式合规={fmt}  疑似编造数字={viol or '无'}")

        results.append({"id": qid, "question": q["question"], "difficulty": q["difficulty"],
                        "pred_sql": pred_sql, "final_answer": final_answer,
                        "ex": ex, "tool_success": tool_success,
                        "format_compliance": fmt, "ungrounded_numbers": viol})

    # 汇总
    n = len(results)
    ex_n = sum(1 for r in results if r.get("ex"))
    tool_n = sum(1 for r in results if r.get("tool_success"))
    fmt_n = sum(1 for r in results if r.get("format_compliance"))
    print("\n================ 汇总 ================")
    print(f"Execution Accuracy (EX): {ex_n}/{n} = {ex_n / n:.1%}")
    print(f"工具调用成功率         : {tool_n}/{n} = {tool_n / n:.1%}")
    print(f"格式合规率             : {fmt_n}/{n} = {fmt_n / n:.1%}")
    print("=======================================")

    out_path = os.path.join(os.path.dirname(__file__), "eval_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"model": model, "results": results}, f, ensure_ascii=False, indent=2)
    print(f"详细报告已写入 {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ChatBI 离线测评 harness")
    ap.add_argument("--live", action="store_true", help="真机调用 Agent（需 OPENAI_API_KEY）")
    ap.add_argument("--offline", action="store_true", help="离线自检（默认）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    ap.add_argument("--model", default="qwen-plus", help="模型名（默认 qwen-plus）")
    args = ap.parse_args()

    if args.live:
        run_live(args.model, args.limit)
    else:
        run_offline()
