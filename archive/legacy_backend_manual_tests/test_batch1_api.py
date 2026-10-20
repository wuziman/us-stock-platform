# -*- coding: utf-8 -*-
"""第一批功能API级集成测试：卖出/历史/警报/总资金/日报/K线指标"""
import json
import sys
import io
import urllib.request
import urllib.parse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = 'http://localhost:8000/api'
PASS = 0
FAIL = 0


def req(method, path, body=None):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={'Content-Type': 'application/json'})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=90).read())
    except urllib.error.HTTPError as e:
        return {'_http_error': e.code, **json.loads(e.read() or b'{}')}


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✓ {name}')
    else:
        FAIL += 1
        print(f'  ✗ {name} {detail}')


print('=== 1. 持仓卖出流程 ===')
added = req('POST', '/portfolio', {
    'stock_code': 'TEST99', 'stock_name': '测试股', 'market': 'US',
    'buy_price': 10.0, 'quantity': 100, 'buy_date': '2026-08-01',
    'stop_loss': 9.2, 'take_profit': 11.5
})
check('添加持仓', 'id' in added)
pid = added.get('id')

sold = req('POST', f'/portfolio/{pid}/sell', {'sell_price': 11.0, 'sell_date': '2026-08-20'})
check('卖出成功', sold.get('message') == '卖出成功')
check('已实现盈亏=+100', abs(sold.get('realized_pnl', 0) - 100.0) < 0.01, str(sold))
check('收益率10%', abs(sold.get('realized_pnl_pct', 0) - 10.0) < 0.01)
check('持有天数19天', sold.get('holding_days') == 19, str(sold.get('holding_days')))

again = req('POST', f'/portfolio/{pid}/sell', {'sell_price': 12.0, 'sell_date': '2026-08-21'})
check('重复卖出被拒绝', again.get('_http_error') == 404)

hist = req('GET', '/portfolio/history')
match = [h for h in hist if h['id'] == pid]
check('历史交易包含该记录', len(match) == 1)
if match:
    check('历史记录盈亏一致', abs((match[0]['realized_pnl'] or 0) - 100.0) < 0.01)
    check('历史持有天数', match[0]['holding_days'] == 19)

holding = req('GET', '/portfolio')
check('已卖出不在当前持仓', all(p['id'] != pid for p in holding))

print('\n=== 2. 总资金设置与仓位预警 ===')
cap = req('PUT', '/portfolio/settings/total_capital?total_capital=100000')
check('设置总资金', cap.get('total_capital') == 100000)
cap2 = req('GET', '/portfolio/settings/total_capital')
check('读取总资金', cap2.get('total_capital') == 100000)
neg = req('PUT', '/portfolio/settings/total_capital?total_capital=-5')
check('负数总资金被拒绝', neg.get('_http_error') == 400)
summary = req('GET', '/portfolio/summary')
check('summary含cash_pct与warnings字段', 'cash_pct' in summary and 'warnings' in summary)

print('\n=== 3. K线指标叠加数据 ===')
h = req('GET', '/stocks/MU/history?market=US&period=3mo')
first = h['data'][0]
last = h['data'][-1]
check('返回ma20字段', last.get('ma20') is not None)
check('返回ma50字段', last.get('ma50') is not None)
check('返回布林带字段', last.get('bb_upper') is not None and last.get('bb_lower') is not None)
check('暖机期指标为null(第11根无MA50)', h['data'][10].get('ma50') is None)
check('MA值合理(现价±30%)', last['close'] * 0.7 < (last['ma20'] or 0) < last['close'] * 1.3)
h_cn = req('GET', '/stocks/002594/history?market=A&period=3mo')
check('A股K线也含指标', h_cn['data'][-1].get('ma20') is not None)

print('\n=== 4. 日报生成（不推送微信）===')
rep = req('GET', '/report/preview')
if '_http_error' in rep:
    print(f'  ⚠️ 预览失败: {rep}（自选股为空属正常业务分支）')
else:
    check('报告文本生成', len(rep.get('report', '')) > 100)
    check('字节数统计', rep.get('char_count', 0) > 100)
    check(f"快照数量={len(rep.get('snapshots', []))}", len(rep.get('snapshots', [])) >= 1)
    snaps = rep.get('snapshots', [])
    if snaps:
        s0 = snaps[0]
        check('快照含三策略', all(k in s0 for k in ['linear', 'nonlinear', 'macd_state']))
        check('快照按评分排序', all(snaps[i]['tech'] >= snaps[i + 1]['tech'] for i in range(len(snaps) - 1)))

print(f'\n{"=" * 40}\n结果: {PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
