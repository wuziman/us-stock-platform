"""
Pydantic模型
"""

from datetime import date, datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, ConfigDict


# 自选股
class WatchlistCreate(BaseModel):
    stock_code: str
    stock_name: str
    market: str


class WatchlistResponse(BaseModel):
    id: int
    stock_code: str
    stock_name: str
    market: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# 分析结果
class AnalysisRequest(BaseModel):
    stock_code: str
    stock_name: str
    mode: str = "simple"  # simple or research


class EarningsRadar(BaseModel):
    date: Optional[str] = None
    days_away: Optional[int] = None
    tag: str = ""
    is_imminent: bool = False


class MarketRegimeResponse(BaseModel):
    vix: float
    qqq_change: float
    status: str
    banner: str
    advice: str


class AnalysisResponse(BaseModel):
    stock_code: str
    stock_name: str
    scores: Dict[str, float]
    recommendation: Dict[str, str]
    price_levels: Dict[str, Any]
    details: Dict[str, Any]
    earnings_radar: Optional[EarningsRadar] = None


# 持仓
class PositionCreate(BaseModel):
    stock_code: str
    stock_name: str
    # 市场判定以后端 detect_market 为准，此字段仅为兼容旧客户端保留
    market: str = "US"
    buy_price: float
    quantity: int
    buy_date: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


class PositionSell(BaseModel):
    """卖出持仓"""
    sell_price: float
    sell_date: datetime


class PositionUpdate(BaseModel):
    """修改持仓：买入价/数量/买入日期/止损止盈（None=不修改）"""
    buy_price: Optional[float] = None
    quantity: Optional[int] = None
    buy_date: Optional[date] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


class PositionResponse(BaseModel):
    id: int
    stock_code: str
    stock_name: str
    market: str
    buy_price: float
    quantity: int
    buy_date: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    status: str = "holding"
    sell_price: Optional[float] = None
    sell_date: Optional[datetime] = None
    created_at: datetime
    current_price: Optional[float] = None
    profit_loss: Optional[float] = None
    profit_loss_pct: Optional[float] = None
    # 已实现盈亏（sold时）
    realized_pnl: Optional[float] = None
    realized_pnl_pct: Optional[float] = None
    holding_days: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


# 回测
class BacktestRequest(BaseModel):
    stock_code: str
    strategy: str  # linear, nonlinear, ma_cross, macd
    period: str = "1y"  # 1y, 3y, 5y
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_capital: float = 100000
    commission_per_trade: float = 1.0  # 每笔交易手续费（美元）
    trailing_stop: bool = False        # 是否启用移动止盈与浮盈+10%保本损
    staged_entry: bool = False         # 是否启用分批金字塔建仓（首仓50%+回撤5%补仓50%）
    params: Optional[Dict[str, float]] = None  # 可选策略自定义参数（如寻优得到的最优tp/sl或fast/slow）


class BacktestResponse(BaseModel):
    stock_code: str
    strategy: str
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe_ratio: float
    win_rate: float
    trade_count: int = 0
    total_fees: float = 0.0  # 总手续费
    commission_per_trade: float = 1.0  # 单笔手续费
    initial_capital: float = 100000
    final_value: Optional[float] = None
    equity_curve: List[Dict[str, Any]] = []
    buy_hold_curve: List[Dict[str, Any]] = []  # 买入持有基准曲线（同日期轴）
    buy_hold_return: Optional[float] = None  # 买入持有总收益%
    period: str = "1y"
    trades: List[Dict[str, Any]]


class BacktestCompareRequest(BaseModel):
    """一键对比4策略请求"""
    stock_code: str
    period: str = "1y"
    initial_capital: float = 100000
    commission_per_trade: float = 1.0
    trailing_stop: bool = False
    staged_entry: bool = False


class OptimizeRequest(BaseModel):
    """策略参数网格寻优请求"""
    stock_code: str
    strategy: str  # linear, nonlinear, ma_cross, macd
    period: str = "1y"
    initial_capital: float = 100000
    commission_per_trade: float = 1.0
    metric: str = "sharpe"  # 排序指标: sharpe/total_return/annual_return/max_drawdown/win_rate/trade_count
    param_grid: Optional[Dict[str, List[float]]] = None  # 覆盖内置网格的部分取值


class WalkForwardRequest(BaseModel):
    """Walk-Forward滚动验证请求"""
    stock_code: str
    strategy: str
    period: str = "5y"
    initial_capital: float = 100000
    commission_per_trade: float = 1.0
    train_ratio: float = 0.6  # 训练窗占测试块之前历史的比例（0.1~1.0）
    segments: int = 2  # 切分段数（服务端限1~4防超时）
