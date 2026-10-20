# -*- coding: utf-8 -*-
"""4策略全矩阵对比：线性/非线性/双均线/MACD vs 买入持有"""
import json
import sys
import io
import urllib.request
import urllib.parse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE = 'http://localhost:8000/api'
STRATEGIES = ['linear', 'nonlinear', 'ma_cross', 'macd']
STRAT_CN = {'linear': '线性', 'nonlinear': '非线性', 'ma_cross': '双均线', 'macd': 'MACD'}

US = [('MU', '美光', 'US'), ('SNDK', '闪迪', 'US'), ('SOXL', '半导体ETF', 'US'),
      ('NKE', '耐克', 'US'), ('AXTI', 'AXT', 'US'), ('AAOI', '光模块', 'US'),
      ('LITE', '光通信', 'US'), ('COHR', '光学', 'US')]
CN = [('002594', '比亚迪', 'A'), ('300750', '宁德时代', 'A'),
      ('600111', '北方稀土', 'A'), ('601012', '隆基绿能', 'A')]


def backtest(code, strategy):
    req = urllib.request.Request(
        BASE + '/backtest',
        data=json.dumps({'stock_code': code, 'strategy': strategy}).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=90).read())


def history(code, market):
    url = f"{BASE}/stocks/{urllib.parse.quote(code)}/history?market={market}&period=1y"
    return json.loads(urllib.request.urlopen(url, timeout=90).read())['data']


def run():
    results = {}   # {code: {'bh': x, strat: {...}}}
    for code, name, mkt in US + CN:
        h = history(code, mkt)
        bh = (h[-1]['close'] / h[0]['open'] - 1) * 100
        results[code] = {'name': name, 'mkt': mkt, 'bh': bh}
        for s in STRATEGIES:
            try:
                r = backtest(code, s)
                if 'trades' not in r:
                    continue
                results[code][s] = {
                    'ret': r['total_return'], 'sharpe': r['sharpe_ratio'],
                    'win': r['win_rate'], 'n': r['trade_count'], 'dd': r['max_drawdown']
                }
            except Exception as e:
                print(f'  ! {code}/{s}: {e}')

    # 明细表
    print('=' * 100)
    print('明细（近1年，%）：策略收益 / 超额(相对买入持有)')
    print('=' * 100)
    hdr = f'{"股票":<16}{"买入持有":>9}'
    for s in STRATEGIES:
        hdr += f'{STRAT_CN[s]:>14}'
    print(hdr)
    for code, name, mkt in US + CN:
        d = results[code]
        row = f'{name}({mkt})'.ljust(0)[:14].ljust(16) + f'{d["bh"]:>8.1f}%'
        for s in STRATEGIES:
            v = d.get(s)
            if v:
                excess = v['ret'] - d['bh']
                mark = '+' if excess > 0 else ' '
                row += f'{v["ret"]:>7.1f}%{mark}{excess:>+5.1f}'
            else:
                row += f'{"--":>13}'
        print(row)

    # 汇总统计
    print()
    print('=' * 100)
    print('汇总（12只股票平均）')
    print('=' * 100)
    print(f'{"策略":<10}{"平均收益":>10}{"跑赢持有次数":>12}{"平均超额":>10}{"平均夏普":>9}{"平均回撤":>9}')
    for s in STRATEGIES:
        rets, sharps, dds, wins_n, excesses = [], [], [], 0, []
        for code, name, mkt in US + CN:
            v = results[code].get(s)
            if not v:
                continue
            rets.append(v['ret'])
            sharps.append(v['sharpe'])
            dds.append(v['dd'])
            ex = v['ret'] - results[code]['bh']
            excesses.append(ex)
            if ex > 0:
                wins_n += 1
        print(f'{STRAT_CN[s]:<10}{sum(rets)/len(rets):>9.1f}%{wins_n:>9}/12'
              f'{sum(excesses)/len(excesses):>+9.1f}%{sum(sharps)/len(sharps):>9.2f}{sum(dds)/len(dds):>8.1f}%')
    bhs = [results[c]['bh'] for c, _, _ in US + CN]
    print(f'{"买入持有":<10}{sum(bhs)/len(bhs):>9.1f}%{"—":>11}{"—":>10}{"—":>9}{"—":>9}')


if __name__ == '__main__':
    run()
