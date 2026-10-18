"""
评分追踪模型：每次分析成功后自动落库，用于验证评分体系的预测力
"""

from sqlalchemy import Column, Integer, String, Float, DateTime

from .database import Base
from .utils.timezone import now_beijing


class ScoreHistory(Base):
    """评分快照（分析即落库，与手动保存的AnalysisHistory互不影响）"""
    __tablename__ = "score_history"

    id = Column(Integer, primary_key=True, index=True)
    stock_code = Column(String(20), nullable=False, index=True)
    stock_name = Column(String(50))
    market = Column(String(10))  # 'A' or 'US'
    tech_score = Column(Float)
    news_score = Column(Float)
    macro_score = Column(Float)
    event_score = Column(Float)
    total_score = Column(Float)
    recommendation = Column(String(20))
    price_at_score = Column(Float)  # 打分时的股价（计算后续收益的基准之一）
    created_at = Column(DateTime, default=now_beijing, index=True)
