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
    """组装LLM消息列表（纯函数）"""
    universe_text = '\n\n'.join(_format_brief(b) for b in universe_briefs)
    user_content = (
        f"# 候选标的简报（共{len(universe_briefs)}只）\n{universe_text}\n\n"
        f"# 小红书博主近期观点（辅助参考，注意甄别信息质量）\n{xhs_digest}\n\n"
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


def run_ai_pick() -> Dict:
    """完整选股流水线：并发拉取简报→小红书→LLM→解析→入库。任何环节失败抛异常由路由转HTTP错误"""
    if not is_ai_configured():
        raise RuntimeError('AI供应商未配置：请先在config/config.json填写ai_provider')

    universe = get_universe()
    with ThreadPoolExecutor(max_workers=min(8, max(len(universe), 1))) as executor:
        briefs = list(executor.map(lambda pair: _stock_brief(pair[0], pair[1]), universe))
    xhs_digest = xhs_service.digest_recent()

    messages = build_selection_messages(briefs, xhs_digest)
    # 推理类模型(如ox-alpha)会消耗大量token思考，需给足输出空间
    raw = chat_completion(messages, temperature=0.4, max_tokens=16000)
    result = parse_picks_response(raw)

    price_map = {b['code']: b.get('price') for b in briefs}
    evidence = {
        'xhs_posts': sum(1 for line in xhs_digest.splitlines() if line.startswith('- [')),
        'news_total': sum(len(b.get('news', [])) for b in briefs),
    }

    today = date.today().isoformat()
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
                created_at=datetime.now())
            db.add(row)
            saved.append({**p, 'price_at_pick': price_map.get(p['code'])})
        db.commit()
    finally:
        db.close()

    logger.info(f"AI选股完成：{[(p['rank'], p['code']) for p in saved]}")
    return {'run_date': today, 'model_commentary': result['market_commentary'],
            'picks': saved, 'universe_size': len(briefs)}


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
