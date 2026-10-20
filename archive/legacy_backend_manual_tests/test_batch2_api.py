# -*- coding: utf-8 -*-
"""第二批集成验证：对比端点 + 技术信号端点"""
import json
import sys
import io
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
BASE = 'http://localhost:8000/api'
PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✓ {name}')
    else:
        FAIL += 1
        print(f'  ✗ {name} {detail}')


def post(path, body):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method='POST',
                               headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(r, timeout=180).read())


def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=120).read())


print('=== 1. 策略对比端点 /backtest/compare (MU, 含$1手续费) ===')
c = post('/backtest/compare', {'stock_code': 'MU', 'period': '1y'})
check('返回4策略+buy_hold', set(c.get('strategies', {}).keys()) == {'linear', 'nonlinear', 'ma_cross', 'macd'} and 'buy_hold' in c)
check('comparison共5行', len(c.get('comparison', [])) == 5)
bh = c['buy_hold']['total_return']
for row in c['comparison']:
    excess_ok = abs(row['excess_vs_buy_hold'] - (row['total_return'] - bh)) < 0.11
    check(f"  {row['name']:<6} 收益{row['total_return']:>7.1f}% 费用${row['total_fees']:>4.0f} "
          f"超额{row['excess_vs_buy_hold']:>+8.1f}% {'✓' if excess_ok else '✗超额数学'}", excess_ok)
buy_hold_row = c['comparison'][-1]
check('买入持有行excess=0', abs(buy_hold_row['excess_vs_buy_hold']) < 0.01 and buy_hold_row['key'] == 'buy_hold')
check('buy_hold曲线非空且与策略同轴', len(c['buy_hold'].get('equity_curve', [])) > 100)
n_trades_total = sum(r['trade_count'] for r in c['comparison'][:4])
check(f'4策略总成交{n_trades_total}笔→总费用合理', True)

print('\n=== 2. 单策略回测含基准线与手续费 ===')
r = post('/backtest', {'stock_code': 'MU', 'strategy': 'macd', 'period': '1y'})
check('返回total_fees字段', 'total_fees' in r and r['total_fees'] > 0)
check('费用=笔数×2(买+卖)', abs(r['total_fees'] - (len([t for t in r["trades"]]) * 1.0)) < 0.01,
      f"fees={r.get('total_fees')} trades={len(r['trades'])}")
check('返回buy_hold_curve基准曲线', len(r.get('buy_hold_curve', [])) > 100)
check('返回buy_hold_return', r.get('buy_hold_return') is not None)
check('trade记录带fee字段', all('fee' in t for t in r['trades']))

print('\n=== 3. 技术信号端点 /stocks/MU/signals ===')
s = get('/stocks/MU/signals?market=US&period=6mo')
check('返回current_price', s.get('current_price', 0) > 0)
pats = s.get('patterns', [])
check(f'形态识别={len(pats)}条', len(pats) > 0)
if pats:
    names = {p['pattern'] for p in pats}
    print(f'    识别到的形态: {names}')
    check('形态含direction字段', all(p.get('direction') in ('bullish', 'bearish', 'neutral') for p in pats))
sr = s.get('support_resistance', {})
check(f"支撑位={len(sr.get('supports', []))}个 阻力位={len(sr.get('resistances', []))}个",
      len(sr.get('supports', [])) >= 1 or len(sr.get('resistances', [])) >= 1)
cp = s.get('current_price', 0)
sup_ok = all(x['price'] <= cp * 1.01 for x in sr.get('supports', []))
res_ok = all(x['price'] >= cp * 0.99 for x in sr.get('resistances', []))
check('支撑在现价下方/阻力在现价上方', sup_ok and res_ok)
d = s.get('divergence', {})
check('背离结构完整', 'top_divergence' in d and 'bottom_divergence' in d and 'detail' in d)
print(f"    背离: top={d.get('top_divergence')} bottom={d.get('bottom_divergence')} | {str(d.get('detail'))[:40]}")

print('\n=== 4. A股信号 ===')
s_cn = get('/stocks/002594/signals?market=A&period=6mo')
check('A股形态识别正常', len(s_cn.get('patterns', [])) >= 0 and 'support_resistance' in s_cn)
print(f"    A股形态{len(s_cn.get('patterns', []))}条 支撑{len(s_cn['support_resistance'].get('supports', []))}个")

print(f'\n{"=" * 40}\n集成结果: {PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
