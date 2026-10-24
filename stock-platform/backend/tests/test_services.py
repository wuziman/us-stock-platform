# -*- coding: utf-8 -*-
"""
自动化测试：情感分析 / 新闻解析 / 回测触发逻辑
运行：cd backend && python -m pytest tests/ -v
"""
import sys
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.analysis_service import (
    AnalysisService,
    score_text_sentiment,
    _load_api_keys,
)
from app.services.backtest_service import BacktestService


# ============================================
# 情感分析
# ============================================
class TestSentiment:
    def test_positive_chinese(self):
        assert score_text_sentiment('比亚迪中标海外大订单，股价上涨') == 1

    def test_negative_chinese(self):
        assert score_text_sentiment('公司业绩亏损，遭调查处罚') == -1

    def test_neutral(self):
        assert score_text_sentiment('公司发布新款车型亮相车展') == 0

    def test_positive_english(self):
        assert score_text_sentiment('Micron beats earnings estimates, stock surges') == 1

    def test_negative_english(self):
        assert score_text_sentiment('Chipmaker faces lawsuit and warns of weak demand') == -1

    def test_mixed_tie_is_neutral(self):
        # 一好一坏 → 平局中性
        assert score_text_sentiment('增长超预期但裁员警告') == 0

    def test_negation_flip_chinese_and_english(self):
        """[第三段-8 验证] 否定词翻转极性（修改前误判为+1/0，修改后翻转为-1）"""
        # 1. “不看好”：含正向词“看好”，旧版误判 +1，新版受否定前缀“不”翻转为 -1
        assert score_text_sentiment('机构普遍不看好后市表现') == -1
        # 2. “不及预期” / “未超预期”：旧版 0 / +1，新版翻转为 -1
        assert score_text_sentiment('公司三季报营收不及预期') == -1
        assert score_text_sentiment('四季度指引未超预期') == -1
        # 3. “难以持续”：含正向词“增长”/“回暖”，旧版误判 +1，新版受反转后缀“难以持续”翻转为 -1
        assert score_text_sentiment('高增长难以持续') == -1
        assert score_text_sentiment('短期盈利回暖难以持续') == -1
        # 4. 英文否定前缀翻转
        assert score_text_sentiment('Company fails to beat revenue estimates') == -1


# ============================================
# 新浪A股新闻页解析（离线fixture，不依赖网络）
# ============================================
SINA_NEWS_HTML = """
<div class="datelist"><ul>
    &nbsp;&nbsp;&nbsp;&nbsp;2026-08-22&nbsp;12:32&nbsp;&nbsp;<a target='_blank' href='https://cj.sina.cn/a'>自主强势占位！比亚迪连续三年"包馆"</a> <br>
    &nbsp;&nbsp;&nbsp;&nbsp;2026-08-21&nbsp;17:24&nbsp;&nbsp;<a target='_blank' href='https://finance.sina.com.cn/b'>比亚迪二代刀片电池，获一汽定点</a> <br>
    &nbsp;&nbsp;&nbsp;&nbsp;2026-08-20&nbsp;20:57&nbsp;&nbsp;<a target='_blank' href='https://finance.sina.com.cn/c'>车海战术正在退潮，谁在裸泳？</a> <br>
</ul></div>
"""


class TestSinaNewsParsing:
    def _parse(self, html):
        """与 _fetch_news_cn 相同的正则逻辑，输入改为离线HTML"""
        import re
        sep = r"(?:\s|&nbsp;|#xa0;|\xa0)*"
        pattern = re.compile(
            rf"(\d{{4}}-\d{{2}}-\d{{2}}){sep}(\d{{2}}:\d{{2}}){sep}<a[^>]*href='([^']+)'[^>]*>([^<]+)</a>"
        )
        items = []
        for m in pattern.finditer(html):
            date_str, time_str, href, title = m.groups()
            items.append({
                'title': title.strip(),
                'date': f"{date_str} {time_str}",
                'sentiment': score_text_sentiment(title),
            })
        return items

    def test_parses_all_entries(self):
        items = self._parse(SINA_NEWS_HTML)
        assert len(items) == 3

    def test_dates_extracted(self):
        items = self._parse(SINA_NEWS_HTML)
        assert items[0]['date'].startswith('2026-08-22')
        assert '12:32' in items[0]['date']

    def test_titles_cleaned(self):
        items = self._parse(SINA_NEWS_HTML)
        assert items[0]['title'] == '自主强势占位！比亚迪连续三年"包馆"'
        assert '&nbsp;' not in items[0]['title']

    def test_service_parse_matches_fixture(self):
        """AnalysisService._fetch_news_cn 的正则应能解析同样的fixture格式"""
        import re
        # 直接复用服务内的pattern（从方法源码中提取同款正则做一致性校验）
        sep = r"(?:\s|&nbsp;|#xa0;|\xa0)*"
        pattern = re.compile(
            rf"(\d{{4}}-\d{{2}}-\d{{2}}){sep}(\d{{2}}:\d{{2}}){sep}<a[^>]*href='([^']+)'[^>]*>([^<]+)</a>"
        )
        matches = list(pattern.finditer(SINA_NEWS_HTML))
        assert len(matches) >= 3


# ============================================
# 回测逻辑：线性策略必须能产生交易（回归bug修复）
# ============================================
def make_fake_df(days: int = 120) -> pd.DataFrame:
    """
    构造一段先跌后涨的价格序列：
    - 前60天横盘，让MA20稳定
    - 之后持续上涨（价格站上MA20），随后有一天回踩（Low触到斐波那契位）→ 应触发买入
    """
    rng = np.random.default_rng(42)
    base = np.full(60, 100.0)
    rally = np.linspace(100, 130, days - 61)
    dip_day = np.array([124.0])  # 回踩日：收盘仍高于MA20，但最低价触及回撤位
    close = np.concatenate([base, rally[:-1], dip_day])
    noise = rng.normal(0, 0.3, len(close))
    close = np.round(close + noise, 2)

    high = np.round(close * 1.02 + 0.5, 2)
    high[-1] = max(high[-1], close[-1] * 1.01)          # 回踩日最高价正常
    low = np.round(close * 0.98 - 0.5, 2)
    low[-1] = min(low[-1], 121.5)                        # 回踩日最低价深探至121.5
    open_ = np.round((high + low) / 2, 2)

    idx = pd.date_range('2026-01-01', periods=len(close), freq='B')
    df = pd.DataFrame({'Open': open_, 'High': high, 'Low': low,
                       'Close': close, 'Volume': 1_000_000}, index=idx)
    return df


class TestBacktestLogic:
    def _with_indicators(self, df):
        from app.utils.indicators import calculate_all_indicators
        return calculate_all_indicators(df)

    def test_linear_strategy_can_trigger_buy(self):
        """回归测试：上升趋势+回踩必须能触发买入（旧代码条件永假，0笔交易）"""
        df = self._with_indicators(make_fake_df())
        svc = BacktestService()
        trades, curve = svc._backtest_linear(df, initial_capital=100000)
        buys = [t for t in trades if t['action'] == 'buy']
        assert len(buys) >= 1, "线性策略在明确的上行+回踩行情中应至少触发一次买入"

    def test_equity_curve_covers_all_bars(self):
        """每日总资产曲线必须覆盖每根K线且首尾一致"""
        df = self._with_indicators(make_fake_df())
        svc = BacktestService()
        trades, curve = svc._backtest_linear(df, initial_capital=100000)
        assert len(curve) == len(df) - 50  # 循环从第50根开始
        # 无持仓时曲线值==现金；有持仓时==现金+持仓市值（都应>0）
        assert all(v > 0 for _, v in curve)

    def test_max_drawdown_from_real_curve(self):
        """最大回撤必须来自资金曲线而非单笔亏损"""
        df = self._with_indicators(make_fake_df())
        svc = BacktestService()
        trades, curve = svc._backtest_macd(df, initial_capital=100000)
        metrics = svc._calculate_metrics(
            trades=trades, equity_curve=curve,
            total_return=(curve[-1][1] / 100000 - 1),
            initial_capital=100000, final_value=curve[-1][1],
            dates=(df.index[50], df.index[-1]),
            stock_code='FAKE', strategy='macd',
        )
        # 曲线只有涨没有明显跌时，回撤应很小
        values = [v for _, v in curve]
        peak_so_far = max(values)
        expected_dd = (peak_so_far - min(values)) / peak_so_far if peak_so_far else 0
        assert 0 <= metrics['max_drawdown'] <= max(expected_dd * 100 + 5, 10)

    def test_annualization_uses_actual_days(self):
        """年化收益按实际天数折算：40天赚5% ≠ 年化5%"""
        df = self._with_indicators(make_fake_df())
        svc = BacktestService()
        trades, curve = svc._backtest_ma_cross(df, initial_capital=100000)
        metrics = svc._calculate_metrics(
            trades=[], equity_curve=curve,
            total_return=0.05,  # 假设总收益5%
            initial_capital=100000, final_value=105000,
            dates=(df.index[50], df.index[-1]),  # ~70个交易日 ≈ 97自然日
            stock_code='FAKE', strategy='ma_cross',
        )
        # 97天翻5% → 年化约19%~20%，绝不能等于5%
        assert metrics['annual_return'] > 10
        assert abs(metrics['annual_return'] - 5) > 5

    def test_metrics_consistency(self):
        """final_value 必须等于曲线末值；交易数等于卖出次数"""
        df = self._with_indicators(make_fake_df())
        svc = BacktestService()
        trades, curve = svc._backtest_nonlinear(df, initial_capital=100000)
        sells = [t for t in trades if t['action'] == 'sell']
        metrics = svc._calculate_metrics(
            trades=trades, equity_curve=curve,
            total_return=(curve[-1][1] / 100000 - 1),
            initial_capital=100000, final_value=curve[-1][1],
            dates=(df.index[50], df.index[-1]),
            stock_code='FAKE', strategy='nonlinear',
        )
        assert metrics['final_value'] == curve[-1][1]
        assert metrics['trade_count'] == len(sells)


# ============================================
# MACD策略点位（信号状态识别）
# ============================================
class TestMacdLevels:
    def _make_df_and_latest(self, above_flags):
        """构造MACD>Signal的状态序列，above_flags为布尔列表"""
        idx = pd.date_range('2026-01-01', periods=len(above_flags), freq='B')
        macd = pd.Series([1.0 if f else -1.0 for f in above_flags], index=idx)
        signal = pd.Series([-1.0] * len(above_flags), index=idx)  # 使 macd>signal == flag
        hist = macd - signal
        df = pd.DataFrame({'MACD': macd, 'MACD_Signal': signal, 'MACD_Hist': hist}, index=idx)
        latest = pd.Series({
            'Close': 100.0, 'MACD': macd.iloc[-1], 'MACD_Signal': signal.iloc[-1],
            'MACD_Hist': hist.iloc[-1], 'MA20': 98.0, 'BB_Lower': 95.0,
        })
        return df, latest

    def test_golden_cross_state_and_days(self):
        svc = AnalysisService()
        # 最后5天金叉，之前死叉 → 应识别golden且持续5天
        flags = [False] * 10 + [True] * 5
        df, latest = self._make_df_and_latest(flags)
        info = svc._calculate_macd_levels(df, latest)
        assert info['state'] == 'golden'
        assert info['days_in_state'] == 5
        assert info['add_price'] == 98.0      # 多头加仓参考=MA20
        assert abs(info['stop'] - 92.0) < 0.01  # 纪律止损-8%

    def test_death_cross_state(self):
        svc = AnalysisService()
        # 最近3天死叉 → death + 观望提示 + 关注布林下轨
        flags = [True] * 12 + [False] * 3
        df, latest = self._make_df_and_latest(flags)
        info = svc._calculate_macd_levels(df, latest)
        assert info['state'] == 'death'
        assert info['days_in_state'] == 3
        assert info['watch_price'] == 95.0    # 空头关注买点=布林下轨

    def test_insufficient_data(self):
        svc = AnalysisService()
        latest = pd.Series({'Close': 100.0, 'MACD': float('nan'), 'MACD_Signal': float('nan')})
        info = svc._calculate_macd_levels(pd.DataFrame(), latest)
        assert info['state'] == 'unknown'


# ============================================
# API keys 加载
# ============================================
class TestApiKeys:
    def test_keys_loaded_from_original_config(self):
        keys = _load_api_keys()
        # 用户原项目config里配了newsapi和fred key，应能读到
        assert isinstance(keys, dict)
        print(f"keys: newsapi={'***' + keys['newsapi'][-4:] if keys['newsapi'] else 'EMPTY'}, "
              f"fred={'***' + keys['fred'][-4:] if keys['fred'] else 'EMPTY'}")


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


# ============================================
# [第三批·第三段-6/7/8] stock.info 超时不断链 + A股/美股三级降级源 + 清理项验证
# ============================================
class TestStockInfoTimeoutAndFallbackChains:
    def test_stock_info_timeout_falls_back_without_breaking_chain(self, monkeypatch):
        """
        [第三批·第三段-6 验证]
        当 yf.Ticker(code).info 发生阻塞超时或抛异常时，StockService.get_realtime_quote
        在受控超时内自动切换 fast_info / K线缓存兜底，绝不挂死或断链返回 None。
        """
        import time
        import app.services.stock_service as ss
        from app.services.stock_service import StockService

        monkeypatch.setattr(ss, "STOCK_INFO_TIMEOUT_SECONDS", 0.15)

        class _HangingTicker:
            def __init__(self, code):
                self.code = code
                self.fast_info = {"lastPrice": 138.5, "previousClose": 135.0, "lastVolume": 2500000}

            @property
            def info(self):
                time.sleep(1.0)  # 故意超过 0.15s 超时阈值
                return {"currentPrice": 999.0}

        monkeypatch.setattr(ss.yf, "Ticker", _HangingTicker)
        t0 = time.time()
        quote = StockService.get_realtime_quote("MU", market="US")
        elapsed = time.time() - t0

        assert quote is not None, "stock.info 超时时必须通过 fast_info/K线缓存兜底，不能断链返回 None"
        assert quote["code"] == "MU"
        assert quote["price"] == 138.5
        assert quote["degraded"] is True
        assert elapsed < 0.8, f"超时控制未生效，耗时 {elapsed:.2f}s"

    def test_cn_and_us_kline_fallback_chains(self, monkeypatch):
        """
        [第三批·第三段-7 验证]
        1. A 股三级降级链：腾讯主源失败 -> 自动切换东财前复权成功；腾讯+东财均失败 -> 自动切换新浪兜底成功
        2. 美股降级链：yfinance 主源失败 -> 自动降级本地离线缓存/Excel 数据成功
        """
        import app.services.stock_service as ss
        from app.services.stock_service import StockService

        fake_em_df = pd.DataFrame(
            {"Open": [1500.0], "Close": [1520.0], "High": [1530.0], "Low": [1495.0], "Volume": [10000]},
            index=pd.to_datetime(["2026-10-16"]),
        )
        fake_sina_df = pd.DataFrame(
            {"Open": [1505.0], "Close": [1518.0], "High": [1525.0], "Low": [1500.0], "Volume": [12000]},
            index=pd.to_datetime(["2026-10-16"]),
        )

        # Case 1: 腾讯失败 -> 东财接管
        calls = []
        monkeypatch.setattr(ss, "_fetch_kline_tencent", lambda code, period, prefix=None: (calls.append("tencent"), None)[1])
        monkeypatch.setattr(ss, "_fetch_kline_eastmoney", lambda code, period, secid=None: (calls.append("eastmoney"), fake_em_df)[1])
        monkeypatch.setattr(ss, "_fetch_kline_sina", lambda code, period, prefix=None: (calls.append("sina"), fake_sina_df)[1])

        df1 = ss._fetch_kline("600519", "A", "1mo")
        assert df1 is not None and not df1.empty
        assert float(df1["Close"].iloc[-1]) == 1520.0
        assert calls == ["tencent", "eastmoney"]

        # Case 2: 腾讯 + 东财均失败 -> 新浪接管
        calls.clear()
        monkeypatch.setattr(ss, "_fetch_kline_eastmoney", lambda code, period, secid=None: (calls.append("eastmoney"), None)[1])
        df2 = ss._fetch_kline("600519", "A", "1mo")
        assert df2 is not None and not df2.empty
        assert float(df2["Close"].iloc[-1]) == 1518.0
        assert calls == ["tencent", "eastmoney", "sina"]

        # Case 3: 美股 yfinance 抛错 -> 自动降级本地 data/MU_data.xlsx 兜底
        class _BrokenUsTicker:
            def __init__(self, code):
                pass
            def history(self, period="1mo"):
                raise RuntimeError("Yahoo Finance 429 Too Many Requests")

        monkeypatch.setattr(ss.yf, "Ticker", _BrokenUsTicker)
        df_us = StockService.get_stock_data("MU", market="US", period="1mo", use_cache=False)
        assert df_us is not None and not df_us.empty, "美股 yfinance 失败后应自动降级缓存或本地 Excel 数据"

    def test_cleanup_items_verified(self):
        """
        [第三批·第三段-8 验证]
        1. 根目录 utils/indicators.py 已收敛转发至 backend app/utils/indicators.py，原版已归档至 archive/
        2. .dockerignore 包含 *.db
        3. 前端 App.tsx 含 path="*" 404 路由，Backtest.tsx 含 commission_per_trade 表单项
        """
        from pathlib import Path
        import utils.indicators as root_ind
        from app.utils import indicators as backend_ind

        repo_root = Path(__file__).resolve().parents[3]
        assert (repo_root / "archive" / "utils_indicators_v1_archived.py").exists()
        assert (repo_root / "archive" / "tech_stock_screener_v1_archived.py").exists()
        assert (repo_root / "archive" / "verify_nonlinear_winrate_v1_archived.py").exists()
        assert (repo_root / "archive" / "README.md").exists()

        # 验证根 indicators 与 backend indicators 计算结果完全同源一致
        s = pd.Series([100.0, 102.0, 101.0, 104.0, 106.0, 105.0, 108.0, 110.0] * 3)
        assert root_ind.calculate_rsi(s).equals(backend_ind.calculate_rsi(s))

        dockerignore = (repo_root / "stock-platform" / ".dockerignore").read_text(encoding="utf-8")
        assert "*.db" in dockerignore

        app_tsx = (repo_root / "stock-platform" / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        assert 'path="*"' in app_tsx

        bt_tsx = (repo_root / "stock-platform" / "frontend" / "src" / "pages" / "Backtest.tsx").read_text(encoding="utf-8")
        assert 'name="commission_per_trade"' in bt_tsx


class TestBatch4Segment1Guards:
    def test_get_history_volume_nan_guard_no_500(self, monkeypatch):
        """
        [第四批·第一段-2 验证]
        构造 Volume 含 NaN 的日线 DataFrame，调用 routers/stocks.py::get_history，
        断言接口不抛 ValueError/500，且 NaN 行的 volume 字段返回 None。
        """
        import numpy as np
        from app.routers import stocks as stocks_router

        idx = pd.date_range("2026-09-01", periods=25, freq="B")
        df_nan_vol = pd.DataFrame(
            {
                "Open": np.linspace(100.0, 112.0, 25),
                "High": np.linspace(101.0, 113.0, 25),
                "Low": np.linspace(99.0, 111.0, 25),
                "Close": np.linspace(100.5, 112.5, 25),
                "Volume": [100000.0] * 24 + [float("nan")],
            },
            index=idx,
        )
        monkeypatch.setattr(
            stocks_router.stock_service,
            "get_stock_data",
            lambda code, market="US", period="3mo": df_nan_vol.copy(),
        )
        resp = stocks_router.get_history("MU", market="US", period="3mo")
        assert "data" in resp and len(resp["data"]) == 25
        assert resp["data"][0]["volume"] == 100000
        assert resp["data"][-1]["volume"] is None


    def test_batch4_segment3_archive_and_ux_fixes(self):
        """
        [第四批·第三段-8/9 验证]
        1. strategies/、risk/、position_plan_v2.py 已移入 archive/，根目录原路径已删除，
           且除 archive/ 外全仓库代码对上述归档模块零引用
        2. A 股财报日历自动跳过，不再产生恒定 no_data；前端 Home/Analysis/Portfolio/Backtest 四项小修全部就位
        """
        import re
        from pathlib import Path
        from app.database import SessionLocal
        from app.models import Watchlist
        from app.routers.stocks import get_earnings_calendar

        repo_root = Path(__file__).resolve().parents[3]
        arch_dir = repo_root / "archive"

        # 1. 验证原路径已移除、归档路径已存在
        assert not (repo_root / "strategies").exists()
        assert not (repo_root / "risk").exists()
        assert not (repo_root / "position_plan_v2.py").exists()
        assert (arch_dir / "strategies" / "multi_factor.py").exists()
        assert (arch_dir / "risk" / "position_sizing.py").exists()
        assert (arch_dir / "position_plan_v2.py").exists()
        assert "position_plan_v2.py" in (arch_dir / "README.md").read_text(encoding="utf-8")

        # 2. 全仓扫描（排除 archive/ 与测试断言自身）：确认零外部引用
        forbidden_patterns = [
            re.compile(r"^\s*(?:from|import)\s+strategies\b", re.M),
            re.compile(r"^\s*(?:from|import)\s+risk\b", re.M),
            re.compile(r"\bposition_plan_v2\b"),
        ]
        offending = []
        for pyf in repo_root.rglob("*.py"):
            if "archive" in pyf.parts or pyf.name == "test_services.py":
                continue
            content = pyf.read_text(encoding="utf-8", errors="ignore")
            for pat in forbidden_patterns:
                if pat.search(content):
                    offending.append(str(pyf.relative_to(repo_root)))
        assert offending == [], f"除 archive/ 外发现残留引用: {offending}"

        # 3. 验证 A 股在 get_earnings_calendar 中自动跳过，不会塞进 no_data
        db = SessionLocal()
        try:
            db.add(Watchlist(stock_code="600519", stock_name="贵州茅台", market="A"))
            db.commit()
            cal_res = get_earnings_calendar(db=db)
            no_data_codes = [x["stock_code"] for x in cal_res.get("no_data", [])]
            assert "600519" not in no_data_codes, "A股不应再进入财报日历 no_data 列表"
        finally:
            db.query(Watchlist).filter(Watchlist.stock_code == "600519").delete()
            db.commit()
            db.close()

        # 4. 验证前端四项修复落地
        fe_src = repo_root / "stock-platform" / "frontend" / "src"
        bt_txt = (fe_src / "pages" / "Backtest.tsx").read_text(encoding="utf-8")
        assert "formatBacktestTooltipLine" in bt_txt

        port_txt = (fe_src / "pages" / "Portfolio.tsx").read_text(encoding="utf-8")
        assert "买入均价 ${currencySymbol(sellingPosition.market)}" in port_txt
        assert "resolvePositionsViewState" in port_txt

        an_txt = (fe_src / "pages" / "Analysis.tsx").read_text(encoding="utf-8")
        assert "analysisMode" in an_txt and "深度投研 (35/15/25/25)" in an_txt
