"""
AI选股服务（仅美股）：Serenity供应链卡点思维
流程：候选池行情/新闻简报（多线程并行拉取） + 小红书博主观点 → LLM按卡点框架分析 → 结构化选股结果入库
另提供小红书博主帖子AI总结（generate_xhs_summaries），供用户快速浏览各博主观点。

LLM通过 llm_client 调OpenAI兼容接口（OpenRouter等），供应商在config.json配置。
"""

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import re
from datetime import date, datetime
from typing import Dict, List

import yfinance as yf
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models_platform import AIPick, XhsPost, XhsSummary
from . import xhs_service
from .llm_client import chat_completion, is_ai_configured

logger = logging.getLogger(__name__)

# 默认候选池（AI算力/半导体/光通信产业链为主，与用户自选风格一致；可在config覆盖）
DEFAULT_UNIVERSE = [
    ('NVDA', 'Nvidia'), ('AMD', 'AMD'), ('AVGO', 'Broadcom'), ('TSM', 'TSMC'),
    ('MU', 'Micron'), ('SNDK', 'Sandisk'), ('WDC', 'Western Digital'),
    ('ASML', 'ASML'), ('AMAT', 'Applied Materials'), ('LRCX', 'Lam Research'),
    ('KLAC', 'KLA'), ('INTC', 'Intel'), ('QCOM', 'Qualcomm'), ('MRVL', 'Marvell'),
    ('SMCI', 'Super Micro'), ('DELL', 'Dell'), ('ANET', 'Arista Networks'),
    ('COHR', 'Coherent'), ('LITE', 'Lumentum'), ('AAOI', 'AOI'),
    ('AXTI', 'AXT'), ('CIEN', 'Ciena'), ('ALAB', 'Astera Labs'),
    ('CRDO', 'Credo'), ('VRT', 'Vertiv'),
]

MAX_UNIVERSE = 20      # 单次送入LLM的标的上限（控制token）
MAX_PICKS = 5          # 每次最多输出推荐数


# ============================================
# 数据简报
# ============================================
def _stock_brief(code: str, name: str) -> Dict:
    """单只标的的轻量简报：价格/市值/PE + 近期新闻标题（失败字段留空不阻塞）"""
    brief: Dict = {'code': code, 'name': name, 'price': None,
                   'market_cap': None, 'pe': None, 'news': []}
    try:
        t = yf.Ticker(code)
        # 价格：fast_info多键名尝试 → info兜底；市值/PE走info
        price = None
        try:
            fi = t.fast_info
            for k in ('last_price', 'lastPrice', 'previous_close_price'):
                v = fi.get(k) if fi else None
                if v:
                    price = float(v)
                    break
        except Exception:
            pass
        full = {}
        try:
            full = t.info or {}
        except Exception:
            pass
        if not price:
            price = full.get('currentPrice') or full.get('regularMarketPrice')
        if price:
            brief['price'] = round(float(price), 2)
        mc = full.get('marketCap')
        if mc:
            brief['market_cap'] = round(float(mc) / 1e9, 1)  # 十亿美元
        if full.get('trailingPE'):
            brief['pe'] = full.get('trailingPE')
    except Exception as e:
        logger.warning(f"行情简报获取失败 {code}: {e}")

    try:
        from .analysis_service import AnalysisService
        news_items = AnalysisService()._fetch_news_us(code, name)
        brief['news'] = [
            {'title': n['title'][:120], 'sentiment': n['sentiment']}
            for n in news_items[:5]
        ]
    except Exception as e:
        logger.warning(f"新闻简报获取失败 {code}: {e}")
    return brief


def _format_brief(b: Dict) -> str:
    """把简报dict拼成喂给LLM的紧凑文本（纯函数）"""
    parts = [f"- {b['code']} {b['name']}"]
    metrics = []
    if b.get('price'):
        metrics.append(f"现价${b['price']}")
    if b.get('market_cap'):
        metrics.append(f"市值{b['market_cap']}B")
    if b.get('pe'):
        metrics.append(f"PE{round(b['pe'], 1)}")
    if metrics:
        parts[0] += f"（{'，'.join(metrics)}）"
    for n in b.get('news', [])[:5]:
        tag = '利好' if n['sentiment'] > 0 else '利空' if n['sentiment'] < 0 else '中性'
        parts.append(f"  · [{tag}] {n['title']}")
    return '\n'.join(parts)


# ============================================
# Prompt构建与响应解析（纯函数，便于单测）
# ============================================
SYSTEM_PROMPT = """你是一位采用"Serenity供应链卡点思维"的美股投资研究专家，只基于给定的数据分析，禁止编造数据。分析框架：
1.【找卡点】沿AI算力产业链寻找不可替代环节：供需缺口、扩产周期受限、认证壁垒高的瓶颈位置
2.【定价权】缺货时谁能提价？客户的第二供应商转换成本有多高？
3.【验证链】每个论点必须指出可验证的数据依据（订单/产能利用率/价格趋势/新闻事件）
4.【压力测试】对每个看多论点给出最强的反驳理由
输出要求：只推荐数据能支撑的标的（宁缺毋滥，最多5个），严格输出如下JSON（不要多余文字）：
{"picks":[{"rank":1,"code":"代码","name":"名称","confidence":"high|medium|low","thesis":"核心论点(2-3句)","bottlenecks":"卡点分析","risks":"最大反方观点","catalysts":"未来催化剂"}],"market_commentary":"本次对AI/半导体板块的整体判断(3句内)"}"""


def build_selection_messages(universe_briefs: List[Dict],
                             xhs_digest: str) -> List[Dict]:
    """组装LLM消息列表（纯函数；当 xhs_digest 为空时自动标注仅基于行情与新闻选股）"""
    universe_text = '\n\n'.join(_format_brief(b) for b in universe_briefs)
    xhs_block = xhs_digest.strip() if xhs_digest else ""
    if not xhs_block or xhs_block == '（暂无小红书博主内容）':
        xhs_block = "（小红书源未配置或无可用缓存，本次仅基于行情与新闻选股）"
    user_content = (
        f"# 候选标的简报（共{len(universe_briefs)}只）\n{universe_text}\n\n"
        f"# 小红书博主近期观点（可选辅助参考，注意甄别信息质量）\n{xhs_block}\n\n"
        f"请按系统框架分析并输出JSON。今天是{date.today().isoformat()}。"
    )
    return [{'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content}]


def parse_picks_response(text: str) -> Dict:
    """从LLM回复中提取JSON结果（容忍```json围栏与前后杂文）。解析失败抛ValueError"""
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        raise ValueError('LLM响应中未找到JSON')
    data = json.loads(m.group(0))
    picks = data.get('picks')
    if not isinstance(picks, list):
        raise ValueError('JSON中缺少picks数组')

    cleaned = []
    for p in picks[:MAX_PICKS]:
        if not isinstance(p, dict) or not p.get('code'):
            continue
        conf = str(p.get('confidence', 'low')).lower()
        cleaned.append({
            'rank': int(p.get('rank', len(cleaned) + 1)),
            'code': str(p['code']).upper().strip(),
            'name': str(p.get('name', ''))[:50],
            'confidence': conf if conf in ('high', 'medium', 'low') else 'medium',
            'thesis': str(p.get('thesis', '')),
            'bottlenecks': str(p.get('bottlenecks', '')),
            'risks': str(p.get('risks', '')),
            'catalysts': str(p.get('catalysts', '')),
        })
    if not cleaned:
        raise ValueError('picks数组为空或全部无效')
    cleaned.sort(key=lambda x: x['rank'])
    return {'picks': cleaned,
            'market_commentary': str(data.get('market_commentary', ''))}


# ============================================
# 主流程
# ============================================
def get_universe() -> List[tuple]:
    """候选池：config.json的ai_provider.universe可覆盖默认（格式 [["NVDA","Nvidia"],...]）"""
    from .llm_client import load_ai_provider_config
    cfg = load_ai_provider_config()
    custom = cfg.get('universe')
    if isinstance(custom, list) and custom:
        out = []
        for item in custom[:MAX_UNIVERSE]:
            if isinstance(item, (list, tuple)) and len(item) >= 1:
                out.append((str(item[0]), str(item[1]) if len(item) > 1 else item[0]))
        if out:
            return out
    return [(c, n) for c, n in DEFAULT_UNIVERSE][:MAX_UNIVERSE]


def run_ai_pick(force: bool = False) -> Dict:
    """完整选股流水线：同日去重检查→并发拉取简报→小红书（可选降级）→LLM→解析→入库。"""
    from ..utils.timezone import today_beijing, now_beijing

    today = today_beijing().isoformat()
    if not force:
        db_check = SessionLocal()
        try:
            existing_rows = (
                db_check.query(AIPick)
                .filter(AIPick.run_date == today)
                .order_by(AIPick.rank.asc(), AIPick.id.asc())
                .all()
            )
            if existing_rows:
                saved_existing = [
                    {
                        'rank': r.rank,
                        'code': r.stock_code,
                        'name': r.stock_name,
                        'confidence': r.confidence,
                        'thesis': r.thesis,
                        'bottlenecks': r.bottlenecks,
                        'risks': r.risks,
                        'catalysts': r.catalysts,
                        'price_at_pick': r.price_at_pick,
                    }
                    for r in existing_rows
                ]
                logger.info(f"AI选股同日去重命中({today})：直接返回已存 {len(saved_existing)} 条记录，不重复调用LLM")
                return {
                    'run_date': today,
                    'already_ran': True,
                    'message': '今日已运行，直接返回当日选股结果（未重复消耗LLM额度）',
                    'model_commentary': existing_rows[0].market_commentary or '',
                    'picks': saved_existing,
                    'universe_size': 0,
                }
        finally:
            db_check.close()

    if not is_ai_configured():
        raise RuntimeError('AI供应商未配置：请先在config/config.json填写ai_provider')

    universe = get_universe()
    with ThreadPoolExecutor(max_workers=min(8, max(len(universe), 1))) as executor:
        briefs = list(executor.map(lambda pair: _stock_brief(pair[0], pair[1]), universe))

    # 小红书降级为可选源：无 Cookie 或抓取/摘要异常时跳过小红书源，xhs_digest 置为空串照常运行
    xhs_digest = ""
    db_xhs = SessionLocal()
    try:
        xhs_cfg = xhs_service.get_xhs_config(db_xhs)
        cookie_ok = bool(xhs_cfg.get('cookie_set'))
    except Exception:
        cookie_ok = False
    finally:
        db_xhs.close()

    if cookie_ok:
        try:
            raw_digest = xhs_service.digest_recent() or ""
            if raw_digest.strip() and raw_digest.strip() != '（暂无小红书博主内容）':
                xhs_digest = raw_digest
        except Exception as e:
            logger.warning(f"小红书摘要读取失败，自动跳过xhs源: {e}")
            xhs_digest = "" 

    messages = build_selection_messages(briefs, xhs_digest)
    # 推理类模型(如ox-alpha)会消耗大量token思考，需给足输出空间
    raw = chat_completion(messages, temperature=0.4, max_tokens=16000)
    result = parse_picks_response(raw)

    price_map = {b['code']: b.get('price') for b in briefs}
    evidence = {
        'xhs_posts': sum(1 for line in xhs_digest.splitlines() if line.startswith('- [')),
        'news_total': sum(len(b.get('news', [])) for b in briefs),
    }

    db = SessionLocal()
    saved = []
    try:
        for p in result['picks']:
            row = AIPick(
                run_date=today, rank=p['rank'], stock_code=p['code'],
                stock_name=p['name'], confidence=p['confidence'],
                thesis=p['thesis'], bottlenecks=p['bottlenecks'],
                risks=p['risks'], catalysts=p['catalysts'],
                market_commentary=result['market_commentary'],
                price_at_pick=price_map.get(p['code']),
                evidence_json=json.dumps(evidence, ensure_ascii=False),
                created_at=now_beijing())
            db.add(row)
            saved.append({**p, 'price_at_pick': price_map.get(p['code'])})
        db.commit()
    finally:
        db.close()

    logger.info(f"AI选股完成：{[(p['rank'], p['code']) for p in saved]}")
    return {'run_date': today, 'model_commentary': result['market_commentary'],
            'picks': saved, 'universe_size': len(briefs),
            'xhs_digest': xhs_digest, 'xhs_skipped': not bool(xhs_digest)}


# ============================================
# 小红书博主帖子总结（每博主一份，覆盖式更新）
# ============================================
SUMMARY_POSTS_LIMIT = 20   # 每位博主取最近多少条帖子参与总结

SUMMARY_SYSTEM_PROMPT = """你是投资研究助理。根据给出的小红书博主近期帖子（标题+内容），用中文写一份供投资者快速浏览的总结：
1. 近期关注的方向与主题
2. 核心观点与态度（乐观/谨慎/中性）
3. 提到的具体标的（代码/名称）及看多/看空倾向
4. 值得注意的风险提示或异常信号（如有）
要求：150-300字，客观归纳帖子原意，不要编造帖子外的信息，不要寒暄，直接输出总结正文。"""


def _post_date(posted_time) -> str:
    """13位毫秒时间戳串→'YYYY-MM-DD'；其他格式/空值原样返回"""
    s = str(posted_time or '')
    if s.isdigit() and len(s) == 13:
        try:
            return datetime.fromtimestamp(int(s) / 1000).strftime('%Y-%m-%d')
        except Exception:
            return s
    return s


def _format_post_line(post: XhsPost) -> str:
    """单条帖子→一行文本：- [日期] 标题：内容前200字"""
    line = f"- [{_post_date(post.posted_time)}] {post.title or ''}"
    content = str(post.content or '')[:200]
    if content:
        line += f"：{content}"
    return line


def build_summary_messages(blogger_name: str, posts: List[XhsPost]) -> List[Dict]:
    """组装博主总结LLM消息（纯函数，便于单测）"""
    post_lines = '\n'.join(_format_post_line(p) for p in posts)
    user_content = (
        f"博主「{blogger_name}」最近的{len(posts)}条帖子如下：\n{post_lines}\n\n"
        f"请按系统要求输出总结。"
    )
    return [{'role': 'system', 'content': SUMMARY_SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content}]


def generate_xhs_summaries() -> Dict:
    """为每个配置博主生成近期帖子AI总结（每博主一次LLM调用，覆盖旧总结）。
    单个博主失败记录error继续；全部失败抛RuntimeError由路由转400"""
    if not is_ai_configured():
        raise RuntimeError('AI供应商未配置：请先在config/config.json填写ai_provider')

    db = SessionLocal()
    try:
        bloggers = xhs_service.get_xhs_config(db)['bloggers']
    finally:
        db.close()
    if not bloggers:
        raise RuntimeError('未配置小红书博主，请先在设置中添加博主链接')

    summaries, errors = [], []
    for b in bloggers:
        db = SessionLocal()
        try:
            posts = (db.query(XhsPost)
                     .filter(XhsPost.blogger_name == b['name'])
                     .order_by(XhsPost.posted_time.desc(), XhsPost.id.desc())
                     .limit(SUMMARY_POSTS_LIMIT).all())
            if not posts:
                errors.append({'blogger': b['name'], 'error': '无缓存帖子，请先抓取'})
                continue

            raw = chat_completion(build_summary_messages(b['name'], posts),
                                  temperature=0.3, max_tokens=2000)
            summary_text = raw.strip()
            dates = sorted(d for d in (_post_date(p.posted_time) for p in posts) if d)
            now = datetime.now()

            row = db.query(XhsSummary).filter(
                XhsSummary.blogger_name == b['name']).first()
            if row:
                row.summary_text = summary_text
                row.posts_count = len(posts)
                row.period_start = dates[0] if dates else None
                row.period_end = dates[-1] if dates else None
                row.created_at = now
            else:
                db.add(XhsSummary(blogger_name=b['name'], summary_text=summary_text,
                                  posts_count=len(posts),
                                  period_start=dates[0] if dates else None,
                                  period_end=dates[-1] if dates else None,
                                  created_at=now))
            db.commit()
            summaries.append({'blogger_name': b['name'], 'summary_text': summary_text,
                              'posts_count': len(posts),
                              'period_start': dates[0] if dates else None,
                              'period_end': dates[-1] if dates else None,
                              'created_at': now.isoformat()})
        except Exception as e:
            logger.warning(f"博主总结生成失败[{b['name']}]: {e}")
            errors.append({'blogger': b['name'], 'error': str(e)})
        finally:
            db.close()

    if not summaries and errors:
        raise RuntimeError(f"博主总结生成失败：{errors[0]['error']}")
    return {'summaries': summaries, 'errors': errors}


def get_xhs_summaries(db: Session) -> List[Dict]:
    """已存博主总结（新→旧）"""
    rows = db.query(XhsSummary).order_by(XhsSummary.created_at.desc()).all()
    return [{
        'blogger_name': r.blogger_name, 'summary_text': r.summary_text,
        'posts_count': r.posts_count, 'period_start': r.period_start,
        'period_end': r.period_end,
        'created_at': r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


# ============================================
# [第三批·第二段-3] AI选股 20/60 交易日业绩复盘（按 confidence 分层统计，未到期标 pending）
# ============================================
def compute_ai_picks_performance(
    picks: List,
    klines_by_code: Dict[str, object] = None,
    kline_map: Dict[str, object] = None,
) -> Dict:
    """
    计算历史 AI 选股记录在入选后 +20 个交易日与 +60 个交易日的真实前向收益与分层胜率（纯函数）：
    - 严格按交易日序列向后数第 20 根 / 第 60 根 K 线收盘价计算收益
    - 若入选日之后交易日不足 20 / 60 根，状态标记为 'pending'（未到期），绝不用未满期现价冒充终值！
    - 按 confidence ('high' / 'medium' / 'low') 及 'overall' 汇总样本数、已到期数、胜率与平均收益
    - 仅做观测统计，不反向修改选股 Prompt 或模型参数
    """
    from bisect import bisect_right

    def _get(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    # 预处理每只股票的交易日收盘序列: code -> (sorted_dates, [closes])
    series_map = {}
    for code, df in (klines_by_code or kline_map or {}).items():
        if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
            continue
        date_closes = {}
        for idx, row in df.iterrows():
            try:
                c = float(row["Close"])
                if c == c and c > 0:
                    d_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                    date_closes[d_str] = c
            except Exception:
                continue
        if date_closes:
            s_dates = sorted(date_closes.keys())
            series_map[code] = (s_dates, [date_closes[d] for d in s_dates])

    items = []
    for p in picks or []:
        code = str(_get(p, "stock_code") or _get(p, "code") or "").upper().strip()
        run_date = str(_get(p, "run_date") or "")[:10]
        conf = str(_get(p, "confidence") or "medium").lower().strip()
        if conf not in ("high", "medium", "low"):
            conf = "medium"
        raw_entry = _get(p, "price_at_pick")

        ret_20d = None
        ret_60d = None
        status_20d = "pending"
        status_60d = "pending"
        entry_px = float(raw_entry) if (raw_entry is not None and float(raw_entry) > 0) else None

        if code in series_map and run_date:
            s_dates, s_closes = series_map[code]
            pos = bisect_right(s_dates, run_date) - 1
            if pos >= 0:
                if entry_px is None:
                    entry_px = s_closes[pos]
                if entry_px and entry_px > 0:
                    if pos + 20 < len(s_closes):
                        c20 = s_closes[pos + 20]
                        ret_20d = round((c20 / entry_px - 1.0) * 100.0, 2)
                        status_20d = "matured"
                    if pos + 60 < len(s_closes):
                        c60 = s_closes[pos + 60]
                        ret_60d = round((c60 / entry_px - 1.0) * 100.0, 2)
                        status_60d = "matured"

        items.append({
            "id": _get(p, "id"),
            "run_date": run_date,
            "rank": _get(p, "rank"),
            "stock_code": code,
            "stock_name": _get(p, "stock_name") or _get(p, "name") or code,
            "confidence": conf,
            "price_at_pick": round(entry_px, 2) if entry_px else None,
            "return_20d_pct": ret_20d,
            "status_20d": status_20d,
            "return_60d_pct": ret_60d,
            "status_60d": status_60d,
        })

    def _summarize_tier(tier_name: str, subset: List[Dict]) -> Dict:
        m20 = [x for x in subset if x["status_20d"] == "matured" and x["return_20d_pct"] is not None]
        m60 = [x for x in subset if x["status_60d"] == "matured" and x["return_60d_pct"] is not None]
        w20 = sum(1 for x in m20 if x["return_20d_pct"] > 0)
        w60 = sum(1 for x in m60 if x["return_60d_pct"] > 0)
        d20_stat = {
            "matured_count": len(m20),
            "pending_count": len(subset) - len(m20),
            "win_count": w20,
            "win_rate": round(w20 / len(m20) * 100.0, 1) if m20 else None,
            "avg_return_pct": round(sum(x["return_20d_pct"] for x in m20) / len(m20), 2) if m20 else None,
        }
        d60_stat = {
            "matured_count": len(m60),
            "pending_count": len(subset) - len(m60),
            "win_count": w60,
            "win_rate": round(w60 / len(m60) * 100.0, 1) if m60 else None,
            "avg_return_pct": round(sum(x["return_60d_pct"] for x in m60) / len(m60), 2) if m60 else None,
        }
        return {
            "confidence": tier_name,
            "total_picks": len(subset),
            "matured_20d": d20_stat["matured_count"],
            "pending_20d": d20_stat["pending_count"],
            "win_rate_20d": d20_stat["win_rate"],
            "avg_return_20d": d20_stat["avg_return_pct"],
            "matured_60d": d60_stat["matured_count"],
            "pending_60d": d60_stat["pending_count"],
            "win_rate_60d": d60_stat["win_rate"],
            "avg_return_60d": d60_stat["avg_return_pct"],
            "d20": d20_stat,
            "d60": d60_stat,
        }

    by_confidence = {
        tier: _summarize_tier(tier, [x for x in items if x["confidence"] == tier])
        for tier in ("high", "medium", "low")
    }
    overall = _summarize_tier("overall", items)

    return {
        "total_picks": len(items),
        "by_confidence": by_confidence,
        "overall": overall,
        "items": items,
        "picks": items,
        "observe_only": True,
        "policy_note": "仅作20/60交易日分层业绩统计，不反向调参",
        "note": "仅作20/60交易日分层业绩统计，未满交易日样本标为 pending 不计入胜率分母，不反向调参",
    }
