"""
模拟实战记账与前向胜率跟踪 API 路由
- POST   /api/paper-trading/open      一键模拟记账开仓
- GET    /api/paper-trading/positions 模拟持仓与历史记录（实时结算）
- GET    /api/paper-trading/summary   模拟盘实战胜率与累计收益统计
- POST   /api/paper-trading/{id}/close 手动平仓
- DELETE /api/paper-trading/{id}      删除记录
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import PaperTradeOpenRequest
from ..services.paper_trading_service import PaperTradingService

router = APIRouter(prefix="/api/paper-trading", tags=["paper-trading"])
paper_service = PaperTradingService()


@router.post("/open")
def open_paper_trade(req: PaperTradeOpenRequest, db: Session = Depends(get_db)):
    """从 5 数操作卡一键记录模拟开仓"""
    try:
        trade = paper_service.open_trade(req, db)
        return {"success": True, "trade_id": trade.id, "message": f"成功记录模拟买入 {trade.stock_code}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模拟开仓失败: {e}")


@router.get("/positions")
def get_paper_positions(db: Session = Depends(get_db)):
    """获取模拟盘全部持仓与历史战绩（自动按实时价结算）"""
    items = paper_service.get_trades_with_settlement(db)
    return {"items": items}


@router.get("/summary")
def get_paper_summary(db: Session = Depends(get_db)):
    """获取模拟盘实战胜率与累计盈亏概览"""
    return paper_service.get_summary(db)


@router.post("/{trade_id}/close")
def close_paper_trade(trade_id: int, db: Session = Depends(get_db)):
    """手动平仓指定模拟单"""
    res = paper_service.close_trade_manually(trade_id, db)
    if not res:
        raise HTTPException(status_code=404, detail="未找到该模拟单或已平仓")
    return {"success": True, "data": res}


@router.delete("/{trade_id}")
def delete_paper_trade(trade_id: int, db: Session = Depends(get_db)):
    """删除指定模拟单"""
    ok = paper_service.delete_trade(trade_id, db)
    if not ok:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"success": True}
