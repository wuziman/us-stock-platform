"""
模拟实战记账与前向胜率跟踪服务 (Paper Trading Service)
核心量化职责：
1. 一键记录 5 数操作指令卡的开仓点位与止盈止损线
2. 实时盯盘与自动按真实行情结算（触及止盈自动止盈，触及止损自动止损）
3. 统计前向实战胜率与累计盈亏百分比
"""

from typing import Dict, List, Optional
from sqlalchemy.orm import Session

from ..models_platform import PaperTrade
from ..schemas import PaperTradeOpenRequest
from ..utils.market import detect_market
from ..utils.timezone import now_beijing
from .stock_service import StockService


class PaperTradingService:
    """模拟记账与胜率跟踪服务"""

    def __init__(self):
        self.stock_service = StockService()

    def open_trade(self, req: PaperTradeOpenRequest, db: Session) -> PaperTrade:
        """从操作卡一键记录模拟开仓"""
        market = req.market or detect_market(req.stock_code)
        trade = PaperTrade(
            stock_code=req.stock_code.strip().upper(),
            stock_name=req.stock_name,
            market=market,
            buy_price=round(req.buy_price, 2),
            buy_date=now_beijing(),
            position_pct=req.position_pct or "20%",
            take_profit_price=round(req.take_profit_price, 2),
            stop_loss_price=round(req.stop_loss_price, 2),
            status="OPEN",
            created_at=now_beijing(),
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        return trade

    def get_trades_with_settlement(self, db: Session) -> List[Dict]:
        """查询模拟持仓与历史记录，并对持仓按实时现价自动校验是否触发止盈止损"""
        trades = db.query(PaperTrade).order_by(PaperTrade.id.desc()).all()
        results: List[Dict] = []

        for t in trades:
            curr_px = None
            unrealized_pct = None

            if t.status == "OPEN":
                try:
                    q = self.stock_service.get_realtime_quote(t.stock_code, t.market)
                    if q and q.get("price"):
                        curr_px = float(q["price"])
                        unrealized_pct = round((curr_px - t.buy_price) / t.buy_price * 100.0, 2)

                        # 自动结算判定
                        if curr_px >= t.take_profit_price:
                            t.status = "PROFIT"
                            t.exit_price = t.take_profit_price
                            t.exit_date = now_beijing()
                            t.exit_reason = "TARGET_HIT"
                            t.pnl_pct = round((t.take_profit_price - t.buy_price) / t.buy_price * 100.0, 2)
                            db.commit()
                        elif curr_px <= t.stop_loss_price:
                            t.status = "LOSS"
                            t.exit_price = min(curr_px, t.stop_loss_price)
                            t.exit_date = now_beijing()
                            t.exit_reason = "STOP_HIT"
                            t.pnl_pct = round((t.exit_price - t.buy_price) / t.buy_price * 100.0, 2)
                            db.commit()
                except Exception:
                    pass

            results.append({
                "id": t.id,
                "stock_code": t.stock_code,
                "stock_name": t.stock_name,
                "market": t.market,
                "buy_price": t.buy_price,
                "buy_date": t.buy_date.strftime("%Y-%m-%d %H:%M") if t.buy_date else None,
                "position_pct": t.position_pct,
                "take_profit_price": t.take_profit_price,
                "stop_loss_price": t.stop_loss_price,
                "status": t.status,
                "current_price": curr_px,
                "unrealized_pnl_pct": unrealized_pct,
                "exit_price": t.exit_price,
                "exit_date": t.exit_date.strftime("%Y-%m-%d %H:%M") if t.exit_date else None,
                "exit_reason": t.exit_reason,
                "pnl_pct": t.pnl_pct,
                "created_at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else None,
            })

        return results

    def get_summary(self, db: Session) -> Dict:
        """统计模拟盘实战胜率与累计盈亏"""
        trades = db.query(PaperTrade).all()
        settled = [t for t in trades if t.status in ("PROFIT", "LOSS", "CLOSED") and t.pnl_pct is not None]
        wins = [t for t in settled if t.status == "PROFIT" or (t.pnl_pct and t.pnl_pct > 0)]
        open_trades = [t for t in trades if t.status == "OPEN"]

        win_rate = (len(wins) / len(settled) * 100.0) if settled else 0.0
        total_pnl = sum(t.pnl_pct for t in settled if t.pnl_pct is not None)

        return {
            "total_settled_trades": len(settled),
            "win_trades": len(wins),
            "loss_trades": len(settled) - len(wins),
            "win_rate_pct": round(win_rate, 2),
            "total_realized_pnl_pct": round(total_pnl, 2),
            "open_trades_count": len(open_trades),
        }

    def close_trade_manually(self, trade_id: int, db: Session) -> Optional[Dict]:
        """手动平仓"""
        t = db.query(PaperTrade).filter(PaperTrade.id == trade_id).first()
        if not t:
            return None
        if t.status == "OPEN":
            try:
                q = self.stock_service.get_realtime_quote(t.stock_code, t.market)
                exit_px = float(q["price"]) if q and q.get("price") else t.buy_price
            except Exception:
                exit_px = t.buy_price

            pnl = round((exit_px - t.buy_price) / t.buy_price * 100.0, 2)
            t.status = "PROFIT" if pnl > 0 else "LOSS"
            t.exit_price = round(exit_px, 2)
            t.exit_date = now_beijing()
            t.exit_reason = "MANUAL_CLOSE"
            t.pnl_pct = pnl
            db.commit()
            db.refresh(t)
        return {"id": t.id, "status": t.status, "pnl_pct": t.pnl_pct}

    def delete_trade(self, trade_id: int, db: Session) -> bool:
        """删除模拟交易记录"""
        t = db.query(PaperTrade).filter(PaperTrade.id == trade_id).first()
        if not t:
            return False
        db.delete(t)
        db.commit()
        return True
