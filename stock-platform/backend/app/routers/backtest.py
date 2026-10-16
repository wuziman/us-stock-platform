"""
回测相关API
"""

from fastapi import APIRouter, HTTPException
from ..schemas import (BacktestRequest, BacktestResponse, BacktestCompareRequest,
                       OptimizeRequest, WalkForwardRequest)
from ..services.backtest_service import BacktestService
from ..utils.market import detect_market

router = APIRouter(prefix="/api/backtest", tags=["backtest"])
backtest_service = BacktestService()


@router.get("/strategies")
async def get_strategies():
    """获取可用策略列表"""
    return {
        "strategies": [
            {
                "id": "linear",
                "name": "线性策略",
                "description": "斐波那契回撤买入，15%止盈/8%止损"
            },
            {
                "id": "nonlinear",
                "name": "非线性策略",
                "description": "均线+RSI买入，46%止盈/8%止损"
            },
            {
                "id": "ma_cross",
                "name": "双均线交叉",
                "description": "MA20/MA50金叉死叉"
            },
            {
                "id": "macd",
                "name": "MACD策略",
                "description": "MACD金叉死叉"
            }
        ]
    }


@router.post("", response_model=BacktestResponse)
def run_backtest(request: BacktestRequest):
    """运行策略回测"""
    # 确定市场类型
    market = detect_market(request.stock_code)

    result = backtest_service.run_backtest(
        stock_code=request.stock_code,
        strategy=request.strategy,
        market=market,
        start_date=request.start_date,
        end_date=request.end_date,
        initial_capital=request.initial_capital,
        period=request.period,
        commission_per_trade=request.commission_per_trade,
        trailing_stop=request.trailing_stop,
        staged_entry=request.staged_entry,
        params=request.params,
    )

    if 'error' in result:
        raise HTTPException(status_code=400, detail=result['error'])

    return result


@router.post("/compare")
def run_backtest_compare(request: BacktestCompareRequest):
    """一键对比4个策略 + 买入持有基准（数据只拉取一次）"""
    # 确定市场类型（纯数字或带后缀点 = A股）
    market = detect_market(request.stock_code)

    result = backtest_service.run_compare(
        stock_code=request.stock_code,
        market=market,
        period=request.period,
        initial_capital=request.initial_capital,
        commission_per_trade=request.commission_per_trade,
        trailing_stop=request.trailing_stop,
        staged_entry=request.staged_entry,
    )

    if 'error' in result:
        raise HTTPException(status_code=400, detail=result['error'])

    return result


@router.post("/optimize")
def optimize_strategy(request: OptimizeRequest):
    """🎯 参数网格寻优：数据只拉一次，遍历参数网格，返回最优参数/全部结果/热力图矩阵"""
    # 确定市场类型（纯数字或带后缀点 = A股）
    market = detect_market(request.stock_code)

    result = backtest_service.optimize(
        stock_code=request.stock_code,
        strategy=request.strategy,
        market=market,
        period=request.period,
        initial_capital=request.initial_capital,
        commission_per_trade=request.commission_per_trade,
        metric=request.metric,
        param_grid=request.param_grid,
    )

    if 'error' in result:
        raise HTTPException(status_code=400, detail=result['error'])

    return result


@router.post("/walkforward")
def walk_forward(request: WalkForwardRequest):
    """🔬 Walk-Forward验证：训练窗选参→样本外测试，拼接OOS净值曲线 vs OOS买入持有"""
    # 确定市场类型（纯数字或带后缀点 = A股）
    market = detect_market(request.stock_code)

    result = backtest_service.walk_forward(
        stock_code=request.stock_code,
        strategy=request.strategy,
        market=market,
        period=request.period,
        initial_capital=request.initial_capital,
        commission_per_trade=request.commission_per_trade,
        train_ratio=request.train_ratio,
        segments=request.segments,
    )

    if 'error' in result:
        raise HTTPException(status_code=400, detail=result['error'])

    return result
