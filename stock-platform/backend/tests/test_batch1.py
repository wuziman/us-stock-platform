# -*- coding: utf-8 -*-
"""第一批功能单元测试 + P0 真实链路回归测试 + 第一段（P1-8 北京时间铁律）测试：
- 日报快照（含不mock _stock_snapshot 的真实链路集成测试）
- 卖出/修改持仓时区 aware vs naive datetime 兼容性与 PositionUpdate.buy_date 类型校验
- 第一段（P1-8）：_to_naive_dt 统一转 Asia/Shanghai 无时区北京时间；naive buy_date 分别配 +08:00 与 Z (UTC) sell_date 时 holding_days 严格一致
- Pydantic Schema gt=0/ge=0 与 Literal['simple', 'research'] 校验
- 跨市场汇率 Setting 唯一单点读取
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.models import Position, Setting
from app.routers.portfolio import (
    _enrich_holding,
    _realized_pnl,
    _to_naive_dt,
    calculate_recommended_position,
    get_portfolio_summary,
    sell_position,
    update_position,
)
from app.schemas import (
    AnalysisRequest,
    BacktestCompareRequest,
    BacktestRequest,
    PositionCreate,
    PositionSell,
    PositionUpdate,
)
from app.services.alert_service import get_usd_cny_rate
from app.services.report_service import ReportService, _load_webhook, split_wechat_chunks


def _make_realistic_ohlcv_df(days: int = 65, base_price: float = 100.0) -> pd.DataFrame:
    """构造真实感日K线DataFrame，供真实 _stock_snapshot 链路计算全部指标与ATR自适应点位"""
    idx = pd.date_range('2026-06-01', periods=days, freq='B')
    t = np.arange(days, dtype=float)
    close = base_price + 4.0 * np.sin(t / 6.0) + 0.15 * t
    open_ = close - 0.4
    high = close + 1.8
    low = close - 1.6
    vol = np.full(days, 1_500_000, dtype=int)
    return pd.DataFrame(
        {'Open': open_, 'High': high, 'Low': low, 'Close': close, 'Volume': vol},
        index=idx,
    )


def _fake_snap(code, name, market='US', tech=6.0):
    """构造一份与_stock_snapshot同构的快照（仅用于纯排版排序单测）"""
    return {
        'code': code, 'name': name, 'market': market,
        'price': 100.0, 'rsi': 50.0, 'tech': tech,
        'rec': '买入', 'color': 'info', 'macd_state': '🟢金叉3天',
        'macd_golden': True, 'macd_add': 99.0, 'macd_watch': None, 'macd_stop': 92.0,
        'linear': {'buy': 95.0, 'profit': 109.25, 'stop': 87.4, 'distance': 5.0},
        'nonlinear': {'buy': 97.0, 'profit': 141.62, 'stop': 89.24, 'distance': 3.0},
    }


class TestReportSnapshot:
    def _svc(self):
        return ReportService()

    def test_real_stock_snapshot_and_report_chain_no_internal_mock(self, monkeypatch):
        """
        [P0-1 核心回归测试] 严禁 mock _stock_snapshot 本身！
        仅 mock 外部数据源 get_stock_data，完整走通：
        _stock_snapshot -> calculate_all_indicators -> calculate_tech_score -> atr_adaptive_levels -> generate_daily_report
        """
        svc = self._svc()
        df_us = _make_realistic_ohlcv_df(65, base_price=120.0)
        df_cn = _make_realistic_ohlcv_df(65, base_price=250.0)

        def fake_get_stock_data(code, market='US', period='3mo', use_cache=True):
            return df_cn.copy() if market == 'A' else df_us.copy()

        monkeypatch.setattr(svc.stock_service, 'get_stock_data', fake_get_stock_data)

        # 1. 直接验证真实 _stock_snapshot 不抛 TypeError 且产出完整 ATR 点位
        snap_mu = svc._stock_snapshot('MU', 'Micron')
        assert snap_mu is not None
        assert snap_mu['market'] == 'US'
        assert 'atr_stop' in snap_mu['linear'] and snap_mu['linear']['atr_stop'] is not None
        assert 'atr_profit' in snap_mu['linear'] and snap_mu['linear']['atr_profit'] is not None
        assert 'atr_stop' in snap_mu['nonlinear'] and snap_mu['nonlinear']['atr_stop'] is not None
        assert 'atr_profit' in snap_mu['nonlinear'] and snap_mu['nonlinear']['atr_profit'] is not None

        # 2. 验证 generate_daily_report 走真实 _stock_snapshot 链路，绝不因参数名错误落入 failed
        result = svc.generate_daily_report([
            {'stock_code': 'MU', 'stock_name': 'Micron'},
            {'stock_code': '002594', 'stock_name': '比亚迪'},
        ])
        assert result['failed'] == [], f"真实链路不应有任何失败股票，实际 failed={result['failed']}"
        assert len(result['snapshots']) == 2
        assert '⚠️ 数据缺失' not in result['report']
        assert 'Micron' in result['report'] and '比亚迪' in result['report']

    def test_report_structure_and_sorting(self, monkeypatch):
        """真实拼装路径：按技术分降序，报告含标题/免责声明/币种符号"""
        svc = self._svc()
        snaps = {
            ('MU', 'Micron'): _fake_snap('MU', 'Micron', 'US', tech=7.0),
            ('002594', '比亚迪'): _fake_snap('002594', '比亚迪', 'A', tech=5.5),
        }
        monkeypatch.setattr(svc, '_stock_snapshot',
                            lambda code, name: snaps[(code, name)])

        result = svc.generate_daily_report([
            {'stock_code': '002594', 'stock_name': '比亚迪'},
            {'stock_code': 'MU', 'stock_name': 'Micron'},
        ])

        report = result['report']
        assert '三策略版' in report
        assert '自选2只' in report
        assert '仅供参考，投资有风险' in report
        assert report.index('1. Micron') < report.index('2. 比亚迪')
        assert '$100.0' in report and '¥100.0' in report

    def test_failed_stocks_listed(self, monkeypatch):
        """快照返回None或抛异常都进failed列表，且不中断其余股票生成"""
        svc = self._svc()

        def fake_snapshot(code, name):
            if code == 'ZZZZZZ':
                return None
            if code == 'BOOM':
                raise RuntimeError('boom')
            return _fake_snap(code, name)

        monkeypatch.setattr(svc, '_stock_snapshot', fake_snapshot)
        result = svc.generate_daily_report([
            {'stock_code': 'ZZZZZZ', 'stock_name': '不存在'},
            {'stock_code': 'BOOM', 'stock_name': '会炸'},
            {'stock_code': 'MU', 'stock_name': 'Micron'},
        ])

        assert result['failed'] == ['ZZZZZZ 不存在', 'BOOM 会炸']
        assert [s['code'] for s in result['snapshots']] == ['MU']
        assert isinstance(result['report'], str)
        assert '⚠️ 数据缺失: ZZZZZZ 不存在, BOOM 会炸' in result['report']

    def test_char_count_bytes(self, monkeypatch):
        """char_count应为UTF-8字节数（微信推送限制依据）"""
        svc = self._svc()
        monkeypatch.setattr(svc, '_stock_snapshot',
                            lambda code, name: _fake_snap(code, name))
        result = svc.generate_daily_report([
            {'stock_code': 'MU', 'stock_name': 'Micron'},
        ])
        expected = len(result['report'].encode('utf-8'))
        assert result['char_count'] == expected

    def test_split_wechat_chunks_lossless(self):
        """超长日报按段落无损分包，不截断丢弃任何股票"""
        cards = [
            f"**{i}. 测试股票 S{i:02d}** <font color=\"info\">强烈买入</font> 8.5分\n"
            f"现价$100.0 RSI50.0 MACD:🟢金叉3天\n"
            f"线性: 买95.0(-5.0%) 盈109.25 损87.4\n"
            f"非线性: 买97.0(-3.0%) 盈141.62 损89.24"
            for i in range(1, 50)
        ]
        long_report = "\n\n".join(cards)
        assert len(long_report.encode('utf-8')) > 3800
        chunks = split_wechat_chunks(long_report, max_bytes=3800)
        assert len(chunks) >= 2
        for c in chunks:
            assert len(c.encode('utf-8')) <= 3800
        rejoined = "\n\n".join(chunks)
        assert "S01" in rejoined and "S49" in rejoined


# ============================================
# 风控规则常量与webhook加载
# ============================================
class TestRiskRules:
    def test_webhook_loaded_from_config(self):
        url = _load_webhook()
        assert url == '' or url.startswith('https://qyapi.weixin.qq.com')

    def test_market_detection(self):
        from app.utils.market import detect_market
        assert detect_market('002594') == 'A'
        assert detect_market('600519.SH') == 'A'
        assert detect_market('MU') == 'US'
        assert detect_market('AAPL') == 'US'


class TestPositionPnlMath:
    """卖出盈亏、2%风险仓位计算器、P0-5/P0-6/P0-7/P0-9 及第一段 P1-8 时区铁律专项验证"""

    def test_profit_case(self):
        pnl, pct = _realized_pnl(11.0, 10.0, 100)
        assert (pnl, pct) == (100.0, 10.0)

    def test_loss_case(self):
        pnl, pct = _realized_pnl(46.0, 50.0, 200)
        assert (pnl, pct) == (-800.0, -8.0)

    def test_zero_buy_price_no_crash(self):
        pnl, pct = _realized_pnl(10.0, 0, 100)
        assert (pnl, pct) == (1000.0, 0)

    def test_calculate_recommended_position_2pct_rule(self):
        res = calculate_recommended_position(100000.0, 100.0, 92.0, win_rate=65.0)
        assert res['recommended_shares'] == 250
        assert res['first_tranche_shares'] == 125
        assert res['position_pct'] == 25.0
        assert res['capped_by_40pct'] is False
        assert res['half_kelly_pct'] is not None and res['half_kelly_pct'] > 0

    def test_calculate_recommended_position_capped_at_40pct(self):
        res = calculate_recommended_position(100000.0, 100.0, 99.0)
        assert res['recommended_shares'] == 400
        assert res['capped_by_40pct'] is True

    def test_p0_5_and_p0_6_update_and_sell_datetime_safety(self):
        """
        [P0-5 & P0-6 验证]
        1. PositionUpdate.buy_date 解析为 datetime 而非 date，修改后调 _enrich_holding 不报 TypeError
        2. sell_position 接收带时区 aware sell_date 减 SQLite 无时区 naive buy_date 不报 TypeError
        """
        db = SessionLocal()
        try:
            pos = Position(
                stock_code='MU',
                stock_name='Micron',
                market='US',
                buy_price=100.0,
                quantity=10,
                buy_date=datetime(2026, 8, 10, 9, 30),  # SQLite 读回的 naive 北京时间
                status='holding',
                created_at=datetime(2026, 8, 10, 9, 30),
            )
            db.add(pos)
            db.commit()

            # P0-6: 用 PositionUpdate 修改买入日期后，_enrich_holding 正常计算 holding_days
            upd = PositionUpdate(buy_date=datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc))
            assert isinstance(upd.buy_date, datetime)
            asyncio.run(update_position(pos.id, upd, db=db))
            enriched = _enrich_holding(pos, {'price': 108.0})
            assert enriched.holding_days >= 0

            # P0-5: 传入带时区的 sell_date 卖出，与 naive buy_date 相减不抛 TypeError
            sell_req = PositionSell(
                sell_price=115.0,
                sell_date=datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc),
            )
            res = asyncio.run(sell_position(pos.id, sell_req, db=db))
            assert res['realized_pnl'] == 150.0
            assert res['holding_days'] == 12
        finally:
            db.query(Position).delete()
            db.commit()
            db.close()

    def test_segment1_beijing_timezone_holding_days_consistency(self):
        """
        [第一段 P1-8 核心验证]
        - _to_naive_dt: aware → astimezone(Asia/Shanghai) 后去 tzinfo；naive → 视为北京时间直接通过
        - naive 北京时间 buy_date 分别配 +08:00 和 Z（UTC）的 sell_date，holding_days 必须一致！
          特别挑选跨日边界时刻：北京时间 2026-08-20 12:00:00+08:00 等于 UTC 2026-08-20 04:00:00Z。
          若错误按 UTC 归一化，04:00 < 10:00 会算成 9 天；按北京时间归一化，两者均为 2026-08-20 12:00:00，
          减去 naive 北京时间 buy_date (2026-08-10 10:00:00) 均严格等于 10 天。
        """
        tz_plus8 = timezone(timedelta(hours=8))
        naive_buy = datetime(2026, 8, 10, 10, 0, 0)  # naive 北京时间 2026-08-10 10:00

        # 同一物理时刻的两种时区表示：+08:00 与 Z (UTC)
        sell_plus8 = datetime(2026, 8, 20, 12, 0, 0, tzinfo=tz_plus8)
        sell_utc_z = datetime(2026, 8, 20, 4, 0, 0, tzinfo=timezone.utc)

        # 1. 验证 _to_naive_dt 直接转换结果完全相同且均为无时区北京时间 2026-08-20 12:00:00
        assert _to_naive_dt(naive_buy) == naive_buy
        assert _to_naive_dt(sell_plus8) == datetime(2026, 8, 20, 12, 0, 0)
        assert _to_naive_dt(sell_utc_z) == datetime(2026, 8, 20, 12, 0, 0)
        assert _to_naive_dt(sell_plus8).tzinfo is None
        assert _to_naive_dt(sell_utc_z).tzinfo is None

        # 2. 走真实 sell_position 接口分别卖出两笔相同 naive 北京时间 buy_date 的持仓
        db = SessionLocal()
        try:
            pos_a = Position(
                stock_code='AAPL', stock_name='Apple', market='US',
                buy_price=200.0, quantity=10, buy_date=naive_buy,
                status='holding',
            )
            pos_b = Position(
                stock_code='AAPL', stock_name='Apple', market='US',
                buy_price=200.0, quantity=10, buy_date=naive_buy,
                status='holding',
            )
            db.add_all([pos_a, pos_b])
            db.commit()

            res_plus8 = asyncio.run(
                sell_position(pos_a.id, PositionSell(sell_price=220.0, sell_date=sell_plus8), db=db)
            )
            res_utc_z = asyncio.run(
                sell_position(pos_b.id, PositionSell(sell_price=220.0, sell_date=sell_utc_z), db=db)
            )

            assert res_plus8['holding_days'] == 10
            assert res_utc_z['holding_days'] == 10
            assert res_plus8['holding_days'] == res_utc_z['holding_days']
        finally:
            db.query(Position).delete()
            db.commit()
            db.close()

    def test_p0_7_schema_validation_constraints(self):
        """[P0-7 验证] 负手续费、非正初始资金、非正买入价与非法 mode 均被 Pydantic 拦截"""
        with pytest.raises(ValidationError):
            BacktestRequest(stock_code='MU', strategy='linear', commission_per_trade=-1.0)
        with pytest.raises(ValidationError):
            BacktestRequest(stock_code='MU', strategy='linear', initial_capital=0)
        with pytest.raises(ValidationError):
            BacktestCompareRequest(stock_code='MU', commission_per_trade=-0.5)
        with pytest.raises(ValidationError):
            PositionCreate(stock_code='MU', stock_name='Micron', buy_price=0, quantity=10, buy_date=datetime.now())
        with pytest.raises(ValidationError):
            PositionCreate(stock_code='MU', stock_name='Micron', buy_price=100, quantity=0, buy_date=datetime.now())
        with pytest.raises(ValidationError):
            AnalysisRequest(stock_code='MU', stock_name='Micron', mode='invalid_mode')
        # 合法的 simple / research 正常通过
        assert AnalysisRequest(stock_code='MU', stock_name='Micron', mode='research').mode == 'research'

    def test_p0_9_single_source_usd_cny_rate(self, monkeypatch):
        """[P0-9 验证] Setting 表中的 usd_cny_rate 被 alert_service 与 portfolio.get_portfolio_summary 统一读取"""
        import app.routers.portfolio as pf_router
        monkeypatch.setattr(pf_router, '_fetch_quotes', lambda positions: [{'price': p.buy_price} for p in positions])
        db = SessionLocal()
        try:
            db.query(Setting).filter(Setting.key == 'usd_cny_rate').delete()
            db.add(Setting(key='usd_cny_rate', value='7.0'))
            db.add(Position(stock_code='MU', stock_name='Micron', market='US',
                            buy_price=100.0, quantity=10, buy_date=datetime.now(),
                            status='holding', created_at=datetime.now()))
            db.add(Position(stock_code='002594', stock_name='比亚迪', market='A',
                            buy_price=700.0, quantity=10, buy_date=datetime.now(),
                            status='holding', created_at=datetime.now()))
            db.commit()

            assert get_usd_cny_rate(db) == 7.0
            summary = get_portfolio_summary(db=db)
            assert summary['usd_cny_rate'] == 7.0
            # 美股 100*10=$1000 + A股 700*10/7.0=$1000 = $2000.0
            assert summary['total_value'] == 2000.0
        finally:
            db.query(Position).delete()
            db.query(Setting).filter(Setting.key == 'usd_cny_rate').delete()
            db.commit()
            db.close()




# ============================================
# [第四段-10a] Router HTTP 层集成测试（覆盖 portfolio / report / backtest / stocks 核心路由）
# ============================================
class TestHttpRoutersIntegration:
    def test_portfolio_router_crud_and_sell_flow(self, monkeypatch):
        """测试 /api/portfolio 建仓 -> 持仓列表 -> 修改 -> 汇总 -> 卖出 -> 历史 -> 删除 全生命周期"""
        import app.routers.portfolio as pf_router
        from fastapi import HTTPException
        from app.models import Position, Setting

        monkeypatch.setattr(pf_router, "_fetch_quotes", lambda positions: [{"price": 112.0} for _ in positions])
        db = SessionLocal()
        try:
            db.query(Position).delete()
            db.commit()

            # 1. 非法止损（>= 买入价）触发 HTTPException(400)
            with pytest.raises(HTTPException):
                asyncio.run(pf_router.add_position(
                    PositionCreate(
                        stock_code="MU", stock_name="Micron", buy_price=100.0,
                        quantity=20, buy_date=datetime(2026, 9, 1, 10, 0), stop_loss=105.0,
                    ),
                    db=db,
                ))

            # 2. 正常建仓
            created = asyncio.run(pf_router.add_position(
                PositionCreate(
                    stock_code="MU", stock_name="Micron", buy_price=100.0,
                    quantity=20, buy_date=datetime(2026, 9, 1, 10, 0),
                    stop_loss=92.0, take_profit=115.0,
                ),
                db=db,
            ))
            assert created.stock_code == "MU" and created.market == "US"

            # 3. 持仓列表与汇总
            holdings = pf_router.get_positions(db=db)
            assert len(holdings) == 1
            assert holdings[0].profit_loss == 240.0
            summary = pf_router.get_portfolio_summary(db=db)
            assert summary["total_positions"] == 1
            assert summary["total_value"] == 2240.0

            # 4. 卖出与历史查询
            sold = asyncio.run(pf_router.sell_position(
                created.id,
                PositionSell(sell_price=115.0, sell_date=datetime(2026, 9, 11, 10, 0)),
                db=db,
            ))
            assert sold["realized_pnl"] == 300.0
            assert sold["holding_days"] == 10

            history = asyncio.run(pf_router.get_position_history(db=db))
            assert len(history) == 1 and history[0].holding_days == 10

            # 5. 删除记录
            del_res = asyncio.run(pf_router.delete_position(created.id, db=db))
            assert del_res["message"] == "删除成功"
        finally:
            db.query(Position).delete()
            db.query(Setting).filter(Setting.key == "total_capital").delete()
            db.commit()
            db.close()

    def test_report_and_backtest_routers_integration(self, monkeypatch):
        """测试 /api/report 与 /api/backtest 路由接口真实调用链路"""
        import app.routers.report as rep_router
        import app.routers.backtest as bt_router
        from app.models import Watchlist
        from app.schemas import BacktestRequest, OptimizeRequest, WalkForwardRequest

        df_long = _make_realistic_ohlcv_df(200, base_price=100.0)
        monkeypatch.setattr(
            rep_router.report_service.stock_service,
            "get_stock_data",
            lambda code, market="US", period="3mo", use_cache=True: df_long.copy(),
        )
        monkeypatch.setattr(
            bt_router.backtest_service.stock_service,
            "get_stock_data",
            lambda code, market="US", period="1y", use_cache=True: df_long.copy(),
        )

        db = SessionLocal()
        try:
            db.query(Watchlist).delete()
            db.add(Watchlist(stock_code="MU", stock_name="Micron", market="US"))
            db.commit()

            # 1. 日报预览与 dry_run 发送
            preview = rep_router.preview_daily_report(db=db)
            assert "Micron" in preview["report"] and preview["failed"] == []
            send_dry = rep_router.send_daily_report(rep_router.ReportSendRequest(dry_run=True), db=db)
            assert send_dry["sent"] is False and "dry_run" in send_dry["message"]

            # 2. 定时调度配置更新与查询
            sched = asyncio.run(rep_router.update_schedule(
                rep_router.ScheduleUpdateRequest(enabled=True, hour=17, minute=30), db=db
            ))
            assert sched["enabled"] is True and sched["hour"] == 17

            # 3. 回测 / 寻优 / Walk-Forward 路由
            bt_out = bt_router.run_backtest(
                BacktestRequest(stock_code="002594", strategy="linear", period="1y")
            )
            assert bt_out["benchmark_code"] == "000300.SS"

            opt_out = bt_router.optimize_strategy(
                OptimizeRequest(stock_code="MU", strategy="linear", period="1y")
            )
            assert opt_out["in_sample_warning"] == "样本内寻优，请用 Walk-Forward 验证"

            wf_out = bt_router.walk_forward(
                WalkForwardRequest(stock_code="MU", strategy="linear", period="1y", segments=2)
            )
            assert "param_stability" in wf_out
            assert wf_out["param_stability"]["stability_rating"] in ("高", "中", "低")
        finally:
            db.query(Watchlist).delete()
            db.commit()
            db.close()



    def test_segment4_add_tranche_in_place_initial_price_anchor_and_reset(self, monkeypatch):
        """
        [第四段-1 验证] POST /api/portfolio/{id}/add-tranche 原行阶梯补仓状态机：
        - 原地更新同一行（不新建第二条 Position 行）
        - initial_price 永久锚定首仓价（100.0），不随摊平均价（98.0）下移
        - 第1档(-5%)、第2档(-10%)、第3档(-15%)严格按 initial_price 校验，满3次后第4次报 400
        - 补仓后 stop_loss = round(new_buy_price * 0.92, 2) 重算并返回 previous_stop_loss / stop_loss_shift_note
        - 清仓卖出后再建仓同代码股票，add_count 与 initial_price 自动重置
        """
        import app.routers.portfolio as pf_router
        from fastapi import HTTPException
        from app.models import Position
        from app.schemas import PositionAddTrancheRequest

        monkeypatch.setattr(pf_router, "_fetch_quotes", lambda positions: [{"price": 94.0} for _ in positions])
        db = SessionLocal()
        try:
            db.query(Position).delete()
            db.commit()

            # 1. 首仓买入 MU 20股 @ $100.0
            pos = asyncio.run(pf_router.add_position(
                PositionCreate(
                    stock_code="MU", stock_name="Micron", buy_price=100.0,
                    quantity=20, buy_date=datetime(2026, 9, 1, 10, 0),
                ),
                db=db,
            ))
            assert pos.initial_price == 100.0
            assert pos.add_count == 0
            assert pos.stop_loss == 92.0

            # 重复调用 add_position 同一持仓中股票应被 400 拦截并引导走 add-tranche
            with pytest.raises(HTTPException):
                asyncio.run(pf_router.add_position(
                    PositionCreate(
                        stock_code="MU", stock_name="Micron", buy_price=95.0,
                        quantity=10, buy_date=datetime(2026, 9, 2, 10, 0),
                    ),
                    db=db,
                ))

            # 2. 跌 4% ($96.0) 未达第1档(-5%即<=95.0) -> 400 拒绝
            with pytest.raises(HTTPException):
                asyncio.run(pf_router.add_position_tranche(
                    pos.id, PositionAddTrancheRequest(add_price=96.0, add_quantity=10), db=db
                ))

            # 3. 第1次补仓：@ $94.0 (-6% >= 5%) 补 10 股 -> 原行更新，加权均价=(2000+940)/30=98.0，initial_price 仍为 100.0
            t1 = asyncio.run(pf_router.add_position_tranche(
                pos.id, PositionAddTrancheRequest(add_price=94.0, add_quantity=10), db=db
            ))
            assert db.query(Position).filter(Position.status == "holding").count() == 1
            assert t1["initial_price"] == 100.0
            assert t1["new_buy_price"] == 98.0
            assert t1["new_quantity"] == 30
            assert t1["add_count"] == 1
            assert t1["previous_stop_loss"] == 92.0
            assert t1["new_stop_loss"] == round(98.0 * 0.92, 2)  # 90.16（随新均价被动下移并显式披露）
            assert "被动下移" in t1["stop_loss_shift_note"]

            # 4. 关键验证：此时均价已降至 98.0，若在 $92.0 尝试第2次补仓（较首仓100仅跌8% < 10%），必须因锚定 initial_price=100 被拒！
            with pytest.raises(HTTPException):
                asyncio.run(pf_router.add_position_tranche(
                    pos.id, PositionAddTrancheRequest(add_price=92.0, add_quantity=10), db=db
                ))

            # 5. 第2次补仓：@ $89.0 (较首仓100跌11% >= 10%) -> 成功，add_count=2
            t2 = asyncio.run(pf_router.add_position_tranche(
                pos.id, PositionAddTrancheRequest(add_price=89.0, add_quantity=10), db=db
            ))
            assert t2["add_count"] == 2
            assert t2["initial_price"] == 100.0

            # 6. 第3次补仓：@ $84.0 (较首仓100跌16% >= 15%) -> 成功，add_count=3
            t3 = asyncio.run(pf_router.add_position_tranche(
                pos.id, PositionAddTrancheRequest(add_price=84.0, add_quantity=10), db=db
            ))
            assert t3["add_count"] == 3
            assert t3["remaining_adds"] == 0

            # 7. 第4次补仓：即使暴跌至 $70.0 或传 force=True，也必须被 3 次硬上限 400 拦截！
            with pytest.raises(HTTPException):
                asyncio.run(pf_router.add_position_tranche(
                    pos.id, PositionAddTrancheRequest(add_price=70.0, add_quantity=10, force=True), db=db
                ))

            # 8. 清仓卖出后再重新买入 MU @ $110.0 -> 新建 holding 行，initial_price=110.0, add_count=0 自动重置
            asyncio.run(pf_router.sell_position(
                pos.id, PositionSell(sell_price=105.0, sell_date=datetime(2026, 9, 20, 10, 0)), db=db
            ))
            reopened = asyncio.run(pf_router.add_position(
                PositionCreate(
                    stock_code="MU", stock_name="Micron", buy_price=110.0,
                    quantity=15, buy_date=datetime(2026, 9, 25, 10, 0),
                ),
                db=db,
            ))
            assert reopened.initial_price == 110.0
            assert reopened.add_count == 0
        finally:
            db.query(Position).delete()
            db.commit()
            db.close()

    def test_segment4_kelly_4level_fallback_l2_daily_cache_and_negative_edge(self, monkeypatch):
        """
        [第四段-3 & 补充项1/2 验证]
        - L1 (stock_profiles.json / 内置实测画像)：LITE 取实测胜率 55.6%、实测盈亏比 1.85 (rr_source=empirical_profile)
        - 负期望拦截：NKE (实测胜率 28.6%, 盈亏比 1.10 -> Kelly <= 0) 在 half_kelly_combined 下返回 0 股 + kelly_warning
        - L2 (1y 回测按天缓存)：未收录于画像的股票 AAPL 触发 1y 回测，同日连续调用仅执行 1 次回测（命中 _L2_BACKTEST_KELLY_CACHE）
        - L3 (手动传参) & 理论名义比标注：手动传 win_rate=60 时，rr_source 明确标为 theoretical_target_stop_ratio (linear=1.875, nonlinear=5.75)
        - L4 (缺数据跳过 Kelly)：无画像、无回测、无手动传参时，kelly_source=skipped，绝不算成 0 股
        """
        import app.routers.portfolio as pf_router
        from app.services.backtest_service import BacktestService

        pf_router.clear_l2_kelly_cache()

        # 1. L1 实测画像正期望标的 LITE
        r_lite = pf_router.calculate_recommended_position(
            capital=100000.0, buy_price=100.0, stop_loss=92.0,
            stock_code="LITE", strategy="linear", total_score=7.0,
            sizing_mode="half_kelly_combined",
        )
        assert r_lite["kelly_source"] == "empirical_profile"
        assert r_lite["rr_source"] == "empirical_profile"
        assert r_lite["recommended_shares"] > 0
        assert r_lite["recommended_shares"] == min(
            r_lite["risk_2pct_shares"], r_lite["half_kelly_shares"], r_lite["score_tier_shares"]
        )

        # 2. 实测负期望标的 NKE -> Kelly <= 0 -> 0 股 + 警告
        r_nke = pf_router.calculate_recommended_position(
            capital=100000.0, buy_price=100.0, stop_loss=92.0,
            stock_code="NKE", strategy="linear", total_score=7.0,
            sizing_mode="half_kelly_combined",
        )
        assert r_nke["half_kelly_pct"] == 0.0
        assert r_nke["recommended_shares"] == 0
        assert r_nke["kelly_warning"] is not None and "负期望" in r_nke["kelly_warning"]

        # 3. L2 1y 回测按天缓存验证（AAPL 不在 EMPIRICAL_STOCK_PROFILES 中）
        bt_calls = []

        def fake_run_backtest(self, code, strat, market="US", period="1y", **kw):
            bt_calls.append((code, strat, period))
            return {
                "win_rate": 58.0,
                "trade_count": 5,
                "trades": [
                    {"action": "sell", "profit_pct": 0.14},
                    {"action": "sell", "profit_pct": 0.16},
                    {"action": "sell", "profit_pct": -0.07},
                ],
            }

        monkeypatch.setattr(BacktestService, "run_backtest", fake_run_backtest)
        r_aapl_1 = pf_router.calculate_recommended_position(
            capital=100000.0, buy_price=100.0, stop_loss=92.0,
            stock_code="AAPL", strategy="linear", sizing_mode="half_kelly_combined",
        )
        r_aapl_2 = pf_router.calculate_recommended_position(
            capital=100000.0, buy_price=100.0, stop_loss=92.0,
            stock_code="AAPL", strategy="linear", sizing_mode="half_kelly_combined",
        )
        assert len(bt_calls) == 1, f"L2 1y回测应按天缓存，但实际调用了 {len(bt_calls)} 次"
        assert r_aapl_1["kelly_source"] == "empirical_backtest_1y"
        assert r_aapl_1["rr_source"] == "empirical_backtest_1y"
        assert r_aapl_1["recommended_shares"] == r_aapl_2["recommended_shares"] > 0

        # 4. L3 手动传参 + 理论名义盈亏比 (1.875 / 5.75) 来源标注
        r_manual_nl = pf_router.calculate_recommended_position(
            capital=100000.0, buy_price=100.0, stop_loss=92.0,
            win_rate=55.0, reward_risk_ratio=5.75, strategy="nonlinear",
            sizing_mode="half_kelly_combined",
        )
        assert r_manual_nl["kelly_source"] == "manual"
        assert r_manual_nl["rr_source"] == "theoretical_target_stop_ratio"
        assert r_manual_nl["reward_risk_ratio_used"] == 5.75

        # 5. L4 缺数据跳过 Kelly（回测无足够交易笔数且未手动传 win_rate -> 绝不报错、绝不算 0 股）
        monkeypatch.setattr(BacktestService, "run_backtest", lambda *a, **kw: {"error": "no data"})
        pf_router.clear_l2_kelly_cache()
        r_skip = pf_router.calculate_recommended_position(
            capital=100000.0, buy_price=100.0, stop_loss=92.0,
            stock_code="UNKNOWN_TICKER", strategy="linear", total_score=7.0,
            sizing_mode="half_kelly_combined",
        )
        assert r_skip["kelly_source"] == "skipped"
        assert r_skip["half_kelly_shares"] is None
        assert r_skip["recommended_shares"] == 250  # 正常走 min(2%风险=250, 评分分档=300)

    def test_segment4_unified_regime_linkage_switch_across_all_three_exits(self, monkeypatch):
        """
        [第四段-2 验证] regime_linkage_enabled 唯一总开关统管全部 3 个决策出口：
        1. report_service.generate_daily_report
        2. analysis_service.analyze_stock
        3. portfolio.get_position_size
        当开关为 True 时三处均联动 CAUTION（买点-2%，仓位×0.5）；
        当开关切为 False 时，大盘横幅仍如实显示 CAUTION，但三处点位与股数全部恢复 NORMAL（1.0）！
        """
        import app.routers.portfolio as pf_router
        from app.models import Setting
        from app.schemas import RiskSwitchesUpdate
        from app.services.analysis_service import AnalysisService
        from app.services.report_service import ReportService

        df_stock = _make_realistic_ohlcv_df(65, base_price=110.0)
        caution_regime = {
            "status": "CAUTION",
            "level": 1,
            "vix": 24.5,
            "qqq_change": -1.8,
            "buy_discount": 0.98,
            "position_scale": 0.5,
            "banner": "🟡 波动防守预警",
            "advice": "买点下移2%，仓位减半",
        }
        monkeypatch.setattr(ReportService, "get_market_regime", lambda self, as_of_date=None: dict(caution_regime))

        rep_svc = ReportService()
        monkeypatch.setattr(rep_svc.stock_service, "get_stock_data", lambda *a, **kw: df_stock.copy())

        an_svc = AnalysisService()
        monkeypatch.setattr(an_svc.stock_service, "get_stock_data", lambda *a, **kw: df_stock.copy())
        monkeypatch.setattr(an_svc, "_analyze_news", lambda *a, **kw: (6.0, {"news": []}))
        monkeypatch.setattr(an_svc, "_analyze_macro", lambda *a, **kw: (6.0, {"indicators": {}}))
        monkeypatch.setattr(an_svc, "_analyze_events", lambda *a, **kw: (6.0, {"events": []}))

        db = SessionLocal()
        try:
            # A. 默认开启 regime_linkage_enabled = True
            pf_router.update_risk_switches_endpoint(RiskSwitchesUpdate(regime_linkage_enabled=True), db=db)

            rep_on = rep_svc.generate_daily_report([{"stock_code": "MU", "stock_name": "Micron"}])
            an_on = an_svc.analyze_stock("MU", "Micron", "US")
            sz_on = pf_router.get_position_size(
                buy_price=100.0, stop_loss=92.0, total_capital=100000.0,
                win_rate=None, reward_risk_ratio=None, stock_code="LITE",
                strategy="linear", total_score=8.0, sizing_mode="half_kelly_combined", db=db,
            )

            assert rep_on["snapshots"][0]["position_scale"] == 0.5
            assert an_on["price_levels"]["position_scale"] == 0.5
            assert sz_on["regime_scale"] == 0.5

            # B. 关闭总开关 regime_linkage_enabled = False -> 三处全部退回 1.0 原始点位与股数
            pf_router.update_risk_switches_endpoint(RiskSwitchesUpdate(regime_linkage_enabled=False), db=db)

            rep_off = rep_svc.generate_daily_report([{"stock_code": "MU", "stock_name": "Micron"}])
            an_off = an_svc.analyze_stock("MU", "Micron", "US")
            sz_off = pf_router.get_position_size(
                buy_price=100.0, stop_loss=92.0, total_capital=100000.0,
                win_rate=None, reward_risk_ratio=None, stock_code="LITE",
                strategy="linear", total_score=8.0, sizing_mode="half_kelly_combined", db=db,
            )

            assert rep_off["regime"]["status"] == "CAUTION"  # 顶部横幅仍保留实况
            assert rep_off["snapshots"][0]["position_scale"] == 1.0
            assert rep_off["snapshots"][0]["linear"]["buy_low"] == rep_on["snapshots"][0]["linear"]["raw_buy"]
            assert an_off["price_levels"]["position_scale"] == 1.0
            assert an_off["price_levels"]["linear"]["buy_low"] == an_on["price_levels"]["linear"]["raw_buy"]
            assert sz_off["regime_scale"] == 1.0
            assert sz_off["recommended_shares"] == sz_on["recommended_shares"] * 2
        finally:
            db.query(Setting).delete()
            db.commit()
            db.close()



    def test_consolidate_legacy_duplicate_holding_rows(self):
        """
        验证存量重复 holding 行自动合并（consolidate_duplicate_holdings）：
        若库中已有同一只股票的多条 holding 记录（如历史版本多次新建的 MU 持仓），
        合并后仅保留最早主行，initial_price 锚定首笔买入价，buy_price 为加权均价，
        quantity 累加，add_count 计入补仓次数，彻底消除重复告警隐患。
        """
        import app.routers.portfolio as pf_router
        from app.models import Position

        db = SessionLocal()
        try:
            db.query(Position).delete()
            # 模拟历史遗留的两行同代码 MU holding 记录：首笔 20股@$100，次笔 10股@$94
            p1 = Position(
                stock_code="MU", stock_name="Micron", market="US",
                buy_price=100.0, quantity=20,
                buy_date=datetime(2026, 8, 1, 10, 0),
                stop_loss=92.0, status="holding",
            )
            p2 = Position(
                stock_code="MU", stock_name="Micron", market="US",
                buy_price=94.0, quantity=10,
                buy_date=datetime(2026, 8, 15, 10, 0),
                stop_loss=86.48, status="holding",
            )
            db.add_all([p1, p2])
            db.commit()
            assert db.query(Position).filter(Position.status == "holding").count() == 2

            res = pf_router.consolidate_duplicate_holdings(db)
            assert res["duplicate_codes_found"] == 1
            m = res["merged"][0]
            assert m["stock_code"] == "MU"
            assert m["initial_price"] == 100.0
            assert m["new_buy_price"] == 98.0
            assert m["new_quantity"] == 30
            assert m["add_count"] == 1
            assert m["new_stop_loss"] == 90.16

            remaining = db.query(Position).filter(Position.status == "holding").all()
            assert len(remaining) == 1
            assert remaining[0].id == p1.id

            # 验证被吃掉的次行转为 status="merged" 软归档，原始税基字段（日期/价格/股数）分毫不丢
            merged_rows = db.query(Position).filter(Position.status == "merged").all()
            assert len(merged_rows) == 1
            assert merged_rows[0].id == p2.id
            assert merged_rows[0].buy_price == 94.0
            assert merged_rows[0].quantity == 10
            assert merged_rows[0].buy_date == datetime(2026, 8, 15, 10, 0)
            assert m["merged_ids"] == [p2.id]
        finally:
            db.query(Position).delete()
            db.commit()
            db.close()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
