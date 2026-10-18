"""
评分追踪服务（纯逻辑，便于测试）：
把历史评分记录与后续行情结合，验证评分体系对收益的预测力
"""

from bisect import bisect_right
from math import sqrt
from typing import Dict, List, Optional


def _record_get(record, key):
    """兼容 dict 与 ORM 对象两种记录形态"""
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def _norm_date_str(value) -> Optional[str]:
    """把 datetime/date/Timestamp/'YYYY-MM-DD ...' 统一为 'YYYY-MM-DD' 字符串"""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    return s[:10] if s else None


def compute_forward_returns(
    records: List,
    closes: Dict[str, float],
    current_price: Optional[float],
) -> List[Dict]:
    """
    为每条评分记录匹配入场价并计算"至今"收益率

    - entry_price：收盘价中 <= 记录日期 的最近一日收盘
      （周末/节假日等非交易日期自动取前一交易日）
    - 记录早于所有交易日、closes为空、无当前价 → 该条跳过

    返回：[{"date","total_score","entry_price","current_price","forward_return_pct"}...]
    """
    matched: List[Dict] = []
    if not closes or not current_price:
        return matched

    dates = sorted(closes.keys())  # ISO日期字符串按字典序即时间序
    for rec in records:
        raw_date = _record_get(rec, "date")
        if raw_date is None:
            raw_date = _record_get(rec, "created_at")  # ScoreHistory ORM行的字段名
        d = _norm_date_str(raw_date)
        if not d:
            continue
        idx = bisect_right(dates, d)
        if idx == 0:
            continue  # 早于所有交易日
        entry_date = dates[idx - 1]
        entry_price = closes[entry_date]
        forward_return_pct = round((current_price / entry_price - 1) * 100, 2)
        matched.append({
            "date": d,
            "total_score": _record_get(rec, "total_score"),
            "entry_price": entry_price,
            "current_price": current_price,
            "forward_return_pct": forward_return_pct,
        })
    return matched


# 分桶定义：左闭右开，最后一桶含上界
_BUCKETS = [
    ("<5分", lambda s: s < 5),
    ("5~6.5分", lambda s: 5 <= s < 6.5),
    ("6.5~8分", lambda s: 6.5 <= s < 8),
    ("≥8分", lambda s: s >= 8),
]


def bucket_stats(records: List) -> List[Dict]:
    """
    按评分区间统计次数与平均后续收益

    返回：[{"bucket","count","avg_return"},...]（avg保留2位，无样本时为None）
    """
    counts = [0] * len(_BUCKETS)
    ret_sums = [0.0] * len(_BUCKETS)
    ret_counts = [0] * len(_BUCKETS)

    for rec in records:
        score = _record_get(rec, "total_score")
        ret = _record_get(rec, "forward_return_pct")
        if score is None:
            continue
        for i, (_, cond) in enumerate(_BUCKETS):
            if cond(score):
                counts[i] += 1
                if ret is not None:
                    ret_sums[i] += ret
                    ret_counts[i] += 1
                break

    return [
        {
            "bucket": label,
            "count": counts[i],
            "avg_return": round(ret_sums[i] / ret_counts[i], 2) if ret_counts[i] else None,
        }
        for i, (label, _) in enumerate(_BUCKETS)
    ]


def pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """
    手写皮尔逊相关系数；n<5 或任一序列方差为0时返回None
    """
    n = len(xs)
    if n != len(ys) or n < 5:
        return None

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return round(cov / sqrt(var_x * var_y), 4)


def _interpret_correlation(corr: Optional[float]) -> str:
    """相关性解读文案"""
    if corr is None:
        return "相关性弱，样本可能不足"
    if corr > 0.3:
        return "评分与后续收益正相关，体系有效"
    if corr < -0.3:
        return "负相关，建议调整权重"
    return "相关性弱，样本可能不足"


def build_tracking(stock_code: str, db_rows: List, history_df=None) -> Dict:
    """
    主入口：评分记录 × 后续行情 → 追踪报告

    - db_rows：ScoreHistory查询结果（升序），dict或ORM对象均可
    - history_df：历史行情DataFrame（DatetimeIndex + Close列），可为None
    - count：累计评分次数（含无法匹配行情的记录）
    """
    rows = list(db_rows)

    # 历史收盘价 → {date_str: close}，最新价取最后一个close
    closes: Dict[str, float] = {}
    current_price: Optional[float] = None
    if history_df is not None and not history_df.empty and "Close" in history_df.columns:
        for idx, row in history_df.iterrows():
            try:
                close = float(row["Close"])
            except (TypeError, ValueError):
                continue
            if close != close:  # NaN
                continue
            closes[_norm_date_str(idx)] = close
        if closes:
            current_price = closes[sorted(closes.keys())[-1]]

    records = compute_forward_returns(rows, closes, current_price)
    correlation = pearson(
        [r["total_score"] for r in records],
        [r["forward_return_pct"] for r in records],
    )

    return {
        "stock_code": stock_code,
        "count": len(rows),
        "records": records,
        "buckets": bucket_stats(records),
        "correlation": correlation,
        "interpretation": _interpret_correlation(correlation),
    }


# ============================================
# [第三批·第二段-4] 四维权重体检报告（3个月滚动窗口 + 各维相关性 + ±10% 权重扰动敏感度矩阵）
# ============================================
# 从 analysis_service 导入全仓库唯一四维权重真源，严禁在本模块重复手写字面量
from .analysis_service import DEFAULT_4D_WEIGHTS, DEFAULT_DIMENSION_WEIGHTS
DIMENSION_LABELS = {
    "technical": "技术面 (tech_score)",
    "news": "消息面 (news_score)",
    "macro": "宏观面 (macro_score)",
    "event": "事件驱动 (event_score)",
}


def _normalize_perturbed_weights(base_weights: Dict[str, float], target_dim: str, delta: float) -> Dict[str, float]:
    """对 target_dim 施加 delta (+0.10 或 -0.10)，其余三维按原比例归一化，确保权重总和严格等于 1.0"""
    w_target = max(0.0, min(1.0, base_weights[target_dim] + delta))
    rem_new = 1.0 - w_target
    other_keys = [k for k in base_weights if k != target_dim]
    rem_old = sum(base_weights[k] for k in other_keys)
    out = {target_dim: round(w_target, 4)}
    for k in other_keys:
        if rem_old > 0:
            out[k] = round(rem_new * (base_weights[k] / rem_old), 4)
        else:
            out[k] = round(rem_new / len(other_keys), 4)
    # 修正四舍五入尾差
    diff = round(1.0 - sum(out.values()), 4)
    if diff != 0 and other_keys:
        out[other_keys[0]] = round(out[other_keys[0]] + diff, 4)
    return out


def build_weight_health_report(
    db_rows: List,
    klines_by_code: Optional[Dict[str, object]] = None,
    closes_by_code: Optional[Dict[str, object]] = None,
    history_df=None,
    window_days: int = 90,
    forward_days: int = 20,
    mode: str = "simple",
    now_dt=None,
) -> Dict:
    """
    四维权重体检报告（纯函数，只观测出报告、绝不自动修改线上权重）：
    1. 截取最近 window_days（默认 90 天即 3 个月滚动窗口）的评分样本
    2. 为每条样本匹配 +forward_days 交易日真实前向收益（若未满期但提供单股 history_df 则兼容最新收盘价）
    3. 分别计算 technical / news / macro / event 四维与前向收益的相关性 r 及方差状态，
       精准定位“过去 3 个月哪一维在裸泳”（相关性最低、为负或恒定无区分度）
    4. 对每一维权重做 +10% / -10% 扰动实验（其余三维按比例归一化），计算高分组合（>=6.5分或Top50%）平均收益变化
    """
    from datetime import datetime, timedelta

    base_weights = dict(DEFAULT_DIMENSION_WEIGHTS.get(mode, DEFAULT_4D_WEIGHTS))
    rows = list(db_rows or [])

    # 解析每只股票的交易日收盘序列
    series_map = {}
    effective_klines = klines_by_code or closes_by_code
    if effective_klines:
        for code, df in effective_klines.items():
            if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
                continue
            dc = {}
            for idx, r in df.iterrows():
                try:
                    c = float(r["Close"])
                    if c == c and c > 0:
                        dc[_norm_date_str(idx)] = c
                except Exception:
                    continue
            if dc:
                sd = sorted(dc.keys())
                series_map[str(code).upper()] = (sd, [dc[d] for d in sd])

    if history_df is not None and not getattr(history_df, "empty", True) and "Close" in history_df.columns:
        dc = {}
        for idx, r in history_df.iterrows():
            try:
                c = float(r["Close"])
                if c == c and c > 0:
                    dc[_norm_date_str(idx)] = c
            except Exception:
                continue
        if dc:
            sd = sorted(dc.keys())
            series_map["__DEFAULT__"] = (sd, [dc[d] for d in sd])

    # 提取有效日期并按最近 window_days 滚动窗口过滤
    dated_rows = []
    for rec in rows:
        raw_dt = _record_get(rec, "date") or _record_get(rec, "created_at")
        d_str = _norm_date_str(raw_dt)
        if not d_str:
            continue
        try:
            dt_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
            dated_rows.append((dt_obj, d_str, rec))
        except Exception:
            continue

    if dated_rows and window_days and window_days > 0:
        max_date = max(x[0] for x in dated_rows)
        cutoff_date = max_date - timedelta(days=window_days)
        dated_rows = [x for x in dated_rows if x[0] >= cutoff_date]

    samples = []
    for dt_obj, d_str, rec in dated_rows:
        code = str(_record_get(rec, "stock_code") or "").upper()
        s_tuple = series_map.get(code) or series_map.get("__DEFAULT__")
        if not s_tuple:
            continue
        s_dates, s_closes = s_tuple
        pos = bisect_right(s_dates, d_str) - 1
        if pos < 0:
            continue
        entry_px = _record_get(rec, "price_at_score")
        entry_px = float(entry_px) if (entry_px is not None and float(entry_px) > 0) else s_closes[pos]
        if not entry_px or entry_px <= 0:
            continue

        if forward_days and pos + forward_days < len(s_closes):
            exit_px = s_closes[pos + forward_days]
        elif pos < len(s_closes) - 1:
            exit_px = s_closes[-1]
        else:
            continue

        fwd_ret = round((exit_px / entry_px - 1.0) * 100.0, 2)
        raw_tech = _record_get(rec, "tech_score") if _record_get(rec, "tech_score") is not None else _record_get(rec, "technical_score")
        tech = float(raw_tech if raw_tech is not None else 5.0)
        news = float(_record_get(rec, "news_score") if _record_get(rec, "news_score") is not None else 5.0)
        macro = float(_record_get(rec, "macro_score") if _record_get(rec, "macro_score") is not None else 5.0)
        event = float(_record_get(rec, "event_score") if _record_get(rec, "event_score") is not None else 5.0)
        samples.append({
            "date": d_str,
            "stock_code": code,
            "technical": tech,
            "news": news,
            "macro": macro,
            "event": event,
            "forward_return_pct": fwd_ret,
        })

    fwd_rets = [s["forward_return_pct"] for s in samples]
    dim_corrs = {}
    for dim in ("technical", "news", "macro", "event"):
        xs = [s[dim] for s in samples]
        corr = pearson(xs, fwd_rets)
        unique_vals = len(set(round(x, 4) for x in xs)) if xs else 0
        var_zero = unique_vals <= 1 and len(xs) >= 5
        # 有效排序分：负相关最差，其次为0方差常数（死水无区分度），其次为近零相关
        effective_score = corr if corr is not None else (-0.01 if var_zero else 0.0)
        dim_corrs[dim] = {
            "dimension": dim,
            "label": DIMENSION_LABELS[dim],
            "base_weight": base_weights[dim],
            "correlation": corr,
            "variance_zero": var_zero,
            "effective_rank_score": round(effective_score, 4),
            "status": (
                "有效正相关" if (corr is not None and corr >= 0.15)
                else "负相关(反向噪音)" if (corr is not None and corr < 0)
                else "死水常数(无区分度)" if var_zero
                else "弱相关/噪音"
            ),
        }

    # 判定“过去 3 个月哪一维在裸泳”（按 effective_rank_score 升序，若相同则权重更高却无效者优先点名）
    ranked_dims = sorted(
        dim_corrs.values(),
        key=lambda d: (d["effective_rank_score"], -d["base_weight"]),
    )
    naked = ranked_dims[0]
    naked_dim = naked["dimension"]
    if naked["correlation"] is not None and naked["correlation"] < 0:
        naked_reason = f"{naked['label']} 与前向收益呈负相关 (r={naked['correlation']})，在占用 {int(naked['base_weight']*100)}% 权重的同时拖累组合选股精度"
    elif naked["variance_zero"]:
        naked_reason = f"{naked['label']} 在滚动窗口内几乎恒定不变（零方差无区分度），白占 {int(naked['base_weight']*100)}% 权重却未提供有效 Alpha 信号"
    else:
        naked_reason = f"{naked['label']} 相关性全维垫底 (r={naked['correlation']})，相对其 {int(naked['base_weight']*100)}% 权重贡献度最低"

    # 计算组合基准收益与 ±10% 权重扰动影响
    def _cohort_return(w_map: Dict[str, float]) -> float:
        if not samples:
            return 0.0
        scored = []
        for s in samples:
            tot = sum(w_map[k] * s[k] for k in ("technical", "news", "macro", "event"))
            scored.append((tot, s["forward_return_pct"]))
        high = [r for sc, r in scored if sc >= 6.5]
        if len(high) < max(1, len(scored) // 4):
            scored.sort(key=lambda x: x[0], reverse=True)
            top_k = max(1, len(scored) // 2)
            high = [r for _, r in scored[:top_k]]
        return round(sum(high) / len(high), 2) if high else 0.0

    base_port_ret = _cohort_return(base_weights)
    perturbations = []
    for dim in ("technical", "news", "macro", "event"):
        for delta in (0.10, -0.10):
            w_pert = _normalize_perturbed_weights(base_weights, dim, delta)
            pert_ret = _cohort_return(w_pert)
            perturbations.append({
                "dimension": dim,
                "label": DIMENSION_LABELS[dim],
                "delta": f"{delta:+.0%}",
                "perturbed_weights": w_pert,
                "base_portfolio_return_pct": base_port_ret,
                "perturbed_portfolio_return_pct": pert_ret,
                "return_impact_pct": round(pert_ret - base_port_ret, 2),
            })

    diagnosis = (
        f"过去{window_days}天滚动窗口（共{len(samples)}条样本）四维体检结论："
        f"【{naked['label']}】正在裸泳（{naked_reason}）。"
    )

    pert_matrix = {}
    for p_item in perturbations:
        d_key = p_item["dimension"]
        pert_matrix.setdefault(d_key, {})[p_item["delta"]] = p_item

    naked_info = {
        "dimension": naked_dim,
        "label": naked["label"],
        "correlation": naked["correlation"],
        "base_weight": naked["base_weight"],
        "reason": naked_reason,
        "conclusion": diagnosis,
    }
    return {
        "window_days": window_days,
        "forward_days": forward_days,
        "mode": mode,
        "sample_count": len(samples),
        "base_weights": base_weights,
        "base_portfolio_return_pct": base_port_ret,
        "dimension_correlations": dim_corrs,
        "dimensions": dim_corrs,
        "swimming_naked_dimension": naked_info,
        "naked_swimmer": naked_info,
        "diagnosis": diagnosis,
        "perturbation_sensitivity": perturbations,
        "perturbation_matrix": pert_matrix,
        "weights_modified": False,
        "policy_note": "仅输出四维相关性与±10%权重扰动诊断报告，不自动修改线上打分权重",
    }
