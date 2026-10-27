"""
十年跨周期量化数据管道服务 (Historical Data Service)
用于跨牛熊状态四维自适应权重算法的数据提取与因子对齐：
1. 10 年美股与 A 股日线行情及大盘基准数据（含 MA200 牛熊线、MA50、ATR14 与波动率）
2. 历史大盘状态序列打标（BULL 牛市 / BEAR 熊市 / SIDEWAYS 震荡市）
3. SEC EDGAR 官方免费接口历史重大事件 (8-K) 与定期报告 (10-Q/10-K) 时间戳
4. Finnhub 历史财报业绩惊喜度 (Earnings Surprise & Calendar) 数据管道与优雅降级
5. 统一特征矩阵对齐生成器（把价格、大盘状态与事件驱动对齐至单一日历轴）
"""

import logging
import os
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    yf = None

from ..utils.app_config import load_config
from ..utils.indicators import calculate_atr
from ..utils.market import detect_market
from .stock_service import _fetch_kline_eastmoney

logger = logging.getLogger(__name__)

# 常用核心标的 SEC CIK 官方代码表（直达查询，无需二次重定向）
POPULAR_SEC_CIKS: Dict[str, str] = {
    "NVDA": "0001045810",
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "MU": "0000723125",
    "AMD": "0000002488",
    "AVGO": "0001730168",
    "QCOM": "0000804328",
    "INTC": "0000050863",
    "TSLA": "0001318605",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
    "META": "0001326801",
    "LITE": "0001633978",
    "COHR": "0000021535",
    "AAOI": "0001158114",
    "AXTI": "0001051627",
    "NKE": "0000320187",
}

SEC_EDGAR_USER_AGENT = "QuantPlatformResearch admin@wuzim-quant.com"


class HistoricalDataService:
    """十年跨周期量化数据管道"""

    def __init__(self, finnhub_key: Optional[str] = None):
        cfg = load_config()
        self.finnhub_key = (
            finnhub_key
            or os.environ.get("FINNHUB_API_KEY")
            or (cfg.get("api_keys") or {}).get("finnhub")
            or ""
        )

    # ============================================
    # 1. 十年 K 线与大盘基准拉取
    # ============================================
    def fetch_ten_year_kline(
        self,
        symbol: str,
        market: Optional[str] = None,
        period: str = "10y",
    ) -> pd.DataFrame:
        """
        拉取 10 年日线数据并计算核心趋势与波动率指标（MA200, MA50, ATR14, 20日年化波动率）
        """
        code = symbol.strip().upper()
        eff_market = market or detect_market(code)

        df = pd.DataFrame()
        if eff_market == "US":
            if yf is None:
                return pd.DataFrame()
            try:
                ticker = yf.Ticker(code)
                df = ticker.history(period=period)
                if df is not None and not df.empty:
                    df.index = pd.to_datetime(df.index).tz_localize(None)
            except Exception as e:
                logger.warning(f"yfinance 拉取 10y 数据失败 ({code}): {e}")
                return pd.DataFrame()
        elif eff_market == "A":
            # A 股调用东财超长日 K 接口（period='max'，按 20140101 起算）
            try:
                df = _fetch_kline_eastmoney(code, period="max")
                if df is not None and not df.empty:
                    df.index = pd.to_datetime(df.index)
            except Exception as e:
                logger.warning(f"东财拉取 A 股 10y 数据失败 ({code}): {e}")
                return pd.DataFrame()

        if df is None or df.empty or len(df) < 50:
            return pd.DataFrame()

        df = df.sort_index()
        # 补齐技术均线与波动率
        df["MA50"] = df["Close"].rolling(50).mean()
        df["MA200"] = df["Close"].rolling(200).mean()
        df["ATR"] = calculate_atr(df, period=14)
        # 20 日历史年化波动率（日收益率标准差 × sqrt(252)）
        daily_ret = df["Close"].pct_change()
        df["Volatility20"] = daily_ret.rolling(20).std() * np.sqrt(252)

        return df

    # ============================================
    # 2. 宏观与大盘状态序列打标 (Regime Classifier)
    # ============================================
    @staticmethod
    def classify_market_regime_series(benchmark_df: pd.DataFrame) -> pd.Series:
        """
        基于 MA200 牛熊生命线 + MA50 + 波动率，对大盘指数每一根 K 线打上市场状态标签：
        - BULL（牛市）：现价位于 MA200 上方，且 MA50 >= MA200
        - BEAR（熊市）：现价位于 MA200 下方，或短期跌破 MA200 超过 3% 且 MA50 < MA200
        - SIDEWAYS（震荡市）：价格在 MA50 与 MA200 间纠缠盘整
        """
        if benchmark_df is None or benchmark_df.empty or "Close" not in benchmark_df:
            return pd.Series(dtype=object)

        df = benchmark_df.copy()
        if "MA200" not in df.columns:
            df["MA200"] = df["Close"].rolling(200).mean()
        if "MA50" not in df.columns:
            df["MA50"] = df["Close"].rolling(50).mean()

        regimes: List[str] = []
        for _, row in df.iterrows():
            c = row.get("Close")
            ma200 = row.get("MA200")
            ma50 = row.get("MA50")

            if pd.isna(ma200) or pd.isna(ma50) or ma200 <= 0:
                regimes.append("SIDEWAYS")
                continue

            if c >= ma200 and ma50 >= ma200:
                regimes.append("BULL")
            elif c < ma200 and ma50 < ma200:
                regimes.append("BEAR")
            elif c >= ma200 and ma50 < ma200:
                # 刚突破牛熊线但中期均线未跟上：偏震荡或弱牛
                regimes.append("SIDEWAYS")
            else:
                # c < ma200 但 ma50 >= ma200：牛市深回调或转熊过渡期
                regimes.append("SIDEWAYS")

        return pd.Series(regimes, index=df.index, name="regime")

    # ============================================
    # 3. SEC EDGAR 官方重大事件 (8-K / 10-Q) 拉取
    # ============================================
    def fetch_sec_edgar_events(
        self,
        ticker: str,
        limit: int = 200,
        target_forms: Optional[Tuple[str, ...]] = ("8-K", "10-Q", "10-K"),
    ) -> List[Dict]:
        """
        从 SEC EDGAR 官方免费 API 获取美股上市公司历史重大事件与定期财报时间戳（官方开放，无需 Key）
        """
        sym = ticker.strip().upper()
        cik = POPULAR_SEC_CIKS.get(sym)
        if not cik:
            logger.info(f"SEC CIK 映射表中未收录 {sym}，跳过 EDGAR 拉取")
            return []

        url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
        headers = {"User-Agent": SEC_EDGAR_USER_AGENT}

        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"SEC EDGAR 返回 HTTP {resp.status_code} ({sym})")
                return []

            data = resp.json()
            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            report_dates = recent.get("reportDate", [])
            accession_numbers = recent.get("accessionNumber", [])

            events: List[Dict] = []
            allowed = set(target_forms or ("8-K", "10-Q", "10-K"))

            for i, f in enumerate(forms):
                if f in allowed and i < len(dates):
                    events.append({
                        "date": dates[i],
                        "report_date": report_dates[i] if i < len(report_dates) else dates[i],
                        "form": f,
                        "ticker": sym,
                        "cik": cik,
                        "accession_number": accession_numbers[i] if i < len(accession_numbers) else "",
                        "is_material_event": f == "8-K",
                        "is_periodic_report": f in ("10-Q", "10-K"),
                    })
                    if len(events) >= limit:
                        break

            return events
        except Exception as e:
            logger.warning(f"SEC EDGAR 获取事件失败 ({sym}): {e}")
            return []

    # ============================================
    # 4. Finnhub 历史财报业绩惊喜度拉取
    # ============================================
    def fetch_finnhub_earnings(
        self,
        ticker: str,
        limit: int = 40,
    ) -> List[Dict]:
        """
        从 Finnhub API 拉取历史季度 EPS 实际值 vs 预期值惊喜度（若无 key 则优雅返回空列表）
        """
        sym = ticker.strip().upper()
        if not self.finnhub_key:
            logger.info(f"未配置 FINNHUB_API_KEY，跳过 Finnhub 财报拉取 ({sym})")
            return []

        url = "https://finnhub.io/api/v1/stock/earnings"
        params = {"symbol": sym, "token": self.finnhub_key}

        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 401:
                logger.warning(f"Finnhub API Key 无效或未授权 ({sym})")
                return []
            if resp.status_code != 200:
                logger.warning(f"Finnhub HTTP {resp.status_code} ({sym})")
                return []

            raw = resp.json()
            if not isinstance(raw, list):
                return []

            surprises: List[Dict] = []
            for item in raw[:limit]:
                actual = item.get("actual")
                estimate = item.get("estimate")
                period = item.get("period")
                surprise = item.get("surprise")
                surprise_pct = item.get("surprisePercent")

                surprises.append({
                    "period": str(period or ""),
                    "actual_eps": float(actual) if actual is not None else None,
                    "estimate_eps": float(estimate) if estimate is not None else None,
                    "surprise": float(surprise) if surprise is not None else None,
                    "surprise_percent": float(surprise_pct) if surprise_pct is not None else None,
                    "is_beat": (surprise is not None and surprise > 0),
                    "is_miss": (surprise is not None and surprise < 0),
                })
            return surprises
        except Exception as e:
            logger.warning(f"Finnhub 财报拉取异常 ({sym}): {e}")
            return []

    # ============================================
    # 5. 特征矩阵对齐生成器 (Unified Feature Timeline)
    # ============================================
    def build_historical_feature_matrix(
        self,
        symbol: str,
        benchmark_symbol: str = "SPY",
        market: str = "US",
        period: str = "10y",
    ) -> pd.DataFrame:
        """
        将单只股票 10 年日线、大盘基准牛熊标签及 SEC 历史重大事件对齐到统一时间轴
        """
        df_stock = self.fetch_ten_year_kline(symbol, market=market, period=period)
        if df_stock.empty:
            return pd.DataFrame()

        df_bench = self.fetch_ten_year_kline(benchmark_symbol, market=market, period=period)
        if not df_bench.empty:
            regime_series = self.classify_market_regime_series(df_bench)
            df_stock["regime"] = regime_series.reindex(df_stock.index).ffill().fillna("SIDEWAYS")
        else:
            df_stock["regime"] = "SIDEWAYS"

        # 默认事件列置空，随后根据 SEC EDGAR 匹配
        df_stock["has_sec_8k"] = False
        df_stock["has_sec_report"] = False

        if market == "US":
            events = self.fetch_sec_edgar_events(symbol, limit=300)
            date_set_8k = {e["date"] for e in events if e.get("is_material_event")}
            date_set_report = {e["date"] for e in events if e.get("is_periodic_report")}

            date_str_index = df_stock.index.strftime("%Y-%m-%d")
            df_stock["has_sec_8k"] = date_str_index.isin(date_set_8k)
            df_stock["has_sec_report"] = date_str_index.isin(date_set_report)

        return df_stock
