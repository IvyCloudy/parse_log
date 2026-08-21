"""生成一组用于测试日志分析的样例数据（JSONL）。

输出文件默认 demo/test_logs.jsonl，每行一条 logData（结构同 log-format.json）。
特点：
  - 覆盖 4 个服务单元(gateway-svc/order-svc/pay-svc/inventory-svc)
  - 覆盖 18 类 Java 层数据库操作报错（连接池/MySQL/Oracle/PG/Redis/Mongo/事务/ORM...）
  - logDate 为「秒级」精度，保证 FileSource 分页按秒前进、不卡死
  - 时间按条递增，天然满足流式递增喂入

用法:
    python demo/gen_test_logs.py                 # 生成默认 demo/test_logs.jsonl
    python demo/gen_test_logs.py --out /tmp/x.jsonl --total 5000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from demo.mock_source import _SERVICES, _TEMPLATES, _log_date  # noqa: E402


def gen(total: int, base_ts: int, span_ms: int) -> List[dict]:
    out: List[dict] = []
    n_tpl = len(_TEMPLATES)
    for i in range(total):
        # 秒级步进：保证每条落在不同秒，便于文件数据源按时间分页
        ts_ms = base_ts + int(span_ms * i / max(1, total - 1))
        ts_ms = (ts_ms // 1000) * 1000  # 截到秒
        svc = _SERVICES[i % len(_SERVICES)]
        tpl = _TEMPLATES[i % n_tpl]
        out.append(tpl(svc, ts_ms, i))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(_THIS_DIR, "test_logs.jsonl"))
    ap.add_argument("--total", type=int, default=2000)
    ap.add_argument("--base", type=int, default=1787278400000)
    ap.add_argument("--span", type=int, default=3_600_000)
    args = ap.parse_args()

    logs = gen(args.total, args.base, args.span)
    with open(args.out, "w", encoding="utf-8") as f:
        for log in logs:
            f.write(json.dumps(log, ensure_ascii=False) + "\n")
    print(f"[生成完成] {len(logs)} 条 -> {args.out}")
    print(f"  时间范围: {logs[0]['logDate']} ~ {logs[-1]['logDate']}")
    print(f"  服务单元(serviceUinitId): {sorted({l['serviceUinitId'] for l in logs})}")
    print(f"  应用名称(appName):        {sorted({l['appName'] for l in logs})}")


if __name__ == "__main__":
    main()
