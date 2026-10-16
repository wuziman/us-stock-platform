"""
每日报告服务
- 汇总自选股：双策略点位（含ATR自适应动态止损与+10%保本位） + MACD状态 + 技术评分
- 并行拉取多只自选股快照，大幅缩短日报生成耗时
- 生成企业微信markdown日报
- 通过webhook推送（支持dry_run预览与超长报告无损智能分包）
"""

from concurrent.futures import ThreadPoolExecutor
import os
import time
from datetime import datetime
from typing import Dict, List, Optional
import requests
import pandas as pd

from .stock_service import StockService
from .tech_score import calculate_tech_score
from ..utils.indicators import calculate_all_indicators
from ..utils.market import detect_market
from ..utils.app_config import load_config
from ..utils.price_levels import (
    LINEAR_PROFIT_PCT,
    NONLINEAR_PROFIT_PCT,
    atr_adaptive_levels,
    discipline_stop,
    linear_buy_point,
    macd_state_days,
    nonlinear_buy_point,
)


def _load_webhook() -> str:
    """企业微信webhook：环境变量优先，其次原项目config（读取走 utils/app_config 单点）"""
    return os.environ.get('WECHAT_WEBHOOK', '') or load_config().get('wechat_webhook', '')


def split_wechat_chunks(report: str, max_bytes: int = 3800) -> List[str]:
    """将超长报告按段落边界（'\\n\\n'）无损切分为多个不超过 max_bytes 的分包"""
    if len(report.encode('utf-8')) <= max_bytes:
        return [report]

    blocks = report.split('\n\n')
    chunks: List[str] = []
    current = ''
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate.encode('utf-8')) > max_bytes:
            if current:
                chunks.append(current)
                current = block
            else:
                # 单个超长块兜底按行切分
                lines = block.split('\n')
                sub = ''
                for ln in lines:
                    cand_ln = f"{sub}\n{ln}" if sub else ln
                    if len(cand_ln.encode('utf-8')) > max_bytes and sub:
                        chunks.append(sub)
                        sub = ln
                    else:
                        sub = cand_ln
                current = sub
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class ReportService:
    """每日报告服务"""

    def __init__(self):
        self.stock_service = StockService()

    # ============================================
    # 单只股票：三策略快照（轻量，不拉新闻）
    # ============================================
    def _stock_snapshot(self, stock_code: str, stock_name: str) -> Optional[Dict]:
        market = detect_market(stock_code)
        df = self.stock_service.get_stock_data(stock_code, market, period="3mo")
        if df is None or df.empty:
            return None

        df = calculate_all_indicators(df)
        latest = df.iloc[-1]
        current = float(latest['Close'])
        rsi = latest.get('RSI', 50)
        rsi = float(rsi) if pd.notna(rsi) else 50.0
        ma20 = latest.get('MA20', current)
        ma20 = float(ma20) if pd.notna(ma20) else current * 0.95
        bb_lower = latest.get('BB_Lower', current * 0.9)
        bb_lower = float(bb_lower) if pd.notna(bb_lower) else current * 0.9
        atr_val = float(latest['ATR']) if 'ATR' in latest and pd.notna(latest['ATR']) else None

        # 线性/非线性点位公式走共享模块（utils/price_levels.py，唯一定义处）
        linear_buy = linear_buy_point(current, ma20)
        nonlinear_buy = nonlinear_buy_point(rsi, ma20, bb_lower)

        # MACD状态与持续天数（共享统计）
        macd = latest.get('MACD')
        signal = latest.get('MACD_Signal')
        if pd.notna(macd) and pd.notna(signal):
            is_golden, days = macd_state_days((df['MACD'] > df['MACD_Signal']).values.tolist())
            macd_state = f"{'🟢金叉' if is_golden else '🔴死叉'}{days}天"
            macd_golden = is_golden
        else:
            macd_state = '未知'
            macd_golden = None

        # 技术评分（调用与分析服务共享的同一实现，含布林带与量价调整）
        tech, _ = calculate_tech_score(latest)

        if tech >= 8:
            rec, color = '强烈买入', 'info'
        elif tech >= 6.5:
            rec, color = '买入', 'info'
        elif tech >= 5:
            rec, color = '观望', 'comment'
        else:
            rec, color = '谨慎', 'warning'

        return {
            'code': stock_code,
            'name': stock_name,
            'market': market,
            'price': round(current, 2),
            'rsi': round(rsi, 1),
            'tech': round(tech, 1),
            'rec': rec,
            'color': color,
            'macd_state': macd_state,
            'macd_golden': macd_golden,
            # MACD策略操作参考
            'macd_add': round(ma20, 2) if macd_golden else None,          # 金叉：回踩MA20加仓参考
            'macd_watch': round(bb_lower, 2) if macd_golden is False else None,  # 死叉：关注布林下轨
            'macd_stop': discipline_stop(current),                        # 纪律止损-8%
            'linear': atr_adaptive_levels(current, linear_buy, LINEAR_PROFIT_PCT, atr=atr_val, atr_mult=2.0),
            'nonlinear': atr_adaptive_levels(current, nonlinear_buy, NONLINEAR_PROFIT_PCT, atr=atr_val, atr_mult=2.2),
        }

    # ============================================
    # 生成日报（企业微信markdown格式，支持并行快照）
    # ============================================
    def generate_daily_report(self, watchlist: List[Dict]) -> Dict:
        """watchlist: [{stock_code, stock_name}] → {report, snapshots, failed}"""
        snapshots: List[Dict] = []
        failed: List[str] = []

        def _safe_snap(item: Dict):
            code = item['stock_code']
            name = item['stock_name']
            try:
                snap = self._stock_snapshot(code, name)
                return (snap, None) if snap else (None, f"{code} {name}")
            except Exception as e:
                print(f"快照失败 {code}: {e}")
                return (None, f"{code} {name}")

        if watchlist:
            with ThreadPoolExecutor(max_workers=min(8, len(watchlist))) as executor:
                results = list(executor.map(_safe_snap, watchlist))
            for snap, err in results:
                if snap:
                    snapshots.append(snap)
                elif err:
                    failed.append(err)

        # 按技术分排序
        snapshots.sort(key=lambda s: s['tech'], reverse=True)

        now = datetime.now()
        lines = [
            f"**📈 股票日报·三策略版** {now.strftime('%m-%d %H:%M')}",
            f"> 自选{len(snapshots)}只 | 按技术评分排序",
            '',
        ]
        for i, s in enumerate(snapshots, 1):
            price_str = f"¥{s['price']}" if s['market'] == 'A' else f"${s['price']}"
            lines.append(
                f"**{i}. {s['name']} {s['code']}** <font color=\"{s['color']}\">{s['rec']}</font> {s['tech']}分"
            )
            lines.append(
                f"现价{price_str} RSI{s['rsi']} MACD:{s['macd_state']}"
            )
            lin_atr_suffix = (
                f" (ATR损{s['linear']['atr_stop']})"
                if s['linear'].get('atr_stop') and s['linear']['atr_stop'] != s['linear']['stop']
                else ""
            )
            lines.append(
                f"线性: 买{s['linear']['buy']}({s['linear']['distance']:+.1f}%) "
                f"盈{s['linear']['profit']} 损{s['linear']['stop']}{lin_atr_suffix}"
            )
            lines.append(
                f"非线性: 买{s['nonlinear']['buy']}({s['nonlinear']['distance']:+.1f}%) "
                f"盈{s['nonlinear']['profit']} 损{s['nonlinear']['stop']}"
            )
            # MACD策略操作参考
            if s.get('macd_golden'):
                lines.append(
                    f"MACD: {s['macd_state']} ✅持有 加仓MA20 {s['macd_add']} "
                    f"离场=死叉 损{s['macd_stop']}"
                )
            elif s.get('macd_golden') is False:
                lines.append(
                    f"MACD: {s['macd_state']} ⏳观望 关注下轨{s['macd_watch']} "
                    f"入场=金叉 损{s['macd_stop']}"
                )
            else:
                lines.append("MACD: 数据不足")
            lines.append('')

        if failed:
            lines.append(f"⚠️ 数据缺失: {', '.join(failed)}")
        lines.append('> 仅供参考，投资有风险')

        report = '\n'.join(lines)
        return {
            'report': report,
            'snapshots': snapshots,
            'failed': failed,
            'generated_at': now.isoformat(),
            'char_count': len(report.encode('utf-8')),
        }

    # ============================================
    # 推送企业微信（支持超长内容按股票卡片无损分包）
    # ============================================
    def send_to_wechat(self, report: str) -> Dict:
        webhook = _load_webhook()
        if not webhook:
            return {'sent': False, 'error': '未配置企业微信Webhook（config/config.json的wechat_webhook）'}

        chunks = split_wechat_chunks(report, max_bytes=3800)
        total = len(chunks)

        try:
            for idx, chunk in enumerate(chunks, 1):
                prefix = f"*(第 {idx}/{total} 节)*\n" if total > 1 else ""
                resp = requests.post(webhook, json={
                    'msgtype': 'markdown',
                    'markdown': {'content': prefix + chunk}
                }, timeout=10)
                data = resp.json()
                if not (resp.status_code == 200 and data.get('errcode') == 0):
                    return {'sent': False, 'error': f"HTTP {resp.status_code}: {data}"}
                if idx < total:
                    time.sleep(1.0)
            return {'sent': True, 'chunks': total}
        except Exception as e:
            return {'sent': False, 'error': str(e)}
