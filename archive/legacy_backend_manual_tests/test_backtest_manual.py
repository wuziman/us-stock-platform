# -*- coding: utf-8 -*-
"""手动测试：回测统计指标 + 权益曲线"""
import json
import sys
import io
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


def backtest(code, strategy):
    req = urllib.request.Request(
        'http://localhost:8000/api/backtest',
        data=json.dumps({'stock_code': code, 'strategy': strategy}).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    return json.loads(urllib.request.urlopen(req, timeout=90).read())


if __name__ == '__main__':
    for code in ['MU', '600519']:
        print(f'=== {code} ===')
        for strat in ['linear', 'nonlinear', 'ma_cross', 'macd']:
            try:
                d = backtest(code, strat)
                if 'error' in d and 'trades' not in d:
                    print(f'  {strat}: ERROR {d["error"]}')
                    continue
                curve = d.get('equity_curve', [])
                # 验证权益曲线单调性与首尾值
                first_v = curve[0]['value'] if curve else None
                last_v = curve[-1]['value'] if curve else None
                print(f'  {strat:10s} 总收益{d["total_return"]:>8}% 年化{d["annual_return"]:>8}% '
                      f'最大回撤{d["max_drawdown"]:>6}% 夏普{d["sharpe_ratio"]:>6} '
                      f'胜率{d["win_rate"]:>6}% 交易{d["trade_count"]:>3}笔 '
                      f'曲线点数{len(curve)} 首值{first_v} 尾值{last_v}')
                # 一致性校验：尾值应等于final_value
                assert last_v == d['final_value'], f'曲线尾值{last_v} != final_value{d["final_value"]}'
            except Exception as e:
                print(f'  {strat}: EXCEPTION {e}')
    print('\n所有一致性校验通过')
