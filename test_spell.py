"""Spell 解析器自测（纯标准库，无需第三方依赖）。

运行: python test_spell.py
"""

from spell_parser import Spell, tokenize, lcs_backtrack, jaccard, is_subsequence


def check(name: str, cond: bool):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    assert cond, f"测试失败: {name}"


def test_lcs_backtrack():
    a = tokenize("Command Failed on node-127")
    b = tokenize("Command Failed on node-235 node-236")
    merged = lcs_backtrack(a, b)
    # 公共部分 Command Failed on，后面标 *
    check("lcs_backtrack 合并", "*" in merged and merged[0] == "Command" and merged[1] == "Failed")


def test_jaccard():
    check("jaccard 相同", abs(jaccard(["a", "b"], ["a", "b"]) - 1.0) < 1e-9)
    # 交集1 / 并集3 = 0.333...，精确断言（原 `or True` 永远为真，等于没测）
    check("jaccard 半", abs(jaccard(["a", "b"], ["a", "c"]) - 1 / 3) < 1e-9)


def test_subsequence():
    check("子序列 是", is_subsequence(["a", "c"], ["a", "b", "c"]))
    check("子序列 否", not is_subsequence(["a", "x"], ["a", "b", "c"]))


def test_spell_basic():
    sp = Spell()
    for m in [
        "ERROR Connection timeout to host 10.0.0.1 after 30s",
        "ERROR Connection timeout to host 10.0.0.2 after 30s",
        "ERROR Connection timeout to host 10.0.0.3 after 30s",
    ]:
        sp.add(m)
    check("合并为 1 类", len(sp.lcs_map) == 1)
    check("计数正确", sp.lcs_map[0].count == 3)
    check("模板含 *", "*" in sp.lcs_map[0].seq)


def test_spell_multi_param():
    sp = Spell()
    sp.add("WARN Disk usage 91% on node-7")
    sp.add("WARN Disk usage 88% on node-9")
    sp.add("WARN Disk usage 95% on node-3")
    obj = sp.lcs_map[0]
    params = obj.extract_params(tokenize("WARN Disk usage 77% on node-42"))
    check("双参数抽取", params == ["77%", "node-42"])


def test_prefilter_efficiency():
    sp = Spell()
    # 先灌入多个模板
    base = [
        "ERROR Connection timeout to host 10.0.0.1 after 30s",
        "ERROR Failed to write file /a.log permission denied",
        "ERROR NullPointer exception in handler t1",
        "ERROR Database connection refused at 2026-08-21 10:00:00",
        "ERROR Out of memory while processing request id=1",
    ]
    for m in base:
        sp.add(m)
    # 再来一批同模板日志，应大量命中前缀树
    for i in range(20):
        sp.add(f"ERROR Connection timeout to host 10.0.0.{i} after 30s")
    check("前缀树命中>0", sp.stats["prefix_tree"] > 0)
    check("总类型数收敛", len(sp.lcs_map) == 5)


def test_prefilter_wildcard_works():
    """回归测试：前缀树 '*' 通配必须真正参与匹配（问题 #6 修复前通配分支是死代码）。

    先用 Jaccard 合并出含 '*' 的模板（host 段参数化），随后不同参数的同类日志应
    命中前缀树（树中 '*' 节点参与匹配），而非常驻 _simple_loop / new_type。
    """
    sp = Spell()
    sp.add("ERROR Connection timeout to host 10.0.0.1 after 30s")
    sp.add("ERROR Connection timeout to host 10.0.0.2 after 30s")
    check("已合并出含 * 模板", "*" in sp.lcs_map[0].seq)
    before = sp.stats["prefix_tree"]
    # 不同 host 的同类日志：应经前缀树 '*' 命中
    sp.add("ERROR Connection timeout to host 10.0.0.3 after 30s")
    check("通配模板命中前缀树", sp.stats["prefix_tree"] > before)
    check("通配未退化成新增类型", len(sp.lcs_map) == 1)


def test_persist():
    import os, tempfile
    sp = Spell()
    for m in ["ERROR a 1", "ERROR a 2", "ERROR b 3"]:
        sp.add(m)
    path = os.path.join(tempfile.gettempdir(), "spell_test_model.json")
    sp.save(path)
    sp2 = Spell.load(path)
    check("加载类型数一致", len(sp2.lcs_map) == len(sp.lcs_map))
    check("加载计数一致", sp2.total_processed == sp.total_processed)
    os.remove(path)


def test_persist_restores_last_seen_and_params():
    """回归测试：load 必须恢复 last_seen 与 params_sample（问题 #10）。"""
    import os, tempfile
    sp = Spell()
    sp.add("ERROR Connection timeout to host 10.0.0.1 after 30s", ts=1234567890000)
    sp.add("ERROR Connection timeout to host 10.0.0.2 after 30s", ts=1234567895000)
    # 上述两条会合并为同一模板（含 *），last_seen 应更新为第二条 ts
    check("两条合并为 1 类", len(sp.lcs_map) == 1)
    path = os.path.join(tempfile.gettempdir(), "spell_test_model2.json")
    sp.save(path)
    sp2 = Spell.load(path)
    obj = sp2.lcs_map[0]
    check("last_seen 数值恢复", obj.last_seen == 1234567895000)
    check("params_sample 恢复", len(obj.params_sample) > 0)
    # 首条日志 line_id 真实为 0，恢复后应保留真实 line_ids（{0,1}），而非被哨兵误删
    check("line_ids 恢复", obj.line_ids == {0, 1})
    os.remove(path)


def test_analyzer_integration():
    """analyzer 端到端：在线解析 + 日志时间 + 维度分桶 + 持久化恢复（问题 #13/#15/#16）。"""
    import json as _json
    import os
    import tempfile
    from data_sources import FileSource
    from analyzer import LogAnalyzer, log_date_to_ms

    rows = []
    for i in range(20):
        app = "order" if i % 2 == 0 else "pay"
        rows.append({
            "serviceUinitId": f"svc{i % 2}",
            "appName": app,
            # 用真实（带毫秒+时区）时间，验证问题 #9 解析不被丢精度
            "logDate": "2026-06-15T10:00:00.123+0800",
            "message": f"ERROR Connection timeout to host 10.0.0.{i} after 30s",
        })
    p = os.path.join(tempfile.gettempdir(), "spell_test_analyzer.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(_json.dumps(r) + "\n")

    src = FileSource(p)
    a = LogAnalyzer(src)
    overview = a.analyze(start_ms=0, end_ms=9999999999999, batch_size=10, by_dim="appName")
    check("analyzer 累计处理", overview["total_processed"] == 20)
    check("维度分桶生成", set(a._by_dim.keys()) == {"order", "pay"})
    # 日志时间被采用：last_seen 应为该毫秒（而非 0 或处理时间）
    obj = a.spell.lcs_map[0]
    check("日志时间作为 last_seen", obj.last_seen == log_date_to_ms("2026-06-15T10:00:00.123+0800"))

    # 持久化 + 恢复
    mp = os.path.join(tempfile.gettempdir(), "spell_test_analyzer_model.json")
    a.save(mp)
    a2 = LogAnalyzer(FileSource(p))
    a2.load(mp)
    check("恢复后维度分桶保留", set(a2._by_dim.keys()) == {"order", "pay"})
    os.remove(p)
    os.remove(mp)


if __name__ == "__main__":
    test_lcs_backtrack()
    test_jaccard()
    test_subsequence()
    test_spell_basic()
    test_spell_multi_param()
    test_prefilter_efficiency()
    test_prefilter_wildcard_works()
    test_persist()
    test_persist_restores_last_seen_and_params()
    test_analyzer_integration()
    print("\n全部测试通过 ✅")
