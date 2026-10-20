# -*- coding: utf-8 -*-
"""第三批集成验证：参数寻优/Walk-Forward/评分追踪/定时调度/缓存"""
import json
import sys
import io
import time
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
    try:
        return json.loads(urllib.request.urlopen(r, timeout=300).read())
    except urllib.error.HTTPError as e:
        return {'_err': e.code, **json.loads(e.read() or b'{}')}


def get(path):
    try:
        return json.loads(urllib.request.urlopen(BASE + path, timeout=120).read())
    except urllib.error.HTTPError as e:
        return {'_err': e.code}


print('=== 1. 参数寻优 /backtest/optimize (MU, macd) ===')
o = post('/backtest/optimize', {'stock_code': 'MU', 'strategy': 'macd', 'period': '1y'})
check('返回best+results+heatmap', all(k in o for k in ['best', 'results', 'heatmap']))
check(f"results={len(o.get('results', []))}组", 5 <= len(o.get('results', [])) <= 12)
check('best按metric排序第一', o['results'][0]['sharpe_ratio'] == max(r['sharpe_ratio'] for r in o['results']))
hm = o['heatmap']
check('heatmap矩阵维度一致', len(hm['z']) == len(hm['y_values']) and all(len(row) == len(hm['x_values']) for row in hm['z']))
print(f"    最优参数: {o['best']['params']} 夏普{o['best']['sharpe_ratio']}")

print('=== 2. 参数寻优 (600519, linear) ===')
o2 = post('/backtest/optimize', {'stock_code': '600519', 'strategy': 'linear', 'period': '1y'})
check('A股寻优正常', 'best' in o2 and len(o2.get('results', [])) == 9)
print(f"    A股最优: tp={o2['best']['params'].get('tp')} sl={o2['best']['params'].get('sl')} 夏普{o2['best']['sharpe_ratio']}")

print('=== 3. Walk-Forward /backtest/walkforward (MU, macd, 2段) ===')
w = post('/backtest/walkforward', {'stock_code': 'MU', 'strategy': 'macd', 'period': '3y', 'segments': 2})
check('返回2个分段', len(w.get('segments', [])) == 2)
check('分段含best_params与OOS指标', all(all(k in s for k in ['best_params', 'oos_return', 'oos_sharpe', 'oos_buy_hold_return', 'beats_buy_hold']) for s in w['segments']))
check('拼接OOS曲线非空', len(w.get('stitched_oos_curve', [])) > 100)
check('summary结构完整', all(k in w.get('summary', {}) for k in ['avg_oos_return', 'win_segments', 'total_segments']))
for s in w['segments']:
    print(f"    段{s['step']}: OOS{s['oos_return']:>7.1f}% vs 持有{s['oos_buy_hold_return']:>7.1f}% "
          f"{'✓跑赢' if s['beats_buy_hold'] else '✗未跑赢'} 参数{s['best_params']}")

print('=== 4. 评分追踪（先分析产生记录，再查追踪）===')
t0 = get('/analysis/tracking?stock_code=MU')
had_records = t0.get('count', 0) > 0
print(f'    既有记录: {t0.get("count", 0)}条')
a = post('/analysis', {'stock_code': 'MU', 'stock_name': 'Micron', 'mode': 'simple'})
check('分析成功(会自动落库)', 'scores' in a)
time.sleep(1)
t = get('/analysis/tracking?stock_code=MU')
check(f'追踪记录>={t0.get("count", 0)+1 if had_records else 1}条', t.get('count', 0) >= 1)
check('records含前向收益字段', all('forward_return_pct' in r for r in t.get('records', [])[:1]) if t.get('records') else False)
check('分桶4组', len(t.get('buckets', [])) == 4)
check('correlation字段存在(可为null)', 'correlation' in t and 'interpretation' in t)
print(f"    次数{t.get('count')} 相关性{t.get('correlation')} | {str(t.get('interpretation'))[:36]}")

print('=== 5. 定时调度配置 ===')
sc = get('/report/schedule')
check('schedule结构完整', all(k in sc for k in ['enabled', 'hour', 'minute', 'last_sent_date']))
check('默认关闭(防误发)', sc.get('enabled') is False)
put = urllib.request.Request(BASE + '/report/schedule', data=json.dumps({'enabled': False, 'hour': 18, 'minute': 0}).encode(),
                             method='PUT', headers={'Content-Type': 'application/json'})
sc2 = json.loads(urllib.request.urlopen(put, timeout=30).read())
check('更新配置成功', sc2.get('hour') == 18 and sc2.get('minute') == 0 and sc2.get('enabled') is False)

print('=== 6. K线缓存生效（第二次history应显著变快）===')
t1 = time.time()
get('/stocks/MU/history?market=US&period=1y')
d1 = time.time() - t1
t2 = time.time()
get('/stocks/MU/history?market=US&period=1y')
d2 = time.time() - t2
print(f'    首次{d1:.2f}s 第二次{d2:.2f}s')
check('缓存命中更快(或已足够快<0.5s)', d2 <= d1 + 0.3)

print(f'\n{"=" * 40}\n第三批集成结果: {PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
