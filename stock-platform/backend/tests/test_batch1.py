# -*- coding: utf-8 -*-
"""第一批功能单元测试：卖出计算 / 日报快照 / 风控规则 / 微信无损分包 / 2%科学建仓与半凯利计算"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.report_service import ReportService, _load_webhook, split_wechat_chunks


# ============================================
# 日报快照（monkeypatch快照函数，不依赖网络）
# ============================================
def _fake_snap(code, name, market='US', tech=6.0):
    """构造一份与_stock_snapshot同构的快照"""
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
        # 排序：技术分高的MU排第1；A股前缀¥、美股前缀$
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
        # 首尾股票均完整保留
        rejoined = "\n\n".join(chunks)
        assert "S01" in rejoined and "S49" in rejoined


# ============================================
# 风控规则常量与webhook加载
# ============================================
class TestRiskRules:
    def test_webhook_loaded_from_config(self):
        url = _load_webhook()
        # 用户原项目config配置了企业微信webhook
        assert url == '' or url.startswith('https://qyapi.weixin.qq.com')

    def test_market_detection(self):
        from app.utils.market import detect_market
        assert detect_market('002594') == 'A'
        assert detect_market('600519.SH') == 'A'
        assert detect_market('MU') == 'US'
        assert detect_market('AAPL') == 'US'


class TestPositionPnlMath:
    """卖出盈亏与2%风险仓位计算器：测试路由使用的共享计算函数"""

    def test_profit_case(self):
        from app.routers.portfolio import _realized_pnl
        pnl, pct = _realized_pnl(11.0, 10.0, 100)
        assert (pnl, pct) == (100.0, 10.0)

    def test_loss_case(self):
        from app.routers.portfolio import _realized_pnl
        pnl, pct = _realized_pnl(46.0, 50.0, 200)
        assert (pnl, pct) == (-800.0, -8.0)

    def test_zero_buy_price_no_crash(self):
        from app.routers.portfolio import _realized_pnl
        pnl, pct = _realized_pnl(10.0, 0, 100)
        assert (pnl, pct) == (1000.0, 0)

    def test_calculate_recommended_position_2pct_rule(self):
        from app.routers.portfolio import calculate_recommended_position
        # 10万本金，买价100，止损92（每股风险8）：2%风险(2000) -> 250股，首仓125股，占25%
        res = calculate_recommended_position(100000.0, 100.0, 92.0, win_rate=65.0)
        assert res['recommended_shares'] == 250
        assert res['first_tranche_shares'] == 125
        assert res['position_pct'] == 25.0
        assert res['capped_by_40pct'] is False
        assert res['half_kelly_pct'] is not None and res['half_kelly_pct'] > 0

    def test_calculate_recommended_position_capped_at_40pct(self):
        from app.routers.portfolio import calculate_recommended_position
        # 止损99（每股风险1）：2%风险可买2000股，但被40%单票上限截断为400股
        res = calculate_recommended_position(100000.0, 100.0, 99.0)
        assert res['recommended_shares'] == 400
        assert res['capped_by_40pct'] is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
