#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股硬科技周度选股引擎 - Alpha TOP 5 猛禽池
- 覆盖美股半导体、AI芯片、光通信、算力基础设施与科技云巨头 (~35-40支高流动性行业领头羊)
- 多线程并发拉取行情、技术指标与基本面估值/华尔街一致目标价
- 四合一量化复合因子打分（动量突破 35分 + 超跌击球点 35分 + 步进回测期望 30分 + 基本面估值/目标价催化润色）
- 财报日历排雷雷达 + ATR(14) 自适应动态止损参考
- 大模型产业逻辑与核心催化剂深度研判
- 输出专属周报推送企业微信 + 存档供 Web 平台调用
"""

import os
import sys
import json
import time
from datetime import datetime, date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests
import pandas as pd
import yfinance as yf

# 避免 Windows 终端输出 Emoji 时的 GBK 编码报错
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# 保证能正确加载 backend 的共享模块（llm_client、price_levels、indicators、stocks）
BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "stock-platform" / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from app.services.llm_client import chat_completion, is_ai_configured
    from app.utils.price_levels import (
        atr_adaptive_levels,
        linear_buy_point,
        nonlinear_buy_point,
        LINEAR_PROFIT_PCT,
        STOP_PCT,
    )
    from app.utils.indicators import calculate_rsi, calculate_macd, calculate_bollinger_bands, calculate_atr
    from app.routers.stocks import pick_next_earnings_date
except ImportError:
    chat_completion = None
    def is_ai_configured(): return False
    LINEAR_PROFIT_PCT = 1.15
    STOP_PCT = 0.92
    def linear_buy_point(current: float, ma20: float) -> float:
        buy = current - 0.5 * (current - ma20) if ma20 < current else current * 0.95
        return min(buy, current * 0.95)
    def nonlinear_buy_point(rsi: float, ma20: float, bb_lower: float) -> float:
        return bb_lower if rsi < 30.0 else ma20
    def atr_adaptive_levels(current: float, buy: float, profit_pct: float, atr=None, **kw) -> dict:
        return {'buy': round(buy, 2), 'stop': round(buy * STOP_PCT, 2), 'profit': round(buy * profit_pct, 2),
                'atr_stop': round(buy * STOP_PCT, 2), 'breakeven_trigger': round(buy * 1.10, 2)}
    def calculate_atr(high, low, close, period=14):
        return (high - low).rolling(period).mean()
    def pick_next_earnings_date(candidates, today: date):
        normalized = []
        for d in candidates or []:
            try:
                normalized.append(d.date() if callable(getattr(d, 'date', None)) else d)
            except Exception:
                continue
        future = [d for d in normalized if isinstance(d, date) and d >= today]
        return min(future) if future else None

# ----------------------------------------------------
# 1. 美股硬科技与算力龙头监控池 (~35-40 支优质龙头)
# ----------------------------------------------------
HARD_TECH_UNIVERSE = [
    # --- AI 芯片与半导体核心龙头 ---
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

    # --- 半导体核心设备与材料 ---
    {"code": "ASML", "name": "阿斯麦", "sector": "EUV光刻机绝对垄断"},
    {"code": "AMAT", "name": "应用材料", "sector": "半导体设备百宝箱"},
    {"code": "LRCX", "name": "泛林半导体", "sector": "先进制程刻蚀与薄膜"},
    {"code": "KLAC", "name": "科磊半导体", "sector": "工艺控制与缺陷检测"},

    # --- 光通信与高速光互联 ---
    {"code": "LITE", "name": "Lumentum", "sector": "光器件与激光器龙头"},
    {"code": "COHR", "name": "Coherent", "sector": "800G光收发与光学材料"},
    {"code": "AAOI", "name": "祥茂光电", "sector": "高弹性数据中心光模块"},
    {"code": "AXTI", "name": "AXT", "sector": "磷化铟光通信衬底材料"},
    {"code": "CIEN", "name": "锡安通讯", "sector": "相干光网络系统龙头"},
    {"code": "FN", "name": "Fabrinet", "sector": "高端光互联代工之王"},

    # --- AI 算力基础设施与服务器网络 ---
    {"code": "VRT", "name": "维谛技术", "sector": "数据中心液冷与电力"},
    {"code": "SMCI", "name": "超微电脑", "sector": "高密AI服务器整机柜"},
    {"code": "ANET", "name": "阿里斯塔", "sector": "800G AI数据中心网络"},
    {"code": "DELL", "name": "戴尔科技", "sector": "企业级AI服务器"},
    {"code": "HPE", "name": "慧与科技", "sector": "高性能超算与企业云"},
    {"code": "PLTR", "name": "Palantir", "sector": "企业级AI决策操作系统"},

    # --- 科技巨头云厂商 (算力采购主力) ---
    {"code": "MSFT", "name": "微软", "sector": "Azure云与商业化AI"},
    {"code": "GOOGL", "name": "谷歌", "sector": "自研TPU与Gemini生态"},
    {"code": "AMZN", "name": "亚马逊", "sector": "AWS云与自研Trainium"},
    {"code": "META", "name": "Meta", "sector": "开源大模型与算力集群"},
    {"code": "AAPL", "name": "苹果", "sector": "Apple Intelligence终端"},
    {"code": "TSLA", "name": "特斯拉", "sector": "FSD端到端自动驾驶算力"}
]


def load_webhook() -> str:
    """加载企业微信 Webhook"""
    webhook = os.environ.get('WECHAT_WEBHOOK', '')
    if not webhook:
        config_path = BASE_DIR / 'config' / 'config.json'
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                    webhook = cfg.get('wechat_webhook', '')
            except Exception:
                pass
    return webhook


def send_wechat_message(webhook: str, content: str) -> bool:
    """企业微信推送（带智能分包）"""
    if not webhook:
        print("[WeChat] 未配置 WECHAT_WEBHOOK，跳过推送")
        return False

    MAX_BYTES = 3800
    encoded = content.encode('utf-8')
    if len(encoded) <= MAX_BYTES:
        return _send_single_payload(webhook, content)

    # 智能按卡片分包
    cards = content.split('\n### ')
    chunks = []
    current = cards[0]
    for card in cards[1:]:
        candidate = current + '\n### ' + card
        if len(candidate.encode('utf-8')) > MAX_BYTES:
            chunks.append(current)
            current = '### ' + card
        else:
            current = candidate
    if current:
        chunks.append(current)

    success = True
    total = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        banner = f"> 📄 **【周度选股内参 分包 ({idx}/{total})】**\n\n" if total > 1 else ""
        ok = _send_single_payload(webhook, banner + chunk)
        success = success and ok
        if idx < total:
            time.sleep(1.5)
    return success


def _send_single_payload(webhook: str, text: str) -> bool:
    payload = {"msgtype": "markdown", "markdown": {"content": text}}
    for attempt in range(3):
        try:
            resp = requests.post(webhook, json=payload, timeout=10)
            if resp.status_code == 200:
                res = resp.json()
                if res.get('errcode') == 0:
                    return True
            time.sleep(1.0)
        except Exception:
            time.sleep(1.0)
    return False


def simulate_pullback_winrate(hist: pd.DataFrame, ma20_series: pd.Series) -> float:
    """
    真实步进式（Walk-Forward）回踩挂单胜率验证（无未来函数）：
    - 空仓时根据前一日收盘价与MA20计算今日挂单买入价（linear_buy_point）
    - 当日 Low <= 挂单价则成交（跳空低开按 Open 成交）
    - 持仓期间严格按日内 High/Low 判定：先判 -8% 止损（风险优先），再判 +15% 止盈，超过 20 个交易日按收盘价平仓
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

    opens = hist['Open'].values
    highs = hist['High'].values
    lows = hist['Low'].values
    closes = hist['Close'].values
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


# ----------------------------------------------------
# 2. 单股票指标计算与复合因子打分（量价 + 回测 + 基本面估值/目标价）
# ----------------------------------------------------
def analyze_candidate(stock_meta: dict) -> dict:
    """拉取单只候选标的历史数据与基本面快照，计算动量、超跌、回测、估值目标价空间与ATR自适应点位"""
    code = stock_meta["code"]
    name = stock_meta["name"]
    sector = stock_meta["sector"]

    try:
        t = yf.Ticker(code)
        hist = t.history(period="6mo")
        if hist is None or hist.empty:
            return None
        hist = hist.dropna(subset=['Close'])
        if len(hist) < 35:
            return None

        close = hist['Close']
        current_price = float(close.iloc[-1])

        # 1. RSI (14) - 含除零保护
        rsi_series = calculate_rsi(close, 14)
        rsi = float(rsi_series.iloc[-1]) if pd.notna(rsi_series.iloc[-1]) else 50.0

        # 2. 均线、布林带与 ATR(14)
        ma20_series = close.rolling(20).mean()
        ma50_series = close.rolling(50).mean()
        ma20 = float(ma20_series.iloc[-1])
        ma50 = float(ma50_series.iloc[-1]) if pd.notna(ma50_series.iloc[-1]) else ma20
        _, _, bb_lower_series = calculate_bollinger_bands(close, 20, 2)
        bb_lower = float(bb_lower_series.iloc[-1])
        atr_series = calculate_atr(hist['High'], hist['Low'], close, 14)
        atr_val = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else None

        # 3. MACD
        macd_series, signal_series, _ = calculate_macd(close, 12, 26, 9)
        macd_val = float(macd_series.iloc[-1])
        signal_val = float(signal_series.iloc[-1])

        # 4. 近20日动量涨跌幅与量比
        m20_return = float((current_price - close.iloc[-20]) / close.iloc[-20] * 100.0)
        vol_ma20 = float(hist['Volume'].rolling(20).mean().iloc[-1]) if 'Volume' in hist.columns else 0.0
        vol_ratio = float(hist['Volume'].iloc[-1] / vol_ma20) if vol_ma20 > 0 else 1.0

        # 5. 基本面估值与华尔街一致目标价空间（轻量读取 t.info，异常静默降级）
        forward_pe = None
        rev_growth_pct = None
        target_price = None
        upside_pct = None
        try:
            info = getattr(t, 'info', None) or {}
            if isinstance(info, dict) and info:
                fpe = info.get('forwardPE') or info.get('trailingPE')
                if fpe and isinstance(fpe, (int, float)) and fpe > 0:
                    forward_pe = round(float(fpe), 1)
                rg = info.get('revenueGrowth')
                if rg is not None and isinstance(rg, (int, float)):
                    rev_growth_pct = round(float(rg) * 100.0, 1)
                tp = info.get('targetMeanPrice')
                if tp and isinstance(tp, (int, float)) and tp > 0 and current_price > 0:
                    target_price = round(float(tp), 2)
                    upside_pct = round((target_price - current_price) / current_price * 100.0, 1)
        except Exception:
            pass

        # 6. 财报日历排雷
        earnings_info = {'date': None, 'days': None, 'tag': '', 'is_imminent': False}
        try:
            cal = t.calendar
            candidates = None
            if isinstance(cal, dict):
                candidates = cal.get('Earnings Date')
            elif cal is not None and hasattr(cal, 'get'):
                candidates = cal.get('Earnings Date')
            today = date.today()
            ed_date = pick_next_earnings_date(candidates, today)
            if ed_date:
                days = (ed_date - today).days
                earnings_info['date'] = ed_date.strftime('%Y-%m-%d')
                earnings_info['days'] = days
                if days == 0:
                    earnings_info['tag'] = "🚨【今晚发布财报】"
                    earnings_info['is_imminent'] = True
                elif 0 < days <= 3:
                    earnings_info['tag'] = f"⚠️【{days}天后财报】"
                    earnings_info['is_imminent'] = True
                elif 3 < days <= 14:
                    earnings_info['tag'] = f"📅【{days}天后财报】"
        except Exception:
            pass

        # ----------------------------------------------------
        # 复合因子打分（满分100分：动量35 + 超跌35 + 回测与基本面30）
        # ----------------------------------------------------
        # 因子 A: 动量突破与量价因子 (0-35 分，严格封顶 35 分)
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
            momentum_score += 2.0  # 温和放量突破加成
        momentum_score = max(0.0, min(35.0, momentum_score))

        # 因子 B: 超跌击球点因子 (0-35 分，空头排列下禁用极端超卖抄底加分)
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

        # 基本面营收高增与华尔街目标价空间加成
        if upside_pct is not None and upside_pct >= 15.0:
            backtest_score += 3.0
        elif upside_pct is not None and upside_pct < -10.0:
            backtest_score -= 3.0
        if rev_growth_pct is not None and rev_growth_pct >= 20.0:
            backtest_score += 2.0

        # 财报排雷扣分
        if earnings_info['is_imminent']:
            backtest_score -= 15.0
        backtest_score = max(0.0, min(30.0, backtest_score))

        total_score = max(0.0, min(100.0, momentum_score + sweetspot_score + backtest_score))

        # 计算推荐伏击点位与 ATR 动态止损位
        linear_buy = linear_buy_point(current_price, ma20)
        nonlinear_buy = nonlinear_buy_point(rsi, ma20, bb_lower)
        lin_levels = atr_adaptive_levels(current_price, linear_buy, LINEAR_PROFIT_PCT, atr=atr_val)

        return {
            'code': code,
            'name': name,
            'sector': sector,
            'current_price': round(current_price, 2),
            'rsi': round(rsi, 1),
            'm20_return': round(m20_return, 1),
            'win_rate': round(win_rate, 1),
            'momentum_score': round(momentum_score, 1),
            'sweetspot_score': round(sweetspot_score, 1),
            'backtest_score': round(backtest_score, 1),
            'total_score': round(total_score, 1),
            'forward_pe': forward_pe,
            'rev_growth_pct': rev_growth_pct,
            'target_price': target_price,
            'upside_pct': upside_pct,
            'earnings': earnings_info,
            'linear_buy': round(linear_buy, 2),
            'nonlinear_buy': round(nonlinear_buy, 2),
            'linear_profit': round(linear_buy * LINEAR_PROFIT_PCT, 2),
            'linear_stop': round(linear_buy * STOP_PCT, 2),
            'atr_stop': lin_levels.get('atr_stop', round(linear_buy * STOP_PCT, 2)),
            'breakeven_trigger': lin_levels.get('breakeven_trigger', round(linear_buy * 1.10, 2)),
        }
    except Exception as e:
        print(f"  [Skip] 计算 {code} 失败: {e}")
        return None


# ----------------------------------------------------
# 3. 执行全池并发扫描
# ----------------------------------------------------
def run_screener():
    """扫描全池硬科技股票并挑选 TOP 5"""
    print("=" * 60)
    print(f"🚀 美股硬科技周度选股引擎启动 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"监控标的池数量: {len(HARD_TECH_UNIVERSE)} 支科技与算力龙头")
    print("=" * 60)

    t0 = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(analyze_candidate, item): item["code"] for item in HARD_TECH_UNIVERSE}
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                results.append(res)

    print(f"✅ 全池数据拉取与因子计算完成，耗时: {time.time() - t0:.2f} 秒，有效标的: {len(results)} 支")

    eligible = [r for r in results if not r['earnings']['is_imminent']]
    if len(eligible) < 5:
        eligible = results

    sorted_results = sorted(eligible, key=lambda x: x['total_score'], reverse=True)
    top5 = sorted_results[:5]

    print("\n🏆【周度 Alpha TOP 5 猛禽池名单出炉】:")
    for rank, item in enumerate(top5, 1):
        print(f"  TOP {rank}: {item['code']} {item['name']} | 赛道: {item['sector']} | 量化总分: {item['total_score']}分 (现价=${item['current_price']})")

    battle_report = review_previous_picks()
    ai_thesis = generate_ai_thesis(top5)

    payload = {
        'scan_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_scanned': len(results),
        'top5': top5,
        'ai_thesis': ai_thesis,
        'battle_report': battle_report,
    }

    data_dir = BASE_DIR / 'data'
    data_dir.mkdir(exist_ok=True)
    output_json = data_dir / 'weekly_alpha_top5.json'
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n📁 选股结果已保存至: {output_json}")

    archive_to_history(payload, data_dir)

    wechat_report = format_wechat_weekly_report(top5, ai_thesis, battle_report)
    report_dir = BASE_DIR / 'reports'
    report_dir.mkdir(exist_ok=True)
    report_file = report_dir / f"weekly_alpha_top5_{datetime.now().strftime('%Y%m%d')}.txt"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(wechat_report)

    webhook = load_webhook()
    print("📱 正在向企业微信推送周度选股内参...")
    ok = send_wechat_message(webhook, wechat_report)
    if ok:
        print("✅ 企业微信周报推送成功！")
    else:
        print("⚠️ 企业微信推送未触发或失败")

    return payload


# ----------------------------------------------------
# 4. 上期猛禽池战绩回测与归因
# ----------------------------------------------------
def review_previous_picks() -> dict | None:
    """加载上一期 TOP 5，并发拉取最新价格，计算周度收益归因"""
    data_dir = BASE_DIR / 'data'
    prev_file = data_dir / 'weekly_alpha_top5.json'
    if not prev_file.exists():
        print("📊 暂无上期猛禽池记录，跳过战绩回测")
        return None

    try:
        with open(prev_file, 'r', encoding='utf-8') as f:
            prev = json.load(f)
    except Exception:
        return None

    prev_top5 = prev.get('top5', [])
    prev_date = prev.get('scan_date', '未知')
    if not prev_top5:
        return None

    print(f"\n📊 正在回测上期猛禽池战绩（入选日期: {prev_date}）...")

    def _review_one(item):
        code = item['code']
        entry_price = item['current_price']
        now_price = entry_price
        try:
            t = yf.Ticker(code)
            h = t.history(period='5d')
            if h is not None and not h.empty:
                h = h.dropna(subset=['Close'])
            if h is not None and not h.empty:
                now_price = float(h['Close'].iloc[-1])
        except Exception:
            now_price = entry_price

        weekly_ret = round((now_price - entry_price) / entry_price * 100, 2) if entry_price else 0.0
        return {
            'code': code,
            'name': item['name'],
            'sector': item.get('sector', ''),
            'entry_price': entry_price,
            'review_price': round(now_price, 2),
            'weekly_return_pct': weekly_ret,
            'is_winner': weekly_ret > 0,
        }

    with ThreadPoolExecutor(max_workers=min(5, len(prev_top5))) as executor:
        picks_review = list(executor.map(_review_one, prev_top5))

    winners = [p for p in picks_review if p['is_winner']]
    losers = [p for p in picks_review if not p['is_winner']]
    avg_ret = round(sum(p['weekly_return_pct'] for p in picks_review) / len(picks_review), 2)
    best = max(picks_review, key=lambda x: x['weekly_return_pct'])
    worst = min(picks_review, key=lambda x: x['weekly_return_pct'])

    report = {
        'prev_scan_date': prev_date,
        'review_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'picks': picks_review,
        'win_count': len(winners),
        'loss_count': len(losers),
        'win_rate': round(len(winners) / len(picks_review) * 100, 1),
        'avg_return_pct': avg_ret,
        'best_pick': {'code': best['code'], 'name': best['name'], 'return_pct': best['weekly_return_pct']},
        'worst_pick': {'code': worst['code'], 'name': worst['name'], 'return_pct': worst['weekly_return_pct']},
    }

    print(f"  上期战绩: 胜率 {report['win_rate']}% ({report['win_count']}胜{report['loss_count']}负) | 等权组合收益 {avg_ret:+.2f}%")
    return report


def archive_to_history(payload: dict, data_dir: Path):
    """将本期选股结果与战报归档到累积历史文件"""
    history_file = data_dir / 'weekly_alpha_history.json'
    history = []
    if history_file.exists():
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
        except Exception:
            history = []

    entry = {
        'scan_date': payload['scan_date'],
        'top5': [
            {'code': s['code'], 'name': s['name'], 'sector': s['sector'],
             'total_score': s['total_score'], 'current_price': s['current_price']}
            for s in payload.get('top5', [])
        ],
        'battle_report': payload.get('battle_report'),
    }
    history.append(entry)

    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"📁 历史战报已归档至: {history_file} (累计 {len(history)} 期)")


def generate_ai_thesis(top5: list) -> str:
    """调用大模型为 TOP 5 标的生成产业逻辑与催化研判"""
    if not (is_ai_configured and is_ai_configured() and chat_completion):
        return "大模型暂未配置，请参考量化打分与技术点位。"

    candidates_text = "\n".join([
        f"- TOP {i}: {s['code']} {s['name']} ({s['sector']}) | 现价 ${s['current_price']}, 量化总分 {s['total_score']}分, "
        f"动量分 {s['momentum_score']}, 超跌分 {s['sweetspot_score']}, 近6月胜率 {s['win_rate']}%, RSI={s['rsi']}, 20日动量 {s['m20_return']:+.1f}%"
        + (f", 远期PE={s['forward_pe']}" if s.get('forward_pe') else "")
        + (f", 目标价空间={s['upside_pct']:+.1f}%" if s.get('upside_pct') is not None else "")
        for i, s in enumerate(top5, 1)
    ])

    prompt = f"""
你是一位专精于美股半导体、AI算力与硬科技产业链的顶级量化对冲基金投研总监。
我们的硬科技周度量化多因子扫描引擎刚刚从全美 35 支顶级科技龙头中，筛选出了本周量化评分最高、形态与盈亏比兼备的【Alpha TOP 5 猛禽池】：

【入选标的量化与估值数据】
{candidates_text}

请为投资者撰写一份高水准的《美股硬科技周度选股内参 · TOP 5 深度逻辑研判》：
1. **宏观与产业链主线前瞻**：用 2 句话点出本周资金在半导体、光互联、AI服务器之间的轮动主线；
2. **TOP 5 核心标的逻辑剖析**：对排名前列的标的逐一点评（结合其商业壁垒、估值/目标价空间、技术突破买点或超跌反弹弹性）；
3. **下周实操伏击指引**：给出挂单策略建议与 ATR 动态止损/浮盈保本防守铁律。
语言风格专业犀利、字字珠玑，排版使用优雅的 Markdown，字数控制在 400-500 字以内。不要输出寒暄客套。
"""
    try:
        res = chat_completion([{"role": "user", "content": prompt}], max_tokens=3000)
        return res.strip()
    except Exception as e:
        print(f"[LLM] 生成周度选股研报失败: {e}")
        return "AI研报生成中遇到临时延迟，各标的量化点位已就绪。"


def format_wechat_weekly_report(top5: list, ai_thesis: str, battle_report: dict = None) -> str:
    """格式化企业微信周度选股消息"""
    now_str = datetime.now().strftime('%m-%d %H:%M')
    lines = [
        f"**【美股硬科技周度选股 · Alpha TOP 5 猛禽池】** {now_str}",
        "> 🦅 **选股定位**: 全市场精选高动量、高胜率、高胜算科技增量标的",
        "> 📌 **池位关系**: 独立于核心10支自选股，提供增量Alpha进攻机会\n",
    ]

    if battle_report:
        br = battle_report
        lines.append("### 📊 上期猛禽池战绩复盘\n")
        lines.append(f"> 入选日期: {br['prev_scan_date']} → 复盘日期: {br['review_date']}")
        lines.append(f"> **等权组合收益: `{br['avg_return_pct']:+.2f}%`** | 胜率: `{br['win_rate']}%` ({br['win_count']}胜{br['loss_count']}负)")
        lines.append(f"> 🏆 最强: **{br['best_pick']['code']}** (`{br['best_pick']['return_pct']:+.2f}%`) | 最弱: {br['worst_pick']['code']} (`{br['worst_pick']['return_pct']:+.2f}%`)\n")
        for p in br.get('picks', []):
            icon = "🟢" if p['is_winner'] else "🔴"
            lines.append(f"- {icon} **{p['code']}** {p['name']} | ${p['entry_price']}→${p['review_price']} (`{p['weekly_return_pct']:+.2f}%`)")
        lines.append("\n---")

    lines.append("### 🧠 AI 投研总监深度逻辑")
    lines.append(ai_thesis)
    lines.append("\n---")
    lines.append("### 🏆 周度 Alpha TOP 5 量化伏击点位表\n")

    for i, s in enumerate(top5, 1):
        val_parts = []
        if s.get('forward_pe'):
            val_parts.append(f"PE `{s['forward_pe']}`")
        if s.get('upside_pct') is not None:
            val_parts.append(f"目标价空间 `{s['upside_pct']:+.1f}%`")
        val_str = (" | " + " | ".join(val_parts)) if val_parts else ""

        lines.append(f"### {i}. {s['code']} {s['name']} | `{s['total_score']}分`")
        lines.append(f"- 🏷️ **赛道标签**: {s['sector']}{val_str}")
        lines.append(f"- 📊 **量化画像**: 现价 **${s['current_price']}** | RSI `{s['rsi']}` | 动量 `{s['m20_return']:+.1f}%` | 模拟胜率 `{s['win_rate']}%`")
        lines.append(f"- 💡 **线性伏击**: 买入 **${s['linear_buy']}** | 止盈 ${s['linear_profit']} (+15%) | 止损 ${s['linear_stop']} (-8% / ATR自适应 ${s.get('atr_stop', s['linear_stop'])})")
        lines.append(f"- 🛡️ **非线性伏击**: 买入 **${s['nonlinear_buy']}** | 保本上移线 ${s.get('breakeven_trigger', round(s['linear_buy']*1.1, 2))} (+10%)")
        lines.append("")

    lines.append("---")
    lines.append("**💡 纪律指引**：")
    lines.append("• 本池标的每周六动态重估，适合作为加仓 Alpha 进攻备选")
    lines.append("• 浮盈达 +10% 后自动将止损上移至成本价+1%锁定胜局，坚决执行止损")
    return "\n".join(lines)


if __name__ == '__main__':
    run_screener()
