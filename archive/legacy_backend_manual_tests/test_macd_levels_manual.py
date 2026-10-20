# -*- coding: utf-8 -*-
"""验证三策略点位API（重点MACD段）"""
import json
import sys
import io
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


def analyze(code, name):
    req = urllib.request.Request(
        'http://localhost:8000/api/analysis',
        data=json.dumps({'stock_code': code, 'stock_name': name, 'mode': 'simple'}).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=90).read())


if __name__ == '__main__':
    for code, name in [('MU', 'Micron'), ('002594', '比亚迪')]:
        d = analyze(code, name)
        p = d['price_levels']
        m = p.get('macd', {})
        print(f'=== {code} {name} 现价{p["current_price"]} ===')
        print(f'  MACD状态: {m.get("state")} 持续{m.get("days_in_state")}天 柱值{m.get("hist")}')
        print(f'  加仓参考(MA20): {m.get("add_price")} | 关注买点(布林下轨): {m.get("watch_price")}')
        print(f'  纪律止损: {m.get("stop")} | 说明: {m.get("note")}')
        assert m.get('state') in ('golden', 'death'), 'MACD状态异常!'
        assert m.get('days_in_state', 0) >= 1, '持续天数应>=1'
        print('  ✓ 校验通过')
