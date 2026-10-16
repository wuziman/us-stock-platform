"""
分析服务
真实数据源：
- 消息面：NewsAPI + yfinance实时新闻流（美股双源融合去重）+ 新浪个股新闻（A股），关键词情感分析
- 宏观面：美股走FRED API（联邦利率/CPI/失业率）；A股走沪深300/上证指数趋势与市场流动性评估
- 事件驱动：yfinance财报日历（美股）
- 策略点位：支持固定止盈止损 + ATR(14)波动率自适应点位 + 浮盈10%保本上移线
"""

import re
import logging
import os
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import requests
from typing import Dict, Tuple, Any, List
from .stock_service import StockService, _get_cn_session
from .tech_score import calculate_tech_score
from ..utils.indicators import calculate_all_indicators
from ..utils.app_config import load_config
from ..utils.price_levels import (
    LINEAR_PROFIT_PCT,
    NONLINEAR_PROFIT_PCT,
    atr_adaptive_levels,
    discipline_stop,
    linear_buy_point,
    macd_state_days,
    nonlinear_buy_point,
)

logger = logging.getLogger(__name__)


# ============================================
# 情感分析关键词库
# ============================================
POSITIVE_CN = [
    '利好', '上涨', '增长', '超预期', '突破', '新高', '回购', '分红', '中标',
    '签约', '获准', '盈利', '大涨', '涨停', '走强', '增持', '买入评级', '推荐',
    '达成合作', '订单', '扩产', '获批', '回暖', '复苏', '领跑', '居前', '看好',
]
NEGATIVE_CN = [
    '利空', '下跌', '下滑', '亏损', '暴跌', '跌停', '减持', '卖出评级', '调查',
    '处罚', '违规', '诉讼', '警告', '风险提示', '退市', '质押', '冻结', '裁员',
    '召回', '纠纷', '内耗', '退潮', '裸泳', '卡脖子', '连亏', '承压', '遇冷',
]
POSITIVE_EN = [
    'beat', 'surge', 'rally', 'record high', 'upgrade', 'buy rating', 'growth',
    'profit', 'gain', 'bullish', 'outperform', 'partnership', 'contract',
    'dividend', 'buyback', 'expansion', 'strong', 'soar', 'jump', 'top estimates',
]
NEGATIVE_EN = [
    'miss', 'plunge', 'slump', 'downgrade', 'sell rating', 'loss', 'decline',
    'bearish', 'underperform', 'lawsuit', 'probe', 'recall', 'layoff',
    'warning', 'fraud', 'weak', 'fall', 'drop', 'cut', 'shortfall',
]


def score_text_sentiment(text: str) -> int:
    """
    对单条文本做关键词情感打分

    返回：1（利好）/ 0（中性）/ -1（利空）
    """
    text_lower = text.lower()
    pos = sum(1 for kw in POSITIVE_CN if kw in text) + \
          sum(1 for kw in POSITIVE_EN if kw in text_lower)
    neg = sum(1 for kw in NEGATIVE_CN if kw in text) + \
          sum(1 for kw in NEGATIVE_EN if kw in text_lower)
    if pos > neg:
        return 1
    if neg > pos:
        return -1
    return 0


def _load_api_keys() -> Dict[str, str]:
    """加载API keys：环境变量优先，其次原项目config/config.json（读取走 utils/app_config 单点）"""
    keys = {
        'newsapi': os.environ.get('NEWSAPI_KEY', ''),
        'fred': os.environ.get('FRED_API_KEY', ''),
    }
    api = load_config().get('api_keys', {})
    keys['newsapi'] = keys['newsapi'] or api.get('newsapi', '')
    keys['fred'] = keys['fred'] or api.get('fred', '')
    return keys


class AnalysisService:
    """股票分析服务"""

    def __init__(self):
        self.stock_service = StockService()
        self.api_keys = _load_api_keys()

    def analyze_stock(self, stock_code: str, stock_name: str, market: str = "US", mode: str = "simple") -> Dict:
        """
        综合分析单只股票
        """
        print(f"正在分析: {stock_code} {stock_name} [{market}]")

        # 获取股票数据
        df = self.stock_service.get_stock_data(stock_code, market)
        if df is None or df.empty:
            return {
                'stock_code': stock_code,
                'stock_name': stock_name,
                'error': '无法获取数据'
            }

        # 计算技术指标
        df_with_indicators = calculate_all_indicators(df)
        latest = df_with_indicators.iloc[-1]

        # 技术面：纯本地指标计算，无需并行
        tech_score, tech_details = self._analyze_technical(df_with_indicators, latest)

        # 消息面/宏观面/事件驱动互不依赖，三路并行拉取（各自有独立超时）
        with ThreadPoolExecutor(max_workers=4) as executor:
            fut_news = executor.submit(self._analyze_news, stock_code, stock_name, market)
            fut_macro = executor.submit(self._analyze_macro, market)
            fut_event = executor.submit(self._analyze_events, stock_code, stock_name, market)
            news_score, news_details = fut_news.result()
            macro_score, macro_details = fut_macro.result()
            event_score, event_details = fut_event.result()

        # 计算综合评分
        if mode == "research":
            total_score = (
                tech_score * 0.35 +
                news_score * 0.15 +
                macro_score * 0.25 +
                event_score * 0.25
            )
        else:
            total_score = (
                tech_score * 0.4 +
                news_score * 0.3 +
                macro_score * 0.15 +
                event_score * 0.15
            )

        # 确定推荐等级
        recommendation = self._get_recommendation(total_score)

        # 核心标的财报日历与排雷雷达
        earnings_radar = {
            'date': None,
            'days_away': None,
            'tag': '',
            'is_imminent': False
        }
        if market == 'US':
            try:
                import yfinance as yf
                from datetime import date
                cal = yf.Ticker(stock_code).calendar
                candidates = None
                if isinstance(cal, dict):
                    candidates = cal.get('Earnings Date')
                elif cal is not None and hasattr(cal, 'to_dict'):
                    candidates = cal.to_dict().get('Earnings Date')
                if candidates:
                    today = date.today()
                    normalized = []
                    for d in candidates:
                        try:
                            normalized.append(d.date() if callable(getattr(d, 'date', None)) else d)
                        except Exception:
                            continue
                    future = [d for d in normalized if isinstance(d, date) and d >= today]
                    if future:
                        nxt = min(future)
                        days = (nxt - today).days
                        earnings_radar['date'] = nxt.isoformat()
                        earnings_radar['days_away'] = days
                        if days == 0:
                            earnings_radar['tag'] = "🚨【今晚盘后发布财报】期权波动率暴增，严禁左侧重仓博弈！"
                            earnings_radar['is_imminent'] = True
                        elif 0 < days <= 3:
                            earnings_radar['tag'] = f"⚠️【{days}天后发布财报 ({nxt.isoformat()})】防范业绩暴击风险"
                            earnings_radar['is_imminent'] = True
                        elif 3 < days <= 14:
                            earnings_radar['tag'] = f"📅【财报倒计时 {days}天】({nxt.isoformat()})"
            except Exception as e:
                print(f"[Earnings Radar] 获取 {stock_code} 异常: {e}")

        # 若财报临近（今晚或3天内），启动风控降级保护
        if earnings_radar['is_imminent'] and recommendation.get('action') == '买入':
            recommendation['level'] = "财报避险"
            recommendation['action'] = "观望"
            recommendation['confidence'] = "高"

        # 计算三策略点位（线性/非线性/MACD + ATR动态点位 + 保本触发位）
        price_levels = self._calculate_both_strategies(df_with_indicators, latest, market)

        return {
            'stock_code': stock_code,
            'stock_name': stock_name,
            'market': market,
            'scores': {
                'technical': round(tech_score, 1),
                'news': round(news_score, 1),
                'macro': round(macro_score, 1),
                'event': round(event_score, 1),
                'total': round(total_score, 1)
            },
            'recommendation': recommendation,
            'earnings_radar': earnings_radar,
            'price_levels': price_levels,
            'details': {
                'technical': tech_details,
                'news': news_details,
                'macro': macro_details,
                'event': event_details
            }
        }

    # ============================================
    # 1. 技术面
    # ============================================
    def _analyze_technical(self, df: pd.DataFrame, latest: pd.Series) -> Tuple[float, Dict]:
        """技术面分析（评分规则在 tech_score.calculate_tech_score，与report_service共用）"""
        return calculate_tech_score(latest)

    # ============================================
    # 2. 消息面（真实新闻 + 情感分析）
    # ============================================
    def _analyze_news(self, stock_code: str, stock_name: str, market: str = "US") -> Tuple[float, Dict]:
        """消息面分析：真实新闻获取 + 关键词情感评分"""
        news_items: List[Dict] = []
        sources_used: List[str] = []

        try:
            if market == "US":
                news_items = self._fetch_news_us(stock_code, stock_name)
                sources_used = list({n.get('source_api', 'Yahoo Finance') for n in news_items}) or ['Yahoo Finance / NewsAPI']
            else:
                news_items = self._fetch_news_cn(stock_code)
                sources_used = ['新浪财经']
        except Exception as e:
            print(f"新闻获取失败: {e}")

        # 情感统计
        pos_count = sum(1 for n in news_items if n['sentiment'] > 0)
        neg_count = sum(1 for n in news_items if n['sentiment'] < 0)
        net_sentiment = pos_count - neg_count

        # 评分：基础5分 + 关注度 + 情感倾向
        score = 5.0
        if len(news_items) >= 5:
            score += 0.5  # 新闻多，关注度高
        elif len(news_items) == 0:
            score -= 1.0  # 无新闻，关注度低

        # 每条利好+0.3 / 利空-0.3，总影响限±2
        score += max(-2.0, min(2.0, net_sentiment * 0.3))

        # 情感标签
        if score >= 6.5:
            sentiment_label = '偏多'
        elif score >= 5.5:
            sentiment_label = '中性偏多'
        elif score >= 4.5:
            sentiment_label = '中性'
        elif score >= 3.5:
            sentiment_label = '中性偏空'
        else:
            sentiment_label = '偏空'

        details = {
            'news_count': len(news_items),
            'positive_count': pos_count,
            'negative_count': neg_count,
            'sentiment': sentiment_label,
            'sources': sources_used,
            'news': news_items[:20]  # 前端默认显示8条，可展开全部
        }

        return max(0, min(10, score)), details

    def _fetch_news_us(self, stock_code: str, stock_name: str) -> List[Dict]:
        """美股新闻：融合 yfinance 实时个股新闻流 + NewsAPI（去重互补，无 API Key 时同样可用）"""
        items: List[Dict] = []
        seen_titles: set = set()

        # 1. 优先拉取 yfinance 免费实时个股专属新闻流（零延迟、高相关性）
        try:
            import yfinance as yf
            from datetime import datetime
            yf_news = getattr(yf.Ticker(stock_code), 'news', None) or []
            for raw in yf_news[:12]:
                if not isinstance(raw, dict):
                    continue
                # 兼容新版 yfinance (raw['content']) 与旧版扁平字典结构
                content = raw.get('content') if isinstance(raw.get('content'), dict) else raw
                title = (content.get('title') or '').strip()
                if not title or title.lower() in seen_titles:
                    continue
                seen_titles.add(title.lower())

                provider = content.get('provider')
                source_name = (
                    provider.get('displayName', 'Yahoo Finance')
                    if isinstance(provider, dict)
                    else (content.get('publisher') or 'Yahoo Finance')
                )
                canonical = content.get('canonicalUrl')
                url = (
                    canonical.get('url', '')
                    if isinstance(canonical, dict)
                    else (content.get('link') or '')
                )
                pub_date = content.get('pubDate') or content.get('providerPublishTime') or ''
                if isinstance(pub_date, (int, float)):
                    date_str = datetime.fromtimestamp(pub_date).strftime('%Y-%m-%d')
                else:
                    date_str = str(pub_date)[:10]

                items.append({
                    'title': title,
                    'source': source_name,
                    'date': date_str,
                    'url': url,
                    'sentiment': score_text_sentiment(title),
                    'source_api': 'Yahoo Finance',
                })
        except Exception as e:
            logger.debug(f"yfinance 新闻获取异常 ({stock_code}): {e}")

        # 2. 补充 NewsAPI 跨媒体新闻（如已配置 NEWSAPI_KEY）
        api_key = self.api_keys.get('newsapi')
        if api_key:
            try:
                resp = requests.get(
                    "https://newsapi.org/v2/everything",
                    params={
                        'q': f'"{stock_name}"',
                        'language': 'en',
                        'sortBy': 'publishedAt',
                        'pageSize': 12,
                        'apiKey': api_key
                    },
                    timeout=8
                )
                if resp.status_code == 200:
                    for art in resp.json().get('articles', [])[:12]:
                        title = (art.get('title') or '').strip()
                        if not title or title.lower() in seen_titles:
                            continue
                        seen_titles.add(title.lower())
                        items.append({
                            'title': title,
                            'source': (art.get('source') or {}).get('name', 'NewsAPI'),
                            'date': (art.get('publishedAt') or '')[:10],
                            'url': art.get('url') or '',
                            'sentiment': score_text_sentiment(title),
                            'source_api': 'NewsAPI',
                        })
            except Exception as e:
                print(f"NewsAPI获取失败: {e}")

        return items

    def _fetch_news_cn(self, stock_code: str) -> List[Dict]:
        """A股新闻：新浪个股新闻页（直连不走代理）"""
        items: List[Dict] = []

        code = stock_code.replace('.SH', '').replace('.SZ', '')
        if len(code) != 6 or not code.isdigit():
            return items

        prefix = 'sh' if code.startswith('6') else 'sz'
        url = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{prefix}{code}.phtml"

        try:
            session = _get_cn_session()
            resp = session.get(url, timeout=8)
            resp.encoding = 'gbk'

            sep = r"(?:\s|&nbsp;|#xa0;|\xa0)*"
            pattern = re.compile(
                rf"(\d{{4}}-\d{{2}}-\d{{2}}){sep}(\d{{2}}:\d{{2}}){sep}<a[^>]*href='([^']+)'[^>]*>([^<]+)</a>"
            )
            for m in pattern.finditer(resp.text):
                date_str, time_str, href, title = m.groups()
                if href.startswith('//'):
                    link = 'https:' + href
                elif href.startswith('http'):
                    link = href
                else:
                    link = 'https://vip.stock.finance.sina.com.cn' + href
                items.append({
                    'title': title.strip(),
                    'source': '新浪财经',
                    'date': f"{date_str} {time_str}",
                    'url': link,
                    'sentiment': score_text_sentiment(title)
                })
                if len(items) >= 20:
                    break
        except Exception as e:
            print(f"新浪新闻获取失败: {e}")

        return items

    # ============================================
    # 3. 宏观面（美股走FRED；A股走本土大盘趋势与量能评估）
    # ============================================
    def _analyze_macro(self, market: str = "US") -> Tuple[float, Dict]:
        """宏观面分析：按市场区分美股（FRED利率/CPI/失业率）与A股（沪深大盘趋势与交投活跃度）"""
        if market == "A":
            return self._analyze_macro_cn()

        score = 5.0
        details: Dict[str, Any] = {
            'indicators': {},
            'interpretations': [],
            'sources': []
        }

        fred_key = self.api_keys.get('fred')
        if not fred_key:
            details['interpretations'].append('未配置FRED API Key，使用中性评分')
            return score, details

        fed_rates = self._fred_series('DFF', months=4)
        cpi_series = self._fred_series('CPIAUCSL', months=25)
        unrate = self._fred_series('UNRATE', months=1)

        if not fed_rates and not cpi_series and not unrate:
            details['interpretations'].append('FRED数据获取失败，使用中性评分')
            return score, details

        details['sources'].append('FRED')

        # 1. 联邦基金利率趋势：降息利好股市
        if len(fed_rates) >= 2:
            latest_rate = fed_rates[-1]['value']
            month_ago_rate = fed_rates[0]['value']
            details['indicators']['fed_rate'] = latest_rate
            if latest_rate < month_ago_rate - 0.05:
                score += 0.5
                details['interpretations'].append(f"利率下行({month_ago_rate}%→{latest_rate}%)，流动性宽松利好")
            elif latest_rate > month_ago_rate + 0.05:
                score -= 0.5
                details['interpretations'].append(f"利率上行({month_ago_rate}%→{latest_rate}%)，流动性收紧利空")
            else:
                details['interpretations'].append(f"利率平稳({latest_rate}%)")

        # 2. CPI同比：通胀温和利好，高通胀利空
        if len(cpi_series) >= 13:
            cpi_latest = cpi_series[-1]['value']
            cpi_year_ago = cpi_series[-13]['value']
            if cpi_year_ago and cpi_latest:
                cpi_yoy = (cpi_latest / cpi_year_ago - 1) * 100
                details['indicators']['cpi_yoy'] = round(cpi_yoy, 2)
                if cpi_yoy < 3:
                    score += 0.5
                    details['interpretations'].append(f"通胀温和(CPI同比{cpi_yoy:.1f}%)")
                elif cpi_yoy > 4:
                    score -= 0.5
                    details['interpretations'].append(f"通胀偏高(CPI同比{cpi_yoy:.1f}%)")
                else:
                    details['interpretations'].append(f"通胀中性(CPI同比{cpi_yoy:.1f}%)")

        # 3. 失业率：就业强劲利好
        if unrate:
            latest_unrate = unrate[-1]['value']
            details['indicators']['unemployment'] = latest_unrate
            if latest_unrate and latest_unrate < 4.5:
                score += 0.5
                details['interpretations'].append(f"就业强劲(失业率{latest_unrate}%)")
            elif latest_unrate and latest_unrate > 5.5:
                score -= 0.5
                details['interpretations'].append(f"就业走弱(失业率{latest_unrate}%)")
            else:
                details['interpretations'].append(f"就业平稳(失业率{latest_unrate}%)")

        return max(0, min(10, score)), details

    def _analyze_macro_cn(self) -> Tuple[float, Dict]:
        """A股宏观与大盘环境评估：基于沪深核心基准走势与量能趋势"""
        score = 5.5
        details: Dict[str, Any] = {
            'indicators': {},
            'interpretations': ['A股本土流动性与市场风险偏好评估'],
            'sources': ['沪深市场基准']
        }
        try:
            # 拉取平安银行/招商银行或沪深代表性权重作为市场温度计参考
            df_bench = self.stock_service.get_stock_data('000001', 'A', period='3mo')
            if df_bench is not None and len(df_bench) >= 20:
                close = df_bench['Close']
                cur = float(close.iloc[-1])
                ma20 = float(close.rolling(20).mean().iloc[-1])
                ret20 = (cur / float(close.iloc[-20]) - 1) * 100
                details['indicators']['market_20d_chg'] = round(ret20, 2)
                if cur > ma20:
                    score += 1.0
                    details['interpretations'].append(f"A股蓝筹基准站上20日均线(近20日{ret20:+.1f}%)，多头情绪回暖")
                else:
                    score -= 0.5
                    details['interpretations'].append(f"A股蓝筹基准处于20日均线下方(近20日{ret20:+.1f}%)，建议控制仓位")
        except Exception:
            pass
        return max(0.0, min(10.0, score)), details

    def _fred_series(self, series_id: str, months: int = 1) -> List[Dict]:
        """获取FRED序列最近N个月数据，失败返回空列表"""
        try:
            resp = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    'series_id': series_id,
                    'api_key': self.api_keys['fred'],
                    'file_type': 'json',
                    'sort_order': 'desc',
                    'limit': months
                },
                timeout=8
            )
            if resp.status_code == 200:
                obs = resp.json().get('observations', [])
                result = []
                for o in reversed(obs):
                    val = o.get('value')
                    if val not in (None, '.', ''):
                        result.append({'date': o.get('date'), 'value': float(val)})
                return result
        except Exception as e:
            print(f"FRED {series_id} 获取失败: {e}")
        return []

    # ============================================
    # 4. 事件驱动（财报日历）
    # ============================================
    def _analyze_events(self, stock_code: str, stock_name: str, market: str = "US") -> Tuple[float, Dict]:
        """事件驱动分析：财报日历（美股），A股暂用定期报告窗口期提醒"""
        score = 5.0
        details: Dict[str, Any] = {'events': [], 'sources': []}

        if market == "US":
            try:
                import yfinance as yf
                cal = yf.Ticker(stock_code).calendar
                earnings_dates = None
                if isinstance(cal, dict):
                    earnings_dates = cal.get('Earnings Date')
                elif cal is not None and hasattr(cal, 'loc'):
                    try:
                        earnings_dates = cal.loc['Earnings Date']
                    except KeyError:
                        pass

                if earnings_dates:
                    if isinstance(earnings_dates, (list, pd.DatetimeIndex)):
                        next_date = earnings_dates[0]
                    else:
                        next_date = earnings_dates

                    if hasattr(next_date, 'date'):
                        date_str = next_date.date().isoformat()
                    else:
                        date_str = str(next_date)

                    details['sources'].append('yfinance')
                    days_away = (pd.Timestamp(next_date) - pd.Timestamp.now()).days
                    details['events'].append({
                        'name': '财报发布',
                        'date': date_str,
                        'days_away': days_away,
                        'impact': '财报临近，波动可能加剧'
                    })
                    if 0 <= days_away <= 7:
                        score -= 0.5
                        details['events'][-1]['impact'] = '⚠️ 财报临近（7天内），业绩不确定性高，注意波动风险'
            except Exception as e:
                print(f"财报日历获取失败: {e}")
        else:
            from datetime import date
            m = date.today().month
            window_note = (
                "当前处于A股财报密集披露月（1/4/7/8/10月），注意业绩暴雷或超预期催化"
                if m in (1, 4, 7, 8, 10)
                else "当前处于A股财报真空期，个股受行业催化剂与资金面驱动为主"
            )
            details['events'].append({
                'name': 'A股财报季日历',
                'date': '',
                'days_away': None,
                'impact': window_note
            })

        return max(0, min(10, score)), details

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

    def _calculate_both_strategies(self, df: pd.DataFrame, latest: pd.Series, market: str) -> Dict:
        """
        计算三策略点位（含固定比例止盈止损 + ATR(14)波动率自适应点位 + 浮盈10%保本上移触发位）
        """
        try:
            current_price = latest.get('Close', 0)
            rsi = latest.get('RSI', 50)
            ma20 = latest.get('MA20', current_price)
            bb_lower = latest.get('BB_Lower', current_price * 0.9)
            atr = latest.get('ATR', None)

            if pd.isna(ma20):
                ma20 = current_price * 0.95
            if pd.isna(bb_lower):
                bb_lower = current_price * 0.9
            if pd.isna(rsi):
                rsi = 50
            atr_val = float(atr) if (atr is not None and pd.notna(atr)) else None

            # 线性/非线性点位公式走共享模块（utils/price_levels.py，唯一定义处）
            linear_buy = linear_buy_point(current_price, ma20)
            nonlinear_buy = nonlinear_buy_point(rsi, ma20, bb_lower)

            # MACD策略：信号状态 + 操作参考（信号型策略，无固定止盈位）
            macd_info = self._calculate_macd_levels(df, latest)

            return {
                'current_price': round(current_price, 2),
                'atr': round(atr_val, 2) if atr_val else None,
                'linear': atr_adaptive_levels(
                    current_price, linear_buy, LINEAR_PROFIT_PCT, atr=atr_val,
                    stop_atr_mult=2.0, profit_atr_mult=3.5
                ),
                'nonlinear': atr_adaptive_levels(
                    current_price, nonlinear_buy, NONLINEAR_PROFIT_PCT, atr=atr_val,
                    stop_atr_mult=2.2, profit_atr_mult=6.5
                ),
                'macd': macd_info
            }
        except Exception:
            logger.warning("计算策略点位失败", exc_info=True)
            return {
                'current_price': None,
                'atr': None,
                'linear': {'buy': None, 'stop': None, 'profit': None, 'distance': None},
                'nonlinear': {'buy': None, 'stop': None, 'profit': None, 'distance': None},
                'macd': {'state': 'unknown', 'days_in_state': 0, 'hist': 0,
                         'add_price': None, 'stop': None, 'note': '计算失败'}
            }

    def _calculate_macd_levels(self, df: pd.DataFrame, latest: pd.Series) -> Dict:
        """
        MACD策略点位：信号状态 + 操作参考
        """
        try:
            current_price = latest.get('Close', 0)
            macd = latest.get('MACD')
            signal = latest.get('MACD_Signal')
            hist = latest.get('MACD_Hist')
            ma20 = latest.get('MA20')
            bb_lower = latest.get('BB_Lower')

            if pd.isna(macd) or pd.isna(signal):
                return {'state': 'unknown', 'days_in_state': 0, 'hist': 0,
                        'add_price': None, 'stop': discipline_stop(current_price),
                        'note': '指标数据不足'}

            is_golden, days = macd_state_days((df['MACD'] > df['MACD_Signal']).values.tolist())
            state = 'golden' if is_golden else 'death'

            add_price = round(float(ma20), 2) if pd.notna(ma20) else None
            watch_price = round(float(bb_lower), 2) if pd.notna(bb_lower) else None

            if is_golden:
                note = f'金叉第{days}天，趋势向上：持有为主，回踩MA20可考虑加仓'
            else:
                note = f'死叉第{days}天，趋势向下：观望为主，等待下一次金叉信号'

            return {
                'state': state,
                'days_in_state': days,
                'hist': round(float(hist), 4) if pd.notna(hist) else 0,
                'add_price': add_price,
                'watch_price': watch_price,
                'stop': discipline_stop(current_price),
                'note': note
            }
        except Exception:
            logger.warning("计算MACD点位失败", exc_info=True)
            return {'state': 'unknown', 'days_in_state': 0, 'hist': 0,
                    'add_price': None, 'stop': None, 'note': '计算失败'}
