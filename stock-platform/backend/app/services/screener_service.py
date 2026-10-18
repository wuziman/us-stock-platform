"""
美股硬科技周度选股服务（Alpha TOP 5 猛禽池，已从原根脚本 tech_stock_screener.py 迁入 platform）：
- 选股结果直接持久化至 SQLite 的 WeeklyTop5Snapshot 表（/api/ai-pick/top5 与 /top5/history 直接读库）
- 首次启动采用非阻塞种子初始化（seed_weekly_snapshots_if_empty），绝不在启动阶段发起全量 35 股网络请求阻塞容器
- 回测胜率因子统一对齐 BacktestService 口径（入场 bar 不判卖出、严防日内前视）
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
try:
    import yfinance as yf
except ImportError:
    yf = None
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models_platform import WeeklyTop5Snapshot
from ..utils.app_config import load_config
from ..utils.indicators import (
    calculate_atr,
    calculate_bollinger_bands,
    calculate_macd,
    calculate_rsi,
)
from ..utils.price_levels import (
    LINEAR_PROFIT_PCT,
    STOP_PCT,
    atr_adaptive_levels,
    linear_buy_point,
    nonlinear_buy_point,
)
from ..utils.timezone import now_beijing, today_beijing
from .llm_client import chat_completion, is_ai_configured

_REPO_ROOT = Path(__file__).resolve().parents[4]

HARD_TECH_UNIVERSE = [
    {"code": "NVDA", "name": "英伟达", "sector": "AI算力总龙头"},
    {"code": "TSM", "name": "台积电", "sector": "先进制程代工垄断"},
    {"code": "AVGO", "name": "博通", "sector": "定制ASIC与交换芯片"},
    {"code": "AMD", "name": "超威半导体", "sector": "GPU与数据中心CPU"},
    {"code": "QCOM", "name": "高通", "sector": "端侧AI与移动平台"},
    {"code": "MU", "name": "美光科技", "sector": "HBM3e高带宽存储"},
    {"code": "ARM", "name": "安谋", "sector": "低功耗高能效架构"},
    {"code": "MRVL", "name": "美满电子", "sector": "定制云算力与网络ASIC"},
    {"code": "INTC", "name": "英特尔", "sector": "传统芯片IDM转型"},
    {"code": "TXN", "name": "德州仪器", "sector": "模拟与汽车芯片基石"},
    {"code": "ADI", "name": "亚德诺", "sector": "高性能模拟与精密信号"},
    {"code": "ON", "name": "安森美", "sector": "汽车与工业碳化硅"},
    {"code": "MPWR", "name": "芯源系统", "sector": "高压电源管理芯片"},
    {"code": "ASML", "name": "阿斯麦", "sector": "EUV光刻机绝对垄断"},
    {"code": "AMAT", "name": "应用材料", "sector": "半导体设备百宝箱"},
    {"code": "LRCX", "name": "泛林半导体", "sector": "先进制程刻蚀与薄膜"},
    {"code": "KLAC", "name": "科磊半导体", "sector": "工艺控制与缺陷检测"},
    {"code": "LITE", "name": "Lumentum", "sector": "光器件与激光器龙头"},
    {"code": "COHR", "name": "Coherent", "sector": "800G光收发与光学材料"},
    {"code": "AAOI", "name": "祥茂光电", "sector": "高弹性数据中心光模块"},
    {"code": "AXTI", "name": "AXT", "sector": "磷化铟光通信衬底材料"},
    {"code": "CIEN", "name": "锡安通讯", "sector": "相干光网络系统龙头"},
    {"code": "FN", "name": "Fabrinet", "sector": "高端光互联代工之王"},
    {"code": "VRT", "name": "维谛技术", "sector": "数据中心液冷与电力"},
    {"code": "SMCI", "name": "超微电脑", "sector": "高密AI服务器整机柜"},
    {"code": "ANET", "name": "阿里斯塔", "sector": "800G AI数据中心网络"},
    {"code": "DELL", "name": "戴尔科技", "sector": "企业级AI服务器"},
    {"code": "HPE", "name": "慧与科技", "sector": "高性能超算与企业云"},
    {"code": "PLTR", "name": "Palantir", "sector": "企业级AI决策操作系统"},
    {"code": "MSFT", "name": "微软", "sector": "Azure云与商业化AI"},
    {"code": "GOOGL", "name": "谷歌", "sector": "自研TPU与Gemini生态"},
    {"code": "AMZN", "name": "亚马逊", "sector": "AWS云与自研Trainium"},
    {"code": "META", "name": "Meta", "sector": "开源大模型与算力集群"},
    {"code": "AAPL", "name": "苹果", "sector": "Apple Intelligence终端"},
    {"code": "TSLA", "name": "特斯拉", "sector": "FSD端到端自动驾驶算力"},
]


def _pick_next_earnings_date(candidates, today: date) -> Optional[date]:
    normalized = []
    for d in candidates or []:
        try:
            normalized.append(d.date() if callable(getattr(d, "date", None)) else d)
        except Exception:
            continue
    future = [d for d in normalized if isinstance(d, date) and d >= today]
    return min(future) if future else None


def simulate_pullback_winrate(hist: pd.DataFrame, ma20_series: pd.Series) -> float:
    """
    步进式（Walk-Forward）回踩挂单胜率验证（与 BacktestService 口径统一：入场 bar 不判卖出）：
    - 空仓时根据前一日收盘价与 MA20 计算今日挂单买入价（linear_buy_point）
    - 当日 Low <= 挂单价则成交（跳空低开按 Open 成交），入场当日不判卖出（防日内前视）
    - 次根 bar 起严格按日内 High/Low 判定：先判 -8% 止损（风险优先），再判 +15% 止盈，超过 20 个交易日按收盘价平仓
    """
    if len(hist) < 35:
        return 50.0

    position = 0
    entry_price = 0.0
    stop_price = 0.0
    profit_price = 0.0
    holding_days = 0
    win_count = 0
    loss_count = 0

    opens = hist["Open"].values
    highs = hist["High"].values
    lows = hist["Low"].values
    closes = hist["Close"].values
    ma20s = ma20_series.values

    for i in range(20, len(hist)):
        prev_close = float(closes[i - 1])
        prev_ma20 = float(ma20s[i - 1]) if pd.notna(ma20s[i - 1]) else prev_close * 0.95
        curr_open = float(opens[i])
        curr_high = float(highs[i])
        curr_low = float(lows[i])
        curr_close = float(closes[i])

        if position == 0:
            pending_buy = linear_buy_point(prev_close, prev_ma20)
            if curr_low <= pending_buy and pending_buy > 0:
                entry_price = min(curr_open, pending_buy)
                stop_price = entry_price * STOP_PCT
                profit_price = entry_price * LINEAR_PROFIT_PCT
                position = 1
                holding_days = 0
        else:
            holding_days += 1
            if curr_low <= stop_price:
                loss_count += 1
                position = 0
            elif curr_high >= profit_price:
                win_count += 1
                position = 0
            elif holding_days >= 20:
                if curr_close > entry_price:
                    win_count += 1
                else:
                    loss_count += 1
                position = 0

    total_trades = win_count + loss_count
    return (win_count / total_trades * 100.0) if total_trades > 0 else 50.0


def analyze_candidate(stock_meta: dict, ticker_cls=None) -> Optional[dict]:
    """拉取单只候选标的历史数据与基本面快照，计算动量(35)+超跌(35)+回测与估值(30)复合总分"""
    code = stock_meta["code"]
    name = stock_meta["name"]
    sector = stock_meta["sector"]
    t_factory = ticker_cls or yf.Ticker

    try:
        t = t_factory(code)
        hist = t.history(period="6mo") if t is not None else pd.DataFrame()
        if hist is None or hist.empty:
            # 离线兜底：若本地有 *_data.xlsx 则读取
            candidates = list(_REPO_ROOT.glob(f"**/{code}*_data.xlsx"))
            if candidates:
                hist = pd.read_excel(candidates[0])
                if "Date" in hist.columns:
                    hist["Date"] = pd.to_datetime(hist["Date"])
                    hist = hist.set_index("Date")
        if hist is None or hist.empty:
            return None
        hist = hist.dropna(subset=["Close"])
        if len(hist) < 35:
            return None

        close = hist["Close"]
        current_price = float(close.iloc[-1])

        rsi_series = calculate_rsi(close, 14)
        rsi = float(rsi_series.iloc[-1]) if pd.notna(rsi_series.iloc[-1]) else 50.0

        ma20_series = close.rolling(20).mean()
        ma50_series = close.rolling(50).mean()
        ma20 = float(ma20_series.iloc[-1])
        ma50 = float(ma50_series.iloc[-1]) if pd.notna(ma50_series.iloc[-1]) else ma20
        _, _, bb_lower_series = calculate_bollinger_bands(close, 20, 2)
        bb_lower = float(bb_lower_series.iloc[-1])
        atr_series = calculate_atr(hist["High"], hist["Low"], close, 14)
        atr_val = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else None

        macd_series, signal_series, _ = calculate_macd(close, 12, 26, 9)
        macd_val = float(macd_series.iloc[-1])
        signal_val = float(signal_series.iloc[-1])

        m20_return = float((current_price - close.iloc[-20]) / close.iloc[-20] * 100.0)
        vol_ma20 = float(hist["Volume"].rolling(20).mean().iloc[-1]) if "Volume" in hist.columns else 0.0
        vol_ratio = float(hist["Volume"].iloc[-1] / vol_ma20) if vol_ma20 > 0 else 1.0

        forward_pe = None
        rev_growth_pct = None
        target_price = None
        upside_pct = None
        try:
            info = getattr(t, "info", None) or {}
            if isinstance(info, dict) and info:
                fpe = info.get("forwardPE") or info.get("trailingPE")
                if fpe and isinstance(fpe, (int, float)) and fpe > 0:
                    forward_pe = round(float(fpe), 1)
                rg = info.get("revenueGrowth")
                if rg is not None and isinstance(rg, (int, float)):
                    rev_growth_pct = round(float(rg) * 100.0, 1)
                tp = info.get("targetMeanPrice")
                if tp and isinstance(tp, (int, float)) and tp > 0 and current_price > 0:
                    target_price = round(float(tp), 2)
                    upside_pct = round((target_price - current_price) / current_price * 100.0, 1)
        except Exception:
            pass

        earnings_info = {"date": None, "days": None, "tag": "", "is_imminent": False}
        try:
            cal = getattr(t, "calendar", None)
            candidates = None
            if isinstance(cal, dict):
                candidates = cal.get("Earnings Date")
            elif cal is not None and hasattr(cal, "get"):
                candidates = cal.get("Earnings Date")
            today = today_beijing()
            ed_date = _pick_next_earnings_date(candidates, today)
            if ed_date:
                days = (ed_date - today).days
                earnings_info["date"] = ed_date.strftime("%Y-%m-%d")
                earnings_info["days"] = days
                if days == 0:
                    earnings_info["tag"] = "🚨【今晚发布财报】"
                    earnings_info["is_imminent"] = True
                elif 0 < days <= 3:
                    earnings_info["tag"] = f"⚠️【{days}天后财报】"
                    earnings_info["is_imminent"] = True
                elif 3 < days <= 14:
                    earnings_info["tag"] = f"📅【{days}天后财报】"
        except Exception:
            pass

        # 因子 A: 动量突破与量价因子 (0-35 分)
        momentum_score = 10.0
        if current_price > ma20:
            momentum_score += 6.0
        if current_price > ma50:
            momentum_score += 4.0
        if ma20 > ma50:
            momentum_score += 5.0
        if macd_val > signal_val:
            momentum_score += 5.0
        if m20_return > 0:
            momentum_score += min(5.0, m20_return * 0.5)
        if current_price > ma20 and 1.2 <= vol_ratio <= 2.8:
            momentum_score += 2.0
        momentum_score = max(0.0, min(35.0, momentum_score))

        # 因子 B: 超跌击球点因子 (0-35 分)
        bearish_alignment = ma20 < ma50
        sweetspot_score = 15.0
        if rsi < 30:
            if not bearish_alignment:
                sweetspot_score += 15.0
        elif rsi < 40:
            if not bearish_alignment:
                sweetspot_score += 10.0
        elif rsi < 50:
            sweetspot_score += 5.0
        elif rsi > 70:
            sweetspot_score -= 10.0

        if current_price <= bb_lower * 1.02 and not bearish_alignment:
            sweetspot_score += 5.0
        sweetspot_score = max(0.0, min(35.0, sweetspot_score))

        # 因子 C: 步进式回测期望 + 基本面目标价空间验证 (0-30 分)
        backtest_score = 15.0
        win_rate = simulate_pullback_winrate(hist, ma20_series)
        if win_rate >= 60.0:
            backtest_score += 12.0
        elif win_rate >= 50.0:
            backtest_score += 8.0
        elif win_rate < 40.0:
            backtest_score -= 5.0

        if upside_pct is not None and upside_pct >= 15.0:
            backtest_score += 3.0
        elif upside_pct is not None and upside_pct < -10.0:
            backtest_score -= 3.0
        if rev_growth_pct is not None and rev_growth_pct >= 20.0:
            backtest_score += 2.0

        if earnings_info["is_imminent"]:
            backtest_score -= 15.0
        backtest_score = max(0.0, min(30.0, backtest_score))

        total_score = max(0.0, min(100.0, momentum_score + sweetspot_score + backtest_score))

        linear_buy = linear_buy_point(current_price, ma20)
        nonlinear_buy = nonlinear_buy_point(rsi, ma20, bb_lower)
        lin_levels = atr_adaptive_levels(current_price, linear_buy, LINEAR_PROFIT_PCT, atr=atr_val)

        return {
            "code": code,
            "name": name,
            "sector": sector,
            "current_price": round(current_price, 2),
            "rsi": round(rsi, 1),
            "m20_return": round(m20_return, 1),
            "win_rate": round(win_rate, 1),
            "momentum_score": round(momentum_score, 1),
            "sweetspot_score": round(sweetspot_score, 1),
            "backtest_score": round(backtest_score, 1),
            "total_score": round(total_score, 1),
            "forward_pe": forward_pe,
            "rev_growth_pct": rev_growth_pct,
            "target_price": target_price,
            "upside_pct": upside_pct,
            "earnings": earnings_info,
            "linear_buy": round(linear_buy, 2),
            "nonlinear_buy": round(nonlinear_buy, 2),
            "linear_profit": round(linear_buy * LINEAR_PROFIT_PCT, 2),
            "linear_stop": round(linear_buy * STOP_PCT, 2),
            "atr_stop": lin_levels.get("atr_stop", round(linear_buy * STOP_PCT, 2)),
            "breakeven_trigger": lin_levels.get("breakeven_trigger", round(linear_buy * 1.10, 2)),
        }
    except Exception:
        return None


def _row_to_payload(row: WeeklyTop5Snapshot) -> Dict:
    try:
        top5 = json.loads(row.top5_json) if row.top5_json else []
    except Exception:
        top5 = []
    try:
        br = json.loads(row.battle_report_json) if row.battle_report_json else None
    except Exception:
        br = None
    return {
        "scan_date": row.scan_date,
        "total_scanned": row.total_scanned or len(top5),
        "top5": top5,
        "ai_thesis": row.ai_thesis or "",
        "battle_report": br,
    }


def save_weekly_snapshot_to_db(payload: Dict, db: Optional[Session] = None) -> Dict:
    """将单期周度 TOP 5 选股结果落库到 WeeklyTop5Snapshot 表"""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        scan_date = str(payload.get("scan_date") or now_beijing().strftime("%Y-%m-%d %H:%M:%S"))
        top5_str = json.dumps(payload.get("top5") or [], ensure_ascii=False)
        br = payload.get("battle_report")
        br_str = json.dumps(br, ensure_ascii=False) if br is not None else None
        existing = db.query(WeeklyTop5Snapshot).filter(
            WeeklyTop5Snapshot.scan_date == scan_date
        ).first()
        if existing:
            existing.total_scanned = int(payload.get("total_scanned") or 0)
            existing.top5_json = top5_str
            existing.ai_thesis = str(payload.get("ai_thesis") or "")
            existing.battle_report_json = br_str
        else:
            db.add(WeeklyTop5Snapshot(
                scan_date=scan_date,
                total_scanned=int(payload.get("total_scanned") or 0),
                top5_json=top5_str,
                ai_thesis=str(payload.get("ai_thesis") or ""),
                battle_report_json=br_str,
                created_at=now_beijing(),
            ))
        db.commit()
        return payload
    finally:
        if close_db:
            db.close()


def seed_weekly_snapshots_if_empty(db: Optional[Session] = None):
    """
    非阻塞快速初始化（<5ms，绝不在启动时发网络请求阻塞容器）：
    - 若数据库 weekly_top5_snapshots 表为空，自动导入 data/weekly_alpha_history.json 与 data/weekly_alpha_top5.json 种子
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        if db.query(WeeklyTop5Snapshot).count() > 0:
            return
        hist_file = _REPO_ROOT / "data" / "weekly_alpha_history.json"
        top5_file = _REPO_ROOT / "data" / "weekly_alpha_top5.json"

        loaded_dates = set()
        if hist_file.exists():
            try:
                weeks = json.loads(hist_file.read_text(encoding="utf-8"))
                for w in weeks if isinstance(weeks, list) else []:
                    sd = w.get("scan_date")
                    if sd and sd not in loaded_dates:
                        loaded_dates.add(sd)
                        save_weekly_snapshot_to_db({
                            "scan_date": sd,
                            "total_scanned": len(HARD_TECH_UNIVERSE),
                            "top5": w.get("top5", []),
                            "ai_thesis": w.get("ai_thesis", "量化复合因子精选硬科技猛禽池"),
                            "battle_report": w.get("battle_report"),
                        }, db=db)
            except Exception:
                pass

        if top5_file.exists():
            try:
                latest = json.loads(top5_file.read_text(encoding="utf-8"))
                if isinstance(latest, dict) and latest.get("scan_date"):
                    save_weekly_snapshot_to_db(latest, db=db)
            except Exception:
                pass
    finally:
        if close_db:
            db.close()


def get_latest_top5_from_db(db: Optional[Session] = None) -> Dict:
    """从 SQLite WeeklyTop5Snapshot 表读取最新一期 TOP 5 选股结果"""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        seed_weekly_snapshots_if_empty(db)
        rows = db.query(WeeklyTop5Snapshot).all()
        if not rows:
            return {
                "scan_date": None,
                "total_scanned": 0,
                "top5": [],
                "ai_thesis": "暂无周度选股数据，请点击上方按钮运行扫描",
            }
        rows.sort(key=lambda r: (str(r.scan_date or ""), r.id or 0), reverse=True)
        return _row_to_payload(rows[0])
    finally:
        if close_db:
            db.close()


def get_top5_history_from_db(db: Optional[Session] = None) -> Dict:
    """从 SQLite WeeklyTop5Snapshot 表读取历期猛禽池战报与累计胜率统计"""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        seed_weekly_snapshots_if_empty(db)
        rows = db.query(WeeklyTop5Snapshot).all()
        rows.sort(key=lambda r: (str(r.scan_date or ""), r.id or 0))
        weeks = []
        for r in rows:
            p = _row_to_payload(r)
            weeks.append({
                "scan_date": p["scan_date"],
                "top5": p["top5"],
                "battle_report": p["battle_report"],
            })

        total_weeks = sum(1 for w in weeks if w.get("battle_report"))
        all_picks = []
        for w in weeks:
            br = w.get("battle_report")
            if br and br.get("picks"):
                all_picks.extend(br["picks"])
        total_picks = len(all_picks)
        total_winners = sum(1 for p in all_picks if p.get("is_winner"))
        overall_win_rate = round(total_winners / total_picks * 100, 1) if total_picks else 0.0
        avg_return = round(sum(float(p.get("weekly_return_pct", 0.0)) for p in all_picks) / total_picks, 2) if total_picks else 0.0
        return {
            "weeks": weeks,
            "cumulative": {
                "total_weeks": total_weeks,
                "total_picks": total_picks,
                "overall_win_rate": overall_win_rate,
                "avg_return_pct": avg_return,
                "total_winners": total_winners,
            } if weeks else None,
        }
    finally:
        if close_db:
            db.close()


def review_previous_picks_from_db(db: Optional[Session] = None, ticker_cls=None) -> Optional[Dict]:
    """从数据库读取上一期 TOP 5 并计算周度收益复盘"""
    prev = get_latest_top5_from_db(db)
    prev_top5 = prev.get("top5") or []
    prev_date = prev.get("scan_date") or "未知"
    if not prev_top5:
        return None

    t_factory = ticker_cls or yf.Ticker

    def _review_one(item):
        code = item["code"]
        entry_price = float(item.get("current_price") or 0.0)
        now_price = entry_price
        try:
            t = t_factory(code)
            h = t.history(period="5d") if t is not None else pd.DataFrame()
            if h is not None and not h.empty:
                h = h.dropna(subset=["Close"])
                if not h.empty:
                    now_price = float(h["Close"].iloc[-1])
        except Exception:
            now_price = entry_price
        weekly_ret = round((now_price - entry_price) / entry_price * 100, 2) if entry_price else 0.0
        return {
            "code": code,
            "name": item.get("name", code),
            "sector": item.get("sector", ""),
            "entry_price": entry_price,
            "review_price": round(now_price, 2),
            "weekly_return_pct": weekly_ret,
            "is_winner": weekly_ret > 0,
        }

    with ThreadPoolExecutor(max_workers=min(5, len(prev_top5))) as executor:
        picks_review = list(executor.map(_review_one, prev_top5))

    winners = [p for p in picks_review if p["is_winner"]]
    losers = [p for p in picks_review if not p["is_winner"]]
    avg_ret = round(sum(p["weekly_return_pct"] for p in picks_review) / len(picks_review), 2)
    best = max(picks_review, key=lambda x: x["weekly_return_pct"])
    worst = min(picks_review, key=lambda x: x["weekly_return_pct"])

    return {
        "prev_scan_date": prev_date,
        "review_date": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
        "picks": picks_review,
        "win_count": len(winners),
        "loss_count": len(losers),
        "win_rate": round(len(winners) / len(picks_review) * 100, 1),
        "avg_return_pct": avg_ret,
        "best_pick": {"code": best["code"], "name": best["name"], "return_pct": best["weekly_return_pct"]},
        "worst_pick": {"code": worst["code"], "name": worst["name"], "return_pct": worst["weekly_return_pct"]},
    }


def generate_ai_thesis(top5: list) -> str:
    """调用大模型为 TOP 5 标的生成产业逻辑与催化研判"""
    if not (is_ai_configured and is_ai_configured() and chat_completion):
        return "量化多因子引擎已根据动量突破(35分)+超跌击球点(35分)+BacktestService步进回测胜率(30分)完成最新一期筛选。"

    candidates_text = "\n".join([
        f"- TOP {i}: {s['code']} {s['name']} ({s['sector']}) | 现价 ${s['current_price']}, 量化总分 {s['total_score']}分, "
        f"动量分 {s['momentum_score']}, 超跌分 {s['sweetspot_score']}, 近6月胜率 {s['win_rate']}%, RSI={s['rsi']}, 20日动量 {s['m20_return']:+.1f}%"
        for i, s in enumerate(top5, 1)
    ])
    prompt = f"请为以下本周美股硬科技 Alpha TOP 5 标的撰写 300 字以内的核心逻辑与伏击点位简评：\n{candidates_text}"
    try:
        res = chat_completion([{"role": "user", "content": prompt}], max_tokens=2000)
        return res.strip()
    except Exception:
        return "AI研报生成中遇到临时延迟，各标的量化点位已就绪。"


def run_screener(
    db: Optional[Session] = None,
    universe: Optional[List[Dict]] = None,
    ticker_cls=None,
    sync_json: Optional[bool] = None,
) -> Dict:
    """执行硬科技全池扫描并将最新 TOP 5 结果持久化入库（同时同步更新 data/weekly_alpha_top5.json 备份）"""
    pool = universe or HARD_TECH_UNIVERSE
    results = []
    with ThreadPoolExecutor(max_workers=min(16, max(len(pool), 1))) as executor:
        futures = {executor.submit(analyze_candidate, item, ticker_cls): item["code"] for item in pool}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                results.append(res)

    eligible = [r for r in results if not r["earnings"]["is_imminent"]]
    if len(eligible) < 5:
        eligible = results

    sorted_results = sorted(eligible, key=lambda x: x["total_score"], reverse=True)
    top5 = sorted_results[:5]

    battle_report = review_previous_picks_from_db(db=db, ticker_cls=ticker_cls)
    ai_thesis = generate_ai_thesis(top5)
    scan_dt_str = now_beijing().strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "scan_date": scan_dt_str,
        "total_scanned": len(results),
        "top5": top5,
        "ai_thesis": ai_thesis,
        "battle_report": battle_report,
    }
    save_weekly_snapshot_to_db(payload, db=db)

    # 同步写一份本地 JSON 备份（仅生产实盘写入，测试隔离环境下不污染 data/weekly_alpha_top5.json）
    should_sync = sync_json if sync_json is not None else ("STOCK_PLATFORM_DB" not in os.environ)
    if should_sync:
        try:
            data_dir = _REPO_ROOT / "data"
            data_dir.mkdir(exist_ok=True)
            (data_dir / "weekly_alpha_top5.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    return payload
