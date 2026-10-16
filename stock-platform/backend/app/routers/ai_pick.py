"""
AI选股相关API
- GET  /api/ai-pick/status       配置状态（不回传key本身）
- POST /api/ai-pick/run          运行一次AI选股（耗时约1-3分钟）
- GET  /api/ai-pick/history      历史选股结果（含并行实时现价与入选至今涨跌幅复盘）
- GET  /api/ai-pick/xhs-config   小红书配置读取
- PUT  /api/ai-pick/xhs-config   小红书配置保存（Cookie/博主列表）
- POST /api/ai-pick/xhs-refresh  手动抓取博主最新帖子
- GET  /api/ai-pick/xhs-summaries 已存博主总结
- POST /api/ai-pick/xhs-summaries 生成各博主近期帖子AI总结
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models_platform import AIPick, XhsPost
from ..services import xhs_service
from ..services.ai_pick_service import generate_xhs_summaries, get_xhs_summaries, run_ai_pick
from ..services.llm_client import is_ai_configured, load_ai_provider_config
from ..services.stock_service import StockService
from ..utils.market import detect_market

router = APIRouter(prefix="/api/ai-pick", tags=["ai-pick"])
stock_service = StockService()


def _repo_root() -> Path:
    """定位仓库根目录（<repo>/stock-platform/backend/app/routers/ai_pick.py 向上5层为 <repo>）"""
    p4 = Path(__file__).resolve().parents[4]
    p3 = Path(__file__).resolve().parents[3]
    if (p4 / "tech_stock_screener.py").exists() or (p4 / "data").exists():
        return p4
    return p3


def _resolve_data_file(filename: str) -> Path:
    """优先定位仓库根 data/<filename>，兼容 stock-platform/data/<filename>"""
    p4_file = Path(__file__).resolve().parents[4] / "data" / filename
    if p4_file.exists():
        return p4_file
    p3_file = Path(__file__).resolve().parents[3] / "data" / filename
    if p3_file.exists():
        return p3_file
    return p4_file


def enrich_picks_with_returns(picks: List[Dict], quote_map: Dict[str, float]) -> List[Dict]:
    """为历史AI选股记录注入最新现价与入选至今收益率（纯函数，便于单测）"""
    enriched = []
    for p in picks:
        item = dict(p)
        code = item.get('stock_code', '')
        entry_px = item.get('price_at_pick')
        curr_px = quote_map.get(code)
        if curr_px is not None and curr_px > 0:
            item['current_price'] = round(float(curr_px), 2)
            if entry_px and entry_px > 0:
                item['return_pct'] = round((float(curr_px) - float(entry_px)) / float(entry_px) * 100, 2)
            else:
                item['return_pct'] = None
        else:
            item['current_price'] = None
            item['return_pct'] = None
        enriched.append(item)
    return enriched


@router.get("/status")
def get_status(db: Session = Depends(get_db)):
    """配置状态总览（key等敏感信息只返回是否已设置）"""
    cfg = load_ai_provider_config()
    xhs_cfg = xhs_service.get_xhs_config(db)
    last_run = (db.query(AIPick).order_by(AIPick.id.desc()).first())
    post_count = db.query(XhsPost).count()
    return {
        'ai_configured': is_ai_configured(),
        'ai_model': cfg['model'] if is_ai_configured() else '',
        'xhs_cookie_set': xhs_cfg['cookie_set'],
        'bloggers': xhs_cfg['bloggers'],
        'cached_posts': post_count,
        'last_run_date': last_run.run_date if last_run else None,
    }


@router.post("/run")
def run_selection():
    """运行一次完整AI选股（拉数据+LLM分析，约1-3分钟；同步def走线程池）"""
    try:
        return run_ai_pick()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI选股失败: {e}")


@router.get("/history")
def get_history(limit: int = 30, include_quotes: bool = True, db: Session = Depends(get_db)):
    """历史选股记录（新→旧，默认并行拉取最新价计算入选至今涨跌幅）"""
    rows = (db.query(AIPick)
            .order_by(AIPick.created_at.desc(), AIPick.rank.asc())
            .limit(limit).all())
    base_list = [{
        'id': r.id,
        'run_date': r.run_date,
        'created_at': r.created_at.isoformat() if r.created_at else None,
        'rank': r.rank,
        'stock_code': r.stock_code,
        'stock_name': r.stock_name,
        'confidence': r.confidence,
        'thesis': r.thesis,
        'bottlenecks': r.bottlenecks,
        'risks': r.risks,
        'catalysts': r.catalysts,
        'market_commentary': r.market_commentary,
        'price_at_pick': r.price_at_pick,
    } for r in rows]

    if not include_quotes or not base_list:
        return enrich_picks_with_returns(base_list, {})

    unique_codes = sorted({r['stock_code'] for r in base_list if r.get('stock_code')})
    quote_map: Dict[str, float] = {}

    def _fetch_px(code: str):
        try:
            q = stock_service.get_realtime_quote(code, detect_market(code))
            return (code, q.get('price') if q else None)
        except Exception:
            return (code, None)

    if unique_codes:
        with ThreadPoolExecutor(max_workers=min(8, len(unique_codes))) as executor:
            for code, px in executor.map(_fetch_px, unique_codes):
                if px is not None and px > 0:
                    quote_map[code] = float(px)

    return enrich_picks_with_returns(base_list, quote_map)


class XhsConfigRequest(BaseModel):
    cookie: str = None       # None=不改，空串=清除
    bloggers: list = None    # [{"name":"显示名","url":"https://www.xiaohongshu.com/user/profile/xxx"}]


@router.get("/xhs-config")
def get_xhs_config(db: Session = Depends(get_db)):
    return xhs_service.get_xhs_config(db)


@router.put("/xhs-config")
def put_xhs_config(req: XhsConfigRequest, db: Session = Depends(get_db)):
    try:
        xhs_service.set_xhs_config(db, cookie=req.cookie, bloggers=req.bloggers)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {'message': '保存成功', **xhs_service.get_xhs_config(db)}


@router.post("/xhs-refresh")
def refresh_xhs():
    """抓取全部博主最新帖子入库（需已配Cookie）"""
    result = xhs_service.refresh_all()
    if result.get('error'):
        raise HTTPException(status_code=400, detail=result['error'])
    return result


class XhsPostsRequest(BaseModel):
    limit: int = 20


@router.post("/xhs-posts")
def list_xhs_posts(req: XhsPostsRequest, db: Session = Depends(get_db)):
    """查看缓存的帖子列表"""
    rows = (db.query(XhsPost)
            .order_by(XhsPost.fetched_at.desc(), XhsPost.id.desc())
            .limit(req.limit).all())
    return [{
        'note_id': r.note_id, 'blogger_name': r.blogger_name,
        'title': r.title, 'url': r.url,
        'fetched_at': r.fetched_at.isoformat() if r.fetched_at else None,
    } for r in rows]


@router.get("/xhs-summaries")
def read_xhs_summaries(db: Session = Depends(get_db)):
    """已存博主总结（新→旧）"""
    return get_xhs_summaries(db)


@router.post("/xhs-summaries")
def refresh_xhs_summaries():
    """为每个配置博主生成近期帖子AI总结（每博主一次LLM调用，约几秒/人）"""
    try:
        return generate_xhs_summaries()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"博主总结生成失败: {e}")


# ----------------------------------------------------
# 周度美股硬科技选股 · Alpha TOP 5 猛禽池 API
# ----------------------------------------------------
@router.get("/top5")
def get_weekly_top5():
    """获取最新周度 Alpha TOP 5 猛禽池数据"""
    import json
    data_file = _resolve_data_file("weekly_alpha_top5.json")
    if data_file.exists():
        try:
            with open(data_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"读取选股数据失败: {e}")
    return {
        'scan_date': None,
        'total_scanned': 0,
        'top5': [],
        'ai_thesis': '暂无周度选股数据，请点击上方按钮运行扫描'
    }


@router.post("/top5/scan")
def trigger_weekly_scan():
    """手动触发一次硬科技全池选股扫描"""
    import sys
    root_dir = _repo_root()
    if str(root_dir) not in sys.path:
        sys.path.insert(0, str(root_dir))
    try:
        from tech_stock_screener import run_screener
        return run_screener()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"选股扫描失败: {e}")


@router.get("/top5/history")
def get_weekly_history():
    """获取猛禽池累积历史战报"""
    import json
    history_file = _resolve_data_file("weekly_alpha_history.json")
    if history_file.exists():
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                weeks = json.load(f)
            # 计算累积统计
            total_weeks = sum(1 for w in weeks if w.get('battle_report'))
            all_picks = []
            for w in weeks:
                br = w.get('battle_report')
                if br and br.get('picks'):
                    all_picks.extend(br['picks'])
            total_picks = len(all_picks)
            total_winners = sum(1 for p in all_picks if p.get('is_winner'))
            overall_win_rate = round(total_winners / total_picks * 100, 1) if total_picks else 0
            avg_return = round(sum(p['weekly_return_pct'] for p in all_picks) / total_picks, 2) if total_picks else 0
            return {
                'weeks': weeks,
                'cumulative': {
                    'total_weeks': total_weeks,
                    'total_picks': total_picks,
                    'overall_win_rate': overall_win_rate,
                    'avg_return_pct': avg_return,
                    'total_winners': total_winners,
                }
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"读取历史战报失败: {e}")
    return {'weeks': [], 'cumulative': None}
