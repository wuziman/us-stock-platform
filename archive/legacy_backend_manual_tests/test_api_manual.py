# -*- coding: utf-8 -*-
"""手动测试脚本：验证分析API的真实数据"""
import json
import sys
import io
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


def analyze(code, name, market_hint='auto'):
    req = urllib.request.Request(
        'http://localhost:8000/api/analysis',
        data=json.dumps({'stock_code': code, 'stock_name': name, 'mode': 'simple'}).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    resp = urllib.request.urlopen(req, timeout=90)
    return json.loads(resp.read())


def show(d):
    if 'error' in d and 'scores' not in d:
        print(f"  ERROR: {d['error']}")
        return
    s = d['scores']
    print(f"  评分: 技术{s['technical']} 消息{s['news']} 宏观{s['macro']} 事件{s['event']} 总分{s['total']} [{d['recommendation']['level']}]")
    news = d['details']['news']
    print(f"  消息面: {news['news_count']}条 | 情感={news['sentiment']} | 利好{news['positive_count']} 利空{news['negative_count']} 来源={news['sources']}")
    for n in news.get('news', [])[:3]:
        tag = '+1' if n['sentiment'] > 0 else ('-1' if n['sentiment'] < 0 else ' 0')
        print(f"    [{tag}] {n['date'][:16]} {n['title'][:42]}")
    macro = d['details']['macro']
    print(f"  宏观: {macro.get('indicators', {})}")
    for i in macro.get('interpretations', [])[:4]:
        print(f"    - {i}")
    for e in d['details'].get('event', {}).get('events', []):
        print(f"  事件: {e['name']} {e.get('date','')} | {e['impact'][:40]}")


if __name__ == '__main__':
    print("=== A股 比亚迪(002594) ===")
    show(analyze('002594', '比亚迪'))
    print("\n=== 美股 MU(Micron) ===")
    show(analyze('MU', 'Micron'))
