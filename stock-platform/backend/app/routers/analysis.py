"""
分析相关API
"""

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from datetime import timedelta
from typing import List, Optional
from ..database import get_db, SessionLocal
from ..utils.timezone import now_beijing
from ..models import AnalysisHistory
from ..models_tracking import ScoreHistory
from ..schemas import AnalysisRequest, AnalysisResponse
from ..services.analysis_service import AnalysisService
from ..services.score_tracking_service import build_tracking, build_weight_health_report
from ..services.stock_service import StockService
from ..utils.market import detect_market

router = APIRouter(prefix="/api/analysis", tags=["analysis"])
analysis_service = AnalysisService()

ANALYSIS_HISTORY_RETENTION_DAYS = 90


def cleanup_expired_analysis_history(
    max_age_days: int = ANALYSIS_HISTORY_RETENTION_DAYS,
    db: Optional[Session] = None,
) -> int:
    """清理超过 max_age_days 天（默认 90 天）的陈旧 AnalysisHistory 记录（SQL 条件批量删除，避免全表大 JSON 拉入内存）"""
    cutoff = now_beijing() - timedelta(days=max_age_days)
    own_session = db is None
    sess = db or SessionLocal()
    try:
        removed = (
            sess.query(AnalysisHistory)
            .filter(AnalysisHistory.created_at.isnot(None), AnalysisHistory.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        if removed > 0:
            sess.commit()
        return int(removed or 0)
    except Exception as e:
        sess.rollback()
        print(f"清理过期AnalysisHistory失败(忽略): {e}")
        return 0
    finally:
        if own_session:
            sess.close()



@router.post("", response_model=AnalysisResponse)
def analyze_stock(request: AnalysisRequest, db: Session = Depends(get_db)):
    """综合分析股票"""
    # 确定市场类型
    market = detect_market(request.stock_code)

    result = analysis_service.analyze_stock(
        stock_code=request.stock_code,
        stock_name=request.stock_name,
        market=market,
        mode=request.mode
    )

    if 'error' in result:
        raise HTTPException(status_code=400, detail=result['error'])

    # 评分追踪：分析成功后自动落库（失败不影响分析响应）
    try:
        scores = result.get('scores') or {}
        db.add(ScoreHistory(
            stock_code=result.get('stock_code', request.stock_code),
            stock_name=result.get('stock_name'),
            market=market,
            tech_score=scores.get('technical'),
            news_score=scores.get('news'),
            macro_score=scores.get('macro'),
            event_score=scores.get('event'),
            total_score=scores.get('total'),
            recommendation=(result.get('recommendation') or {}).get('level'),
            price_at_score=(result.get('price_levels') or {}).get('current_price'),
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"评分追踪落库失败: {e}")

    # 分析历史自动落库（与 /save 同构）。此前只有手动 save 接口写这张表，
    # 前端"已自动落库"的注释与事实不符，分析历史/最近分析列表因此恒为空
    try:
        db.add(AnalysisHistory(
            stock_code=result.get('stock_code', request.stock_code),
            stock_name=result.get('stock_name'),
            tech_score=scores.get('technical'),
            news_score=scores.get('news'),
            macro_score=scores.get('macro'),
            event_score=scores.get('event'),
            total_score=scores.get('total'),
            recommendation=(result.get('recommendation') or {}).get('level'),
            price_levels=result.get('price_levels'),
            details=result.get('details'),
        ))
        db.commit()
        cleanup_expired_analysis_history(max_age_days=ANALYSIS_HISTORY_RETENTION_DAYS, db=db)
    except Exception as e:
        db.rollback()
        print(f"分析历史落库失败: {e}")

    return result


@router.post("/save")
async def save_analysis(result: AnalysisResponse, db: Session = Depends(get_db)):
    """保存分析结果"""
    history = AnalysisHistory(
        stock_code=result.stock_code,
        stock_name=result.stock_name,
        tech_score=result.scores.get('technical'),
        news_score=result.scores.get('news'),
        macro_score=result.scores.get('macro'),
        event_score=result.scores.get('event'),
        total_score=result.scores.get('total'),
        recommendation=result.recommendation.get('level'),
        price_levels=result.price_levels,
        details=result.details
    )
    db.add(history)
    db.commit()
    db.refresh(history)
    return {"id": history.id, "message": "保存成功"}


@router.get("/history", response_model=List[dict])
async def get_analysis_history(
    stock_code: Optional[str] = None,
    limit: int = 20,
    db: Session = Depends(get_db)
):
    """获取分析历史"""
    query = db.query(AnalysisHistory)

    if stock_code:
        query = query.filter(AnalysisHistory.stock_code == stock_code)

    history = query.order_by(AnalysisHistory.created_at.desc()).limit(limit).all()

    return [{
        "id": h.id,
        "stock_code": h.stock_code,
        "stock_name": h.stock_name,
        "market": detect_market(h.stock_code),
        "total_score": h.total_score,
        "recommendation": h.recommendation,
        "created_at": h.created_at
    } for h in history]


@router.get("/history/{history_id}")
async def get_analysis_detail(history_id: int, db: Session = Depends(get_db)):
    """获取分析详情"""
    history = db.query(AnalysisHistory).filter(AnalysisHistory.id == history_id).first()
    if not history:
        raise HTTPException(status_code=404, detail="未找到")

    return {
        "id": history.id,
        "stock_code": history.stock_code,
        "stock_name": history.stock_name,
        "scores": {
            "technical": history.tech_score,
            "news": history.news_score,
            "macro": history.macro_score,
            "event": history.event_score,
            "total": history.total_score
        },
        "recommendation": history.recommendation,
        "price_levels": history.price_levels,
        "details": history.details,
        "created_at": history.created_at
    }


@router.get("/tracking")
def get_score_tracking(stock_code: str, db: Session = Depends(get_db)):
    """评分追踪：验证历史评分对后续收益的预测力"""
    rows = (
        db.query(ScoreHistory)
        .filter(ScoreHistory.stock_code == stock_code)
        .order_by(ScoreHistory.created_at.asc(), ScoreHistory.id.asc())
        .all()
    )
    if not rows:
        raise HTTPException(status_code=400, detail="暂无评分记录")

    # 拉近1年历史收盘，计算每次评分至今的收益
    market = rows[-1].market or "US"
    try:
        df = StockService.get_stock_data(stock_code, market, period="1y")
    except Exception as e:
        print(f"评分追踪获取行情失败: {e}")
        df = None

    return build_tracking(stock_code, rows, df)


@router.get("/weight-health")
def get_weight_health_report(
    stock_code: Optional[str] = None,
    window_days: int = 90,
    forward_days: int = 20,
    mode: str = "simple",
    db: Session = Depends(get_db),
):
    """四维权重体检报告：90天滚动窗口各维相关性 + 权重±10%扰动对组合收益影响（只观测不改权重）"""
    query = db.query(ScoreHistory)
    if stock_code:
        query = query.filter(ScoreHistory.stock_code == stock_code)
    rows = query.order_by(ScoreHistory.created_at.asc(), ScoreHistory.id.asc()).all()
    if not rows:
        return build_weight_health_report([], {}, window_days=window_days, forward_days=forward_days, mode=mode)

    codes = sorted({(r.stock_code, r.market or "US") for r in rows if r.stock_code})
    kline_map = {}
    for code, mkt in codes:
        try:
            df = StockService.get_stock_data(code, mkt, period="1y")
            if df is not None and not df.empty:
                kline_map[code] = df
        except Exception:
            pass

    return build_weight_health_report(
        rows,
        klines_by_code=kline_map,
        window_days=window_days,
        forward_days=forward_days,
        mode=mode,
    )
