# -*- coding: utf-8 -*-
"""
AI选股纯函数测试：prompt构建 / 响应解析 / 小红书页面解析 / 路径定位与实盘战绩收益计算
不依赖网络、数据库与LLM调用。
运行：cd backend && python -m pytest tests/test_ai_pick.py -v
"""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ai_pick_service import (
    _format_brief,
    _format_post_line,
    _post_date,
    build_selection_messages,
    build_summary_messages,
    parse_picks_response,
    SUMMARY_POSTS_LIMIT,
    SUMMARY_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)
from app.services.xhs_service import _extract_initial_state, _walk_find_notes
from app.routers.ai_pick import _repo_root, enrich_picks_with_returns


def _fake_brief(**overrides):
    b = {
        'code': 'NVDA', 'name': 'Nvidia', 'price': 180.5,
        'market_cap': 4400.0, 'pe': 52.3,
        'news': [{'title': 'Nvidia raises data center guidance', 'sentiment': 1}],
    }
    b.update(overrides)
    return b


# ============================================
# 简报格式化与prompt构建
# ============================================
class TestFormatBrief:
    def test_full_brief(self):
        text = _format_brief(_fake_brief())
        assert 'NVDA Nvidia' in text
        assert '现价$180.5' in text and '市值4400.0B' in text and 'PE52.3' in text
        assert '[利好]' in text and 'guidance' in text

    def test_missing_fields_tolerated(self):
        text = _format_brief({'code': 'MU', 'name': 'Micron', 'price': None,
                              'market_cap': None, 'pe': None, 'news': []})
        assert '- MU Micron' in text
        assert '$' not in text.split('\n')[0]


class TestBuildSelectionMessages:
    def test_structure_and_content(self):
        msgs = build_selection_messages([_fake_brief()], '- [博主A] 标题：内容')
        assert len(msgs) == 2
        assert msgs[0]['role'] == 'system'
        assert '卡点' in SYSTEM_PROMPT and '压力测试' in SYSTEM_PROMPT
        user = msgs[1]['content']
        assert '共1只' in user and 'NVDA' in user
        assert '博主A' in user
        assert '今天是' in user


# ============================================
# 响应解析
# ============================================
VALID_JSON = '''{"picks":[
  {"rank":2,"code":"mu","name":"Micron","confidence":"HIGH","thesis":"HBM供需缺口","bottlenecks":"产能受限","risks":"周期见顶","catalysts":"涨价"},
  {"rank":1,"code":"NVDA","name":"Nvidia","confidence":"high","thesis":"算力卡点","bottlenecks":"CoWoS","risks":"估值","catalysts":"财报"}
],"market_commentary":"板块景气延续"}'''


class TestParsePicksResponse:
    def test_plain_json(self):
        result = parse_picks_response(VALID_JSON)
        assert [p['code'] for p in result['picks']] == ['NVDA', 'MU']  # 按rank排序
        assert result['picks'][0]['confidence'] == 'high'
        assert result['market_commentary'] == '板块景气延续'

    def test_fenced_and_noisy_response(self):
        noisy = f'好的，以下是分析结果：\n```json\n{VALID_JSON}\n```\n以上仅供参考'
        result = parse_picks_response(noisy)
        assert len(result['picks']) == 2

    def test_code_normalized_upper(self):
        result = parse_picks_response(VALID_JSON)
        assert result['picks'][1]['code'] == 'MU'

    def test_invalid_confidence_falls_back(self):
        import json as _json
        data = _json.loads(VALID_JSON)
        data['picks'][0]['confidence'] = 'extreme'  # rank=2的MU
        result = parse_picks_response(_json.dumps(data))
        assert result['picks'][1]['confidence'] == 'medium'  # 排序后MU在[1]

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            parse_picks_response('抱歉我无法输出JSON')
        with pytest.raises(ValueError):
            parse_picks_response('{"picks": []}')

    def test_max_five_picks(self):
        import json as _json
        data = _json.loads(VALID_JSON)
        base = data['picks'][0]
        data['picks'] = [{**base, 'rank': i, 'code': f'S{i}'} for i in range(1, 9)]
        result = parse_picks_response(_json.dumps(data))
        assert len(result['picks']) == 5


# ============================================
# 小红书页面解析
# ============================================
class TestXhsParsing:
    HTML = '''
    <html><script>
    window.__INITIAL_STATE__={"user":{"nickname":"老张","notes":[{"id":"65f0aa","displayTitle":"HBM产能全梳理","desc":"SK海力士扩产不及预期","time":1750000000000},["不是笔记的数组项"]],"x":undefined},"other":undefined}
    </script></html>
    '''

    def test_extract_and_walk(self):
        state = _extract_initial_state(self.HTML)
        assert state is not None
        assert state['other'] is None  # undefined已转null
        notes = []
        _walk_find_notes(state, notes)
        assert len(notes) == 1
        assert notes[0]['note_id'] == '65f0aa'
        assert 'HBM' in notes[0]['title']
        assert notes[0]['url'].endswith('/explore/65f0aa')

    def test_login_wall_returns_none(self):
        assert _extract_initial_state('<html>请登录</html>') is None


# ============================================
# 博主帖子总结：格式化与prompt构建
# ============================================
def _fake_post(**overrides):
    base = dict(note_id='n1', title='HBM产能全梳理',
                content='SK海力士扩产不及预期，国产替代窗口打开',
                posted_time='1750000000000')
    base.update(overrides)
    return SimpleNamespace(**base)


class TestPostFormatting:
    def test_ms_timestamp_to_date(self):
        expected = datetime.fromtimestamp(1750000000).strftime('%Y-%m-%d')
        assert _post_date('1750000000000') == expected

    def test_non_ms_passthrough(self):
        assert _post_date('2025-06-15') == '2025-06-15'
        assert _post_date('') == ''
        assert _post_date(None) == ''

    def test_format_post_line(self):
        line = _format_post_line(_fake_post())
        assert '[' in line and ']' in line          # 含日期段
        assert 'HBM产能全梳理' in line
        assert '国产替代' in line

    def test_format_post_line_truncates_content(self):
        line = _format_post_line(_fake_post(content='字' * 300))
        assert '字' * 201 not in line

    def test_format_post_line_empty_content(self):
        line = _format_post_line(_fake_post(content='', title='只有标题'))
        assert '只有标题' in line
        assert '：' not in line


class TestBuildSummaryMessages:
    def test_structure_and_content(self):
        posts = [_fake_post(),
                 _fake_post(note_id='n2', title='光模块涨价')]
        msgs = build_summary_messages('半导体老张', posts)
        assert len(msgs) == 2
        assert msgs[0]['role'] == 'system'
        assert '总结' in SUMMARY_SYSTEM_PROMPT and '标的' in SUMMARY_SYSTEM_PROMPT
        user = msgs[1]['content']
        assert '半导体老张' in user and '2条帖子' in user
        assert 'HBM产能全梳理' in user and '光模块涨价' in user

    def test_limit_constant(self):
        assert SUMMARY_POSTS_LIMIT == 20


class TestAiPickRouterHelpers:
    def test_repo_root_points_to_true_repo(self):
        root = _repo_root()
        assert (root / "tech_stock_screener.py").exists()

    def test_enrich_picks_with_returns(self):
        raw = [
            {'stock_code': 'NVDA', 'price_at_pick': 100.0},
            {'stock_code': 'MU', 'price_at_pick': 80.0},
            {'stock_code': 'AAPL', 'price_at_pick': None},
        ]
        enriched = enrich_picks_with_returns(raw, {'NVDA': 115.0, 'AAPL': 200.0})
        assert enriched[0]['current_price'] == 115.0
        assert enriched[0]['return_pct'] == 15.0
        assert enriched[1]['current_price'] is None
        assert enriched[1]['return_pct'] is None
        assert enriched[2]['current_price'] == 200.0
        assert enriched[2]['return_pct'] is None




class TestWeeklyTop5DbEngine:
    def test_top5_reads_from_db_and_scan_date_within_8_days(self, monkeypatch):
        """
        [第三批·第一段-1 验证]
        1. screener 引擎已迁入 platform (app.services.screener_service)，/api/ai-pick/top5 读 WeeklyTop5Snapshot 表
        2. 首次启动非阻塞导入种子后，AIPick 页拿到的 scan_date 距今 < 8 天
        3. /api/ai-pick/top5/scan 触发扫描后直接写入 WeeklyTop5Snapshot 表
        """
        import pandas as pd
        import numpy as np
        from datetime import datetime
        from app.database import SessionLocal
        from app.models_platform import WeeklyTop5Snapshot
        from app.routers.ai_pick import get_weekly_top5, get_weekly_history
        from app.services import screener_service
        from app.utils.timezone import today_beijing

        db = SessionLocal()
        try:
            db.query(WeeklyTop5Snapshot).delete()
            db.commit()

            # 1. 空表调用 get_weekly_top5 自动触发非阻塞种子入库，且最新 scan_date 距今 < 8 天
            latest = get_weekly_top5(db=db)
            assert latest["scan_date"] is not None
            scan_dt = datetime.strptime(latest["scan_date"][:10], "%Y-%m-%d").date()
            days_diff = (today_beijing() - scan_dt).days
            assert 0 <= days_diff < 8, f"scan_date ({scan_dt}) 距今 {days_diff} 天，超过 8 天阈值！"
            assert len(latest["top5"]) == 5
            assert db.query(WeeklyTop5Snapshot).count() >= 1

            # 2. 手动触发 /top5/scan（仅注入外部日线数据，走真实 screener_service 链路写库）
            idx = pd.date_range(end="2026-10-17", periods=65, freq="B")
            c = np.linspace(100.0, 130.0, 65)
            fake_df = pd.DataFrame(
                {"Open": c - 0.5, "High": c + 1.5, "Low": c - 1.5, "Close": c, "Volume": 1_000_000},
                index=idx,
            )

            class _FakeTicker:
                def __init__(self, code):
                    self.calendar = {}
                    self.info = {"forwardPE": 25.0, "revenueGrowth": 0.25, "targetMeanPrice": 155.0}
                def history(self, period="6mo"):
                    return fake_df.copy()

            scanned = screener_service.run_screener(
                db=db,
                universe=screener_service.HARD_TECH_UNIVERSE[:6],
                ticker_cls=_FakeTicker,
            )
            assert scanned["total_scanned"] == 6
            assert len(scanned["top5"]) == 5
            hist_res = get_weekly_history(db=db)
            assert len(hist_res["weeks"]) >= 1
        finally:
            db.query(WeeklyTop5Snapshot).delete()
            db.commit()
            db.close()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


class TestAiPicksPerformanceTracking:
    def test_ai_picks_20d_60d_performance_and_pending_isolation(self):
        """
        [第三批·第二段-3 验证]
        1. T+20 / T+60 交易日收益与胜率按 confidence (high/medium/low) 分层统计
        2. 距今不足 20/60 个交易日的选股记录严格标为 'pending'，绝不用当前现价冒充 20d/60d 收益参与胜率分母
        3. 只统计观测，不反向修改任何模型或策略参数
        """
        import pandas as pd
        import numpy as np
        from app.services.ai_pick_service import compute_ai_picks_performance

        # 构造 70 个交易日的日线序列（2026-06-01 起）
        b_days = pd.date_range(start="2026-06-01", periods=75, freq="B")
        # NVDA: 单调上涨（第 0 天=100, 第 20 天=120, 第 60 天=160）
        nvda_closes = np.linspace(100.0, 174.0, 75)
        df_nvda = pd.DataFrame({"Close": nvda_closes}, index=b_days)
        # AMD: 下跌（第 0 天=100, 第 20 天=90, 第 60 天=80）
        amd_closes = np.linspace(100.0, 70.0, 75)
        df_amd = pd.DataFrame({"Close": amd_closes}, index=b_days)

        d0 = b_days[0].strftime("%Y-%m-%d")     # 已满 60 交易日
        d_mid = b_days[45].strftime("%Y-%m-%d") # 已满 20 交易日（45+20=65 < 75），未满 60 交易日（45+60=105 >= 75）
        d_recent = b_days[65].strftime("%Y-%m-%d")  # 距最新仅 9 个交易日 -> 20d 和 60d 均为 pending

        picks = [
            {"id": 1, "run_date": d0, "stock_code": "NVDA", "stock_name": "NVIDIA", "confidence": "high", "price_at_pick": float(nvda_closes[0])},
            {"id": 2, "run_date": d0, "stock_code": "AMD", "stock_name": "AMD", "confidence": "low", "price_at_pick": float(amd_closes[0])},
            {"id": 3, "run_date": d_mid, "stock_code": "NVDA", "stock_name": "NVIDIA", "confidence": "medium", "price_at_pick": float(nvda_closes[45])},
            {"id": 4, "run_date": d_recent, "stock_code": "NVDA", "stock_name": "NVIDIA", "confidence": "high", "price_at_pick": float(nvda_closes[65])},
        ]

        perf = compute_ai_picks_performance(picks, kline_map={"NVDA": df_nvda, "AMD": df_amd})
        assert perf["total_picks"] == 4

        # 检查第 4 条（未满 20 交易日）：必须为 pending，且 return_20d_pct / return_60d_pct 为 None
        p4 = [p for p in perf["picks"] if p["id"] == 4][0]
        assert p4["status_20d"] == "pending" and p4["return_20d_pct"] is None
        assert p4["status_60d"] == "pending" and p4["return_60d_pct"] is None

        # 检查第 3 条（满 20d 但未满 60d）：20d 为 matured，60d 为 pending
        p3 = [p for p in perf["picks"] if p["id"] == 3][0]
        assert p3["status_20d"] == "matured" and p3["return_20d_pct"] > 0
        assert p3["status_60d"] == "pending" and p3["return_60d_pct"] is None

        # 检查按 confidence 分层统计：high 共 2 条，其中 1 条 matured（胜），1 条 pending（不计入分母）
        high_tier = perf["by_confidence"]["high"]
        assert high_tier["total_picks"] == 2
        assert high_tier["d20"]["matured_count"] == 1
        assert high_tier["d20"]["pending_count"] == 1
        assert high_tier["d20"]["win_rate"] == 100.0

        # low 共 1 条，matured 且亏损 -> 胜率 0.0%
        low_tier = perf["by_confidence"]["low"]
        assert low_tier["d20"]["matured_count"] == 1
        assert low_tier["d20"]["win_rate"] == 0.0
        assert low_tier["d60"]["matured_count"] == 1
        assert low_tier["d60"]["win_rate"] == 0.0


class TestAiPickSameDayDedup:
    def test_run_ai_pick_same_day_dedup_no_second_llm_call(self, monkeypatch):
        """
        [第四批·第一段-1 验证]
        同日连续调用两次 run_ai_pick()：
        1. 第一次正常调用 LLM 并写入 ai_picks 表
        2. 第二次跑前检测到当日已存在记录，直接返回当日已存结果（already_ran=True），
           库中只有一份记录，且第二次绝不消耗 LLM 调用（llm_calls == 1）
        """
        import app.services.ai_pick_service as aps
        from app.database import SessionLocal
        from app.models_platform import AIPick
        from app.utils.timezone import today_beijing

        today = today_beijing().isoformat()
        db = SessionLocal()
        try:
            db.query(AIPick).filter(AIPick.run_date == today).delete()
            db.commit()

            llm_calls = []

            def fake_chat_completion(messages, temperature=0.4, max_tokens=16000):
                llm_calls.append(messages)
                return '{"picks":[{"rank":1,"code":"NVDA","name":"Nvidia","confidence":"high","thesis":"HBM与NVLink垄断","bottlenecks":"CoWoS封装","risks":"CSP资本开支放缓","catalysts":"Blackwell放量"},{"rank":2,"code":"MU","name":"Micron","confidence":"medium","thesis":"HBM3e份额提升","bottlenecks":"良率爬坡","risks":"DRAM价格波动","catalysts":"财报超预期"}],"market_commentary":"AI算力景气延续"}'

            monkeypatch.setattr(aps, "is_ai_configured", lambda: True)
            monkeypatch.setattr(aps, "get_universe", lambda: [("NVDA", "Nvidia"), ("MU", "Micron")])
            monkeypatch.setattr(aps, "_stock_brief", lambda c, n: {"code": c, "name": n, "price": 125.0, "market_cap": 3000.0, "pe": 35.0, "news": []})
            monkeypatch.setattr(aps.xhs_service, "digest_recent", lambda: "")
            monkeypatch.setattr(aps, "chat_completion", fake_chat_completion)

            # 第 1 次调用：执行完整流水线并入库
            res1 = aps.run_ai_pick()
            assert res1["run_date"] == today
            assert len(res1["picks"]) == 2
            assert len(llm_calls) == 1
            count_after_first = db.query(AIPick).filter(AIPick.run_date == today).count()
            assert count_after_first == 2

            # 第 2 次同日调用：直接命中同日去重，不调 LLM，不重复插库
            res2 = aps.run_ai_pick()
            assert res2.get("already_ran") is True
            assert "今日已运行" in res2.get("message", "")
            assert len(res2["picks"]) == 2
            assert len(llm_calls) == 1, f"同日第二次调用不应消耗 LLM，实际调用 {len(llm_calls)} 次"
            count_after_second = db.query(AIPick).filter(AIPick.run_date == today).count()
            assert count_after_second == 2, f"库中只应保留一份当日记录(2条)，实际有 {count_after_second} 条"
        finally:
            db.query(AIPick).filter(AIPick.run_date == today).delete()
            db.commit()
            db.close()


class TestXhsOptionalDegradation:
    def test_run_ai_pick_without_cookie_completes_with_empty_xhs_digest(self, monkeypatch):
        """
        [第四批·第三段-7 验证]
        无小红书 Cookie 时调用 run_ai_pick(force=True)：
        1. 自动跳过小红书源，不抛错、不中断
        2. 正常完成行情+新闻简报组装与 LLM 选股入库
        3. 返回结果中 xhs_digest == "" 且 xhs_skipped is True
        4. 发给 LLM 的消息中明确注明“小红书源未配置或无可用缓存，本次仅基于行情与新闻选股”
        """
        import app.services.ai_pick_service as aps
        from app.database import SessionLocal
        from app.models import Setting
        from app.models_platform import AIPick
        from app.services.xhs_service import KEY_XHS_COOKIE
        from app.utils.timezone import today_beijing

        today = today_beijing().isoformat()
        db = SessionLocal()
        orig_cookie = None
        try:
            row = db.query(Setting).filter(Setting.key == KEY_XHS_COOKIE).first()
            if row:
                orig_cookie = row.value
                row.value = ""
                db.commit()
            db.query(AIPick).filter(AIPick.run_date == today).delete()
            db.commit()

            captured_messages = []

            def fake_chat_completion(messages, temperature=0.4, max_tokens=16000):
                captured_messages.append(messages)
                return '{"picks":[{"rank":1,"code":"AVGO","name":"Broadcom","confidence":"high","thesis":"AI定制ASIC龙头","bottlenecks":"先进封装","risks":"客户自研节奏","catalysts":"新大客户放量"}],"market_commentary":"仅基于行情与新闻判断"}'

            monkeypatch.setattr(aps, "is_ai_configured", lambda: True)
            monkeypatch.setattr(aps, "get_universe", lambda: [("AVGO", "Broadcom")])
            monkeypatch.setattr(aps, "_stock_brief", lambda c, n: {"code": c, "name": n, "price": 175.0, "market_cap": 800.0, "pe": 30.0, "news": [{"title": "Broadcom AI ASIC orders surge", "sentiment": 1}]})
            monkeypatch.setattr(aps, "chat_completion", fake_chat_completion)

            res = aps.run_ai_pick(force=True)
            assert len(res["picks"]) == 1
            assert res["picks"][0]["code"] == "AVGO"
            assert res["xhs_digest"] == ""
            assert res["xhs_skipped"] is True
            assert len(captured_messages) == 1
            user_prompt = captured_messages[0][1]["content"]
            assert "本次仅基于行情与新闻选股" in user_prompt
        finally:
            if orig_cookie is not None:
                r = db.query(Setting).filter(Setting.key == KEY_XHS_COOKIE).first()
                if r:
                    r.value = orig_cookie
                    db.commit()
            db.query(AIPick).filter(AIPick.run_date == today).delete()
            db.commit()
            db.close()
