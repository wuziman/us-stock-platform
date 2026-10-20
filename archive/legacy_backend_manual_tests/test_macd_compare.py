# -*- coding: utf-8 -*-
"""MACD策略 vs 买入持有 基准对比"""
import json
import sys
import io
import urllib.request
import urllib.parse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = 'http://localhost:8000/api'


def get_json(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=90).read())


def backtest(code, strategy):
    req = urllib.request.Request(
        BASE + '/backtest',
        data=json.dumps({'stock_code': code, 'strategy': strategy}).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=90).read())


def history(code, market):
    return get_json(f'/stocks/{urllib.parse.quote(code)}/history?market={market}&period=1y')['data']


US = [('MU', '美光', 'US'), ('SNDK', '闪迪', 'US'), ('SOXL', '半导体ETF', 'US'),
      ('NKE', '耐克', 'US'), ('AXTI', 'AXT', 'US'), ('AAOI', '光模块', 'US'),
      ('LITE', '光通信', 'US'), ('COHR', '光学', 'US')]
CN = [('002594', '比亚迪', 'A'), ('300750', '宁德时代', 'A'),
      ('600111', '北方稀土', 'A'), ('601012', '隆基绿能', 'A')]

print(f'{"股票":<14}{"MACD总收益":>10}{"买入持有":>10}{"超额":>9}{"夏普":>7}{"胜率":>7}{"笔数":>5}{"回撤":>8}')
print('-' * 70)

for code, name, mkt in US + CN:
    try:
        r = backtest(code, 'macd')
        if 'error' in r and 'trades' not in r:
            print(f'{name:<14} ERROR: {r["error"]}')
            continue
        # 买入持有基准
        h = history(code, mkt)
        bh_ret = (h[-1]['close'] / h[0]['open'] - 1) * 100
        excess = r['total_return'] - bh_ret
        print(f'{code} {name:<10} {r["total_return"]:>9.1f}% {bh_ret:>9.1f}% '
              f'{excess:>+8.1f}% {r["sharpe_ratio"]:>6.2f} {r["win_rate"]:>6.1f}% '
              f'{r["trade_count"]:>3} {r["max_drawdown"]:>7.1f}%')
    except Exception as e:
        print(f'{code} {name}: EXCEPTION {e}')
