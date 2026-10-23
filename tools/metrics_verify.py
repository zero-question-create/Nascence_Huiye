# tools/metrics_verify.py
# ========================================================================
# 计数器只读核对工具。
#
# 设计约定（本项目既有口径）：
#   - metrics_daily.jsonl 允许同一天多行，读取端按日期合并求和；
#   - metrics_baseline.json 记录“上次每日结算时的累计值”；
#   - metrics_counters.json 记录“最近一次保存时的累计值”。
#
# 本工具只读文件，不修复、不改写任何数据。
#
# 用法：
#   python tools/metrics_verify.py
# ========================================================================

import json
import os
import sys
from collections import defaultdict

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, "data", "test")
DAILY_FILE = os.path.join(DATA_DIR, "metrics_daily.jsonl")
BASELINE_FILE = os.path.join(DATA_DIR, "metrics_baseline.json")
COUNTERS_FILE = os.path.join(DATA_DIR, "metrics_counters.json")

FIELDS = (
    "mem_created", "mem_deleted", "link_created", "link_deleted",
    "wordweb_created", "msg_sent", "self_ref", "active_attempt", "active_success",
)


def _load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        print(f"[错误] 无法解析 {path}: {e}")
        return None


def _load_daily():
    rows = []
    bad = 0
    if not os.path.exists(DAILY_FILE):
        return rows, bad
    with open(DAILY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(rec, dict) or not str(rec.get("date", "")).strip():
                bad += 1
                continue
            rows.append(rec)
    return rows, bad


def main():
    print("=" * 60)
    print("Nascence 计数器只读核对")
    print("=" * 60)

    rows, bad = _load_daily()
    baseline = _load_json(BASELINE_FILE)
    counters = _load_json(COUNTERS_FILE)

    print(f"每日日志: {len(rows)} 行，无法解析/缺日期: {bad} 行")
    print(f"基线文件: {'存在' if baseline is not None else '缺失'}")
    print(f"计数快照: {'存在' if counters is not None else '缺失'}")

    merged = defaultdict(lambda: {key: 0 for key in FIELDS})
    per_day_rows = defaultdict(int)
    for rec in rows:
        date = str(rec.get("date")).strip()
        per_day_rows[date] += 1
        for key in FIELDS:
            try:
                merged[date][key] += int(rec.get(key, 0) or 0)
            except (TypeError, ValueError):
                pass

    print("\n按日期合并后的增量：")
    for date in sorted(merged):
        print(f"  {date}（{per_day_rows[date]} 行）: {dict(merged[date])}")

    print("\n全期合计：")
    totals = {key: sum(merged[d][key] for d in merged) for key in FIELDS}
    print(f"  {totals}")

    print("\n与快照/基线的关系：")
    if counters is not None and baseline is not None:
        for key in FIELDS:
            expected = int(baseline.get(key, 0) or 0) + totals.get(key, 0)
            actual = int(counters.get(key, 0) or 0)
            note = "一致" if actual >= expected else "快照小于基线+日志合计（可能尚未保存）"
            print(f"  {key}: baseline={baseline.get(key, 0)} + daily={totals.get(key, 0)}"
                  f" -> 期望≥{expected}，counters={actual}（{note}）")
    elif counters is None:
        print("  计数快照缺失，跳过一致性检查")

    print("\n提示：")
    zero_fields = [key for key in ("active_attempt", "active_success") if totals.get(key, 0) == 0]
    if zero_fields:
        print(f"  {', '.join(zero_fields)} 全期为 0。若不使用主动发言指标，可从导出一并移除。")
    multi = [d for d, n in per_day_rows.items() if n > 1]
    if multi:
        print(f"  同日多行的日期：{', '.join(sorted(multi)[:8])}"
              f"{' ...' if len(multi) > 8 else ''}（读取端按日合并，属设计内行为）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
