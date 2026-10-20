# -*- coding: utf-8 -*-
"""手动测试：A股全市场搜索（列表外的股票）"""
import json
import sys
import io
import urllib.request
import urllib.parse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


def search(q):
    url = f'http://localhost:8000/api/stocks/search?q={urllib.parse.quote(q)}&market=all'
    return json.loads(urllib.request.urlopen(url, timeout=30).read())['results']


if __name__ == '__main__':
    # 这些都不在原30只硬编码列表里
    for q in ['宁德时代', '北方稀土', '300750', '隆基绿能', '601127']:
        results = search(q)
        print(f'{q}: {len(results)}条')
        for r in results[:3]:
            print(f'  {r["code"]} {r["name"]} [{r["market"]}]')
