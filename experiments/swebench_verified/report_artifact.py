#!/usr/bin/env python3
"""Build a portable-report input from independently validated pilot evidence."""
import argparse
import collections
import datetime
import json
import statistics
import sqlite3


def main():
    from pathlib import Path
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", type=Path, required=True)
    a = ap.parse_args()
    p = a.package
    d = json.loads((p / "pilot-analysis/analysis.json").read_text())
    rows = [json.loads(line) for line in (p / "pilot/cold/samples.jsonl").read_text().splitlines()]
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["task"]["instance_id"], r["mode"])].append(r)
    cold = [{"task": task, "system": "FullCopy" if mode == "baseline" else "OverlayFS",
             "mean_ms": statistics.mean(r["cold_start_ns"] for r in values) / 1e6,
             "provision_ms": statistics.mean(r["filesystem_provision_ns"] for r in values) / 1e6,
             "samples": len(values)} for (task, mode), values in sorted(groups.items())]
    tools, memory = [], []
    for label, r in sorted(d["systems"].items()):
        tools.append({"system": label, **r["tool_ms"], "nonzero": r["nonzero_tool_exits"], "deadline": r["absolute_deadline_violations"]})
        m = r["memory"]
        memory.append({"system": label,
            "sandbox_mib": m["sandbox_memory_current_bytes"]["time_weighted_mean_mib"],
            "service_mib": m["task_service_cgroup_sum_bytes"]["time_weighted_mean_mib"],
            "peak_mib": m["task_service_cgroup_sum_bytes"]["peak_mib"],
            "swap_mib": m["sandbox_memory_swap_bytes"]["time_weighted_mean_mib"]})
    # Reproducible SQL projections for the report builder's required SQL source
    # contract. Metric calculations remain independently recorded in analyze.py.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE cold_samples (raw TEXT)")
    db.executemany("INSERT INTO cold_samples VALUES (?)", [(json.dumps(r),) for r in rows])
    db.execute("CREATE TABLE validated_analysis (raw TEXT)")
    db.execute("INSERT INTO validated_analysis VALUES (?)", (json.dumps(d),))
    cold_sql = """SELECT json_extract(raw,'$.task.instance_id') AS task,
CASE json_extract(raw,'$.mode') WHEN 'baseline' THEN 'FullCopy' ELSE 'OverlayFS' END AS system,
AVG(json_extract(raw,'$.cold_start_ns')) / 1000000.0 AS mean_ms,
AVG(json_extract(raw,'$.filesystem_provision_ns')) / 1000000.0 AS provision_ms,
COUNT(*) AS samples FROM cold_samples GROUP BY task, system ORDER BY task, system"""
    tool_sql = """SELECT s.key AS system,
json_extract(s.value,'$.tool_ms.n') AS n, json_extract(s.value,'$.tool_ms.mean') AS mean,
json_extract(s.value,'$.tool_ms.p50') AS p50, json_extract(s.value,'$.tool_ms.p95') AS p95,
json_extract(s.value,'$.tool_ms.p99') AS p99, json_extract(s.value,'$.tool_ms.max') AS max,
json_extract(s.value,'$.nonzero_tool_exits') AS nonzero,
json_extract(s.value,'$.absolute_deadline_violations') AS deadline
FROM validated_analysis, json_each(validated_analysis.raw,'$.systems') AS s ORDER BY system"""
    memory_sql = """SELECT s.key AS system,
json_extract(s.value,'$.memory.sandbox_memory_current_bytes.time_weighted_mean_mib') AS sandbox_mib,
json_extract(s.value,'$.memory.task_service_cgroup_sum_bytes.time_weighted_mean_mib') AS service_mib,
json_extract(s.value,'$.memory.task_service_cgroup_sum_bytes.peak_mib') AS peak_mib,
json_extract(s.value,'$.memory.sandbox_memory_swap_bytes.time_weighted_mean_mib') AS swap_mib
FROM validated_analysis, json_each(validated_analysis.raw,'$.systems') AS s ORDER BY system"""
    def project(sql, expected):
        actual = [dict(r) for r in db.execute(sql)]
        assert actual == expected, "report SQL and independently calculated metrics differ"
        return actual
    cold = project(cold_sql, cold)
    tools = project(tool_sql, tools)
    memory = project(memory_sql, memory)
    ratio = d["tool_vs_fullcopy"]["T1-S2"]
    title = "Crate SWE-bench Pilot Results"
    sources = [{"id": "analysis", "label": "Validated four-task pilot analysis", "path": "pilot-analysis/analysis.json"},
               {"id": "cold", "label": "Raw no-pool cold-start measurements", "path": "pilot/cold/samples.jsonl",
                "query": {"engine": "SQLite", "language": "sql", "sql": cold_sql,
                          "description": "cold_samples.raw stores one unchanged JSONL measurement per row; SQL computes each task/mode mean."}},
               {"id": "protocol", "label": "Preregistered methods and task selection", "path": "PREREGISTRATION.md"}]
    for ident, sql in (("tools", tool_sql), ("memory", memory_sql)):
        sources.append({"id": ident, "label": "Validated " + ident + " metric projection", "path": "pilot-analysis/analysis.json",
            "query": {"engine": "SQLite", "language": "sql", "sql": sql,
                      "description": "validated_analysis.raw stores analysis.json; analyze.py recomputes metrics from raw replay JSON. This SQL only projects the already validated metrics, not the underlying calculation."}})
    def md(ident, body, source=None):
        return {"id": ident, "type": "markdown", "body": body, **({"sourceId": source} if source else {})}
    def table(ident, dataset, title, columns):
        return {"id": ident, "title": title, "dataset": dataset, "sourceId": dataset,
                "defaultSort": {"field": "system", "direction": "asc"},
                "columns": [{"field": field, "label": label, **({"format": "number"} if field != "system" else {})} for field, label in columns]}
    blocks = [md("title", "# " + title),
        md("summary", "## 四任务预检完成，尚不是正式论文结果\n\n"
           f"每组保留 48 次真实工具调用、6 次非零退出码；命令、退出码和最终源码状态匹配。"
           f"T1-S2 / FullCopy 的工具平均延迟比为 {ratio['mean_ratio']:.3f}×，p95 为 {ratio['p95_ratio']:.3f}×，p99 为 {ratio['p99_ratio']:.3f}×。"
           "这些是共享 nsl17 上一次回放的描述性数据，不构成显著性或隔离 Caden 因果收益结论。32 任务、三次重复的正式结果尚未完成。", "analysis"),
        md("definitions", "## 先区分三个测量边界\n\n"
           "冷启动：服务收到创建请求至第一个正常 API 命令成功返回，无 ready pool；主机已暖、同一基底已在本地。工具延迟：响应就绪至命令输出返回，含唤醒、排队和恢复，不含 LLM 等待与创建。内存：包括 LLM 等待与 ready-pool 准备的生命周期时间加权 cgroup 计费均值；不是独占物理 DRAM。", "protocol"),
        md("cold_section", "## 冷启动按任务核对，避免只看总体平均\n\n"
           "下图每个柱为同一任务、同一配置三次独立创建的均值。FullCopy 与 OverlayFS 使用完全相同的仓库和已安装 conda 依赖；复制包括依赖树中已有的包缓存。"
           "差异反映这一定义下的本地工作区构建路径，不包括镜像拉取，也不代表 ready-pool 命中延迟。", "cold"),
        {"id": "cold_chart_block", "type": "chart", "chartId": "cold_chart"},
        md("tools_section", "## 工具平均值与尾部一起报告\n\n"
           "每组仅 48 个命令级样本，p99 接近最大观测值。保留失败、短命令以及正常的提交标记命令；非零退出码不是 SWE-bench 失败任务数，零退出码也不保证复合 shell 命令内的所有测试通过。"
           "预注册相对尾延迟门槛为 FullCopy 的 1.10 倍；均值有利不能替代尾部检查。", "analysis"),
        {"id": "tool_table_block", "type": "table", "tableId": "tool_table"},
        md("memory_section", "## cgroup 计费不等同于共享主机的物理内存节省\n\n"
           "service-tree 已包括其 sandbox 子 cgroup，不能再相加。计费包含文件缓存，缓存的归属与首次访问、运行顺序有关；共享主机的 MemAvailable 变化另存原始数据，不能证明独占 DRAM 收益。"
           f"T1-S2 实际回收事件 {d['systems']['T1-S2']['reclaim_events']} 次，预测恢复准备字节 {d['systems']['T1-S2']['speculative_prepared_bytes']}；零值不应写成策略收益。", "analysis"),
        {"id": "memory_table_block", "type": "table", "tableId": "memory_table"},
        md("methods", "## 真实 agent 采集，固定命令回放\n\n"
           "从固定 SWE-bench Verified revision 按仓库预选任务，再用 gpt-5.6-terra 在官方环境中采集真实命令。预检覆盖 Django、SymPy、Astropy 和 pytest 各一题。"
           "采集包含源码检索、读取、修改、复现和真实测试；不是 MCTS，也不是完整 SWE-bench 解题率评测。对照时不重新调用模型；保留实测 LLM 等待时长（1.0×）。"
           "并发上限为 4，固定队列分波次执行；它不是恒定占用或固定绝对唤醒时刻的开放到达流。", "protocol"),
        md("limits", "## 预检只验证流程，不能分离调度因果收益\n\n"
           "F0-S0 与 T1-S2 同时改变文件系统和调度机制，因此不能把其差值归给 Caden。主机共享、每组只有一次回放、少量尾样本，以及不同任务对应不同基底，都限制推论。"
           "每题基底只消费一次，ready-pool 复用机会有限。最终源码核对的审计开销不计入工具延迟，但包括在生命周期内存和吞吐窗口中。", "analysis"),
        md("next", "## 下一步：完成预注册的 32 任务消融\n\n"
           "在预检通过后运行 32 个固定任务，比较 F0-S0、T1-S0、T1-S1 和 T1-S2，采用三次交错顺序重复。先回答真实命令工作量是否匹配，再报告冷启动、工具均值与尾部、内存、唤醒、失败与吞吐。"
           "待解决的问题是：收益是否跨任务和运行顺序稳定；预测策略是否确有可回收驻留内存；尾部是否满足相对门槛。正式验证前不替换论文性能数字。", "protocol")]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    artifact = {"surface": "report", "manifest": {"version": 1, "surface": "report", "title": title,
        "generatedAt": now, "sources": sources, "blocks": blocks,
        "charts": [{"id": "cold_chart", "title": "Cold-start latency by task", "type": "bar", "dataset": "cold",
            "sourceId": "cold", "intent": "comparison", "question": "How does local cold-start latency differ across the four fixed tasks?",
            "rationale": "Grouped bars compare matched mean startup endpoints; three observations per task and mode are descriptive only.",
            "palette": {"kind": "categorical"}, "legend": {"position": "top"},
            "encodings": {"x": {"field": "task", "type": "nominal", "label": "Task"},
                          "y": {"field": "mean_ms", "type": "quantitative", "label": "Mean cold start (ms)"},
                          "color": {"field": "system", "type": "nominal"}}}],
        "tables": [table("tool_table", "tools", "Tool latency (ms)", [("system", "System"), ("n", "Calls"), ("mean", "Mean"), ("p50", "p50"), ("p95", "p95"), ("p99", "p99")]),
                   table("memory_table", "memory", "Cgroup charges (MiB)", [("system", "System"), ("sandbox_mib", "Sandbox mean"), ("service_mib", "Service-tree mean"), ("peak_mib", "Service-tree peak"), ("swap_mib", "Swap mean")])]},
        "snapshot": {"version": 1, "generatedAt": now, "status": "partial",
            "accessIssues": [{"id": "formal_pending", "dataset": "formal", "message": "32-task, three-repetition formal campaign has not completed; this report contains pilot results only."}],
            "datasets": {"cold": cold, "tools": tools, "memory": memory}}, "sources": sources}
    (p / "pilot-analysis/artifact.json").write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
