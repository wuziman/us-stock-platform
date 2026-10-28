"""
自选股批量买入雷达服务 (Watchlist Buy Radar Service)
核心量化职责：
1. 并发扫描自选股池与主流标的
2. 计算每只标的当前价与官方自适应 ATR 买入位的偏差距离
3. 智能按“距离买点最近”降序/升序排序
4. 输出三色状态标签（🟢已触发买入、🟡即将触达、⚪等待回调）
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import Watchlist
from ..utils.market import detect_market
from ..utils.price_levels import linear_buy_point
from .stock_service import StockService

PRESET_RADAR_UNIVERSE = [
    {"code": "SOXL", "name": "Direxion Semiconductor Bull 3X", "market": "US"},
    {"code": "NVDA", "name": "英伟达", "market": "US"},
    {"code": "TSLA", "name": "特斯拉", "market": "US"},
    {"code": "AAPL", "name": "苹果", "market": "US"},
    {"code": "MU", "name": "美光科技", "market": "US"},
    {"code": "AMD", "name": "超威半导体", "market": "US"},
    {"code": "MSFT", "name": "微软", "market": "US"},
    {"code": "QQQ", "name": "纳指100 ETF", "market": "US"},
    {"code": "002594", "name": "比亚迪", "market": "A"},
    {"code": "600111", "name": "北方稀土", "market": "A"},
]


class RadarService:
    """批量买入雷达服务"""

    def __init__(self):
        self.stock_service = StockService()

    def scan_stock_radar_item(self, stock_code: str, stock_name: str, market: Optional[str] = None) -> Optional[Dict]:
        """单只股票雷达指标测算"""
        sym = stock_code.strip().upper()
        eff_market = market or detect_market(sym)

        try:
            quote = self.stock_service.get_realtime_quote(sym, eff_market)
            if not quote or quote.get("price") is None:
                return None

            curr_px = float(quote["price"])
            change_pct = float(quote.get("change_percent") or 0.0)

            # 获取日线均线与买点
            df = self.stock_service.get_stock_data(sym, eff_market, period="3mo")
            if df is None or len(df) < 20:
                return None

            ma20 = float(df["Close"].rolling(20).mean().iloc[-1])
            prev_close = float(df["Close"].iloc[-1])
            buy_px = round(linear_buy_point(prev_close, ma20), 2)

            # 距离买点的溢价/折价百分比：现价高出买点多少 %
            distance_pct = round((curr_px - buy_px) / curr_px * 100.0, 2)

            is_leveraged = sym in ("SOXL", "SOXS", "TQQQ", "SQQQ", "UPRO", "SPXU")
            pos_pct = "10%" if is_leveraged else ("20%" if eff_market == "US" else "15%")

            # 状态分档
            if curr_px <= buy_px or distance_pct <= 0.5:
                status = "TRIGGERED"
                status_label = "已触达·建议买入"
                status_color = "green"
            elif distance_pct <= 2.0:
                status = "APPROACHING"
                status_label = "即将触达·准备挂单"
                status_color = "gold"
            else:
                status = "WAITING"
                status_label = "等待回调"
                status_color = "default"

            return {
                "stock_code": sym,
                "stock_name": stock_name,
                "market": eff_market,
                "current_price": round(curr_px, 2),
                "change_percent": round(change_pct, 2),
                "buy_price": buy_px,
                "distance_pct": distance_pct,
                "position_pct": pos_pct,
                "status": status,
                "status_label": status_label,
                "status_color": status_color,
            }
        except Exception:
            return None

    def scan_radar_universe(self, db: Optional[Session] = None) -> List[Dict]:
        """并发扫描自选股与主流科技标的并按距离买点最近排序"""
        universe_map: Dict[str, Dict] = {item["code"]: dict(item) for item in PRESET_RADAR_UNIVERSE}

        # 若提供了数据库，自动合并用户自选股
        if db is not None:
            try:
                watchlist_items = db.query(Watchlist).all()
                for w in watchlist_items:
                    code = w.stock_code.strip().upper()
                    if code not in universe_map:
                        universe_map[code] = {
                            "code": code,
                            "name": w.stock_name or code,
                            "market": w.market or detect_market(code),
                        }
            except Exception:
                pass

        results: List[Dict] = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            future_to_stock = {
                executor.submit(self.scan_stock_radar_item, s["code"], s["name"], s["market"]): s["code"]
                for s in universe_map.values()
            }
            for fut in as_completed(future_to_stock):
                res = fut.result()
                if res is not None:
                    results.append(res)

        # 排序：距离买点最近的排在最前 (已触发 -> 即将触达 -> 等待回调)
        results.sort(key=lambda x: (x["distance_pct"]))
        return results
