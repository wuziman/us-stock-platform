"""
股票数据服务
支持A股（腾讯/东财前复权日K，新浪不复权兜底）和美股（YFinance）
"""

import re
import json
import yfinance as yf
import pandas as pd
import requests
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from ..database import SessionLocal
from ..models_platform import KlineCache


# K线缓存有效期（分钟）
CACHE_TTL_MINUTES = 30


# 美股/ETF扩展列表
US_STOCKS = {
    # 科技巨头
    'AAPL': 'Apple', 'GOOGL': 'Alphabet', 'MSFT': 'Microsoft',
    'AMZN': 'Amazon', 'NVDA': 'NVIDIA', 'META': 'Meta',
    'TSLA': 'Tesla', 'AMD': 'AMD', 'NFLX': 'Netflix',
    'CRM': 'Salesforce', 'ADBE': 'Adobe', 'INTC': 'Intel',
    'ORCL': 'Oracle', 'CSCO': 'Cisco', 'QCOM': 'Qualcomm',

    # 半导体
    'MU': 'Micron', 'AVGO': 'Broadcom', 'TXN': 'Texas Instruments',
    'AMAT': 'Applied Materials', 'LRCX': 'Lam Research',
    'KLAC': 'KLA Corporation', 'MRVL': 'Marvell', 'ON': 'ON Semiconductor',
    'COHR': 'Coherent', 'AXTI': 'AXT', 'AAOI': 'Applied Optoelectronics',
    'LITE': 'Lumentum', 'SNDK': 'SanDisk',

    # ETF
    'SOXL': 'Direxion Semiconductor Bull 3X', 'SOXS': 'Direxion Semiconductor Bear 3X',
    'QQQ': 'Invesco QQQ Trust', 'QQQM': 'Invesco NASDAQ 100',
    'SPY': 'SPDR S&P 500 ETF', 'VOO': 'Vanguard S&P 500',
    'IVV': 'iShares Core S&P 500', 'VTI': 'Vanguard Total Stock Market',
    'ARKK': 'ARK Innovation ETF', 'ARKG': 'ARK Genomic Revolution',
    'XLF': 'Financial Select Sector', 'XLK': 'Technology Select Sector',
    'XLE': 'Energy Select Sector', 'XLV': 'Health Care Select Sector',
    'GLD': 'SPDR Gold Shares', 'SLV': 'iShares Silver Trust',
    'TLT': 'iShares 20+ Year Treasury', 'BND': 'Vanguard Total Bond Market',
    'SQQQ': 'ProShares UltraPro Short QQQ', 'TQQQ': 'ProShares UltraPro QQQ',

    # 消费品
    'NKE': 'Nike', 'SBUX': 'Starbucks', 'MCD': "McDonald's",
    'TGT': 'Target', 'WMT': 'Walmart', 'COST': 'Costco',
    'KO': 'Coca-Cola', 'PEP': 'PepsiCo', 'PG': 'Procter & Gamble',

    # 金融
    'JPM': 'JPMorgan Chase', 'BAC': 'Bank of America', 'WFC': 'Wells Fargo',
    'GS': 'Goldman Sachs', 'MS': 'Morgan Stanley', 'V': 'Visa',
    'MA': 'Mastercard', 'PYPL': 'PayPal', 'SQ': 'Block',

    # 医疗
    'JNJ': 'Johnson & Johnson', 'PFE': 'Pfizer', 'UNH': 'UnitedHealth',
    'MRK': 'Merck', 'ABBV': 'AbbVie', 'LLY': 'Eli Lilly',

    # 中概股
    'NIO': 'NIO', 'XPEV': 'XPeng', 'LI': 'Li Auto',
    'BABA': 'Alibaba', 'JD': 'JD.com', 'PDD': 'Pinduoduo',
    'BIDU': 'Baidu', 'NTES': 'NetEase',

    # 其他
    'BA': 'Boeing', 'CAT': 'Caterpillar', 'DIS': 'Disney',
    'VZ': 'Verizon', 'T': 'AT&T'
}


# 常用A股列表（用于搜索）
CN_STOCKS = {
    '600519': '贵州茅台', '000858': '五粮液', '000333': '美的集团',
    '601318': '中国平安', '600036': '招商银行', '000001': '平安银行',
    '600276': '恒瑞医药', '002594': '比亚迪', '600111': '北方稀土',
    '601012': '隆基绿能', '300750': '宁德时代', '002475': '立讯精密',
    '600887': '伊利股份', '000651': '格力电器', '601888': '中国中免',
    '600900': '长江电力', '601166': '兴业银行', '000568': '泸州老窖',
    '002714': '牧原股份', '300059': '东方财富', '002352': '顺丰控股',
    '601398': '工商银行', '600030': '中信证券', '601899': '紫金矿业',
    '002415': '海康威视', '600809': '山西汾酒', '300015': '爱尔眼科',
    '000538': '云南白药', '603259': '药明康德', '002304': '洋河股份',
}


def _get_cn_session():
    """创建不走代理的session用于访问国内网站"""
    session = requests.Session()
    session.proxies = {'http': None, 'https': None}
    session.trust_env = False
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    return session


# ============================================
# K线缓存（SQLite，TTL 30分钟，读写失败自动降级直连）
# ============================================
def _cache_key(market: str, stock_code: str, period: str) -> str:
    return f"{market}:{stock_code}:{period}"


def _df_to_records(df: pd.DataFrame):
    """DataFrame → 可JSON序列化的记录列表（日期转字符串）"""
    reset = df.reset_index()
    idx_col = reset.columns[0]
    reset[idx_col] = pd.to_datetime(reset[idx_col]).dt.strftime('%Y-%m-%d %H:%M:%S')
    return reset.to_dict(orient='records')


def _df_from_records(records) -> pd.DataFrame:
    """记录列表 → DataFrame（首列还原为DatetimeIndex）"""
    df = pd.DataFrame(records)
    idx_col = df.columns[0]
    df[idx_col] = pd.to_datetime(df[idx_col])
    return df.set_index(idx_col)


def _read_kline_cache(cache_key: str):
    """读缓存：命中且未过期返回记录列表；未命中/过期/出错一律返回None（降级直连）"""
    try:
        db = SessionLocal()
        try:
            row = db.query(KlineCache).filter(KlineCache.cache_key == cache_key).first()
            if row is None or not row.fetched_at:
                return None
            if datetime.now() - row.fetched_at >= timedelta(minutes=CACHE_TTL_MINUTES):
                return None
            return json.loads(row.data_json)
        finally:
            db.close()
    except Exception as e:
        print(f"读取K线缓存失败(降级直连): {e}")
        return None


def _write_kline_cache(cache_key: str, df: pd.DataFrame):
    """写/更新缓存（upsert）。失败不影响主流程"""
    try:
        data_json = json.dumps(_df_to_records(df), ensure_ascii=False)
        db = SessionLocal()
        try:
            row = db.query(KlineCache).filter(KlineCache.cache_key == cache_key).first()
            if row:
                row.data_json = data_json
                row.fetched_at = datetime.now()
            else:
                db.add(KlineCache(cache_key=cache_key, data_json=data_json,
                                  fetched_at=datetime.now()))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        print(f"写入K线缓存失败(忽略): {e}")


def clear_kline_cache() -> int:
    """清空全部K线缓存（备用），返回清除条数"""
    db = SessionLocal()
    try:
        count = db.query(KlineCache).delete()
        db.commit()
        return count
    finally:
        db.close()


def cleanup_expired_kline_cache(max_age_hours: int = 24) -> int:
    """自动清理超过 max_age_hours 小时的陈旧K线缓存记录，控制SQLite体积膨胀"""
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    db = SessionLocal()
    try:
        rows = db.query(KlineCache).all()
        removed = 0
        for row in rows:
            if row.fetched_at and row.fetched_at < cutoff:
                db.delete(row)
                removed += 1
        if removed > 0:
            db.commit()
        return removed
    except Exception as e:
        print(f"清理过期K线缓存失败(忽略): {e}")
        return 0
    finally:
        db.close()


def _fetch_kline_eastmoney(code: str, period: str) -> Optional[pd.DataFrame]:
    """东方财富前复权日K（免费公开接口）：数字镜像子域直连（push2his主域被
    本机Clash代理拦截直连必断），镜像不可用时回退系统代理。
    任一步失败返回None，由调用方决定降级路径"""
    try:
        # secid 前缀：沪市(6开头)=1，其余=0（深市）；fqt=1 前复权
        secid = ('1.' if code.startswith('6') else '0.') + code

        # period 映射为起始日期（东财接口按 beg/end 区间取数）
        period_days = {
            '1mo': 40, '3mo': 100, '6mo': 200,
            '1y': 380, '2y': 760, '3y': 1140, '5y': 1900, 'max': 7500
        }
        days = period_days.get(period, 100)
        beg = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')

        url = "https://92.push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            'secid': secid,
            'fields1': 'f1,f2,f3,f4,f5,f6',
            'fields2': 'f51,f52,f53,f54,f55,f56',  # 日期,开,收,高,低,量
            'klt': '101',   # 日K
            'fqt': '1',     # 前复权
            'beg': beg,
            'end': '20500101',
        }

        response = None
        try:
            response = _get_cn_session().get(url, params=params, timeout=10)
        except requests.RequestException:
            # 直连被限流/断开时，回退走系统默认路由（代理出口通常可达东财）
            print(f"东财直连失败，尝试系统代理: {code}")
            fallback = requests.Session()
            fallback.trust_env = True
            response = fallback.get(url, params=params, timeout=10)

        payload = response.json()
        klines = (payload.get('data') or {}).get('klines') or []
        if not klines:
            print(f"东财未返回K线数据: {code}")
            return None

        rows = []
        for line in klines:
            parts = line.split(',')
            rows.append({
                'Date': parts[0],
                'Open': float(parts[1]),
                'Close': float(parts[2]),
                'High': float(parts[3]),
                'Low': float(parts[4]),
                'Volume': int(float(parts[5]))
            })

        df = pd.DataFrame(rows)
        df['Date'] = pd.to_datetime(df['Date'])
        return df.set_index('Date')
    except Exception as e:
        print(f"东财拉取失败({code}): {e}")
        return None


def _fetch_kline_sina(code: str, period: str) -> Optional[pd.DataFrame]:
    """新浪日K（不复权）——仅作东财不可用时的降级来源"""
    try:
        prefix = 'sh' if code.startswith('6') else 'sz'
        symbol = f"{prefix}{code}"

        # 按period映射天数（新浪接口最长1023天≈4年）
        period_days = {
            '1mo': 30, '3mo': 90, '6mo': 185,
            '1y': 370, '2y': 740, '3y': 1023, '5y': 1023, 'max': 1023
        }
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {
            'symbol': symbol,
            'scale': '240',  # 日K
            'ma': 'no',
            'datalen': str(period_days.get(period, 90))
        }

        session = _get_cn_session()
        response = session.get(url, params=params, timeout=10)
        data = response.json()

        if data:
            rows = []
            for item in data:
                rows.append({
                    'Date': item['day'],
                    'Open': float(item['open']),
                    'High': float(item['high']),
                    'Low': float(item['low']),
                    'Close': float(item['close']),
                    'Volume': int(item['volume'])
                })

            df = pd.DataFrame(rows)
            df['Date'] = pd.to_datetime(df['Date'])
            return df.set_index('Date')

        return None
    except Exception as e:
        print(f"新浪拉取失败({code}): {e}")
        return None


def _fetch_kline_tencent(code: str, period: str) -> Optional[pd.DataFrame]:
    """腾讯前复权日K（web.ifzq.gtimg.cn公开接口，无需key；单次最多约800根，
    5y/max封顶）。东财WAF对本机python客户端间歇性重置连接，故以腾讯为主源"""
    try:
        prefix = 'sh' if code.startswith('6') else 'sz'
        symbol = f"{prefix}{code}"

        # 按period映射根数（A股年交易日约243；接口单次上限约800根）
        period_bars = {
            '1mo': 30, '3mo': 70, '6mo': 135,
            '1y': 260, '2y': 510, '3y': 760, '5y': 800, 'max': 800
        }
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        params = {'param': f"{symbol},day,,,{period_bars.get(period, 70)},qfq"}

        session = _get_cn_session()
        response = session.get(url, params=params, timeout=10)
        node = (response.json().get('data') or {}).get(symbol) or {}
        klines = node.get('qfqday') or node.get('day') or []
        if not klines:
            print(f"腾讯未返回K线数据: {code}")
            return None

        rows = []
        for bar in klines:
            rows.append({
                'Date': bar[0],
                'Open': float(bar[1]),
                'Close': float(bar[2]),   # 腾讯数组顺序：日期,开,收,高,低,量
                'High': float(bar[3]),
                'Low': float(bar[4]),
                'Volume': int(bar[5])
            })

        df = pd.DataFrame(rows)
        df['Date'] = pd.to_datetime(df['Date'])
        return df.set_index('Date')
    except Exception as e:
        print(f"腾讯拉取失败({code}): {e}")
        return None


def _fetch_kline(stock_code: str, market: str, period: str) -> Optional[pd.DataFrame]:
    """真实拉取K线数据：美股走yfinance；A股主路为腾讯前复权日K、东财前复权备选
    （除权除息缺口不再被当成真实涨跌参与指标与回测），均不可用时降级新浪不复权"""
    try:
        if market == "US":
            stock = yf.Ticker(stock_code)
            df = stock.history(period=period)
            if not df.empty:
                df.index = pd.to_datetime(df.index).tz_localize(None)
                return df

        elif market == "A":
            code = stock_code.replace('.SH', '').replace('.SZ', '')

            if len(code) != 6 or not code.isdigit():
                print(f"无效的A股代码格式: {stock_code}")
                return None

            # 主源腾讯前复权；东财前复权备选（其WAF间歇性拦截python客户端）；新浪不复权兜底
            for fetcher in (_fetch_kline_tencent, _fetch_kline_eastmoney):
                df = fetcher(code, period)
                if df is not None and not df.empty:
                    return df
            print(f"警告: {stock_code} 前复权源均失败，降级新浪不复权数据")
            return _fetch_kline_sina(code, period)

        return None
    except Exception as e:
        print(f"获取{stock_code}数据失败: {e}")
        return None


class StockService:
    """股票数据服务"""

    @staticmethod
    def get_stock_data(stock_code: str, market: str = "US", period: str = "3mo",
                       use_cache: bool = True) -> Optional[pd.DataFrame]:
        """
        获取股票历史数据（带SQLite缓存：命中且未过期直接返回，否则拉取后写缓存）。
        use_cache=False 跳过读缓存强制刷新（拉到新数据后仍回写缓存）
        """
        key = _cache_key(market, stock_code, period)

        # 1. 读缓存（未命中/过期/出错返回None，自动降级直连）
        if use_cache:
            cached = _read_kline_cache(key)
            if cached is not None:
                try:
                    return _df_from_records(cached)
                except Exception as e:
                    print(f"缓存数据还原失败(降级直连): {e}")

        # 2. 直连拉取
        df = _fetch_kline(stock_code, market, period)

        # 3. 拉取成功则更新缓存
        if df is not None and not df.empty:
            _write_kline_cache(key, df)

        return df

    @staticmethod
    def get_realtime_quote(stock_code: str, market: str = "US") -> Optional[Dict]:
        """
        获取实时行情
        """
        try:
            if market == "US":
                stock = yf.Ticker(stock_code)
                info = stock.info
                return {
                    'code': stock_code,
                    'name': info.get('shortName', US_STOCKS.get(stock_code, stock_code)),
                    'price': info.get('currentPrice', info.get('regularMarketPrice', 0)),
                    'change': info.get('regularMarketChange', 0),
                    'change_pct': info.get('regularMarketChangePercent', 0),
                    'volume': info.get('volume', 0),
                    'market_cap': info.get('marketCap', 0),
                    'pe_ratio': info.get('trailingPE', None),
                    'pb_ratio': info.get('priceToBook', None)
                }

            elif market == "A":
                code = stock_code.replace('.SH', '').replace('.SZ', '')
                prefix = 'sh' if code.startswith('6') else 'sz'
                symbol = f"{prefix}{code}"

                # 使用新浪财经实时接口
                url = f"https://hq.sinajs.cn/list={symbol}"
                session = _get_cn_session()
                session.headers.update({
                    'Referer': 'https://finance.sina.com.cn'
                })
                response = session.get(url, timeout=10)

                # 解析返回数据
                content = response.text
                match = re.search(r'"(.+)"', content)
                if match:
                    fields = match.group(1).split(',')
                    if len(fields) >= 32:
                        # 下标：0名称/1今开/2昨收/3现价；涨跌无现成字段，用现价-昨收自算
                        price = float(fields[3]) if fields[3] else 0
                        prev_close = float(fields[2]) if fields[2] else 0
                        if price <= 0:
                            # 停牌/集合竞价时段新浪把现价置0.000而非空串，
                            # 按无效处理返回None，让持仓端回退买入价而不是显示-100%
                            return None
                        return {
                            'code': stock_code,
                            'name': fields[0],
                            'price': price,
                            'change': round(price - prev_close, 3),
                            'change_pct': round((price / prev_close - 1) * 100, 3) if prev_close else 0,
                            'volume': int(fields[8]) if fields[8] else 0,
                            'market_cap': 0,
                            'pe_ratio': None,
                            'pb_ratio': None
                        }

            return None
        except Exception as e:
            print(f"获取{stock_code}实时行情失败: {e}")
            return None

    @staticmethod
    def search_stocks(query: str, market: str = "all") -> List[Dict]:
        """
        搜索股票
        """
        results = []

        try:
            # 搜索美股
            if market in ["US", "all"]:
                query_upper = query.upper()
                query_lower = query.lower()

                for code, name in US_STOCKS.items():
                    if query_upper == code:
                        results.insert(0, {
                            'code': code,
                            'name': name,
                            'market': 'US'
                        })
                    elif query_upper in code or query_lower in name.lower():
                        results.append({
                            'code': code,
                            'name': name,
                            'market': 'US'
                        })

            # 搜索A股（新浪suggest实时搜索全市场，失败回退本地列表）
            if market in ["A", "all"]:
                cn_results = StockService._search_cn_suggest(query)
                if cn_results:
                    results.extend(cn_results)
                else:
                    # 兜底：本地热门列表
                    query_lower = query.lower()
                    for code, name in CN_STOCKS.items():
                        if query in name or query_lower in code.lower():
                            market_type = 'SH' if code.startswith('6') else 'SZ'
                            results.append({
                                'code': code,
                                'name': name,
                                'market': 'A',
                                'market_type': market_type
                            })

        except Exception as e:
            print(f"搜索股票失败: {e}")

        return results[:20]

    @staticmethod
    def _search_cn_suggest(query: str) -> List[Dict]:
        """
        新浪suggest接口实时搜索A股全市场
        返回格式：var suggest="名称,类型11(沪深A),代码,szXXXXXX,..,;..."
        """
        items: List[Dict] = []
        try:
            from urllib.parse import quote as url_quote
            url = f"https://suggest3.sinajs.cn/suggest/type=&key={url_quote(query)}&name=suggest"
            session = _get_cn_session()
            session.headers['Referer'] = 'https://finance.sina.com.cn'
            resp = session.get(url, timeout=6)
            resp.encoding = 'gbk'

            text = resp.text.strip()
            start, end = text.find('"'), text.rfind('"')
            if start == -1 or end <= start:
                return items

            # 每条记录以";"分隔，字段以","分隔：[0]名称 [1]类型 [2]代码 [3]带前缀全码
            for entry in text[start + 1:end].split(';'):
                fields = entry.split(',')
                if len(fields) < 4:
                    continue
                name, type_code, code, full_code = fields[0], fields[1], fields[2], fields[3]
                # 类型11=沪深A股；只收6位纯数字代码
                if type_code != '11' or len(code) != 6 or not code.isdigit():
                    continue
                # 纯数字查询时name可能是"szXXXXXX"形式，规范化为空让前端只显示代码
                if name.lower().replace('.', '') == full_code.lower().replace('.', ''):
                    name = ''
                items.append({
                    'code': code,
                    'name': name,
                    'market': 'A',
                    'market_type': 'SH' if code.startswith('6') else 'SZ'
                })
                if len(items) >= 15:
                    break
        except Exception as e:
            print(f"suggest搜索A股失败: {e}")
        return items
