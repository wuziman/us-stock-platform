"""
Pydantic模型
"""

from datetime import datetime
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, ConfigDict, Field


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
    mode: Literal["simple", "research", "adaptive"] = "simple"


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


class TradeActionCard(BaseModel):
    action_type: str = "WAIT_AND_SEE"
    action_label: str = "观望不买"
    current_price: float = 0.0
    buy_price: float = 0.0
    position_pct: str = "0%"
    take_profit_price: float = 0.0
    stop_loss_price: float = 0.0
    one_sentence_reason: str = ""
    risk_reward_ratio: str = "1.88:1"
    # 周线双底座分级操盘体系（战役二接入）
    buy_tier1: Optional[float] = 0.0
    buy_tier2: Optional[float] = 0.0
    tier1_position_pct: str = "0%"
    tier2_position_pct: str = "0%"
    total_position_pct: str = "0%"
    priority_score: Optional[float] = None


class AnalysisResponse(BaseModel):
    stock_code: str
    stock_name: str
    market: str = "US"
    scores: Dict[str, float]
    recommendation: Dict[str, str]
    price_levels: Dict[str, Any]
    details: Dict[str, Any]
    earnings_radar: Optional[EarningsRadar] = None
    regime_adaptive: Optional[Dict[str, Any]] = None
    action_card: Optional[TradeActionCard] = None


# 模拟实战记账
class PaperTradeOpenRequest(BaseModel):
    stock_code: str
    stock_name: str
    market: str = "US"
    buy_price: float = Field(..., gt=0)
    position_pct: str = "20%"
    take_profit_price: float = Field(..., gt=0)
    stop_loss_price: float = Field(..., gt=0)


class PaperTradeResponse(BaseModel):
    id: int
    stock_code: str
    stock_name: Optional[str] = None
    market: str = "US"
    buy_price: float
    buy_date: datetime
    position_pct: str
    take_profit_price: float
    stop_loss_price: float
    status: str
    current_price: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None
    exit_price: Optional[float] = None
    exit_date: Optional[datetime] = None
    exit_reason: Optional[str] = None
    pnl_pct: Optional[float] = None
    created_at: Optional[datetime] = None


# 持仓
class PositionCreate(BaseModel):
    stock_code: str
    stock_name: str
    # 市场判定以后端 detect_market 为准，此字段仅为兼容旧客户端保留
    market: str = "US"
    buy_price: float = Field(..., gt=0)
    quantity: int = Field(..., ge=1)
    buy_date: datetime
    stop_loss: Optional[float] = Field(None, gt=0)
    take_profit: Optional[float] = Field(None, gt=0)


class PositionAddTrancheRequest(BaseModel):
    """
    原行阶梯补仓请求：
    - 严格在原 holding 持仓行原地更新，不新建持仓行
    - 跌幅阈值（第1档-5%、第2档-10%、第3档-15%）永久锚定首次建仓价 initial_price
    - 补仓后默认按新加权均价×0.92重算纪律止损（原仓位绝对止损线随新均价同步下移）
    """
    add_price: float = Field(..., gt=0)
    add_quantity: int = Field(..., ge=1)
    stop_loss: Optional[float] = Field(None, gt=0)
    strategy_type: Literal["linear", "nonlinear"] = "linear"
    force: bool = False  # True=允许未达档位跌幅时手动强制补仓（仍受最多3次硬上限约束）


class RiskSwitchesUpdate(BaseModel):
    """实盘风控增强四开关更新请求（默认全部开启 Default Risk-Control On）"""
    sizing_mode: Optional[Literal["half_kelly_combined", "fixed_2pct"]] = None
    trailing_stop_enabled: Optional[bool] = None
    staged_add_enabled: Optional[bool] = None
    regime_linkage_enabled: Optional[bool] = None


class PositionSell(BaseModel):
    """卖出持仓（支持全仓卖出或传入 sell_quantity 分批部分止盈/减仓）"""
    sell_price: float = Field(..., gt=0)
    sell_date: datetime
    sell_quantity: Optional[int] = Field(None, ge=1)


class PositionUpdate(BaseModel):
    """修改持仓：买入价/数量/买入日期/止损止盈（None=不修改；buy_date与ORM列同为datetime）"""
    buy_price: Optional[float] = Field(None, gt=0)
    quantity: Optional[int] = Field(None, ge=1)
    buy_date: Optional[datetime] = None
    stop_loss: Optional[float] = Field(None, gt=0)
    take_profit: Optional[float] = Field(None, gt=0)


class PositionResponse(BaseModel):
    id: int
    stock_code: str
    stock_name: str
    market: str
    buy_price: float
    initial_price: Optional[float] = None
    add_count: int = 0
    highest_price: Optional[float] = None
    quantity: int
    buy_date: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_stop_price: Optional[float] = None
    effective_stop_loss: Optional[float] = None
    add_position_signal: Optional[Dict[str, Any]] = None
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
    initial_capital: float = Field(100000, gt=0)
    commission_per_trade: float = Field(1.0, ge=0)  # 每笔交易手续费（美元，禁止负数刷收益）
    slippage_bps: float = Field(0.0, ge=0.0, le=500.0)  # 单边滑点基点（默认0，10 bps = 0.10%）
    trailing_stop: bool = False        # 是否启用移动止盈与浮盈+10%保本损
    staged_entry: bool = False         # 是否启用分批金字塔建仓（首仓50%+回撤5%补仓50%）
    params: Optional[Dict[str, float]] = None  # 可选策略自定义参数


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
    slippage_bps: float = 0.0  # 单边滑点基点
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
    initial_capital: float = Field(100000, gt=0)
    commission_per_trade: float = Field(1.0, ge=0)
    slippage_bps: float = Field(0.0, ge=0.0, le=500.0)
    trailing_stop: bool = False
    staged_entry: bool = False


class OptimizeRequest(BaseModel):
    """策略参数网格寻优请求"""
    stock_code: str
    strategy: str  # linear, nonlinear, ma_cross, macd
    period: str = "1y"
    initial_capital: float = Field(100000, gt=0)
    commission_per_trade: float = Field(1.0, ge=0)
    metric: str = "sharpe"  # 排序指标: sharpe/total_return/annual_return/max_drawdown/win_rate/trade_count
    param_grid: Optional[Dict[str, List[float]]] = None  # 覆盖内置网格的部分取值


class WalkForwardRequest(BaseModel):
    """Walk-Forward滚动验证请求"""
    stock_code: str
    strategy: str
    period: str = "5y"
    initial_capital: float = Field(100000, gt=0)
    commission_per_trade: float = Field(1.0, ge=0)
    train_ratio: float = Field(0.6, ge=0.1, le=1.0)  # 训练窗占测试块之前历史的比例（0.1~1.0）
    segments: int = Field(2, ge=1, le=4)  # 切分段数（服务端限1~4防超时）
