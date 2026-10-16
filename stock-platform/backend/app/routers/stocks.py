"""
股票相关API
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pandas as pd
import yfinance as yf
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from ..database import get_db
from ..models import Watchlist
from ..schemas import WatchlistCreate, WatchlistResponse, MarketRegimeResponse
from ..services.stock_service import StockService

router = APIRouter(prefix="/api/stocks", tags=["stocks"])
stock_service = StockService()


def pick_next_earnings_date(candidates, today: date) -> Optional[date]:
    """从yfinance返回的财报日期候选中挑最近的未来日期（纯函数，便于单测）"""
    normalized = []
    for d in candidates or []:
        try:
            normalized.append(d.date() if callable(getattr(d, 'date', None)) else d)
        except Exception:
            continue
    future = [d for d in normalized if isinstance(d, date) and d >= today]
    return min(future) if future else None


@router.get("/search")
def search_stocks(q: str = Query(..., min_length=1), market: str = "all"):
    """搜索股票"""
    results = stock_service.search_stocks(q, market)
    return {"results": results}


@router.get("/{code}/quote")
def get_quote(code: str, market: str = "US"):
    """获取实时行情"""
    quote = stock_service.get_realtime_quote(code, market)
    if not quote:
        raise HTTPException(status_code=404, detail="股票未找到")
    return quote


@router.get("/{code}/history")
def get_history(code: str, market: str = "US", period: str = "3mo"):
    """获取历史数据（含MA均线、布林带、RSI与MACD指标，供前端多周期K线与副图叠加）"""
    df = stock_service.get_stock_data(code, market, period)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail="无法获取数据")

    from ..utils.indicators import calculate_all_indicators
    df = calculate_all_indicators(df)

    def _num(v, digits=3):
        return round(float(v), digits) if v is not None and not pd.isna(v) else None

    # 转换为JSON格式
    data = []
    for index, row in df.iterrows():
        data.append({
            'date': index.strftime('%Y-%m-%d'),
            'open': _num(row['Open']),
            'high': _num(row['High']),
            'low': _num(row['Low']),
            'close': _num(row['Close']),
            'volume': int(row['Volume']),
            'ma5': _num(row.get('MA5')),
            'ma20': _num(row.get('MA20')),
            'ma50': _num(row.get('MA50')),
            'bb_upper': _num(row.get('BB_Upper')),
            'bb_mid': _num(row.get('BB_Middle')),
            'bb_lower': _num(row.get('BB_Lower')),
            'rsi': _num(row.get('RSI'), 2),
            'macd': _num(row.get('MACD'), 3),
            'macd_signal': _num(row.get('MACD_Signal'), 3),
            'macd_hist': _num(row.get('MACD_Hist'), 3),
        })

    return {"data": data}


@router.get("/{code}/signals")
def get_signals(code: str, market: str = "US", period: str = "6mo"):
    """技术信号：K线形态识别 + 支撑阻力位 + MACD背离"""
    try:
        df = stock_service.get_stock_data(code, market, period)
        if df is None or df.empty:
            raise HTTPException(status_code=404, detail="无法获取数据")

        from ..utils.indicators import calculate_all_indicators
        from ..services.pattern_service import (
            detect_candlestick_patterns,
            detect_support_resistance,
            detect_macd_divergence,
        )
        df = calculate_all_indicators(df)

        patterns = detect_candlestick_patterns(df)
        support_resistance = detect_support_resistance(df)
        divergence = detect_macd_divergence(df)

        return {
            'code': code,
            'current_price': round(float(df['Close'].iloc[-1]), 3),
            'patterns': patterns,
            'support_resistance': support_resistance,
            'divergence': divergence,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"信号计算失败: {e}")


# 自选股与大盘风控API
@router.get("/market-regime", response_model=MarketRegimeResponse)
def get_market_regime():
    """实时监控美股宏观大盘波动率与黑天鹅熔断状态 (VIX + QQQ)"""
    vix_val = 15.0
    qqq_change = 0.0

    def _fetch_vix():
        vix_t = yf.Ticker("^VIX")
        v_hist = vix_t.history(period="5d")
        return float(v_hist['Close'].iloc[-1]) if not v_hist.empty else 15.0

    def _fetch_qqq():
        qqq_t = yf.Ticker("QQQ")
        q_hist = qqq_t.history(period="5d")
        if len(q_hist) >= 2:
            prev = float(q_hist['Close'].iloc[-2])
            curr = float(q_hist['Close'].iloc[-1])
            return (curr - prev) / prev * 100.0
        return 0.0

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_vix = executor.submit(_fetch_vix)
        f_qqq = executor.submit(_fetch_qqq)
        try:
            vix_val = f_vix.result(timeout=8)
        except Exception:
            pass
        try:
            qqq_change = f_qqq.result(timeout=8)
        except Exception:
            pass

    if vix_val >= 28.0 or qqq_change <= -2.5:
        status = "CIRCUIT_BREAKER"
        banner = f"🚨 大盘黑天鹅熔断触发 (VIX={vix_val:.1f} | 纳指 {qqq_change:+.2f}%)"
        advice = "市场恐慌抛售，系统已自动冻结买入指令，严禁逆势接飞刀！"
    elif vix_val >= 20.0 or qqq_change <= -1.5:
        status = "CAUTION"
        banner = f"🟡 大盘波动预警防守 (VIX={vix_val:.1f} | 纳指 {qqq_change:+.2f}%)"
        advice = "波动率上升，多头情绪受抑，建议仓位减半并严格设止损"
    else:
        status = "NORMAL"
        banner = f"🟢 市场环境健康 (恐慌指数 VIX={vix_val:.1f} | 纳指 {qqq_change:+.2f}%)"
        advice = "宏观与波动率适宜，按量化策略正常伏击建仓"

    return {
        "vix": round(vix_val, 2),
        "qqq_change": round(qqq_change, 2),
        "status": status,
        "banner": banner,
        "advice": advice
    }


@router.get("/earnings-calendar")
def get_earnings_calendar(db: Session = Depends(get_db)):
    """自选股财报日历（并行拉取）：美股走yfinance日历；A股尝试.SZ/.SS后缀，拿不到则日期为空"""
    watchlist = db.query(Watchlist).order_by(Watchlist.created_at.desc()).all()
    if not watchlist:
        return {'items': [], 'no_data': []}

    today = date.today()

    def _fetch_one_calendar(w):
        symbol = w.stock_code
        if (w.market or 'US') == 'A':
            code = w.stock_code.replace('.SH', '').replace('.SZ', '')
            symbol = code + ('.SS' if code.startswith('6') else '.SZ')
        entry = {
            'stock_code': w.stock_code,
            'stock_name': w.stock_name,
            'market': w.market or 'US',
            'earnings_date': None,
            'days_away': None,
        }
        try:
            cal = yf.Ticker(symbol).calendar
            candidates = None
            if isinstance(cal, dict):
                candidates = cal.get('Earnings Date')
            elif cal is not None and hasattr(cal, 'to_dict'):
                candidates = cal.to_dict().get('Earnings Date')
            nxt = pick_next_earnings_date(candidates, today)
            if nxt:
                entry['earnings_date'] = nxt.isoformat()
                entry['days_away'] = (nxt - today).days
        except Exception as e:
            print(f"财报日历获取失败 {w.stock_code}: {e}")
        return entry

    with ThreadPoolExecutor(max_workers=min(8, len(watchlist))) as executor:
        entries = list(executor.map(_fetch_one_calendar, watchlist))

    upcoming = []
    no_data = []
    for entry in entries:
        if entry['earnings_date']:
            upcoming.append(entry)
        else:
            no_data.append({'stock_code': entry['stock_code'], 'stock_name': entry['stock_name']})

    upcoming.sort(key=lambda x: x['earnings_date'])
    return {'items': upcoming, 'no_data': no_data}


@router.get("/watchlist", response_model=List[WatchlistResponse])
async def get_watchlist(db: Session = Depends(get_db)):
    """获取自选股列表"""
    return db.query(Watchlist).order_by(Watchlist.created_at.desc()).all()


@router.get("/watchlist/quotes")
def get_watchlist_quotes(db: Session = Depends(get_db)):
    """自选股实时行情（并行拉取），供首页驾驶舱表格填充现价/涨跌。
    独立端点：列表本身保持秒开，行情由前端渐进填充"""
    watchlist = db.query(Watchlist).order_by(Watchlist.created_at.desc()).all()
    if not watchlist:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(watchlist))) as executor:
        quotes = list(executor.map(
            lambda w: stock_service.get_realtime_quote(w.stock_code, w.market or 'US'),
            watchlist
        ))
    return [
        {
            'stock_code': w.stock_code,
            'price': (q or {}).get('price'),
            'change_pct': (q or {}).get('change_pct'),
        }
        for w, q in zip(watchlist, quotes)
    ]


@router.post("/watchlist", response_model=WatchlistResponse)
async def add_to_watchlist(item: WatchlistCreate, db: Session = Depends(get_db)):
    """添加到自选股"""
    existing = db.query(Watchlist).filter(
        Watchlist.stock_code == item.stock_code,
        Watchlist.market == item.market
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="已在自选股列表中")

    watchlist_item = Watchlist(**item.model_dump())
    db.add(watchlist_item)
    db.commit()
    db.refresh(watchlist_item)
    return watchlist_item


@router.delete("/watchlist/{item_id}")
async def remove_from_watchlist(item_id: int, db: Session = Depends(get_db)):
    """从自选股删除"""
    item = db.query(Watchlist).filter(Watchlist.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="未找到")
    db.delete(item)
    db.commit()
    return {"message": "删除成功"}
