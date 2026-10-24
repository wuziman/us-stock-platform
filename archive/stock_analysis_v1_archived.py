"""
股票综合分析模块
包含四个维度：技术面、消息面、宏观面、事件驱动
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple
import json
from pathlib import Path
import os
import sys

# 引入后端共享模块（若可用）
_BASE_DIR = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _BASE_DIR / "stock-platform" / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

try:
    from app.services.tech_score import calculate_tech_score
    from app.utils.indicators import calculate_all_indicators
except Exception:
    calculate_tech_score = None
    calculate_all_indicators = None


class StockAnalyzer:
    """股票综合分析器"""

    def __init__(self, config_path: str = "config/config.json"):
        """初始化分析器"""
        self.config = self._load_config(config_path)
        # 优先使用环境变量，其次使用配置文件
        self.newsapi_key = os.environ.get('NEWSAPI_KEY') or self.config.get("api_keys", {}).get("newsapi", "")
        self.fred_key = os.environ.get('FRED_API_KEY') or self.config.get("api_keys", {}).get("fred", "")

    def _load_config(self, config_path: str) -> dict:
        """加载配置文件"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def analyze_stock(self, stock_code: str, stock_name: str, mode: str = "simple") -> Dict:
        """
        综合分析单只股票
        返回：评分、推荐等级、建议买入价、止盈位、止损位

        参数：
            mode: "simple"（简单版，技40%+新30%+宏15%+事15%）或 "research"（研究验证版，技35%+新15%+宏25%+事25%）
        """
        print(f"\n{'='*60}")
        print(f"正在分析: {stock_code} {stock_name} [{mode}版]")
        print(f"{'='*60}")

        # 1. 技术面分析
        tech_score, tech_details = self._analyze_technical(stock_code)
        print(f"技术面分析完成: {tech_score}/10")

        # 2. 消息面分析
        news_score, news_details = self._analyze_news(stock_code, stock_name)
        print(f"消息面分析完成: {news_score}/10")

        # 3. 宏观面分析
        macro_score, macro_details = self._analyze_macro()
        print(f"宏观面分析完成: {macro_score}/10")

        # 4. 事件驱动分析
        event_score, event_details = self._analyze_events(stock_code, stock_name)
        print(f"事件驱动分析完成: {event_score}/10")

        # 计算综合评分（根据模式选择权重）
        if mode == "research":
            total_score = (
                tech_score * 0.35 +
                news_score * 0.15 +
                macro_score * 0.25 +
                event_score * 0.25
            )
            print("使用研究验证权重: 技35% + 新15% + 宏25% + 事25%")
        else:
            total_score = (
                tech_score * 0.40 +
                news_score * 0.30 +
                macro_score * 0.15 +
                event_score * 0.15
            )
            print("使用简单版权重: 技40% + 新30% + 宏15% + 事15%")

        # 确定推荐等级
        recommendation = self._get_recommendation(total_score)

        # 计算价格点位
        price_levels = self._calculate_price_levels(stock_code, tech_details)

        return {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "scores": {
                "technical": round(tech_score, 1),
                "news": round(news_score, 1),
                "macro": round(macro_score, 1),
                "event": round(event_score, 1),
                "total": round(total_score, 1)
            },
            "recommendation": recommendation,
            "price_levels": price_levels,
            "details": {
                "technical": tech_details,
                "news": news_details,
                "macro": macro_details,
                "event": event_details
            }
        }

    def _analyze_technical(self, stock_code: str) -> Tuple[float, Dict]:
        """
        技术面分析（与后端 tech_score 统一：空头排列下禁用超卖加分，含本地 Excel 兜底）
        """
        try:
            df = pd.DataFrame()
            try:
                stock = yf.Ticker(stock_code)
                df = stock.history(period="3mo")
            except Exception:
                pass

            if df is None or df.empty:
                candidates = list(_BASE_DIR.glob(f"**/{stock_code}*_data.xlsx"))
                if candidates:
                    df = pd.read_excel(candidates[0])
                    if 'Date' in df.columns:
                        df['Date'] = pd.to_datetime(df['Date'])
                        df = df.set_index('Date')

            if df is None or df.empty:
                return 5.0, {"error": "无法获取数据"}

            df = df.dropna(subset=['Close'])
            if df.empty:
                return 5.0, {"error": "收盘价为空"}

            if calculate_all_indicators is not None and calculate_tech_score is not None:
                df_ind = calculate_all_indicators(df)
                latest = df_ind.iloc[-1]
                score, base_details = calculate_tech_score(latest)
                current_price = float(latest['Close'])
                current_ma20 = float(latest['MA20']) if pd.notna(latest.get('MA20')) else current_price
                current_ma50 = float(latest['MA50']) if pd.notna(latest.get('MA50')) else current_price
                current_bb_upper = float(latest['BB_Upper']) if pd.notna(latest.get('BB_Upper')) else current_price * 1.1
                current_bb_lower = float(latest['BB_Lower']) if pd.notna(latest.get('BB_Lower')) else current_price * 0.9
                details = {
                    **base_details,
                    "current_price": round(current_price, 2),
                    "ma20": round(current_ma20, 2),
                    "ma50": round(current_ma50, 2),
                    "bb_upper": round(current_bb_upper, 2),
                    "bb_lower": round(current_bb_lower, 2),
                    "support_levels": [round(current_bb_lower, 2), round(current_ma20, 2)],
                    "resistance_levels": [round(current_bb_upper, 2), round(current_ma50, 2)]
                }
                return score, details

            # 兜底计算（与 tech_score 规则一致）
            delta = df['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            rsi = np.where((loss == 0) & (gain > 0), 100.0, np.where((loss == 0) & (gain == 0), 50.0, rsi))
            current_rsi = float(rsi[-1]) if len(rsi) > 0 else 50.0

            exp1 = df['Close'].ewm(span=12, adjust=False).mean()
            exp2 = df['Close'].ewm(span=26, adjust=False).mean()
            macd = exp1 - exp2
            signal = macd.ewm(span=9, adjust=False).mean()
            current_macd = float(macd.iloc[-1])
            current_signal = float(signal.iloc[-1])

            ma20 = df['Close'].rolling(window=20).mean()
            ma50 = df['Close'].rolling(window=50).mean()
            current_price = float(df['Close'].iloc[-1])
            current_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else current_price
            current_ma50 = float(ma50.iloc[-1]) if pd.notna(ma50.iloc[-1]) else current_price

            bb_middle = df['Close'].rolling(window=20).mean()
            bb_std = df['Close'].rolling(window=20).std()
            bb_upper = bb_middle + 2 * bb_std
            bb_lower = bb_middle - 2 * bb_std
            current_bb_upper = float(bb_upper.iloc[-1]) if pd.notna(bb_upper.iloc[-1]) else current_price * 1.1
            current_bb_lower = float(bb_lower.iloc[-1]) if pd.notna(bb_lower.iloc[-1]) else current_price * 0.9

            bearish_alignment = current_ma20 < current_ma50
            score = 5.0
            if current_rsi < 30 and not bearish_alignment:
                score += 2.0
            elif current_rsi < 40 and not bearish_alignment:
                score += 1.0
            elif current_rsi > 70:
                score -= 2.0
            elif current_rsi > 60:
                score -= 1.0

            score += 1.0 if current_macd > current_signal else -1.0
            score += 0.5 if current_price > current_ma20 else (-0.5 if current_price < current_ma20 else 0)
            score += 0.5 if current_price > current_ma50 else (-0.5 if current_price < current_ma50 else 0)
            score += 0.5 if current_ma20 > current_ma50 else (-0.5 if current_ma20 < current_ma50 else 0)

            if current_price < current_bb_lower and not bearish_alignment:
                score += 1.0
            elif current_price > current_bb_upper:
                score -= 1.0

            score = max(0.0, min(10.0, score))
            details = {
                "current_price": round(current_price, 2),
                "rsi": round(current_rsi, 2),
                "macd": round(current_macd, 4),
                "macd_signal": round(current_signal, 4),
                "ma20": round(current_ma20, 2),
                "ma50": round(current_ma50, 2),
                "bb_upper": round(current_bb_upper, 2),
                "bb_lower": round(current_bb_lower, 2),
                "support_levels": [round(current_bb_lower, 2), round(current_ma20, 2)],
                "resistance_levels": [round(current_bb_upper, 2), round(current_ma50, 2)]
            }
            return score, details

        except Exception as e:
            print(f"技术面分析失败: {e}")
            return 5.0, {"error": str(e)}

    def _analyze_news(self, stock_code: str, stock_name: str) -> Tuple[float, Dict]:
        """消息面分析：NewsAPI + 关键词情感评分"""
        news_data = []
        score = 5.0
        details = {"sources": [], "sentiment": "中性"}

        try:
            if self.newsapi_key:
                url = "https://newsapi.org/v2/everything"
                params = {
                    "q": f'"{stock_name}"',
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": 10,
                    "apiKey": self.newsapi_key
                }
                response = requests.get(url, params=params, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    articles = data.get("articles", [])
                    for article in articles[:5]:
                        news_data.append({
                            "title": article.get("title", ""),
                            "source": article.get("source", {}).get("name", ""),
                            "published": article.get("publishedAt", ""),
                            "api_source": "NewsAPI"
                        })
                    details["sources"].append("NewsAPI")
        except Exception as e:
            print(f"NewsAPI获取失败: {e}")

        if len(news_data) > 3:
            score += 1.0
        elif len(news_data) > 0:
            score += 0.5
        else:
            score -= 0.5

        details["news_count"] = len(news_data)
        details["news"] = news_data[:3]
        return max(0.0, min(10.0, score)), details

    def _analyze_macro(self) -> Tuple[float, Dict]:
        """宏观面分析：使用 FRED API 获取联邦利率与失业率并动态打分"""
        score = 5.0
        details = {"indicators": {}, "sources": []}

        try:
            if self.fred_key:
                indicators = {
                    "fed_rate": "DFF",
                    "unemployment": "UNRATE",
                }
                for name, series_id in indicators.items():
                    try:
                        url = "https://api.stlouisfed.org/fred/series/observations"
                        params = {
                            "series_id": series_id,
                            "api_key": self.fred_key,
                            "file_type": "json",
                            "sort_order": "desc",
                            "limit": 1
                        }
                        response = requests.get(url, params=params, timeout=8)
                        if response.status_code == 200:
                            observations = response.json().get("observations", [])
                            if observations and observations[0].get("value") not in (None, ".", ""):
                                details["indicators"][name] = float(observations[0].get("value"))
                    except Exception as e:
                        print(f"获取{name}失败: {e}")

                details["sources"].append("FRED API")
                fed_rate = details["indicators"].get("fed_rate")
                if fed_rate is not None:
                    if fed_rate <= 3.75:
                        score += 1.5
                    elif fed_rate <= 4.75:
                        score += 0.5
                    else:
                        score -= 0.5
                unrate = details["indicators"].get("unemployment")
                if unrate is not None:
                    if unrate < 4.5:
                        score += 0.5
                    elif unrate > 5.5:
                        score -= 0.5
        except Exception as e:
            print(f"FRED API获取失败: {e}")

        return max(0.0, min(10.0, score)), details

    def _analyze_events(self, stock_code: str, stock_name: str) -> Tuple[float, Dict]:
        """事件驱动分析：通过 yfinance 财报日历评估近期事件风险"""
        score = 5.0
        details = {"upcoming_events": [], "sources": []}

        try:
            cal = yf.Ticker(stock_code).calendar
            ed = None
            if isinstance(cal, dict) and cal.get("Earnings Date"):
                ed = cal["Earnings Date"][0]
            elif hasattr(cal, "get") and cal.get("Earnings Date"):
                ed = cal.get("Earnings Date")[0]
            if ed:
                today = date.today()
                ed_date = ed.date() if hasattr(ed, "date") else ed
                if isinstance(ed_date, date):
                    days = (ed_date - today).days
                    details["sources"].append("yfinance")
                    details["upcoming_events"].append({
                        "name": "财报发布",
                        "date": ed_date.isoformat(),
                        "days_away": days
                    })
                    if 0 <= days <= 7:
                        score -= 0.5
        except Exception:
            pass

        return max(0.0, min(10.0, score)), details

    def _get_recommendation(self, score: float) -> Dict:
        """根据评分确定推荐等级"""
        if score >= 8.0:
            return {"level": "强烈推荐", "action": "买入", "confidence": "高"}
        elif score >= 6.5:
            return {"level": "推荐", "action": "买入", "confidence": "中"}
        elif score >= 5.0:
            return {"level": "中性", "action": "持有", "confidence": "中"}
        elif score >= 3.5:
            return {"level": "谨慎", "action": "观望", "confidence": "中"}
        else:
            return {"level": "不推荐", "action": "卖出", "confidence": "高"}

    def _calculate_price_levels(self, stock_code: str, tech_details: Dict) -> Dict:
        """计算价格点位（斐波那契回撤位 + ATR动态止损）"""
        try:
            current_price = tech_details.get("current_price", 0)
            ma20 = tech_details.get("ma20", current_price)
            bb_lower = tech_details.get("bb_lower", current_price * 0.9)
            bb_upper = tech_details.get("bb_upper", current_price * 1.1)
            support_levels = tech_details.get("support_levels", [])
            resistance_levels = tech_details.get("resistance_levels", [])

            atr_approx = (bb_upper - bb_lower) / 4
            max_buy_distance = 0.08

            if ma20 < current_price:
                price_range = current_price - ma20
                fib_382 = current_price - 0.382 * price_range
                fib_500 = current_price - 0.500 * price_range
                fib_618 = current_price - 0.618 * price_range
            else:
                fib_382 = current_price * 0.9618
                fib_500 = current_price * 0.95
                fib_618 = current_price * 0.9382

            rsi = tech_details.get("rsi", 50)
            if rsi > 60:
                fib_buy, fib_level = fib_382, "38.2%"
            elif rsi > 45:
                fib_buy, fib_level = fib_500, "50%"
            else:
                fib_buy, fib_level = fib_618, "61.8%"

            if support_levels:
                valid_supports = [s for s in support_levels if current_price * (1 - max_buy_distance) <= s <= current_price]
                if valid_supports:
                    nearest_support = max(valid_supports)
                    buy_price = nearest_support if abs(nearest_support - current_price) < abs(fib_buy - current_price) else fib_buy
                else:
                    buy_price = fib_buy
            else:
                buy_price = fib_buy

            buy_price = min(max(buy_price, current_price * (1 - max_buy_distance)), current_price)
            atr_stop = buy_price - 1.5 * atr_approx

            if support_levels:
                valid_supports = [s for s in support_levels if s < buy_price]
                stop_loss = max(atr_stop, max(valid_supports) * 0.98) if valid_supports else atr_stop
            else:
                stop_loss = atr_stop

            stop_loss = max(stop_loss, buy_price * 0.92)
            risk = buy_price - stop_loss
            take_profit_rr2 = buy_price + 2 * risk

            if resistance_levels:
                valid_resistance = [r for r in resistance_levels if r >= current_price]
                take_profit = min(take_profit_rr2, min(valid_resistance)) if valid_resistance else take_profit_rr2
            else:
                take_profit = take_profit_rr2

            take_profit = max(take_profit, buy_price * 1.05)

            return {
                "current_price": round(current_price, 2),
                "buy_price": round(buy_price, 2),
                "take_profit": round(take_profit, 2),
                "stop_loss": round(stop_loss, 2),
                "support_levels": support_levels,
                "resistance_levels": resistance_levels,
                "atr": round(atr_approx, 2),
                "fib_level": fib_level,
                "risk_reward_ratio": round((take_profit - buy_price) / (buy_price - stop_loss), 2) if buy_price > stop_loss else 0
            }
        except Exception as e:
            print(f"价格点位计算失败: {e}")
            return {
                "current_price": 0, "buy_price": 0, "take_profit": 0, "stop_loss": 0,
                "support_levels": [], "resistance_levels": [], "atr": 0,
                "fib_level": "N/A", "risk_reward_ratio": 0
            }


def main():
    """主函数"""
    analyzer = StockAnalyzer()
    result = analyzer.analyze_stock("MU", "美光科技")
    print("\n" + "="*60)
    print(f"股票: {result['stock_code']} {result['stock_name']} | 综合评分: {result['scores']['total']}/10")


if __name__ == "__main__":
    main()
