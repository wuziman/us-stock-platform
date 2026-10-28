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
from .stock_service import StockService, _get_cn_session, _resolve_cn_symbol
from .tech_score import calculate_tech_score
from ..utils.indicators import calculate_all_indicators
from ..utils.app_config import load_config
from ..utils.price_levels import (
    CAUTION_BUY_DISCOUNT,
    LINEAR_PROFIT_PCT,
    NONLINEAR_PROFIT_PCT,
    apply_regime_to_levels,
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


# 否定前缀词与反转后缀短语（用于将“不看好”“未超预期”“高增长难以持续”“不及预期”等极性翻转）
NEGATION_PREFIX_CN = (
    "不", "未", "非", "无", "没", "没有", "不再", "并不", "并非", "难", "难以", "缺乏", "未见",
)
NEGATION_SUFFIX_CN = (
    "难以持续", "难以为继", "不及预期", "未达预期", "低于预期", "受阻", "乏力", "放缓", "存疑", "落空",
)
NEGATIVE_COMPOUND_CN = (
    "不及预期", "低于预期", "未达预期", "难以持续", "难以为继", "由盈转亏", "增收不增利",
)
NEGATION_PREFIX_EN_RE = re.compile(r"(?:\bnot\b|\bno\b|\bnever\b|\bfails?\s+to\b|\bunable\s+to\b)\s*$", re.I)


def score_text_sentiment(text: str) -> int:
    """
    对单条文本做关键词情感打分（支持否定前缀与反转后缀极性翻转）：
    - 正向词前紧邻否定前缀（如“不看好”“未超预期”“不再增持”）或后接反转短语（如“增长难以持续”）→ 极性翻转为利空(-1)
    - 负向词前紧邻否定/消除前缀（如“未亏损”“解除冻结”）→ 抵消负向极性
    - 显式复合利空词（如“不及预期”“低于预期”“难以持续”）直接计入利空

    返回：1（利好）/ 0（中性）/ -1（利空）
    """
    if not text:
        return 0
    text_lower = text.lower()
    pos = 0
    neg = 0

    clause_splitter = re.compile(r"[，。；！？,.;!?\n]")
    clauses = clause_splitter.split(text)

    for clause in clauses:
        if not clause:
            continue
        matched_compound_neg = False
        for comp in NEGATIVE_COMPOUND_CN:
            if comp in clause:
                neg += 1
                matched_compound_neg = True

        for kw in POSITIVE_CN:
            start_pos = 0
            while True:
                idx = clause.find(kw, start_pos)
                if idx == -1:
                    break
                prefix = clause[max(0, idx - 4):idx]
                suffix = clause[idx + len(kw):min(len(clause), idx + len(kw) + 6)]
                has_neg_prefix = any(prefix.endswith(p) or p in prefix for p in NEGATION_PREFIX_CN)
                has_neg_suffix = any(s in suffix for s in NEGATION_SUFFIX_CN)
                if has_neg_prefix:
                    neg += 1
                elif has_neg_suffix or matched_compound_neg:
                    if not matched_compound_neg:
                        neg += 1
                else:
                    pos += 1
                start_pos = idx + len(kw)

        for kw in NEGATIVE_CN:
            start_pos = 0
            while True:
                idx = clause.find(kw, start_pos)
                if idx == -1:
                    break
                prefix = clause[max(0, idx - 4):idx]
                if any(p in prefix for p in ("未", "不", "无", "没", "解除", "消除")):
                    pass
                else:
                    neg += 1
                start_pos = idx + len(kw)

    for kw in POSITIVE_EN:
        for m in re.finditer(re.escape(kw), text_lower):
            prefix_en = text_lower[max(0, m.start() - 15):m.start()]
            if NEGATION_PREFIX_EN_RE.search(prefix_en):
                neg += 1
            else:
                pos += 1

    for kw in NEGATIVE_EN:
        if kw in text_lower:
            neg += 1

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


# ============================================
# 四维评分宪法级权重常量（全仓库唯一真源，严禁在其他模块重复手写字面量）
# ============================================
DEFAULT_4D_WEIGHTS = {
    "technical": 0.40,
    "news": 0.30,
    "macro": 0.15,
    "event": 0.15,
}
DEFAULT_DIMENSION_WEIGHTS = {
    "simple": DEFAULT_4D_WEIGHTS,
    "research": {
        "technical": 0.35,
        "news": 0.15,
        "macro": 0.25,
        "event": 0.25,
    },
    "adaptive": DEFAULT_4D_WEIGHTS,
}

# 十年 Walk-Forward 滚动前向寻优产出的各状态最优自适应权重矩阵
REGIME_OPTIMAL_WEIGHTS: Dict[str, Dict[str, float]] = {
    "BULL": {"technical": 0.20, "news": 0.30, "macro": 0.20, "event": 0.30},
    "BEAR": {"technical": 0.20, "news": 0.10, "macro": 0.40, "event": 0.30},
    "SIDEWAYS": {"technical": 0.20, "news": 0.20, "macro": 0.20, "event": 0.40},
}

REGIME_MODE_DESCRIPTIONS: Dict[str, str] = {
    "BULL": "牛市动量增强模式",
    "BEAR": "熊市防御风控模式",
    "SIDEWAYS": "震荡市平衡模式",
}


class AnalysisService:
    """股票分析服务"""

    def __init__(self):
        self.stock_service = StockService()
        self.api_keys = _load_api_keys()

    def _resolve_market_regime(self, market: str) -> Tuple[str, str, Dict[str, float]]:
        """识别大盘基准当前所处的牛熊状态 (BULL/BEAR/SIDEWAYS) 并返回对应的自适应最优权重"""
        benchmark_code = "000300.SS" if market == "A" else "SPY"
        try:
            df_bench = self.stock_service.get_stock_data(benchmark_code, market, period="1y")
            if df_bench is not None and not df_bench.empty and len(df_bench) >= 50:
                from .historical_data_service import HistoricalDataService
                regimes = HistoricalDataService.classify_market_regime_series(df_bench)
                if not regimes.empty:
                    regime = str(regimes.iloc[-1]).upper()
                    if regime in REGIME_OPTIMAL_WEIGHTS:
                        return regime, REGIME_MODE_DESCRIPTIONS[regime], REGIME_OPTIMAL_WEIGHTS[regime]
        except Exception as e:
            logger.info(f"大盘自适应状态推导降级为震荡市: {e}")

        return "SIDEWAYS", REGIME_MODE_DESCRIPTIONS["SIDEWAYS"], REGIME_OPTIMAL_WEIGHTS["SIDEWAYS"]

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

        # 解析大盘自适应状态与最优权重
        curr_regime, mode_desc, adaptive_weights = self._resolve_market_regime(market)

        # 分别计算固定静态分与牛熊自适应分
        static_w = DEFAULT_DIMENSION_WEIGHTS.get(mode if mode in DEFAULT_DIMENSION_WEIGHTS else "simple", DEFAULT_4D_WEIGHTS)
        static_total = round(
            tech_score * static_w["technical"] +
            news_score * static_w["news"] +
            macro_score * static_w["macro"] +
            event_score * static_w["event"],
            1,
        )

        adaptive_total = round(
            tech_score * adaptive_weights["technical"] +
            news_score * adaptive_weights["news"] +
            macro_score * adaptive_weights["macro"] +
            event_score * adaptive_weights["event"],
            1,
        )

        # 确定生效的总评分与权重
        if mode == "research":
            total_score = static_total
            applied_weights = static_w
        elif mode == "adaptive":
            total_score = adaptive_total
            applied_weights = adaptive_weights
        else:
            # simple 默认采用自适应最佳权重
            total_score = adaptive_total
            applied_weights = adaptive_weights

        regime_adaptive = {
            "regime": curr_regime,
            "mode_desc": mode_desc,
            "weights": applied_weights,
            "adaptive_total_score": adaptive_total,
            "static_total_score": static_total,
            "score_diff": round(adaptive_total - static_total, 1),
        }

        # 确定推荐等级
        recommendation = self._get_recommendation(total_score)

        # 核心标的财报日历与排雷雷达（由 _analyze_events 在并行线程池中一次性拉取并传入，消除二次重复请求）
        default_radar = {
            'date': None,
            'days_away': None,
            'tag': '',
            'is_imminent': False
        }
        earnings_radar = (
            event_details.pop('earnings_radar', None)
            if isinstance(event_details, dict)
            else None
        ) or default_radar

        # 若财报临近（今晚或3天内），启动风控降级保护
        if earnings_radar['is_imminent'] and recommendation.get('action') == '买入':
            recommendation['level'] = "财报避险"
            recommendation['action'] = "观望"
            recommendation['confidence'] = "高"

        # 计算三策略点位（线性/非线性/MACD + ATR动态点位 + 保本触发位）
        price_levels = self._calculate_both_strategies(df_with_indicators, latest, market)

        # 构建极简行动决策卡（傻瓜式5数交易卡）
        action_card = self._build_action_card(
            current_price=float(latest['Close']),
            total_score=total_score,
            price_levels=price_levels,
            recommendation=recommendation,
            earnings_radar=earnings_radar,
            stock_code=stock_code,
        )

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
            'regime_adaptive': regime_adaptive,
            'action_card': action_card,
            'price_levels': price_levels,
            'details': {
                'technical': tech_details,
                'news': news_details,
                'macro': macro_details,
                'event': event_details
            }
        }

    @staticmethod
    def _build_action_card(
        current_price: float,
        total_score: float,
        price_levels: Dict,
        recommendation: Dict,
        earnings_radar: Dict,
        stock_code: str,
    ) -> Dict[str, Any]:
        """构建极简行动决策卡（纯净5大核心数字+大白话行动指南）"""
        is_leveraged = stock_code.upper() in ("SOXL", "SOXS", "TQQQ", "SQQQ", "UPRO", "SPXU")
        linear_levels = price_levels.get("linear", {})
        raw_buy = linear_levels.get("buy") or (current_price * 0.95)
        buy_px = round(float(raw_buy), 2)
        profit_px = round(float(linear_levels.get("atr_profit") or linear_levels.get("profit") or (buy_px * 1.15)), 2)
        stop_px = round(float(linear_levels.get("atr_stop") or linear_levels.get("stop") or (buy_px * 0.92)), 2)

        risk = max(0.01, buy_px - stop_px)
        reward = max(0.01, profit_px - buy_px)
        rr_str = f"{round(reward / risk, 2)}:1"

        if earnings_radar.get("is_imminent"):
            action_type = "WAIT_AND_SEE"
            action_label = "财报避险·观望"
            one_sentence_reason = "3天内面临重磅财报公布，波动率极高，严禁左侧博弈，待业绩落地后再择机入场"
            position_pct = "0%"
        elif total_score < 3.5 or recommendation.get("action") == "卖出":
            action_type = "SELL"
            action_label = "坚决离场"
            one_sentence_reason = f"多头指标破位且综合评分仅{total_score}分，趋势向下，严禁逆势抄底"
            position_pct = "0%"
        elif total_score >= 6.5:
            if abs(current_price - buy_px) / current_price < 0.015:
                action_type = "BUY_NOW"
                action_label = "现价分批进"
                one_sentence_reason = f"综合评分{total_score}分处于强势主升区，现价已达支撑位，可立即建立首仓"
            else:
                action_type = "BUY_ON_PULLBACK"
                action_label = "挂单等回踩"
                one_sentence_reason = f"动量良好(评分{total_score}分)，但现价偏离支撑，建议挂单${buy_px}等回踩分批建仓"
            position_pct = "15%" if is_leveraged else ("30%" if total_score >= 8.0 else "20%")
        else:
            action_type = "WAIT_AND_SEE"
            action_label = "挂单等深度回踩"
            one_sentence_reason = f"处于整理蓄势期(评分{total_score}分)，严禁追高，仅建议在强支撑${buy_px}轻仓埋伏"
            position_pct = "10%" if is_leveraged else "15%"

        return {
            "action_type": action_type,
            "action_label": action_label,
            "current_price": round(current_price, 2),
            "buy_price": buy_px,
            "position_pct": position_pct,
            "take_profit_price": profit_px,
            "stop_loss_price": stop_px,
            "one_sentence_reason": one_sentence_reason,
            "risk_reward_ratio": rr_str,
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
        """A股新闻：新浪个股新闻页（直连不走代理，统一复用 _resolve_cn_symbol 兼容 .SS/.SH/.SZ）"""
        items: List[Dict] = []

        code, prefix, _ = _resolve_cn_symbol(stock_code)
        if len(code) != 6 or not code.isdigit():
            return items

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
        """A股宏观与大盘环境评估：基于沪深300指数（000300.SS）走势与量能趋势"""
        score = 5.5
        details: Dict[str, Any] = {
            'indicators': {'benchmark': '000300.SS'},
            'interpretations': ['A股本土流动性与沪深300(000300.SS)大盘风险偏好评估'],
            'sources': ['沪深300指数 (000300.SS)']
        }
        try:
            # 拉取沪深300指数（000300.SS）作为A股大盘基准（替代旧版单股 000001.SZ）
            df_bench = self.stock_service.get_stock_data('000300.SS', 'A', period='3mo')
            if df_bench is not None and len(df_bench) >= 20:
                close = df_bench['Close']
                cur = float(close.iloc[-1])
                ma20 = float(close.rolling(20).mean().iloc[-1])
                ret20 = (cur / float(close.iloc[-20]) - 1) * 100
                details['indicators']['market_20d_chg'] = round(ret20, 2)
                if cur > ma20:
                    score += 1.0
                    details['interpretations'].append(f"沪深300基准(000300.SS)站上20日均线(近20日{ret20:+.1f}%)，多头情绪回暖")
                else:
                    score -= 0.5
                    details['interpretations'].append(f"沪深300基准(000300.SS)处于20日均线下方(近20日{ret20:+.1f}%)，建议控制仓位")
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
        """事件驱动分析：财报日历与排雷雷达（美股单次拉取复用，时区安全），A股用定期报告窗口期提醒"""
        from datetime import date

        score = 5.0
        earnings_radar = {
            'date': None,
            'days_away': None,
            'tag': '',
            'is_imminent': False,
        }
        details: Dict[str, Any] = {'events': [], 'sources': [], 'earnings_radar': earnings_radar}

        if market == "US":
            try:
                import yfinance as yf
                cal = yf.Ticker(stock_code).calendar
                candidates = None
                if isinstance(cal, dict):
                    candidates = cal.get('Earnings Date')
                elif cal is not None and hasattr(cal, 'to_dict'):
                    candidates = cal.to_dict().get('Earnings Date')
                elif cal is not None and hasattr(cal, 'loc'):
                    try:
                        candidates = cal.loc['Earnings Date']
                    except KeyError:
                        pass

                if candidates is not None:
                    if not isinstance(candidates, (list, tuple, pd.DatetimeIndex, pd.Series)):
                        candidates = [candidates]
                    today = date.today()
                    normalized = []
                    for d in candidates:
                        try:
                            if callable(getattr(d, 'date', None)):
                                normalized.append(d.date())
                            elif isinstance(d, date):
                                normalized.append(d)
                            else:
                                normalized.append(pd.Timestamp(d).date())
                        except Exception:
                            continue

                    if normalized:
                        future = [d for d in normalized if d >= today]
                        nxt = min(future) if future else normalized[0]
                        days_away = (nxt - today).days
                        date_str = nxt.isoformat()

                        details['sources'].append('yfinance')
                        impact = '财报临近，波动可能加剧'
                        if 0 <= days_away <= 7:
                            score -= 0.5
                            impact = '⚠️ 财报临近（7天内），业绩不确定性高，注意波动风险'

                        details['events'].append({
                            'name': '财报发布',
                            'date': date_str,
                            'days_away': days_away,
                            'impact': impact,
                        })

                        if days_away >= 0:
                            earnings_radar['date'] = date_str
                            earnings_radar['days_away'] = days_away
                            if days_away == 0:
                                earnings_radar['tag'] = "🚨【今晚盘后发布财报】期权波动率暴增，严禁左侧重仓博弈！"
                                earnings_radar['is_imminent'] = True
                            elif 0 < days_away <= 3:
                                earnings_radar['tag'] = f"⚠️【{days_away}天后发布财报 ({date_str})】防范业绩暴击风险"
                                earnings_radar['is_imminent'] = True
                            elif 3 < days_away <= 14:
                                earnings_radar['tag'] = f"📅【财报倒计时 {days_away}天】({date_str})"
            except Exception as e:
                print(f"财报日历获取失败: {e}")
        else:
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

            # 读取全局 regime_linkage_enabled 开关与当前大盘风控状态（统管日报/分析/建仓三处）
            from .alert_service import is_regime_linkage_enabled
            from .report_service import ReportService
            linkage_on = is_regime_linkage_enabled()
            regime = ReportService().get_market_regime()
            raw_status = (regime.get('status') or 'NORMAL').upper()
            eff_status = raw_status if linkage_on else 'NORMAL'

            linear_levels = apply_regime_to_levels(
                current_price, linear_buy, LINEAR_PROFIT_PCT,
                regime_status=eff_status, atr=atr_val,
                stop_atr_mult=2.0, profit_atr_mult=3.5,
            )
            nonlinear_levels = apply_regime_to_levels(
                current_price, nonlinear_buy, NONLINEAR_PROFIT_PCT,
                regime_status=eff_status, atr=atr_val,
                stop_atr_mult=2.2, profit_atr_mult=6.5,
            )

            # MACD策略：信号状态 + 操作参考（受熔断/预警联动控制）
            macd_info = self._calculate_macd_levels(df, latest)
            if eff_status == 'CIRCUIT_BREAKER':
                macd_info['add_price'] = None
                macd_info['watch_price'] = None
            elif eff_status == 'CAUTION':
                if macd_info.get('add_price') is not None:
                    macd_info['add_price'] = round(macd_info['add_price'] * CAUTION_BUY_DISCOUNT, 2)
                if macd_info.get('watch_price') is not None:
                    macd_info['watch_price'] = round(macd_info['watch_price'] * CAUTION_BUY_DISCOUNT, 2)

            return {
                'current_price': round(current_price, 2),
                'atr': round(atr_val, 2) if atr_val else None,
                'regime_status': eff_status,
                'raw_regime_status': raw_status,
                'regime_linkage_enabled': linkage_on,
                'position_scale': linear_levels.get('position_scale', 1.0),
                'linear': linear_levels,
                'nonlinear': nonlinear_levels,
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
