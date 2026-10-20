# -*- coding: utf-8 -*-
"""跨牛熊周期对比：3年/5年 × 4策略 × 全部股票"""
import json
import sys
import io
import urllib.request
import urllib.parse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = 'http://localhost:8000/api'
STRATEGIES = ['linear', 'nonlinear', 'ma_cross', 'macd']
STRAT_CN = {'linear': '线性', 'nonlinear': '非线性', 'ma_cross': '双均线', 'macd': 'MACD'}

US = [('MU', '美光'), ('SNDK', '闪迪'), ('SOXL', '半导体ETF'), ('NKE', '耐克'),
      ('AXTI', 'AXT'), ('AAOI', '光模块'), ('LITE', '光通信'), ('COHR', '光学')]
CN = [('002594', '比亚迪'), ('300750', '宁德时代'), ('600111', '北方稀土'), ('601012', '隆基绿能')]


def backtest(code, strategy, period):
    req = urllib.request.Request(
        BASE + '/backtest',
        data=json.dumps({'stock_code': code, 'strategy': strategy, 'period': period}).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def history(code, market):
    url = f"{BASE}/stocks/{urllib.parse.quote(code)}/history?market={market}&period=5y"
    return json.loads(urllib.request.urlopen(url, timeout=90).read())['data']


def run_period(period_label):
    print(f'\n{"#" * 100}\n# 回测区间: {period_label}\n{"#" * 100}')
    agg = {s: {'rets': [], 'anns': [], 'excess': [], 'wins': 0, 'sharps': [], 'dds': []} for s in STRATEGIES}
    bh_all = []

    for code, name in US + CN:
        market = 'A' if code.isdigit() else 'US'
        try:
            h = history(code, market)
            bh = (h[-1]['close'] / h[0]['open'] - 1) * 100
        except Exception as e:
            print(f'{name}: 历史数据失败 {e}')
            continue
        bh_all.append(bh)
        line = f'{name:<8} 持有{bh:>+9.1f}%'
        for s in STRATEGIES:
            try:
                r = backtest(code, s, period_label)
                if 'trades' not in r:
                    line += f' | {STRAT_CN[s]}=ERR:{r.get("error")}'
                    continue
                ex = r['total_return'] - bh
                a = agg[s]
                a['rets'].append(r['total_return'])
                a['anns'].append(r['annual_return'])
                a['excess'].append(ex)
                a['sharps'].append(r['sharpe_ratio'])
                a['dds'].append(r['max_drawdown'])
                if ex > 0:
                    a['wins'] += 1
                line += f' | {STRAT_CN[s]}{r["total_return"]:>+8.1f}%({ex:>+7.1f})'
            except Exception as e:
                line += f' | {STRAT_CN[s]}=EXC:{e}'
        print(f'{line}   [{h[0]["date"]}~{h[-1]["date"]}, {len(h)}根]')

    n = len(bh_all)
    print(f'\n--- {period_label} 汇总（{n}只）---')
    print(f'{"策略":<8}{"平均总收益":>11}{"平均年化":>10}{"跑赢持有":>10}{"平均超额":>10}{"平均夏普":>9}{"平均回撤":>9}')
    for s in STRATEGIES:
        a = agg[s]
        if not a['rets']:
            continue
        k = len(a['rets'])
        print(f'{STRAT_CN[s]:<8}{sum(a["rets"]) / k:>10.1f}%{sum(a["anns"]) / k:>9.1f}%'
              f'{a["wins"]:>7}/{n}{sum(a["excess"]) / k:>+9.1f}%'
              f'{sum(a["sharps"]) / k:>9.2f}{sum(a["dds"]) / k:>8.1f}%')
    print(f'{"买入持有":<8}{sum(bh_all) / n:>10.1f}%')


if __name__ == '__main__':
    run_period('5y')
    run_period('3y')
