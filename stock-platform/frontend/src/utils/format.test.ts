import { describe, expect, it } from 'vitest';
import { colors } from '../theme/tokens';
import { buildCsvContent, calcPositionSize, currencySymbol, fmtDist, fmtPx, pctColor, resolvePositionsViewState } from './format';

describe('fmtPx 价格格式化', () => {
  it('两位小数带美元符（默认美股）', () => {
    expect(fmtPx(910.43)).toBe('$910.43');
    expect(fmtPx(864.9)).toBe('$864.90');
    expect(fmtPx(864.9, 'US')).toBe('$864.90');
  });

  it('A股市场使用人民币符号 ¥', () => {
    expect(fmtPx(1500.5, 'A')).toBe('¥1500.50');
    expect(currencySymbol('A')).toBe('¥');
    expect(currencySymbol('US')).toBe('$');
  });

  it('null 显示占位符（后端指标不足时点位为null）', () => {
    expect(fmtPx(null)).toBe('--');
    expect(fmtPx(null, 'A')).toBe('--');
  });
});

describe('fmtDist 距离格式化', () => {
  it('正数带加号', () => {
    expect(fmtDist(5)).toBe('+5.00%');
  });

  it('负数自带减号', () => {
    expect(fmtDist(-3.2)).toBe('-3.20%');
  });

  it('零不带符号', () => {
    expect(fmtDist(0)).toBe('0.00%');
  });

  it('null 显示占位符', () => {
    expect(fmtDist(null)).toBe('--');
  });
});

describe('pctColor 涨跌三色（DESIGN.md 盈亏语义）', () => {
  it('正→profit绿', () => {
    expect(pctColor(1.5)).toBe(colors.profit);
  });

  it('负→loss红', () => {
    expect(pctColor(-0.1)).toBe(colors.loss);
  });

  it('零→textSecondary灰（零值不得着绿）', () => {
    expect(pctColor(0)).toBe(colors.textSecondary);
  });
});

describe('calcPositionSize 2%定额风险与40%单票上限计算器', () => {
  it('按2%最大风险计算股数并遵守40%单票上限', () => {
    // 10万资金，买价100，止损92（每股风险8元）：2%风险=2000元 -> 250股（市值25000，占25% <= 40%）
    const res = calcPositionSize(100000, 100, 92);
    expect(res.recommendedShares).toBe(250);
    expect(res.firstTrancheShares).toBe(125);
    expect(res.positionPct).toBe(25.0);
    expect(res.cappedBy40Pct).toBe(false);
  });

  it('止损极窄时自动被40%单票上限截断', () => {
    // 10万资金，买价100，止损99（每股风险1元）：2%风险=2000股，但40%上限=400股
    const res = calcPositionSize(100000, 100, 99);
    expect(res.recommendedShares).toBe(400);
    expect(res.cappedBy40Pct).toBe(true);
  });
});

describe('buildCsvContent UTF-8 BOM 与转义', () => {
  it('包含BOM头且正确转义逗号与双引号', () => {
    const csv = buildCsvContent(['代码', '备注'], [['MU', '含,逗号'], ['NVDA', '含"引号"']]);
    expect(csv.startsWith('\uFEFF代码,备注\r\n')).toBe(true);
    expect(csv.includes('MU,"含,逗号"')).toBe(true);
    expect(csv.includes('NVDA,"含""引号"""')).toBe(true);
  });
});

describe('resolvePositionsViewState 持仓页三态（loading / error / empty / ready）防误导', () => {
  it('加载中优先返回 loading', () => {
    expect(resolvePositionsViewState(true, false, 0)).toBe('loading');
    expect(resolvePositionsViewState(true, true, 0)).toBe('loading');
  });
  it('接口失败时即使 positions.length===0 也必须返回 error，严禁返回 empty 渲染成暂无持仓', () => {
    expect(resolvePositionsViewState(false, true, 0)).toBe('error');
  });
  it('接口成功且长度为 0 时才返回 empty', () => {
    expect(resolvePositionsViewState(false, false, 0)).toBe('empty');
  });
  it('接口成功且有持仓时返回 ready', () => {
    expect(resolvePositionsViewState(false, false, 3)).toBe('ready');
  });
});
