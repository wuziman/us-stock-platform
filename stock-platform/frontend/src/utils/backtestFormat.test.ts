import { describe, expect, it } from 'vitest';
import { colors } from '../theme/tokens';
import { METRIC_LABEL, STRATEGY_COLORS, fmtParams, periodLabel, formatBacktestTooltipLine } from './backtestFormat';

describe('periodLabel 回测周期标签', () => {
  it('映射已知周期', () => {
    expect(periodLabel('1y')).toBe('近1年');
    expect(periodLabel('3y')).toBe('近3年');
    expect(periodLabel('5y')).toBe('近5年');
  });

  it('未知/空周期兜底为近1年（与后端默认口径一致）', () => {
    expect(periodLabel(undefined)).toBe('近1年');
    expect(periodLabel('')).toBe('近1年');
  });
});

describe('fmtParams 参数组合展示', () => {
  it('键值对拼接', () => {
    expect(fmtParams({ window: 20, k: 2 })).toBe('window=20, k=2');
  });

  it('空参数返回空串', () => {
    expect(fmtParams({})).toBe('');
  });
});

describe('METRIC_LABEL / STRATEGY_COLORS', () => {
  it('指标中文名齐全', () => {
    expect(METRIC_LABEL.sharpe).toBe('夏普比率');
    expect(METRIC_LABEL.max_drawdown).toBe('最大回撤%');
  });

  it('四策略各有着色且互不重复', () => {
    const cs = Object.values(STRATEGY_COLORS);
    expect(new Set(cs).size).toBe(cs.length);
    expect(STRATEGY_COLORS.linear).toBe(colors.primary);
  });
});

describe('formatBacktestTooltipLine 回测B/S散点tooltip防$NaN', () => {
  it('B/S散点value为[date, equity]数组时不出现$NaN并展示成交价与总资产', () => {
    const buyLine = formatBacktestTooltipLine({
      marker: '<span>●</span>',
      seriesName: '买入点 (B)',
      value: ['2026-05-10', 102500],
      data: { value: ['2026-05-10', 102500], price: 95.5, shares: 200 },
    });
    expect(buyLine.includes('NaN')).toBe(false);
    expect(buyLine.includes('$95.50')).toBe(true);
    expect(buyLine.includes('200股')).toBe(true);

    const sellLine = formatBacktestTooltipLine({
      marker: '<span>●</span>',
      seriesName: '卖出点 (S)',
      value: ['2026-06-01', 115000],
      data: { value: ['2026-06-01', 115000], price: 110.0, profit_pct: 0.1518 },
    });
    expect(sellLine.includes('NaN')).toBe(false);
    expect(sellLine.includes('+15.18%')).toBe(true);
  });
});
