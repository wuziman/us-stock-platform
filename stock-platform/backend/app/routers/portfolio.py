"""
持仓与实盘风控决策链API（路由层）
核心领域逻辑已下沉至 app/services/portfolio_service.py，本文件负责 HTTP 路由与参数装配。
"""

from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Position, Setting
from ..models_platform import PortfolioSnapshot
from ..schemas import (
    PositionAddTrancheRequest,
    PositionCreate,
    PositionResponse,
    PositionSell,
    PositionUpdate,
    RiskSwitchesUpdate,
)
from ..services.alert_service import (
    KEY_REGIME_LINKAGE_ENABLED,
    KEY_SIZING_MODE,
    KEY_STAGED_ADD_ENABLED,
    KEY_TRAILING_STOP_ENABLED,
    compute_drawdown_stats,
    get_risk_switches,
)
from ..services.portfolio_service import (
    ADD_TRANCHE_THRESHOLDS,
    DEFAULT_RISK_PER_TRADE,
    MAX_ADD_TRANCHES,
    THEORETICAL_TARGET_STOP_RR,
    _enrich_holding,
    _get_total_capital,
    _get_usd_cny_rate,
    _realized_pnl,
    _to_naive_dt,
    calculate_recommended_position,
    clear_l2_kelly_cache,  # noqa: F401
    compute_portfolio_summary,
    consolidate_duplicate_holdings,
)
from ..services.report_service import ReportService
from ..services.stock_service import StockService
from ..utils.market import detect_market
from ..utils.price_levels import STOP_PCT
from ..utils.timezone import now_beijing

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])
stock_service = StockService()


def _fetch_quotes(positions: List[Position]) -> list:
    """并行拉取全部持仓实时行情"""
    if not positions:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(positions))) as executor:
        return list(executor.map(
            lambda p: stock_service.get_realtime_quote(p.stock_code, detect_market(p.stock_code)),
            positions
        ))


@router.get("/settings/risk-switches")
def get_risk_switches_endpoint(db: Session = Depends(get_db)):
    """获取实盘风控增强 4 开关配置（默认全部开启 Default Risk-Control On）"""
    return get_risk_switches(db)


@router.put("/settings/risk-switches")
def update_risk_switches_endpoint(req: RiskSwitchesUpdate, db: Session = Depends(get_db)):
    """更新实盘风控增强 4 开关配置"""
    updates = {}
    if req.sizing_mode is not None:
        updates[KEY_SIZING_MODE] = req.sizing_mode
    if req.trailing_stop_enabled is not None:
        updates[KEY_TRAILING_STOP_ENABLED] = "true" if req.trailing_stop_enabled else "false"
    if req.staged_add_enabled is not None:
        updates[KEY_STAGED_ADD_ENABLED] = "true" if req.staged_add_enabled else "false"
    if req.regime_linkage_enabled is not None:
        updates[KEY_REGIME_LINKAGE_ENABLED] = "true" if req.regime_linkage_enabled else "false"

    for k, v in updates.items():
        row = db.query(Setting).filter(Setting.key == k).first()
        if row:
            row.value = v
        else:
            db.add(Setting(key=k, value=v))
    if updates:
        db.commit()
    return {"message": "风控开关已更新", **get_risk_switches(db)}


@router.get("/position-size")
def get_position_size(
    buy_price: float = Query(..., gt=0),
    stop_loss: Optional[float] = Query(None),
    total_capital: Optional[float] = Query(None),
    risk_per_trade: float = Query(DEFAULT_RISK_PER_TRADE, gt=0, le=0.2),
    win_rate: Optional[float] = Query(None),
    reward_risk_ratio: Optional[float] = Query(None),
    stock_code: Optional[str] = Query(None),
    strategy: str = Query("linear"),
    total_score: Optional[float] = Query(None),
    sizing_mode: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    基于 min(2%定额风险, 半凯利, 评分分档) × 大盘风控系数 计算科学建仓股数与首仓/三档补仓方案。
    受 Setting 表中的 sizing_mode 与 regime_linkage_enabled 开关实时控制。
    """
    def _q(val, default=None):
        return getattr(val, "default", default) if type(val).__name__ in ("Query", "FieldInfo") else val

    stop_loss = _q(stop_loss)
    total_capital = _q(total_capital)
    risk_per_trade = float(_q(risk_per_trade, DEFAULT_RISK_PER_TRADE) or DEFAULT_RISK_PER_TRADE)
    win_rate = _q(win_rate)
    reward_risk_ratio = _q(reward_risk_ratio)
    stock_code = _q(stock_code)
    strategy = str(_q(strategy, "linear") or "linear")
    total_score = _q(total_score)
    sizing_mode = _q(sizing_mode)

    cap = total_capital if (total_capital is not None and total_capital > 0) else _get_total_capital(db)
    switches = get_risk_switches(db)
    eff_mode = sizing_mode or str(switches.get("sizing_mode", "half_kelly_combined"))
    linkage_on = bool(switches.get("regime_linkage_enabled", True))

    regime_status = "NORMAL"
    regime_scale = 1.0
    if linkage_on:
        regime = ReportService().get_market_regime()
        regime_status = str(regime.get("status", "NORMAL"))
        regime_scale = float(regime.get("position_scale", 1.0))

    default_rr = THEORETICAL_TARGET_STOP_RR.get(strategy, THEORETICAL_TARGET_STOP_RR['linear'])
    return calculate_recommended_position(
        capital=cap,
        buy_price=buy_price,
        stop_loss=stop_loss,
        risk_per_trade=risk_per_trade,
        win_rate=win_rate,
        reward_risk_ratio=reward_risk_ratio if reward_risk_ratio is not None else default_rr,
        stock_code=stock_code,
        strategy=strategy,
        total_score=total_score,
        sizing_mode=eff_mode,
        regime_scale=regime_scale,
        regime_status=regime_status,
    )


@router.get("", response_model=List[PositionResponse])
def get_positions(db: Session = Depends(get_db)):
    """获取当前持仓列表（含实时盈亏 + 移动保本止损线 + 阶梯补仓状态机提示）"""
    positions = db.query(Position).filter(Position.status == "holding").all()
    quotes = _fetch_quotes(positions)
    switches = get_risk_switches(db)
    total_cap = _get_total_capital(db)
    return [_enrich_holding(pos, quote, switches=switches, total_capital=total_cap)
            for pos, quote in zip(positions, quotes)]


@router.get("/history", response_model=List[PositionResponse])
async def get_position_history(db: Session = Depends(get_db)):
    """已卖出持仓（历史交易，含已实现盈亏与持有天数）"""
    positions = db.query(Position).filter(Position.status == "sold") \
        .order_by(Position.sell_date.desc()).all()

    result = []
    for pos in positions:
        realized_pnl = None
        realized_pct = None
        days = None
        if pos.sell_price is not None:
            realized_pnl = (pos.sell_price - pos.buy_price) * pos.quantity
            realized_pct = (pos.sell_price - pos.buy_price) / pos.buy_price * 100 if pos.buy_price else 0
        sell_dt = _to_naive_dt(pos.sell_date)
        buy_dt = _to_naive_dt(pos.buy_date)
        if sell_dt and buy_dt:
            days = max((sell_dt - buy_dt).days, 0)

        result.append(PositionResponse(
            id=pos.id,
            stock_code=pos.stock_code,
            stock_name=pos.stock_name,
            market=pos.market,
            buy_price=pos.buy_price,
            initial_price=pos.initial_price or pos.buy_price,
            add_count=int(pos.add_count or 0),
            highest_price=pos.highest_price or pos.buy_price,
            quantity=pos.quantity,
            buy_date=buy_dt or now_beijing(),
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            status=pos.status,
            sell_price=pos.sell_price,
            sell_date=sell_dt,
            created_at=_to_naive_dt(pos.created_at) or now_beijing(),
            realized_pnl=round(realized_pnl, 2) if realized_pnl is not None else None,
            realized_pnl_pct=round(realized_pct, 2) if realized_pct is not None else None,
            holding_days=days,
        ))
    return result


@router.post("/{position_id}/add-tranche")
async def add_position_tranche(
    position_id: int,
    req: PositionAddTrancheRequest,
    db: Session = Depends(get_db),
):
    """原行阶梯补仓接口（In-Place Update）"""
    position = db.query(Position).filter(
        Position.id == position_id, Position.status == "holding"
    ).first()
    if not position:
        raise HTTPException(status_code=404, detail="持仓不存在或已卖出")

    add_cnt = int(position.add_count or 0)
    if add_cnt >= MAX_ADD_TRANCHES:
        raise HTTPException(
            status_code=400,
            detail=f"已达{MAX_ADD_TRANCHES}次阶梯补仓上限，严禁继续摊平",
        )

    initial_px = float(position.initial_price) if position.initial_price is not None else float(position.buy_price)
    required_drop_pct = ADD_TRANCHE_THRESHOLDS[add_cnt]
    trigger_px = round(initial_px * (1.0 - required_drop_pct / 100.0), 4)
    actual_drop_pct = (initial_px - req.add_price) / initial_px * 100.0 if initial_px > 0 else 0.0

    if not req.force and req.add_price > trigger_px + 1e-6:
        raise HTTPException(
            status_code=400,
            detail=(
                f"第{add_cnt + 1}档阶梯补仓要求较首仓价({initial_px:.2f})下跌至少{required_drop_pct:.0f}%"
                f"（触发价≤{trigger_px:.2f}，当前补仓价{req.add_price:.2f}仅下跌{actual_drop_pct:.1f}%）"
            ),
        )

    old_qty = int(position.quantity)
    old_buy = float(position.buy_price)
    old_stop = position.stop_loss

    new_qty = old_qty + int(req.add_quantity)
    new_buy = round((old_qty * old_buy + int(req.add_quantity) * float(req.add_price)) / new_qty, 4)

    if req.stop_loss is not None:
        if req.stop_loss >= new_buy:
            raise HTTPException(status_code=400, detail="止损位应低于补仓后新均价")
        new_stop = round(float(req.stop_loss), 2)
    else:
        new_stop = round(new_buy * STOP_PCT, 2)

    position.initial_price = initial_px
    position.buy_price = new_buy
    position.quantity = new_qty
    position.add_count = add_cnt + 1
    position.stop_loss = new_stop
    position.highest_price = max(float(position.highest_price or old_buy), float(req.add_price))
    db.commit()

    return {
        "message": f"第{position.add_count}次阶梯补仓成功",
        "position_id": position.id,
        "stock_code": position.stock_code,
        "initial_price": round(initial_px, 4),
        "previous_buy_price": round(old_buy, 4),
        "new_buy_price": new_buy,
        "previous_quantity": old_qty,
        "added_quantity": int(req.add_quantity),
        "new_quantity": new_qty,
        "add_count": position.add_count,
        "remaining_adds": MAX_ADD_TRANCHES - position.add_count,
        "previous_stop_loss": old_stop,
        "new_stop_loss": new_stop,
        "stop_loss_shift_note": (
            "补仓后纪律止损按新加权均价×0.92重算，原仓位绝对止损线随新均价被动下移"
            "（总敞口受单票40%上限与最多3次补仓硬上限约束）"
        ),
    }


@router.post("/{position_id}/sell")
async def sell_position(position_id: int, sell: PositionSell, db: Session = Depends(get_db)):
    """
    卖出持仓（支持全额清仓或分批止盈/部分减仓）：
    - 当 sell_quantity 未传或等于当前持仓股数时：全额清仓，status 转为 'sold'
    - 当 1 <= sell_quantity < position.quantity 时：原持仓股数扣减并保持 'holding'，
      同时拆分生成一条 status='sold' 的历史成交记录，实现分批止盈入账
    """
    position = db.query(Position).filter(
        Position.id == position_id, Position.status == "holding"
    ).first()
    if not position:
        raise HTTPException(status_code=404, detail="持仓不存在或已卖出")

    sell_qty = int(sell.sell_quantity) if sell.sell_quantity is not None else int(position.quantity)
    if sell_qty <= 0 or sell_qty > int(position.quantity):
        raise HTTPException(
            status_code=400,
            detail=f"卖出数量({sell_qty})不能超过当前持仓数量({position.quantity})",
        )

    sell_dt = _to_naive_dt(sell.sell_date)
    buy_dt = _to_naive_dt(position.buy_date)
    realized_pnl, realized_pct = _realized_pnl(sell.sell_price, position.buy_price, sell_qty)
    holding_days = max((sell_dt - buy_dt).days, 0) if (sell_dt and buy_dt) else 0

    if sell_qty < int(position.quantity):
        # 分批止盈/部分减仓：原持仓扣减股数，新建一条 sold 记录保存本批已实现盈亏
        position.quantity = int(position.quantity) - sell_qty
        sold_tranche = Position(
            stock_code=position.stock_code,
            stock_name=position.stock_name,
            market=position.market,
            buy_price=position.buy_price,
            initial_price=position.initial_price or position.buy_price,
            add_count=int(position.add_count or 0),
            highest_price=position.highest_price or position.buy_price,
            quantity=sell_qty,
            buy_date=buy_dt or position.buy_date,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            status="sold",
            sell_price=sell.sell_price,
            sell_date=sell_dt,
        )
        db.add(sold_tranche)
        db.commit()
        return {
            "message": "分批止盈/减仓成功",
            "partial": True,
            "sold_quantity": sell_qty,
            "remaining_quantity": int(position.quantity),
            "realized_pnl": realized_pnl,
            "realized_pnl_pct": realized_pct,
            "holding_days": holding_days,
        }

    # 全额清仓
    position.status = "sold"
    position.sell_price = sell.sell_price
    position.sell_date = sell_dt
    db.commit()

    return {
        "message": "卖出成功",
        "partial": False,
        "sold_quantity": sell_qty,
        "remaining_quantity": 0,
        "realized_pnl": realized_pnl,
        "realized_pnl_pct": realized_pct,
        "holding_days": holding_days,
    }


@router.post("", response_model=PositionResponse)
async def add_position(position: PositionCreate, db: Session = Depends(get_db)):
    """添加首仓持仓"""
    if position.stop_loss is not None and position.stop_loss >= position.buy_price:
        raise HTTPException(status_code=400, detail="止损位应低于买入价")
    if position.take_profit is not None and position.take_profit <= position.buy_price:
        raise HTTPException(status_code=400, detail="止盈位应高于买入价")

    existing_holding = db.query(Position).filter(
        Position.stock_code == position.stock_code,
        Position.status == "holding",
    ).first()
    if existing_holding is not None:
        raise HTTPException(
            status_code=400,
            detail=f"{position.stock_code} 已有持仓中记录(id={existing_holding.id})，请调用阶梯补仓接口 /api/portfolio/{existing_holding.id}/add-tranche",
        )

    data = position.model_dump()
    data['market'] = detect_market(position.stock_code)
    data['buy_date'] = _to_naive_dt(position.buy_date)
    data['initial_price'] = float(position.buy_price)
    data['add_count'] = 0
    data['highest_price'] = float(position.buy_price)
    if data.get('stop_loss') is None:
        data['stop_loss'] = round(float(position.buy_price) * STOP_PCT, 2)

    db_position = Position(**data)
    db.add(db_position)
    db.commit()
    db.refresh(db_position)

    return PositionResponse(
        id=db_position.id,
        stock_code=db_position.stock_code,
        stock_name=db_position.stock_name,
        market=db_position.market,
        buy_price=db_position.buy_price,
        initial_price=db_position.initial_price,
        add_count=int(db_position.add_count or 0),
        highest_price=db_position.highest_price,
        quantity=db_position.quantity,
        buy_date=db_position.buy_date,
        stop_loss=db_position.stop_loss,
        take_profit=db_position.take_profit,
        status=db_position.status,
        created_at=db_position.created_at,
    )


@router.put("/{position_id}")
async def update_position(position_id: int, update: PositionUpdate,
                          db: Session = Depends(get_db)):
    """修改持仓：买入价/数量/买入日期/止损止盈"""
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise HTTPException(status_code=404, detail="未找到")

    if update.buy_price is not None:
        if update.buy_price <= 0:
            raise HTTPException(status_code=400, detail="买入价格必须大于0")
        position.buy_price = update.buy_price
    if update.quantity is not None:
        if update.quantity < 1:
            raise HTTPException(status_code=400, detail="数量必须大于0")
        position.quantity = update.quantity
    if update.buy_date is not None:
        position.buy_date = _to_naive_dt(update.buy_date)
    for field in ("stop_loss", "take_profit"):
        if field in update.model_fields_set:
            setattr(position, field, getattr(update, field))

    db.commit()
    return {"message": "更新成功"}


@router.delete("/{position_id}")
async def delete_position(position_id: int, db: Session = Depends(get_db)):
    """删除记录"""
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise HTTPException(status_code=404, detail="未找到")

    db.delete(position)
    db.commit()
    return {"message": "删除成功"}


@router.get("/settings/total_capital")
async def get_total_capital(db: Session = Depends(get_db)):
    return {"total_capital": _get_total_capital(db)}


@router.put("/settings/total_capital")
async def set_total_capital(total_capital: float, db: Session = Depends(get_db)):
    if total_capital < 0:
        raise HTTPException(status_code=400, detail="总资金不能为负")
    row = db.query(Setting).filter(Setting.key == "total_capital").first()
    if row:
        row.value = str(total_capital)
    else:
        db.add(Setting(key="total_capital", value=str(total_capital)))
    db.commit()
    return {"message": "保存成功", "total_capital": total_capital}


@router.get("/settings/usd_cny_rate")
async def get_usd_cny_rate_endpoint(db: Session = Depends(get_db)):
    """读取跨市场折算汇率（USD/CNY）"""
    return {"usd_cny_rate": _get_usd_cny_rate(db)}


@router.put("/settings/usd_cny_rate")
async def set_usd_cny_rate_endpoint(usd_cny_rate: float = Query(..., gt=0, le=50), db: Session = Depends(get_db)):
    """更新跨市场折算汇率（USD/CNY）"""
    row = db.query(Setting).filter(Setting.key == "usd_cny_rate").first()
    if row:
        row.value = str(round(float(usd_cny_rate), 4))
    else:
        db.add(Setting(key="usd_cny_rate", value=str(round(float(usd_cny_rate), 4))))
    db.commit()
    return {"message": "汇率保存成功", "usd_cny_rate": _get_usd_cny_rate(db)}


@router.get("/summary")
def get_portfolio_summary(db: Session = Depends(get_db)):
    """持仓汇总 + 仓位集中度检查"""
    return compute_portfolio_summary(db=db, fetch_quotes_fn=_fetch_quotes)


@router.get("/equity-curve")
def get_equity_curve(db: Session = Depends(get_db)):
    """组合净值曲线（每日快照）+ 回撤统计"""
    snaps = db.query(PortfolioSnapshot).order_by(PortfolioSnapshot.snap_date.asc()).all()
    curve = [{'date': s.snap_date, 'value': s.total_value} for s in snaps]
    stats = compute_drawdown_stats([s.total_value for s in snaps])
    return {'curve': curve, **stats}


@router.post("/consolidate-duplicates")
def consolidate_duplicates_endpoint(db: Session = Depends(get_db)):
    """手动触发存量重复 holding 持仓行检查与原地合并"""
    return consolidate_duplicate_holdings(db)
