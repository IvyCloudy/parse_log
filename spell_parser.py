"""Spell: Streaming Parser for Event Logs using LCS.

基于论文 "Spell: Streaming Parsing of System Event Logs" (Min Du, Feifei Li)
的流式日志解析器实现。

核心思想：
- 把每条日志按分隔符切成 token 序列
- 用最长公共子序列 (LCS) 找出日志的「消息类型」(模板)，变化部分用 '*' 占位
- 通过「前缀树预过滤 + 简单循环 + Jaccard+LCS 兜底」三级策略，将单条日志的
  解析复杂度从 O(m*n^2) 降到摊还 O(n)，支持在线流式处理

本模块不依赖任何第三方库，可直接 import 使用。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


# ---------------------------------------------------------------------------
# 基础工具：分词、LCS、Jaccard
# ---------------------------------------------------------------------------

DEFAULT_DELIMITERS = r"\s+|=|:|\[|\]|\(|\)|<|>|,"
_TOKEN_RE = re.compile(DEFAULT_DELIMITERS)


def tokenize(message: str, delimiters: str = DEFAULT_DELIMITERS) -> List[str]:
    """把一条日志消息切成 token 序列。连续分隔符会被合并。"""
    # 用分隔符切分，并过滤掉空串
    return [t for t in re.split(delimiters, message) if t]


def lcs_length(a: Sequence[str], b: Sequence[str]) -> int:
    """标准 LCS 长度，动态规划 O(|a|*|b|)。"""
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return 0
    # 滚动数组节省空间
    prev = [0] * (m + 1)
    for i in range(n):
        cur = [0] * (m + 1)
        ai = a[i]
        for j in range(m):
            if ai == b[j]:
                cur[j + 1] = prev[j] + 1
            else:
                cur[j + 1] = max(prev[j + 1], cur[j])
        prev = cur
    return prev[m]


def lcs_backtrack(a: Sequence[str], b: Sequence[str]) -> List[str]:
    """计算 a 与 b 的 LCS 序列，并在两者不一致的位置插入 '*' 作为参数占位符。

    返回的是「合并后的模板」：保留公共部分，分歧处用 '*' 标记，相邻 '*' 合并为一个。
    """
    n, m = len(a), len(b)
    # dp[i][j] = LCS 长度
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n):
        for j in range(m):
            if a[i] == b[j]:
                dp[i + 1][j + 1] = dp[i][j] + 1
            else:
                dp[i + 1][j + 1] = max(dp[i][j + 1], dp[i + 1][j])

    # 回溯构造模板
    i, j = n, m
    result: List[str] = []
    while i > 0 and j > 0:
        if a[i - 1] == b[j - 1]:
            result.append(a[i - 1])
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            # a 侧多出的部分 -> 参数
            result.append("*")
            i -= 1
        else:
            # b 侧多出的部分 -> 参数
            result.append("*")
            j -= 1
    while i > 0:
        result.append("*")
        i -= 1
    while j > 0:
        result.append("*")
        j -= 1

    result.reverse()
    # 合并相邻 '*'
    merged: List[str] = []
    for tok in result:
        if tok == "*" and merged and merged[-1] == "*":
            continue
        merged.append(tok)
    return merged


def jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    """集合 Jaccard 相似度（token 去重后）。"""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def is_subsequence(sub: Sequence[str], seq: Sequence[str]) -> bool:
    """判断 sub 是否为 seq 的子序列（双指针，O(|sub|+|seq|)）。"""
    it = iter(seq)
    return all(any(tok == s for s in it) for tok in sub)


# ---------------------------------------------------------------------------
# 前缀树（用于 O(n) 预过滤）
# ---------------------------------------------------------------------------

class _PrefixTreeNode:
    __slots__ = ("children", "seq_ids")

    def __init__(self) -> None:
        self.children: Dict[str, "_PrefixTreeNode"] = {}
        self.seq_ids: Set[int] = set()  # 以当前节点为结尾的 LCSObject id


# 模板中的参数占位符
WILDCARD = "*"


class PrefixTree:
    """索引所有已知模板的「常量骨架」，用于 O(n) 预过滤。

    论文前缀树法（III-D.2）：把已有消息类型建前缀树，对 σ 逐字符与树比较，
    未被任何节点匹配的字符即参数。这里等价于——模板（含 '*'）能「覆盖」日志 s，
    即按模板顺序，其常量 token 依次作为 s 的子序列出现（'*' 通配中间任意 token）。

    实现要点：
    - 树节点按「常量 token」建索引（'*' 不参与路径，仅作为通配匹配规则）
    - 持有 seq_id -> seq 的映射，使 match 能拿到模板真实长度用于阈值判断
    - match 用「字符串状态集」做通配子序列匹配，复杂度 O(n * 模板数相关)，
      在模板数量适中时接近 O(n)
    """

    def __init__(self) -> None:
        self.root = _PrefixTreeNode()
        self.seq_of: Dict[int, List[str]] = {}  # seq_id -> 完整模板 seq

    def insert(self, seq: Sequence[str], seq_id: int) -> None:
        self.seq_of[seq_id] = list(seq)
        node = self.root
        for tok in seq:
            # 注意：WILDCARD 也作为真实节点插入。match() 依赖 '*' 节点来跳过
            # 日志中的可变 token，若在此处跳过则预过滤退化为纯常量骨架匹配，
            # 绝大多数日志会落到 O(m*n) 的 _simple_loop。
            node = node.children.setdefault(tok, _PrefixTreeNode())
        node.seq_ids.add(seq_id)

    def remove(self, seq: Sequence[str], seq_id: int) -> None:
        self.seq_of.pop(seq_id, None)
        node = self.root
        path = [node]
        for tok in seq:
            node = node.children.get(tok)
            if node is None:
                return
            path.append(node)
        path[-1].seq_ids.discard(seq_id)
        # 自底向上清理空节点
        for tok, node in zip(reversed(list(seq)), reversed(path[:-1])):
            child = node.children.get(tok)
            if child is None:
                break
            if not child.children and not child.seq_ids:
                del node.children[tok]
            else:
                break

    def match(self, s: Sequence[str]) -> Tuple[Optional[int], int]:
        """在树中找「能覆盖 s」的模板，返回 (seq_id, 模板真实长度)。

        覆盖判定：模板常量 token 作为 s 的子序列依次出现（'*' 通配任意 token）。
        例如模板 [A, *, B] 能覆盖 s=[A, x, y, B]。返回匹配且模板最长的（并列取 id 最小）。

        实现说明：'*' 节点代表「匹配日志中恰好 1 个可变 token」，向前推进 pos+1。
        这与 Spell 算法语义一致——'*' 是 join 时在两条等长 LCS 间插入的占位符，
        表示「此处日志与原模板多/少了一段内容」。BFS 状态为 (node, pos)，
        通过 visited 去重避免重复展开，复杂度可控。
        """
        best_id: Optional[int] = None
        best_len = 0

        visited = set()
        frontier = [(self.root, 0)]
        while frontier:
            nxt = []
            for node, pos in frontier:
                key = (id(node), pos)
                if key in visited:
                    continue
                visited.add(key)
                if node.seq_ids:
                    for cid in node.seq_ids:
                        seq_len = len(self.seq_of[cid])
                        if seq_len > best_len or (
                            seq_len == best_len and best_id is not None and cid < best_id
                        ):
                            best_len = seq_len
                            best_id = cid
                # 通配：'*' 消费日志中 1 个 token（pos+1）
                wc = node.children.get(WILDCARD)
                if wc is not None and pos < len(s):
                    nxt.append((wc, pos + 1))
                # 常量：在 s[pos:] 中找该 token 首次出现并前进
                for tok, child in node.children.items():
                    if tok == WILDCARD:
                        continue
                    for k in range(pos, len(s)):
                        if s[k] == tok:
                            nxt.append((child, k + 1))
                            break
            frontier = nxt

        if best_id is None:
            return None, 0
        return best_id, best_len



# ---------------------------------------------------------------------------
# LCSObject / Spell 解析器
# ---------------------------------------------------------------------------

class LCSObject:
    __slots__ = ("seq", "line_ids", "count", "last_seen", "params_sample", "_template_cache")

    def __init__(self, seq: List[str], line_id: int, ts=None) -> None:
        self.seq = seq               # 模板 token 序列，参数位置为 '*'
        self.line_ids: Set[int] = {line_id}
        self.count = 1
        self.last_seen = ts
        self.params_sample: List[List[str]] = []  # 最近几次参数值样本
        self._template_cache: Optional[str] = None

    def template(self) -> str:
        if self._template_cache is None:
            self._template_cache = " ".join(self.seq)
        return self._template_cache

    def extract_params(self, tokens: Sequence[str]) -> List[str]:
        """从一条日志 token 序列中抽取模板 '*' 位置对应的参数值。

        对齐规则：常量 token 必须与 tokens 中对应位置严格相等并前进；遇到 '*'
        时收集 tokens 中从当前位置到「下一个常量 token 在 tokens 中顺序出现」之间
        的所有 token 作为参数。
        """
        params: List[str] = []
        ti = 0
        n = len(tokens)
        for idx, tok in enumerate(self.seq):
            if tok == "*":
                # 找下一个常量 token（在 seq 中）于 tokens 中的首次出现位置
                next_const = None
                for nxt in self.seq[idx + 1:]:
                    if nxt != "*":
                        next_const = nxt
                        break
                buf: List[str] = []
                if next_const is None:
                    # 后面全是 '*' 或已结束，收集剩余全部
                    buf = list(tokens[ti:n])
                    ti = n
                else:
                    # 在 tokens[ti:] 中找 next_const 首次出现
                    for k in range(ti, n):
                        if tokens[k] == next_const:
                            break
                    else:
                        k = n
                    buf = list(tokens[ti:k])
                    ti = k
                params.append(" ".join(buf))
            else:
                if ti < n and tokens[ti] == tok:
                    ti += 1
        return params


class Spell:
    """流式结构化日志解析器。"""

    def __init__(self, tau: float = 0.5, delimiters: str = DEFAULT_DELIMITERS) -> None:
        self.tau = tau                  # 匹配阈值（默认序列长度的一半）
        self.delimiters = delimiters
        self.lcs_map: List[LCSObject] = []
        self.tree = PrefixTree()
        self._next_id = 0
        self.total_processed = 0
        # 统计各级预过滤命中次数（对应论文 Table IV）
        self.stats = {"prefix_tree": 0, "simple_loop": 0, "naive_lcs": 0, "new_type": 0}
        # 各类型 token 集合，用于 _simple_loop 候选预筛（问题 #5：模板上千后避免
        # 对每条日志全量遍历 lcs_map 做子序列判定）。并行于 lcs_map 的索引。
        self._type_tokens: List[Set[str]] = []

    # -- 对外接口 ----------------------------------------------------------

    def add(self, message: str, ts=None) -> LCSObject:
        """处理一条新日志，返回其归属的 LCSObject（已更新）。"""
        tokens = tokenize(message, self.delimiters)
        self.total_processed += 1
        obj = self._process(tokens, self.total_processed - 1, ts)
        return obj

    def parse_batch(self, messages: Iterable[str], ts_list=None) -> List[LCSObject]:
        """批量处理（仍逐条流式解析，保持在线语义）。"""
        results = []
        for idx, msg in enumerate(messages):
            ts = ts_list[idx] if ts_list else None
            results.append(self.add(msg, ts))
        return results

    # -- 核心流程 ----------------------------------------------------------

    def _process(self, s: List[str], line_id: int, ts) -> LCSObject:
        n = len(s)
        if n == 0:
            # 空日志：作为独立类型
            return self._new_type(s, line_id, ts)
        threshold = max(1, int(self.tau * n))

        # 步骤 1：前缀树预过滤 O(n)
        seq_id, matched_len = self.tree.match(s)
        if seq_id is not None and matched_len >= threshold:
            self.stats["prefix_tree"] += 1
            return self._attach(seq_id, s, line_id, ts)

        # 步骤 2：简单循环 O(m*n)
        best_id, best_len = self._simple_loop(s, threshold)
        if best_id is not None:
            self.stats["simple_loop"] += 1
            return self._merge_and_attach(best_id, s, line_id, ts)

        # 步骤 3：Jaccard 过滤 + LCS 兜底（仅对少数日志）
        merged_id = self._jaccard_lcs_step(s, threshold, line_id, ts)
        if merged_id is not None:
            self.stats["naive_lcs"] += 1
            return self.lcs_map[merged_id]

        # 步骤 4：全新消息类型
        self.stats["new_type"] += 1
        return self._new_type(s, line_id, ts)

    def _simple_loop(self, s: List[str], threshold: int) -> Tuple[Optional[int], int]:
        """遍历 LCSMap，找是 s 子序列且长度>=threshold 的模板，并列取 |seq| 最小。

        优化（问题 #5）：先用日志 token 集合与「各类型 token 集合」求交集做候选预筛，
        无交集的类型不可能是子序列，直接跳过，避免对每条日志全量 O(类型数×日志长)。
        """
        s_set = set(s)
        best_id: Optional[int] = None
        best_len = -1
        best_seq_len = None
        for idx, obj in enumerate(self.lcs_map):
            q = obj.seq
            if len(q) < threshold:
                continue
            # 候选预筛：模板 token 与日志 token 无交集则不可能是子序列
            if self._type_tokens[idx] and not (self._type_tokens[idx] & s_set):
                continue
            if is_subsequence(q, s):
                if len(q) > best_len or (
                    len(q) == best_len and best_seq_len is not None and len(q) < best_seq_len
                ):
                    best_len = len(q)
                    best_seq_len = len(q)
                    best_id = idx
        return best_id, best_len

    def _jaccard_lcs_step(
        self, s: List[str], threshold: int, line_id: int, ts
    ) -> Optional[int]:
        """对 Jaccard >= 0.5 的候选算 LCS，超阈值则合并。

        注：论文原文用 "more than half"（>0.5），但实践中参数恰好占一半的日志
        （如 WARN Disk usage 91% on node-7）Jaccard==0.5 会被漏掉。放宽到 >=0.5
        可提升合并召回，且仍保留 LCS 长度阈值做最终把关，不会误合并。
        """
        candidates = [
            (idx, obj)
            for idx, obj in enumerate(self.lcs_map)
            if jaccard(obj.seq, s) >= 0.5
        ]
        if not candidates:
            return None
        # 取 LCS 最长者
        best_idx = None
        best_lcs_len = -1
        for idx, obj in candidates:
            llen = lcs_length(obj.seq, s)
            if llen > best_lcs_len:
                best_lcs_len = llen
                best_idx = idx
        if best_idx is not None and best_lcs_len >= threshold:
            self._merge_and_attach(best_idx, s, line_id, ts, update_tree=True)
            return best_idx
        return None

    def _attach(self, seq_id: int, s: List[str], line_id: int, ts) -> LCSObject:
        obj = self.lcs_map[seq_id]
        obj.line_ids.add(line_id)
        obj.count += 1
        obj.last_seen = ts
        self._record_params(obj, s)
        return obj

    def _merge_and_attach(
        self, seq_id: int, s: List[str], line_id: int, ts, update_tree: bool = False
    ) -> LCSObject:
        """回溯生成新模板，更新 LCSObject 与前缀树。"""
        obj = self.lcs_map[seq_id]
        new_seq = lcs_backtrack(obj.seq, s)
        # 更新树索引
        self.tree.remove(obj.seq, seq_id)
        obj.seq = new_seq
        obj._template_cache = None  # seq 改变，失效缓存
        self._type_tokens[seq_id] = set(new_seq)  # 同步倒排索引
        self.tree.insert(new_seq, seq_id)
        obj.line_ids.add(line_id)
        obj.count += 1
        obj.last_seen = ts
        self._record_params(obj, s)
        return obj

    def _new_type(self, s: List[str], line_id: int, ts) -> LCSObject:
        obj = LCSObject(list(s), line_id, ts)
        self.lcs_map.append(obj)
        self._type_tokens.append(set(obj.seq))  # 同步倒排索引
        self.tree.insert(obj.seq, self._next_id)
        self._next_id += 1
        return obj

    def _record_params(self, obj: LCSObject, s: List[str]) -> None:
        """抽取参数样本（保留最近 5 次），用于错误归因与审计。"""
        if "*" not in obj.seq:
            return
        try:
            params = obj.extract_params(s)
        except Exception:
            return
        if params:
            obj.params_sample.append(params)
            if len(obj.params_sample) > 5:
                obj.params_sample.pop(0)

    # -- 分析辅助 ----------------------------------------------------------

    def templates(self) -> List[Tuple[str, int]]:
        """返回 (模板, 出现次数) 列表，按次数降序。对应论文「Top-N 错误」。"""
        return sorted(
            ((o.template(), o.count) for o in self.lcs_map),
            key=lambda x: -x[1],
        )

    def summary(self) -> str:
        lines = [
            f"总处理日志条数: {self.total_processed}",
            f"发现消息类型数: {len(self.lcs_map)}",
            "预过滤命中分布: "
            + ", ".join(f"{k}={v}({v/max(1,self.total_processed)*100:.2f}%)"
                        for k, v in self.stats.items()),
            "Top 错误模板:",
        ]
        for tmpl, cnt in self.templates()[:10]:
            lines.append(f"  {cnt:>7}  {tmpl}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """序列化模板库，便于持久化（服务重启恢复）。"""
        return {
            "tau": self.tau,
            "total_processed": self.total_processed,
            "stats": self.stats,
            "types": [
                {
                    "seq": o.seq,
                    "count": o.count,
                    "line_ids": sorted(o.line_ids)[:100],  # 截断避免过大
                    # last_seen 保持原始数值类型（时间戳），JSON 兼容
                    "last_seen": o.last_seen,
                    "params_sample": o.params_sample,
                }
                for o in self.lcs_map
            ],
        }

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "Spell":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        sp = cls(tau=data.get("tau", 0.5))
        sp.total_processed = data.get("total_processed", 0)
        sp.stats = data.get("stats", sp.stats)
        for t in data.get("types", []):
            # 用哨兵 -1 占位，避免与真实 line_id=0（首条日志）冲突
            obj = LCSObject(t["seq"], -1, ts=t.get("last_seen"))
            obj.count = t["count"]
            # 丢弃哨兵并恢复真实 line_ids（旧版用 0 占位会误删真实的 0）
            obj.line_ids.discard(-1)
            restored_ids = t.get("line_ids") or []
            obj.line_ids.update(restored_ids)
            # 恢复参数样本（若文件来自旧版本无该字段则安全跳过）
            obj.params_sample = t.get("params_sample") or []
            sp.lcs_map.append(obj)
            sp._type_tokens.append(set(obj.seq))  # 同步倒排索引
            sp.tree.insert(obj.seq, sp._next_id)
            sp._next_id += 1
        return sp
