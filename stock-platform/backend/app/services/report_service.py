"""
每日报告服务
- 汇总自选股：双策略点位（含ATR自适应动态止损与+10%保本位） + MACD状态 + 技术评分
- 市场风控状态（NORMAL / CAUTION / CIRCUIT_BREAKER）真实联动买卖点位与仓位比例（受 regime_linkage_enabled 全局总开关统管）：
  * CAUTION 档：linear / nonlinear 买点（buy / buy_low）真实下移 2%，建议仓位减半（position_scale=0.5）
  * CIRCUIT_BREAKER 档：全面冻结新开仓买点（buy=None, buy_low=None, macd_add=None, macd_watch=None, position_scale=0.0）
  * 当 regime_linkage_enabled=false 时：顶部大盘风控横幅照常展示实况，但点位与仓位强制按 NORMAL (1.0) 输出原始值
- 并行拉取多只自选股快照，大幅缩短日报生成耗时
- 生成企业微信markdown日报
- 通过webhook推送（支持dry_run预览与超长报告无损智能分包）
"""

from concurrent.futures import ThreadPoolExecutor
import os
import time
from typing import Dict, List, Optional
import requests
import pandas as pd

from ..database import SessionLocal
from ..models import Setting
from .stock_service import StockService
from .tech_score import calculate_tech_score
from ..utils.indicators import calculate_all_indicators
from ..utils.market import detect_market
from ..utils.app_config import load_config
from ..utils.timezone import now_beijing
from ..utils.price_levels import (
    CAUTION_BUY_DISCOUNT,
    LINEAR_PROFIT_PCT,
    NONLINEAR_PROFIT_PCT,
    apply_regime_to_levels,
    discipline_stop,
    evaluate_market_regime,
    linear_buy_point,
    macd_state_days,
    nonlinear_buy_point,
)


def _is_regime_linkage_on() -> bool:
    """读取 Setting 表中的 regime_linkage_enabled 开关（统一委托 alert_service.is_regime_linkage_enabled，默认 True）"""
    from .alert_service import is_regime_linkage_enabled
    return is_regime_linkage_enabled()


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
        self._active_regime: Optional[Dict] = None
        self._active_as_of_date: Optional[str] = None
        self._active_linkage_enabled: bool = True

    def get_market_regime(self, as_of_date: Optional[str] = None) -> Dict:
        """
        获取大盘波动率与熔断风控状态（支持传入 as_of_date 回放指定历史交易日，如 '2026-10-15'）。
        优先通过 stock_service 拉取 ^VIX 与 QQQ 日线；若离线无数据则安全回退 NORMAL。
        """
        vix_val = 15.0
        qqq_change = 0.0
        try:
            vix_df = self.stock_service.get_stock_data("^VIX", "US", period="1mo")
            if vix_df is not None and not vix_df.empty and 'Close' in vix_df.columns:
                vix_df = vix_df.dropna(subset=['Close'])
                if as_of_date:
                    vix_df = vix_df[vix_df.index <= as_of_date]
                if not vix_df.empty:
                    vix_val = float(vix_df['Close'].iloc[-1])
        except Exception:
            pass

        try:
            qqq_df = self.stock_service.get_stock_data("QQQ", "US", period="1mo")
            if qqq_df is not None and not qqq_df.empty and 'Close' in qqq_df.columns:
                qqq_df = qqq_df.dropna(subset=['Close'])
                if as_of_date:
                    qqq_df = qqq_df[qqq_df.index <= as_of_date]
                if len(qqq_df) >= 2:
                    c1 = float(qqq_df['Close'].iloc[-1])
                    c0 = float(qqq_df['Close'].iloc[-2])
                    if c0 > 0:
                        qqq_change = (c1 - c0) / c0 * 100.0
        except Exception:
            pass

        return evaluate_market_regime(vix_val, qqq_change)

    # ============================================
    # 单只股票：三策略快照（轻量，不拉新闻，支持熔断状态真实联动点位）
    # ============================================
    def _stock_snapshot(
        self,
        stock_code: str,
        stock_name: str,
        regime: Optional[Dict] = None,
        as_of_date: Optional[str] = None,
    ) -> Optional[Dict]:
        market = detect_market(stock_code)
        df = self.stock_service.get_stock_data(stock_code, market, period="3mo")
        if df is None or df.empty:
            return None

        eff_as_of = as_of_date or self._active_as_of_date
        if eff_as_of:
            df = df[df.index <= eff_as_of]
            if df.empty:
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

        # 线性/非线性常态基准买点（走 utils/price_levels.py 唯一定义处）
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

        # 若调用方显式传入 regime 参数则强制按传入值执行；否则尊重全局 regime_linkage_enabled 开关
        if regime is not None:
            eff_regime = regime
            linkage_on = True
        else:
            eff_regime = self._active_regime or {'status': 'NORMAL', 'position_scale': 1.0}
            linkage_on = self._active_linkage_enabled

        raw_regime_status = (eff_regime.get('status') or 'NORMAL').upper()
        effective_status = raw_regime_status if linkage_on else 'NORMAL'

        linear_levels = apply_regime_to_levels(
            current, linear_buy, LINEAR_PROFIT_PCT,
            regime_status=effective_status, atr=atr_val,
            stop_atr_mult=2.0, profit_atr_mult=3.5,
        )
        nonlinear_levels = apply_regime_to_levels(
            current, nonlinear_buy, NONLINEAR_PROFIT_PCT,
            regime_status=effective_status, atr=atr_val,
            stop_atr_mult=2.2, profit_atr_mult=6.5,
        )

        macd_add = round(ma20, 2) if macd_golden else None
        macd_watch = round(bb_lower, 2) if macd_golden is False else None
        position_scale = 1.0

        if effective_status == 'CAUTION':
            position_scale = 0.5
            if macd_add is not None:
                macd_add = round(macd_add * CAUTION_BUY_DISCOUNT, 2)
            if macd_watch is not None:
                macd_watch = round(macd_watch * CAUTION_BUY_DISCOUNT, 2)
            if '买入' in rec:
                rec, color = '谨慎建仓(半仓)', 'warning'
        elif effective_status == 'CIRCUIT_BREAKER':
            position_scale = 0.0
            macd_add = None
            macd_watch = None
            rec, color = '熔断避险', 'warning'

        return {
            'code': stock_code,
            'name': stock_name,
            'market': market,
            'price': round(current, 2),
            'rsi': round(rsi, 1),
            'tech': round(tech, 1),
            'rec': rec,
            'color': color,
            'regime_status': effective_status,
            'raw_regime_status': raw_regime_status,
            'regime_linkage_enabled': linkage_on,
            'position_scale': position_scale,
            'macd_state': macd_state,
            'macd_golden': macd_golden,
            # MACD策略操作参考（熔断档全部置None，禁止新开仓）
            'macd_add': macd_add,
            'macd_watch': macd_watch,
            'macd_stop': discipline_stop(current),                        # 纪律止损-8%
            'linear': linear_levels,
            'nonlinear': nonlinear_levels,
        }

    # ============================================
    # 生成日报（企业微信markdown格式，支持并行快照与市场风控状态真实联动）
    # ============================================
    def generate_daily_report(
        self,
        watchlist: List[Dict],
        regime: Optional[Dict] = None,
        as_of_date: Optional[str] = None,
    ) -> Dict:
        """watchlist: [{stock_code, stock_name}] → {report, snapshots, failed, regime}"""
        snapshots: List[Dict] = []
        failed: List[str] = []

        linkage_on = True if regime is not None else _is_regime_linkage_on()
        resolved_regime = regime if regime is not None else self.get_market_regime(as_of_date=as_of_date)
        prev_regime = self._active_regime
        prev_as_of = self._active_as_of_date
        prev_linkage = self._active_linkage_enabled
        self._active_regime = resolved_regime
        self._active_as_of_date = as_of_date
        self._active_linkage_enabled = linkage_on

        def _safe_snap(item: Dict):
            code = item['stock_code']
            name = item['stock_name']
            try:
                snap = self._stock_snapshot(code, name)
                return (snap, None) if snap else (None, f"{code} {name}")
            except Exception as e:
                print(f"快照失败 {code}: {e}")
                return (None, f"{code} {name}")

        try:
            if watchlist:
                with ThreadPoolExecutor(max_workers=min(8, len(watchlist))) as executor:
                    results = list(executor.map(_safe_snap, watchlist))
                for snap, err in results:
                    if snap:
                        snapshots.append(snap)
                    elif err:
                        failed.append(err)
        finally:
            self._active_regime = prev_regime
            self._active_as_of_date = prev_as_of
            self._active_linkage_enabled = prev_linkage

        # 按技术分排序
        snapshots.sort(key=lambda s: s['tech'], reverse=True)

        now = now_beijing()
        regime_status = (resolved_regime.get('status') or 'NORMAL').upper()
        lines = [
            f"**📈 股票日报·三策略版** {now.strftime('%m-%d %H:%M')}",
            f"> 自选{len(snapshots)}只 | 按技术评分排序",
        ]
        if regime_status != 'NORMAL':
            lines.append(f"> 🛡️ **大盘风控**: {resolved_regime.get('banner', regime_status)}")
            if linkage_on:
                lines.append(f"> 📌 **执行指令**: {resolved_regime.get('advice', '')}")
            else:
                lines.append("> 📌 **执行指令**: 熔断联动开关已关闭，以下展示未折算的原始策略点位")
        lines.append('')

        for i, s in enumerate(snapshots, 1):
            price_str = f"¥{s['price']}" if s['market'] == 'A' else f"${s['price']}"
            lines.append(
                f"**{i}. {s['name']} {s['code']}** <font color=\"{s['color']}\">{s['rec']}</font> {s['tech']}分"
            )
            lines.append(
                f"现价{price_str} RSI{s['rsi']} MACD:{s['macd_state']}"
            )
            if s['linear'].get('buy') is None:
                lines.append(
                    f"线性: 🚫熔断禁买(无新开仓点位) 持仓保护损{s['linear']['stop']}"
                )
            else:
                lin_atr_suffix = (
                    f" (ATR损{s['linear']['atr_stop']})"
                    if s['linear'].get('atr_stop') and s['linear']['atr_stop'] != s['linear']['stop']
                    else ""
                )
                caution_tag = " [买点已下移2%/半仓]" if s.get('regime_status') == 'CAUTION' else ""
                lines.append(
                    f"线性: 买{s['linear']['buy']}({s['linear']['distance']:+.1f}%) "
                    f"盈{s['linear']['profit']} 损{s['linear']['stop']}{lin_atr_suffix}{caution_tag}"
                )

            if s['nonlinear'].get('buy') is None:
                lines.append(
                    f"非线性: 🚫熔断禁买(无新开仓点位) 持仓保护损{s['nonlinear']['stop']}"
                )
            else:
                caution_tag = " [买点已下移2%/半仓]" if s.get('regime_status') == 'CAUTION' else ""
                lines.append(
                    f"非线性: 买{s['nonlinear']['buy']}({s['nonlinear']['distance']:+.1f}%) "
                    f"盈{s['nonlinear']['profit']} 损{s['nonlinear']['stop']}{caution_tag}"
                )

            if s.get('regime_status') == 'CIRCUIT_BREAKER':
                lines.append(f"MACD: {s['macd_state']} 🚨熔断禁买(无新开仓点位) 持仓保护损{s['macd_stop']}")
            elif s.get('macd_golden'):
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
            'regime': resolved_regime,
            'regime_linkage_enabled': linkage_on,
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
