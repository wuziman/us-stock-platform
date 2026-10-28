"""
量化交易平台 - FastAPI后端
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from .database import engine, Base
from .routers import stocks_router, analysis_router, backtest_router, portfolio_router, report_router
from .routers.ai_pick import router as ai_pick_router
from .routers.paper_trading import router as paper_trading_router
from .services.scheduler_service import start_scheduler
from . import models_platform  # noqa: F401  注册平台扩展表模型（须在create_all之前导入）

# 创建数据库表
Base.metadata.create_all(bind=engine)


def _run_migrations():
    """轻量迁移：为已存在的表补充新增列（SQLite不支持自动迁移）"""
    migrations = [
        "ALTER TABLE positions ADD COLUMN sell_price FLOAT",
        "ALTER TABLE positions ADD COLUMN sell_date DATETIME",
        "ALTER TABLE positions ADD COLUMN initial_price FLOAT",
        "ALTER TABLE positions ADD COLUMN add_count INTEGER DEFAULT 0",
        "ALTER TABLE positions ADD COLUMN highest_price FLOAT",
    ]
    with engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                # 列已存在，跳过
                pass
    # 启动时自动排查并原地合并存量重复 holding 行（防历史多行持仓导致重复止损告警）
    try:
        from .database import SessionLocal
        from .routers.portfolio import consolidate_duplicate_holdings
        db = SessionLocal()
        try:
            consolidate_duplicate_holdings(db)
        finally:
            db.close()
    except Exception:
        pass


_run_migrations()

# 创建FastAPI应用
app = FastAPI(
    title="量化交易平台",
    description="支持A股和美股的四维度分析系统",
    version="1.0.0"
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],  # 前端地址
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(stocks_router)
app.include_router(analysis_router)
app.include_router(backtest_router)
app.include_router(portfolio_router)
app.include_router(report_router)
app.include_router(ai_pick_router)
app.include_router(paper_trading_router)


@app.get("/")
async def root():
    """根路径"""
    return {
        "message": "量化交易平台API",
        "version": "1.0.0",
        "docs": "/docs"
    }


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy"}


# ============================================
# 后台定时调度器（自动日报）
# ============================================
@app.on_event("startup")
async def _start_background_scheduler():
    """应用启动时开启定时调度（每30分钟检查一次日报推送窗口）"""
    start_scheduler(app)
