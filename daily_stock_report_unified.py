"""
每日股票分析报告 - 统一双策略与AI晨报升级版
1. 真实四维评分（技术面[复用后端空头排列防接刀评分] + NewsAPI/LLM语义新闻 + FRED宏观 + 事件驱动）
2. 30秒AI晨报精要
3. 企业微信分段双消息推送（消息①: AI晨报投研；消息②: 量化双策略点位表）
4. 平滑降级与高可用容错
"""

import json
import os
import re
import sys
import time
from datetime import datetime, date
from pathlib import Path
import requests
from concurrent.futures import ThreadPoolExecutor
import yfinance as yf
import pandas as pd
import numpy as np

# 确保 Windows 终端 UTF-8 编码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# 引入 stock-platform 后端服务的共享模块（消除双轨实现漂移）
BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / 'stock-platform' / 'backend'
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from app.services.llm_client import chat_completion, is_ai_configured
    from app.services.tech_score import calculate_tech_score
    from app.utils.indicators import calculate_all_indicators
    from app.utils.price_levels import (
        LINEAR_PROFIT_PCT,
        NONLINEAR_PROFIT_PCT,
        atr_adaptive_levels,
        levels_with_targets,
        linear_buy_point,
        nonlinear_buy_point,
    )
    from app.routers.stocks import pick_next_earnings_date
except Exception:
    chat_completion = None
    def is_ai_configured():
        return False
    calculate_tech_score = None
    calculate_all_indicators = None
    LINEAR_PROFIT_PCT = 1.15
    NONLINEAR_PROFIT_PCT = 1.46
    def linear_buy_point(current: float, ma20: float) -> float:
        buy = current - 0.5 * (current - ma20) if ma20 < current else current * 0.95
        return min(buy, current * 0.95)
    def nonlinear_buy_point(rsi: float, ma20: float, bb_lower: float) -> float:
        return bb_lower if rsi < 30.0 else ma20
    def levels_with_targets(current: float, buy: float, profit_pct: float) -> dict:
        return {
            'buy': round(buy, 2),
            'stop': round(buy * 0.92, 2),
            'profit': round(buy * profit_pct, 2),
            'distance': round((current - buy) / current * 100, 2) if current else 0.0,
        }
    def pick_next_earnings_date(candidates, today: date):
        normalized = []
        for d in candidates or []:
            try:
                normalized.append(d.date() if callable(getattr(d, 'date', None)) else d)
            except Exception:
                continue
        future = [d for d in normalized if isinstance(d, date) and d >= today]
        return min(future) if future else None


def load_app_config() -> dict:
    """加载配置：优先读取系统环境变量，回退读取 config/config.json"""
    config = {}
    config_path = BASE_DIR / "config" / "config.json"
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            print(f"[Config] 读取本地 config.json 异常: {e}")

    # 环境变量覆盖
    api_keys = config.get("api_keys", {})
    if os.environ.get("NEWSAPI_KEY"):
        api_keys["newsapi"] = os.environ.get("NEWSAPI_KEY")
    if os.environ.get("FRED_API_KEY"):
        api_keys["fred"] = os.environ.get("FRED_API_KEY")
    if os.environ.get("WECHAT_WEBHOOK"):
        config["wechat_webhook"] = os.environ.get("WECHAT_WEBHOOK")

    # 针对 Gemini / OpenRouter 密钥支持环境变量传入
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("AI_API_KEY")
    if gemini_key:
        ai_p = api_keys.get("ai_provider", {})
        ai_p["api_key"] = gemini_key
        api_keys["ai_provider"] = ai_p

    config["api_keys"] = api_keys
    return config


def _send_single_wechat_payload(content: str, webhook_url: str, msg_type: str = "markdown") -> bool:
    """底层单包推送方法"""
    payload = {
        "msgtype": msg_type,
        msg_type: {
            "content": content
        }
    }
    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        if response.status_code == 200:
            res_data = response.json()
            if res_data.get("errcode") == 0:
                return True
            else:
                if msg_type == "markdown":
                    return _send_single_wechat_payload(content, webhook_url, msg_type="text")
                print(f"[WeChat] 推送错误: {res_data}")
                return False
        return False
    except Exception as e:
        print(f"[WeChat] 请求异常: {e}")
        return False


def send_wechat_message(content: str, webhook_url: str, msg_type: str = "markdown") -> bool:
    """智能分段推送：遇到长内容按标的卡片自动无损分包，绝不截断丢弃任何股票数据"""
    if not webhook_url:
        print("[WeChat] 未配置企业微信 Webhook URL，跳过推送")
        return False

    max_bytes = 3800 if msg_type == "markdown" else 2000
    content_bytes = content.encode('utf-8')

    # 未超限直接单包推送
    if len(content_bytes) <= max_bytes:
        success = _send_single_wechat_payload(content, webhook_url, msg_type)
        if success:
            print(f"[WeChat] {msg_type} 消息推送成功 (单包 {len(content_bytes)} 字节)")
        return success

    # 超限时按标的卡片分割 ('\n### ') 无损分包
    print(f"[WeChat] 消息长度 {len(content_bytes)} 字节超过单包上限 ({max_bytes})，启动智能无损分包...")
    cards = content.split("\n### ")
    header = cards[0]
    stock_cards = ["### " + c for c in cards[1:]] if len(cards) > 1 else [cards[0]]

    chunks = []
    curr_chunk = header

    for card in stock_cards:
        trial = curr_chunk + "\n\n" + card if curr_chunk else card
        if len(trial.encode('utf-8')) > max_bytes:
            chunks.append(curr_chunk)
            curr_chunk = card
        else:
            curr_chunk = trial

    if curr_chunk:
        chunks.append(curr_chunk)

    total_chunks = len(chunks)
    all_success = True
    for idx, chunk in enumerate(chunks, 1):
        page_prefix = f"*(第 {idx}/{total_chunks} 节)*\n\n" if total_chunks > 1 else ""
        success = _send_single_wechat_payload(page_prefix + chunk, webhook_url, msg_type)
        all_success = all_success and success
        print(f"[WeChat] 分包 ({idx}/{total_chunks}) 推送{'成功' if success else '失败'}")
        if idx < total_chunks:
            time.sleep(1.5)

    return all_success


def fetch_macro_data(fred_key: str):
    """通过 FRED API 动态获取宏观流动性指标（利率/CPI等）并评估宏观分"""
    indicators = {}
    macro_score = 5.0
    summary = "宏观基准中性"
    degraded = False

    if not fred_key:
        return macro_score, summary, True

    try:
        url = "https://api.stlouisfed.org/fred/series/observations"
        # 获取联邦基金有效利率 (DFF)
        resp = requests.get(url, params={
            'series_id': 'DFF',
            'api_key': fred_key,
            'file_type': 'json',
            'sort_order': 'desc',
            'limit': 1
        }, timeout=8)

        if resp.status_code == 200:
            obs = resp.json().get('observations', [])
            if obs and obs[0].get('value') not in (None, '.', ''):
                indicators['fed_rate'] = float(obs[0]['value'])

        fed_rate = indicators.get('fed_rate')
        if fed_rate is not None:
            if fed_rate <= 3.75:
                macro_score = 6.8
                summary = f"联邦基准利率降至 {fed_rate}%，流动性宽裕，利好半导体与科技股估值"
            elif fed_rate <= 4.75:
                macro_score = 5.8
                summary = f"联邦基准利率处于 {fed_rate}% 降息观察期，市场流动性平稳"
            else:
                macro_score = 4.5
                summary = f"联邦基准利率维持在 {fed_rate}% 高位区间，估值扩张承压"
        else:
            degraded = True
    except Exception as e:
        print(f"[Macro] FRED API 获取异常: {e}")
        degraded = True

    return macro_score, summary, degraded


def fetch_market_regime_and_circuit_breaker() -> dict:
    """
    监控大盘黑天鹅风险与波动率熔断状态：
    1. ^VIX (恐慌指数):
       - < 20: 🟢 常态温和 (Normal，做多环境良好)
       - 20 ~ 28: 🟡 波动防守预警 (Caution: 买点下移2%，仓位减半防守)
       - >= 28: 🔴 极端恐慌/黑天鹅熔断 (Circuit Breaker: 全面冻结买入，禁止接飞刀)
    2. QQQ (纳斯达克100 ETF 单日涨跌幅):
       - 单日跌幅 <= -2.5%: 强制触发熔断避险
    """
    regime = {
        'status': 'NORMAL',          # 'NORMAL', 'CAUTION', 'CIRCUIT_BREAKER'
        'level': 0,                  # 0: 常态, 1: 预警, 2: 熔断
        'vix': 15.0,
        'qqq_change': 0.0,
        'banner': '🟢 市场环境健康 (VIX 15.0 | 纳指平稳)',
        'advice': '量化策略正常运行，按计划挂单'
    }
    try:
        # 获取 VIX
        vix_ticker = yf.Ticker("^VIX")
        vix_hist = vix_ticker.history(period="5d")
        if vix_hist is not None and not vix_hist.empty:
            regime['vix'] = float(vix_hist['Close'].iloc[-1])

        # 获取 QQQ
        qqq_ticker = yf.Ticker("QQQ")
        qqq_hist = qqq_ticker.history(period="5d")
        if qqq_hist is not None and len(qqq_hist) >= 2:
            c1 = qqq_hist['Close'].iloc[-1]
            c0 = qqq_hist['Close'].iloc[-2]
            regime['qqq_change'] = float(((c1 - c0) / c0) * 100)

        vix = regime['vix']
        qqq_chg = regime['qqq_change']

        # 熔断判定逻辑
        if vix >= 28.0 or qqq_chg <= -2.5:
            regime['status'] = 'CIRCUIT_BREAKER'
            regime['level'] = 2
            regime['banner'] = f"🚨 市场熔断避险 (恐慌指数 VIX={vix:.1f} | 纳指 {qqq_chg:+.2f}%)"
            regime['advice'] = "市场遭遇系统性抛压，已全面冻结左侧买入，严禁逆势接飞刀！"
        elif vix >= 20.0 or qqq_chg <= -1.5:
            regime['status'] = 'CAUTION'
            regime['level'] = 1
            regime['banner'] = f"🟡 波动防守预警 (恐慌指数 VIX={vix:.1f} | 纳指 {qqq_chg:+.2f}%)"
            regime['advice'] = "市场振幅剧烈，建议买点下移2%深幅埋伏，持仓仓位减半防守"
        else:
            regime['status'] = 'NORMAL'
            regime['level'] = 0
            regime['banner'] = f"🟢 市场环境健康 (恐慌指数 VIX={vix:.1f} | 纳指 {qqq_chg:+.2f}%)"
            regime['advice'] = "宏观与波动率适宜，按量化策略正常伏击建仓"
    except Exception as e:
        print(f"[Regime] 市场波动率获取异常: {e}")

    return regime


def fetch_news_and_evaluate_sentiment(stocks: list, newsapi_key: str):
    """抓取各标的新闻并通过大模型进行语义情感打分（自动读取 config.json 配置模型，无硬编码）"""
    news_dict = {}
    degraded = False

    # 1. 多线程并发抓取各标的新闻标题（融合 yfinance 实时个股新闻 + NewsAPI 双源）
    def _fetch_single_news(stock_tuple):
        stock_code, stock_name = stock_tuple
        titles = []
        seen = set()
        # 优先拉取 yfinance 实时个股新闻（无需 Key、零延迟）
        try:
            raw_news = getattr(yf.Ticker(stock_code), 'news', None) or []
            for item in raw_news[:4]:
                if not isinstance(item, dict):
                    continue
                content = item.get('content') if isinstance(item.get('content'), dict) else item
                t = (content.get('title') or '').strip()
                if t and t.lower() not in seen:
                    seen.add(t.lower())
                    titles.append(t)
        except Exception:
            pass
        # 补充 NewsAPI
        if newsapi_key and len(titles) < 4:
            try:
                resp = requests.get(
                    "https://newsapi.org/v2/everything",
                    params={
                        'q': f'"{stock_name}"',
                        'language': 'en',
                        'sortBy': 'publishedAt',
                        'pageSize': 3,
                        'apiKey': newsapi_key
                    },
                    timeout=6
                )
                if resp.status_code == 200:
                    articles = resp.json().get('articles', [])
                    for a in articles:
                        t = (a.get('title') or '').strip()
                        if t and t.lower() not in seen:
                            seen.add(t.lower())
                            titles.append(t)
            except Exception:
                pass
        return stock_code, titles[:4]

    with ThreadPoolExecutor(max_workers=min(8, len(stocks))) as executor:
        for s_code, s_titles in executor.map(_fetch_single_news, stocks):
            news_dict[s_code] = s_titles
    if not any(news_dict.values()) and not newsapi_key:
        degraded = True

    # 2. 调用大模型进行语义打分与关键事件萃取（不硬编码模型名，使用配置的主模型）
    llm_scores = {}
    if is_ai_configured():
        prompt_content = "你是一位资深美股量化分析师。请评估以下股票近期新闻的情感倾向，并给出评分与关键事件：\n\n"
        for code, name in stocks:
            titles = news_dict.get(code, [])
            titles_str = " | ".join(titles) if titles else "暂无近24小时突发新闻"
            prompt_content += f"- 股票: {code} ({name})\n  新闻标题: {titles_str}\n"

        prompt_content += """
请以合法 JSON 格式输出每只股票的评估结果（不要添加 markdown 代码块标签，仅返回纯 JSON）：
{
  "MU": {"score": 6.5, "event": "DRAM价格平稳回暖，存储需求扩张"},
  "SOXL": {"score": 5.5, "event": "半导体板块震荡上行"}
}
说明：score 为 0.0 到 10.0 的浮点数，5.0 为中性，>5.0 为偏多利好，<5.0 为偏空。event 限15字以内。
"""
        try:
            res_text = chat_completion([{"role": "user", "content": prompt_content}], max_tokens=6000)
            clean_json = re.sub(r'```json\s*|\s*```', '', res_text.strip())
            llm_scores = json.loads(clean_json)
        except Exception as ex:
            print(f"[News-LLM] 大模型情感打分异常: {ex}")
            degraded = True

    # 3. 汇总打分结果
    results_news = {}
    for code, name in stocks:
        stock_eval = llm_scores.get(code)
        if stock_eval and 'score' in stock_eval:
            results_news[code] = {
                'score': float(stock_eval.get('score', 5.0)),
                'event': stock_eval.get('event', '暂无重大突发事件'),
                'titles': news_dict.get(code, [])
            }
        else:
            titles = news_dict.get(code, [])
            results_news[code] = {
                'score': 5.5 if titles else 5.0,
                'event': '行情平稳(规则估分)',
                'titles': titles
            }

    return results_news, degraded


def calculate_stock_technical_and_strategies(stock_code: str, stock_name: str):
    """单次拉取历史数据，复用后端共享指标、技术面评分（含空头排列防接刀）与双策略点位推算"""
    try:
        data = pd.DataFrame()
        stock = None
        try:
            stock = yf.Ticker(stock_code)
            data = stock.history(period="3mo")
        except Exception:
            pass

        if data is None or data.empty:
            # 自动从本地 Excel 兜底加载，保障离线与限流时可用
            candidates = list(BASE_DIR.glob(f"**/{stock_code}*_data.xlsx"))
            if candidates:
                data = pd.read_excel(candidates[0])
                if 'Date' in data.columns:
                    data['Date'] = pd.to_datetime(data['Date'])
                    data = data.set_index('Date')
                print(f"  [Fallback] {stock_code} 启用本地缓存数据 ({len(data)} 条)")
            else:
                return None

        # 剔除盘前空占位行或未完成蜡烛
        data = data.dropna(subset=['Close'])
        if data.empty:
            return None

        # 优先使用后端共享指标与技术面评分（空头排列下禁用超卖加分）
        if calculate_all_indicators is not None and calculate_tech_score is not None:
            data = calculate_all_indicators(data)
            latest = data.iloc[-1]
            tech_score, _ = calculate_tech_score(latest)
            current_price = float(latest['Close'])
            rsi = float(latest['RSI']) if pd.notna(latest.get('RSI')) else 50.0
            ma20 = float(latest['MA20']) if pd.notna(latest.get('MA20')) else current_price * 0.95
            bb_lower = float(latest['BB_Lower']) if pd.notna(latest.get('BB_Lower')) else current_price * 0.90
        else:
            delta = data['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi_s = 100 - (100 / (1 + rs))
            rsi_s = np.where((loss == 0) & (gain > 0), 100.0, np.where((loss == 0) & (gain == 0), 50.0, rsi_s))
            data['RSI'] = pd.Series(rsi_s, index=data.index)

            exp1 = data['Close'].ewm(span=12, adjust=False).mean()
            exp2 = data['Close'].ewm(span=26, adjust=False).mean()
            data['MACD'] = exp1 - exp2
            data['MACD_Signal'] = data['MACD'].ewm(span=9, adjust=False).mean()
            data['MA20'] = data['Close'].rolling(window=20).mean()
            data['MA50'] = data['Close'].rolling(window=50).mean()
            data['BB_Middle'] = data['Close'].rolling(window=20).mean()
            data['BB_Std'] = data['Close'].rolling(window=20).std()
            data['BB_Upper'] = data['BB_Middle'] + 2 * data['BB_Std']
            data['BB_Lower'] = data['BB_Middle'] - 2 * data['BB_Std']

            latest = data.iloc[-1]
            current_price = float(latest['Close'])
            rsi = float(latest['RSI']) if pd.notna(latest['RSI']) else 50.0
            ma20 = float(latest['MA20']) if pd.notna(latest['MA20']) else current_price * 0.95
            ma50 = float(latest['MA50']) if pd.notna(latest['MA50']) else current_price * 0.95
            bb_lower = float(latest['BB_Lower']) if pd.notna(latest['BB_Lower']) else current_price * 0.90
            bb_upper = float(latest['BB_Upper']) if pd.notna(latest['BB_Upper']) else current_price * 1.10
            bearish = ma20 < ma50

            tech_score = 5.0
            if rsi < 30 and not bearish:
                tech_score += 2.0
            elif rsi < 40 and not bearish:
                tech_score += 1.0
            elif rsi > 70:
                tech_score -= 2.0
            elif rsi > 60:
                tech_score -= 1.0
            tech_score += 1.0 if latest['MACD'] > latest['MACD_Signal'] else -1.0
            tech_score += 0.5 if current_price > ma20 else (-0.5 if current_price < ma20 else 0)
            tech_score += 0.5 if current_price > ma50 else (-0.5 if current_price < ma50 else 0)
            tech_score += 0.5 if ma20 > ma50 else (-0.5 if ma20 < ma50 else 0)
            if current_price < bb_lower and not bearish:
                tech_score += 1.0
            elif current_price > bb_upper:
                tech_score -= 1.0
            tech_score = max(0.0, min(10.0, tech_score))

        # 2. 线性与非线性策略点位（走 price_levels 统一纯函数）
        linear_buy = linear_buy_point(current_price, ma20)
        nonlinear_buy = nonlinear_buy_point(rsi, ma20, bb_lower)
        linear_levels = levels_with_targets(current_price, linear_buy, LINEAR_PROFIT_PCT)
        nonlinear_levels = levels_with_targets(current_price, nonlinear_buy, NONLINEAR_PROFIT_PCT)

        # 3. 核心标的财报日历与排雷雷达
        earnings_info = {
            'date': None,
            'days': None,
            'tag': '',
            'is_imminent': False
        }
        try:
            cal = stock.calendar if stock is not None else None
            candidates = None
            if isinstance(cal, dict):
                candidates = cal.get('Earnings Date')
            elif cal is not None and hasattr(cal, 'get'):
                candidates = cal.get('Earnings Date')
            today = datetime.now().date()
            ed_date = pick_next_earnings_date(candidates, today)
            if ed_date:
                days = (ed_date - today).days
                earnings_info['date'] = ed_date.strftime('%Y-%m-%d')
                earnings_info['days'] = days
                if days == 0:
                    earnings_info['tag'] = "🚨【今晚盘后发布财报】期权IV高企，严禁重仓博弈开盲盒！"
                    earnings_info['is_imminent'] = True
                elif 0 < days <= 3:
                    earnings_info['tag'] = f"⚠️【{days}天后发布财报 ({earnings_info['date']})】防范业绩暴击风险"
                    earnings_info['is_imminent'] = True
                elif 3 < days <= 14:
                    earnings_info['tag'] = f"📅【财报倒计时 {days}天】({earnings_info['date']})"
        except Exception:
            pass

        return {
            'stock_code': stock_code,
            'stock_name': stock_name,
            'current_price': current_price,
            'rsi': rsi,
            'tech_score': tech_score,
            'earnings': earnings_info,
            'linear': {
                'buy': linear_levels['buy'],
                'stop': linear_levels['stop'],
                'profit': linear_levels['profit'],
            },
            'nonlinear': {
                'buy': nonlinear_levels['buy'],
                'stop': nonlinear_levels['stop'],
                'profit': nonlinear_levels['profit'],
            },
            'linear_distance': linear_levels['distance'],
            'nonlinear_distance': nonlinear_levels['distance'],
        }
    except Exception as e:
        print(f"[Tech] 计算 {stock_code} 失败: {e}")
        return None


# 标的历史 2 年量化实测默认画像（若 data/stock_profiles.json 存在则动态覆盖）
DEFAULT_STOCK_HISTORICAL_PROFILES = {
    'MU': {
        'badge': '🛡️ 核心存储',
        'stat': '非线性盈亏比 4.7:1 | 均单收益 +6.5% | 累计 +95.1%',
        'summary': '形态健康，适合底仓+右侧突破'
    },
    'SOXL': {
        'badge': '⚡ 3倍杠杆ETF',
        'stat': '高弹性大波动 | 历史回撤 59.7% | 累计 +30.0%',
        'summary': '仅限短线波段，严禁死扛，严守-8%止损'
    },
    'COHR': {
        'badge': '🏆 算力光互联龙头',
        'stat': '线性胜率 54.3% | 盈亏比 1.89:1 | 累计 +552.2%',
        'summary': 'AI硬件核心受益，中长线胜率极高'
    },
    'LITE': {
        'badge': '🏆 光器件冠军标的',
        'stat': '线性胜率 55.6% | 盈亏比 1.85:1 | 累计 +862.1%',
        'summary': '回测表现全场第一，均线低吸成功率极高'
    },
    'AXTI': {
        'badge': '💎 光通信上游材料',
        'stat': '线性胜率 50.0% | 盈亏比 1.80:1 | 累计 +442.0%',
        'summary': '高Beta弹性品种，回撤适中(+442%)'
    },
    'AAOI': {
        'badge': '🌊 高弹性光模块',
        'stat': '线性盈亏比 2.07:1 | 累计 +163.7% | 回撤 50.7%',
        'summary': '高赔率博弈标的，振幅大'
    },
    'SNDK': {
        'badge': '📈 存储二线蓝筹',
        'stat': '线性胜率 54.5% | 盈亏比 1.80:1 | 累计 +245.4%',
        'summary': '稳健反弹品种，中短线表现优异'
    },
    'NVDA': {
        'badge': '👑 AI算力总龙头',
        'stat': '非线性盈亏比 2.44:1 | 主升浪动量型 | 胜率 41.7%',
        'summary': '全球AI总龙头，主升浪强劲，深调即黄金坑'
    },
    'AVGO': {
        'badge': '🌟 ASIC算力王者',
        'stat': '线性胜率 60.0% (全场最高) | 累计 +122.5% | 低回撤 22.4%',
        'summary': '全场胜率与稳健度第一，机构压舱石'
    },
    'NKE': {
        'badge': '⚠️ 逆周期避坑标的',
        'stat': '实测负期望 (累计 -21.3%) | 胜率仅 28.6%',
        'summary': '传统消费阴跌逆风，模型不适用，切忌盲目抄底'
    }
}


def load_historical_profiles() -> dict:
    """加载历史回测画像：优先读取 verify_nonlinear_winrate.py 生成的动态 JSON，兜底默认画像"""
    profiles = dict(DEFAULT_STOCK_HISTORICAL_PROFILES)
    dynamic_path = BASE_DIR / "data" / "stock_profiles.json"
    if dynamic_path.exists():
        try:
            with open(dynamic_path, 'r', encoding='utf-8') as f:
                custom = json.load(f)
            if isinstance(custom, dict):
                for code, val in custom.items():
                    if isinstance(val, dict):
                        profiles[code] = {**profiles.get(code, {}), **val}
        except Exception:
            pass
    return profiles


STOCK_HISTORICAL_PROFILES = load_historical_profiles()


def generate_ai_morning_briefing(macro_summary: str, stock_details: list, regime: dict) -> str:
    """生成 Message 1：30秒AI晨报与投研精要（结合大盘熔断风控状态）"""
    if not (is_ai_configured and is_ai_configured() and chat_completion):
        return f"**【AI 晨报与投研精要】** {datetime.now().strftime('%m-%d %H:%M')}\n\n⚠️ 大模型尚未配置，宏观风向参考：{macro_summary}"

    profiles = load_historical_profiles()
    stocks_text = "\n".join([
        f"- {s['stock_code']} {s['stock_name']}: 现价 ${s['current_price']:.2f}, RSI={s['rsi']:.1f}, "
        f"综合评分 {s['total_score']:.1f}, 距线性买点 {s['linear_distance']:.1f}%, 距非线性买点 {s['nonlinear_distance']:.1f}%, 事件: {s['event']}"
        f" | 历史实测: {profiles.get(s['stock_code'], {}).get('stat', '实测中')}"
        + (f" | 财报雷达: {s.get('earnings', {}).get('tag', '')}" if s.get('earnings', {}).get('tag') else "")
        for s in stock_details
    ])

    regime_status = regime.get('status', 'NORMAL')
    circuit_breaker_instruction = ""
    if regime_status == 'CIRCUIT_BREAKER':
        circuit_breaker_instruction = f"""
⚠️【最高优先级熔断警报】：当前美股大盘触发极端【黑天鹅熔断保护】（VIX={regime.get('vix', 0):.1f}，纳指 {regime.get('qqq_change', 0):+.2f}%）！
市场遭遇泥沙俱下的系统性抛压。请在晨报第一板块以极其严厉、清醒的专业口吻通报大盘黑天鹅风险，严禁推荐任何个股左侧抄底！指导投资者空仓或现金为王，切忌盲目接下坠飞刀！
"""
    elif regime_status == 'CAUTION':
        circuit_breaker_instruction = f"""
🟡【波动防守预警】：当前大盘波动率偏高（VIX={regime.get('vix', 0):.1f}），指示风险偏好收缩。建议提示投资者保持防守姿态，建仓仓位建议减半，且必须严格执行 -8% 纪律止损。
"""

    imminent_earnings = [s for s in stock_details if s.get('earnings', {}).get('is_imminent')]
    earnings_instruction = ""
    if imminent_earnings:
        e_lines = [f"- {s['stock_code']} {s['stock_name']}: {s['earnings']['tag']}" for s in imminent_earnings]
        earnings_instruction = f"""
📅【重点财报排雷指示】：
以下标的财报近在眼前，请在【重点异动解读】或【风险与纪律提示】板块作为头条警示：
{chr(10).join(e_lines)}
强调财报日前后期权隐含波动率（IV）极高，开盲盒下注盈亏比极差，严禁盲目重仓，建议空仓避开财报窗口期，等待财报后右侧趋势明朗再行决策！
"""

    prompt = f"""
你是一位华尔街资深科技股与半导体产业链量化投研专家。
请根据今日自选股技术面状态、真实新闻事件、美联储宏观数据、大盘波动率风控状态、财报日历排雷雷达以及【历史 2 年实盘回测画像】，为投资者撰写一份企业微信【30秒AI晨报精要】（Message 1/2）。

【今日市场大盘与监控数据】
- 大盘风控态势：{regime.get('banner', '')}
- 宏观流动性环境：{macro_summary}
- 自选股监控与历史实测一览：
{stocks_text}

{circuit_breaker_instruction}
{earnings_instruction}

【撰写规范】
1. 语言专业犀利，符合顶级科技投资晨会口吻，排版使用优雅的 Markdown 格式；
2. 结合各标的历史 2 年量化实测战绩（如 LITE/COHR 累计收益超 500% 的冠军成色，以及 NKE 历史负期望的避坑提示）；
3. 包含以下三个核心板块：
   - 🌐 **【大盘与宏观风向】**：用2~3句话归纳美联储流动性、大盘波动率（VIX）与科技成长股风险偏好；
   - 🎯 **【重点异动标的解读】**：精选 2~3 只最值得关注或重点规避的标的，给出精辟逻辑与操作建议；
   - ⚠️ **【风险与纪律提示】**：针对临近财报暴击风险、大盘极端波动、SOXL 杠杆损耗、破位止损纪律或避坑标的提出警示。
4. 全文控制在 400~550 字以内，不要输出任何系统开场白或客套话。
"""
    try:
        briefing = chat_completion([{"role": "user", "content": prompt}], max_tokens=4000)
        return f"**【AI 晨报与投研精要】** {datetime.now().strftime('%m-%d %H:%M')}\n\n" + briefing.strip()
    except Exception as e:
        print(f"[Briefing] 生成晨报失败: {e}")
        return f"**【AI 晨报与投研精要】** {datetime.now().strftime('%m-%d %H:%M')}\n\n⚠️ 生成异常: {e}\n宏观风向: {macro_summary}"


def format_strategy_matrix(results: list, is_degraded: bool, regime: dict) -> str:
    """生成 Message 2：量化双策略点位表（带大盘熔断风控指示、财报雷达与历史真实回测画像）"""
    sorted_results = sorted(results, key=lambda x: x['total_score'], reverse=True)
    profiles = load_historical_profiles()
    report = []
    report.append(f"**【量化双策略点位表】** {datetime.now().strftime('%m-%d %H:%M')}")
    report.append(f"> 🛡️ **大盘风控状态**: {regime.get('banner', '')}")
    report.append(f"> 📌 **量化执行指令**: {regime.get('advice', '')}\n")
    if is_degraded:
        report.append("> ⚠️ 注：部分外部 API 延迟，已启动规则降级保障。\n")

    for i, res in enumerate(sorted_results, 1):
        code = res['stock_code']
        name = res['stock_name']
        price = res['current_price']
        score = res['total_score']
        rec = res['recommendation']
        icon = res['icon']
        linear = res['linear']
        nonlinear = res['nonlinear']

        profile = profiles.get(code, {'badge': '', 'stat': ''})
        badge_str = f" | {profile['badge']}" if profile.get('badge') else ""

        earnings = res.get('earnings', {})
        earnings_tag = earnings.get('tag', '')

        report.append(f"### {i}. {code} {name} {icon}{rec} (`{score:.1f}分`){badge_str}")
        if earnings_tag:
            report.append(f"- 📅 **财报雷达**: `{earnings_tag}`")
        stat_line = f" | 实测: `{profile['stat']}`" if profile.get('stat') else ""
        report.append(f"- 现价 **${price:.2f}** (技`{res['tech_score']:.1f}` 新`{res['news_score']:.1f}` 宏`{res['macro_score']:.1f}`){stat_line}")
        report.append(f"- 💡 **线性**: 买入 **${linear['buy']:.2f}** (-{res['linear_distance']:.1f}%) | 止盈 ${linear['profit']:.2f} (+15%) | 止损 ${linear['stop']:.2f} (-8%)")
        report.append(f"- 🛡️ **非线性**: 买入 **${nonlinear['buy']:.2f}** (-{res['nonlinear_distance']:.1f}%) | 止盈 ${nonlinear['profit']:.2f} (+46%) | 止损 ${nonlinear['stop']:.2f} (-8%)")
        report.append("")

    report.append("---")
    report.append("**操作纪律与指引**：")
    report.append("• 🏆 优选回测累计收益高、胜率过半的标的（如 LITE、COHR）")
    report.append("• ⚠️ 远离历史实测负期望标的（如 NKE），防范阴跌亏损")
    report.append("• 严格执行 -8% 日内止损，切忌追高 All in")

    return "\n".join(report)


def main():
    """主执行函数"""
    print("=" * 60)
    print(f"量化股票日报与 AI 晨报分析 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    config = load_app_config()
    api_keys = config.get("api_keys", {})
    news_key = api_keys.get("newsapi", "")
    fred_key = api_keys.get("fred", "")
    webhook_url = config.get("wechat_webhook", "")

    stocks_config = config.get("stocks", [
        {"code": "MU", "name": "美光科技"},
        {"code": "SOXL", "name": "半导体ETF"},
        {"code": "NVDA", "name": "英伟达"},
        {"code": "AVGO", "name": "博通"},
        {"code": "COHR", "name": "Coherent光学材料"},
        {"code": "NKE", "name": "耐克"},
        {"code": "AXTI", "name": "AXT光通信材料"},
        {"code": "AAOI", "name": "祥茂光电光模块"},
        {"code": "LITE", "name": "Lumentum光器件"},
        {"code": "SNDK", "name": "闪迪"}
    ])
    stocks = [(s["code"], s["name"]) for s in stocks_config]

    # 1. 多线程并发获取：[宏观流动性] + [全股票技术面] + [新闻资讯与语义打分]
    print(f"\n[1/3] [并发加速] 开始多线程并发获取数据（共 {len(stocks)} 支标的）...", flush=True)
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=16) as executor:
        macro_future = executor.submit(fetch_macro_data, fred_key)
        regime_future = executor.submit(fetch_market_regime_and_circuit_breaker)
        tech_futures = {code: executor.submit(calculate_stock_technical_and_strategies, code, name) for code, name in stocks}
        news_future = executor.submit(fetch_news_and_evaluate_sentiment, stocks, news_key)

        macro_score, macro_summary, macro_degraded = macro_future.result()
        print(f"  [OK] 宏观面完成: 利率基准 {macro_score}/10 | {macro_summary}", flush=True)

        regime = regime_future.result()
        print(f"  [OK] 风控态势完成: {regime['banner']}", flush=True)

        tech_results = []
        for code, name in stocks:
            try:
                res = tech_futures[code].result()
                if res:
                    tech_results.append(res)
                    e_tag = f" | {res['earnings']['tag']}" if res.get('earnings', {}).get('tag') else ""
                    print(f"  [OK] 技术面完成: {code} ({name}) 现价=${res['current_price']:.2f}{e_tag}", flush=True)
            except Exception as e:
                print(f"  [FAIL] 技术面异常 {code}: {e}", flush=True)

        news_results, news_degraded = news_future.result()
        print(f"  [OK] 资讯与大模型打分完成: 共 {len(news_results)} 支标的", flush=True)

    print(f"  [PERF] 全部并发数据拉取耗时: {time.time() - t0:.2f} 秒\n", flush=True)

    # 4. 综合四维度打分（技术40% + 消息30% + 宏观15% + 事件15%）
    full_results = []
    for item in tech_results:
        code = item['stock_code']
        news_info = news_results.get(code, {'score': 5.0, 'event': '平稳运行'})
        tech_s = item['tech_score']
        news_s = news_info['score']
        macro_s = macro_score
        earnings_info = item.get('earnings', {})
        # 若财报 7 天内临近，事件分下调提醒风险
        days_away = earnings_info.get('days')
        event_s = 4.5 if (days_away is not None and 0 <= days_away <= 7) else 5.0

        total_s = (
            tech_s * 0.40 +
            news_s * 0.30 +
            macro_s * 0.15 +
            event_s * 0.15
        )

        if total_s >= 8.0:
            rec = "强烈买入"
            icon = "🟢🟢"
        elif total_s >= 6.5:
            rec = "买入"
            icon = "🟢"
        elif total_s >= 5.0:
            rec = "观望"
            icon = "🟡"
        elif total_s >= 3.5:
            rec = "谨慎观望"
            icon = "🟠"
        else:
            rec = "不建议买入"
            icon = "🔴"

        # 大盘黑天鹅熔断与防守降级保护
        if regime.get('status') == 'CIRCUIT_BREAKER':
            rec = "避险"
            icon = "🔴"
        elif regime.get('status') == 'CAUTION' and "买入" in rec:
            rec = "谨慎建仓"
            icon = "🟡"

        # 核心标的财报临近排雷保护
        if earnings_info.get('is_imminent'):
            if "买入" in rec:
                rec = "财报避险"
                icon = "⚠️"

        item['news_score'] = news_s
        item['macro_score'] = macro_s
        item['event_score'] = event_s
        item['total_score'] = total_s
        item['recommendation'] = rec
        item['icon'] = icon
        item['event'] = news_info['event']
        full_results.append(item)

    is_degraded = macro_degraded or news_degraded

    # 5. 生成报告
    print("\n[4/4] 生成分段晨报 (AI 晨报精要 + 量化策略矩阵)...")
    message_1_briefing = generate_ai_morning_briefing(macro_summary, full_results, regime)
    message_2_matrix = format_strategy_matrix(full_results, is_degraded, regime)

    # 保存报告至本地 reports 目录
    report_dir = BASE_DIR / "reports"
    report_dir.mkdir(exist_ok=True)
    today_str = datetime.now().strftime('%Y%m%d')
    briefing_file = report_dir / f"ai_briefing_{today_str}.txt"
    matrix_file = report_dir / f"unified_report_{today_str}.txt"

    with open(briefing_file, 'w', encoding='utf-8') as f:
        f.write(message_1_briefing)
    with open(matrix_file, 'w', encoding='utf-8') as f:
        f.write(message_2_matrix)

    print(f"\n报告已存档:\n- {briefing_file}\n- {matrix_file}")

    # 6. 企业微信推送（分段发送）
    print("\n正在推送企业微信通知 (双消息)...")
    print("推送 Message 1: AI 晨报与投研精要...")
    send_wechat_message(message_1_briefing, webhook_url, msg_type="markdown")

    print("等待 1.5 秒推送 Message 2: 量化双策略点位表...")
    time.sleep(1.5)
    send_wechat_message(message_2_matrix, webhook_url, msg_type="markdown")

    print("\n今日分析与推送流程顺利完成！")


if __name__ == "__main__":
    main()
