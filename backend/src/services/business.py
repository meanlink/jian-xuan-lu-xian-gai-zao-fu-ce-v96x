"""同一作业员路线改造前后的配对复测，不涉及库存业务。

分析支持按段推进：每段的配对差用 Welford 方式汇总为
``n / mean / m2`` 三个量，最终按段合并。整批计算也走同一条
汇总与合成路径，因此分段结果与整批结果完全一致；先做
``before - after`` 再累计，也避免测时数值很大、差值很小时
直接累加平方造成的抵消误差。
"""
import math
import os
import random

from scipy import stats

TEAMS = ("全部", "一组", "二组", "三组", "四组")


def metadata():
    return {"name": "拣选路线改造复测", "short": "pick-paired", "tagline": "让同一个人的前后差，回答路线是否缩短。", "client": "北岭履约 · 80 名作业员的流程改善试点组", "workflow": "改善员把两周作业均时直接比较，忽略人员结构和熟练度差异。", "pains": ["不同人员混在前后平均中", "缺失配对无法识别", "改善秒数缺少区间与个体差异"], "upstream": "匿名作业员配对测时 BL_route_retest.csv", "downstream": "流程改善组决定路线方案是否扩大复测",
            "entities": ["配对复测样本", "路线改善核验", "班组复测简报"], "dataset_name": "80 名作业员 · 原路线与新路线配对",
            "columns": [{"key": "operator", "label": "匿名作业员", "type": "string"}, {"key": "team", "label": "试点小组", "type": "string"}, {"key": "before", "label": "原路线/秒", "type": "number"}, {"key": "after", "label": "新路线/秒", "type": "number"}],
            "parameters": [{"key": "team", "label": "试点小组", "type": "select", "default": "全部", "options": [{"value": x, "label": x} for x in TEAMS]}, {"key": "confidence", "label": "置信度", "type": "select", "default": "0.95", "options": [{"value": str(x), "label": f"{x*100:g}%"} for x in [.9, .95, .99]]}],
            "workbench_title": "人员配对改善核验", "action_label": "计算配对差与区间", "unit": "秒", "accent": "oklch(47% 0.075 248)", "style": "blueprint",
            "seed_story": ["07:50 改善员导入 BL_route_retest.csv，共 80 人。", "08:10 核对 OP-001 至 OP-080 的前后测时完整。", "08:30 选择全部小组、95% 置信区间。", "08:35 执行配对检验，查看平均节省秒数。", "08:50 打开逐人差值，核对反向改善样本。", "09:10 单独选择四组，比较改善差异。", "09:30 导出班组简报，安排负改善人员的路线走查。"],
            "method": "逐人差值 d=before−after；配对 t=mean(d)/(sd(d)/√n)，自由度 n−1；置信区间由 t 分布给出；标准化配对效应 dz=mean(d)/sd(d)。", "limitations": ["顺序、学习效应可能混入路线效果", "不对个人绩效作评价", "需保持复测订单难度和测时口径一致"]}


def seed_records():
    rng = random.Random(1006)
    rows = []
    for i in range(80):
        j = i % 4
        before = rng.gauss(410 + 8 * j, 38)
        improvement = rng.gauss([31, 22, 16, 6][j], 12)
        rows.append({"operator": f"OP-{i+1:03}", "team": ["一组", "二组", "三组", "四组"][j], "before": round(before, 3), "after": round(before - improvement, 3)})
    return rows


def segment_size():
    """每段条数。可由环境变量调整，便于验证暂停/恢复与公平调度。"""
    try:
        return max(1, int(os.getenv("ANALYSIS_SEGMENT_SIZE", "8")))
    except (TypeError, ValueError):
        return 8


def parse_params(params):
    team = str(params.get("team", "全部"))
    try:
        confidence = float(params.get("confidence", .95))
    except (ValueError, TypeError):
        raise ValueError("置信度只接受 0.90/0.95/0.99")
    if confidence not in (.9, .95, .99) or team not in TEAMS:
        raise ValueError("未知小组或置信度")
    return team, confidence


def prepare(records, params):
    """校验整批样本并返回筛选后的配对行。

    与旧实现保持同样的校验口径：所有记录都必须字段完整、测时为正
    有限数、每人只有一条配对；筛选参数只影响参与统计的行，但重复
    作业员在整批范围内识别。
    """
    team, confidence = parse_params(params)
    clean, seen = [], set()
    for r in records:
        try:
            name, group = str(r["operator"]), str(r["team"])
            before, after = float(r["before"]), float(r["after"])
            if name in seen or not all(math.isfinite(x) and x > 0 for x in (before, after)):
                raise ValueError()
            seen.add(name)
            if team == "全部" or team == group:
                clean.append({"operator": name, "team": group, "before": before, "after": after, "difference": before - after})
        except (KeyError, ValueError, TypeError):
            raise ValueError("每人仅能有一条完整配对，测时必须为正有限数")
    if len(clean) < 3:
        raise ValueError("至少需要 3 组完整配对")
    return team, confidence, clean


def chunks(rows, size=None):
    size = size or segment_size()
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def summarize(rows):
    """把一段配对差汇总为 n / mean / m2，可在任意进程/时间点重算。

    只累计差值，不累计原始测时；单段内部用 Welford 增量更新，按段
    整体覆盖即可安全重试，不会重复累计人数。
    """
    n, mean, m2 = 0, 0.0, 0.0
    for row in rows:
        x = float(row["difference"])
        n += 1
        delta = x - mean
        mean += delta / n
        m2 += delta * (x - mean)
    return {"n": n, "mean": mean, "m2": m2}


def with_rows(summary, rows):
    """把持久化的段汇总与该段的输入行组合，供精确合成使用。"""
    return {**summary, "rows": list(rows or [])}


def combine(partials):
    """按段合成 n / mean / m2。

    各段携带原始配对行时，用 ``math.fsum`` 对全部差值做精确求和与
    两遍方差，合成结果与段边界无关，和整批计算完全一致；差值先
    由 before−after 得到，避免大测时直接平方累计。缺少明细行的
    旧汇总则退回 Chan 并行合并公式。
    """
    parts = list(partials)
    if parts and all("rows" in part for part in parts):
        values = [float(row["difference"]) for part in parts for row in (part.get("rows") or [])]
        n = len(values)
        if n == 0:
            return 0, 0.0, 0.0
        mean = math.fsum(values) / n
        m2 = math.fsum((x - mean) ** 2 for x in values)
        return n, mean, m2
    n, mean, m2 = 0, 0.0, 0.0
    for part in parts:
        part_n = int(part.get("n") or 0)
        if part_n <= 0:
            continue
        if n == 0:
            n, mean, m2 = part_n, float(part["mean"]), float(part["m2"])
            continue
        delta = float(part["mean"]) - mean
        total = n + part_n
        mean += delta * part_n / total
        m2 += float(part["m2"]) + delta * delta * n * part_n / total
        n = total
    return n, mean, m2


def build_result(partials, team, confidence):
    """由各段汇总合成最终结果；全部段到齐后才调用。"""
    n, mean, m2 = combine(partials)
    if n < 3:
        raise ValueError("至少需要 3 组完整配对")
    variance = m2 / (n - 1)
    if variance < 0 and variance > -1e-12:
        variance = 0.0
    sd = math.sqrt(variance) if variance > 0 else 0.0
    if sd <= 1e-12:
        raise ValueError("配对差完全相同，无法估计标准误")
    se, df = sd / math.sqrt(n), n - 1
    t = mean / se
    p = float(2 * stats.t.sf(abs(t), df))
    margin = float(stats.t.ppf((1 + confidence) / 2, df)) * se
    low, high = mean - margin, mean + margin
    clean = [row for part in partials for row in (part.get("rows") or [])]
    diffs = [float(r["difference"]) for r in clean]
    rows = [{**r, "before": round(float(r["before"]), 3), "after": round(float(r["after"]), 3), "difference": round(float(r["difference"]), 3), "review": "缩短" if float(r["difference"]) > 0 else "需走查"} for r in sorted(clean, key=lambda r: float(r["difference"]))]
    teams = sorted(set(r["team"] for r in clean))
    return {"summary": f"{n} 人完整配对，平均节省 {mean:.3f} 秒；{confidence*100:g}% 区间 [{low:.3f}, {high:.3f}] 秒。",
            "metrics": [{"label": "平均节省", "value": round(mean, 5), "unit": "秒"}, {"label": "配对 p 值", "value": float(f"{p:.6g}")}, {"label": "配对效应 dz", "value": round(mean / sd, 5)}, {"label": "需走查人员", "value": int(sum(d <= 0 for d in diffs)), "unit": "人"}],
            "table": {"columns": [{"key": "operator", "label": "匿名人员"}, {"key": "team", "label": "小组"}, {"key": "before", "label": "原路线/秒"}, {"key": "after", "label": "新路线/秒"}, {"key": "difference", "label": "节省/秒"}, {"key": "review", "label": "复测建议"}], "rows": rows},
            "chart": {"type": "bar", "labels": teams, "series": [{"name": "平均节省秒数", "values": [round(sum(float(r["difference"]) for r in clean if r["team"] == x) / max(1, sum(1 for r in clean if r["team"] == x)), 4) for x in teams]}]},
            "findings": [f"区间下界 {low:.4f} 秒，上界 {high:.4f} 秒；配对差标准差 {sd:.4f} 秒。", f"t={t:.4f}，df={df}，分析小组：{team}。", f"{int(sum(d > 0 for d in diffs))} 人路线缩短，{int(sum(d <= 0 for d in diffs))} 人需要走查，不能仅依据总均值推断所有人受益。"], "method": metadata()["method"], "warnings": ["前后顺序固定，不能排除学习效应；结果用于下一轮复测设计。"]}


def analyze(records, params):
    """整批分析接口：与分段执行共用 prepare/summarize/combine。"""
    team, confidence, clean = prepare(records, params)
    partials = [with_rows(summarize(chunk), chunk) for chunk in chunks(clean)]
    return build_result(partials, team, confidence)
