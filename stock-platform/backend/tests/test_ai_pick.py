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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
