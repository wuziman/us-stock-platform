"""
数据库模型（所有时间列统一存储无时区 naive 北京时间 Asia/Shanghai，详见 TIMEZONE.md）
"""

from sqlalchemy import Column, Integer, String, Float, DateTime, JSON
from .database import Base
from .utils.timezone import now_beijing


class Watchlist(Base):
    """自选股列表"""
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, index=True)
    stock_code = Column(String(20), nullable=False, index=True)
    stock_name = Column(String(50), nullable=False)
    market = Column(String(10), nullable=False)  # 'A' or 'US'
    created_at = Column(DateTime, default=now_beijing)


class AnalysisHistory(Base):
    """分析历史记录"""
    __tablename__ = "analysis_history"

    id = Column(Integer, primary_key=True, index=True)
    stock_code = Column(String(20), nullable=False, index=True)
    stock_name = Column(String(50), nullable=False)
    tech_score = Column(Float)
    news_score = Column(Float)
    macro_score = Column(Float)
    event_score = Column(Float)
    total_score = Column(Float)
    recommendation = Column(String(20))
    price_levels = Column(JSON)
    details = Column(JSON)
    created_at = Column(DateTime, default=now_beijing)


class Position(Base):
    """持仓记录（支持原行阶梯补仓状态机与移动保本止损跟踪）"""
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, index=True)
    stock_code = Column(String(20), nullable=False, index=True)
    stock_name = Column(String(50), nullable=False)
    market = Column(String(10), nullable=False)
    buy_price = Column(Float, nullable=False)       # 当前加权平均持仓成本价
    initial_price = Column(Float)                   # 首次建仓基准价（5%/10%/15%三档补仓永久锚定此价格，不随摊平下移）
    add_count = Column(Integer, default=0)          # 已执行阶梯补仓次数（0~3，硬上限3次）
    highest_price = Column(Float)                   # 建仓以来最高价（用于+10%保本上移与最高价回落8%移动止损）
    quantity = Column(Integer, nullable=False)
    buy_date = Column(DateTime, nullable=False)
    stop_loss = Column(Float)
    take_profit = Column(Float)
    status = Column(String(20), default="holding")  # holding, sold, merged（被合并保留原始税基与复盘记录）
    sell_price = Column(Float)                      # 卖出价格（sold时填写）
    sell_date = Column(DateTime)                    # 卖出日期（naive 北京时间）
    created_at = Column(DateTime, default=now_beijing)


class Setting(Base):
    """平台设置（键值对，如总资金、汇率与实盘风控增强四开关）"""
    __tablename__ = "settings"

    key = Column(String(50), primary_key=True)
    value = Column(String(200))
