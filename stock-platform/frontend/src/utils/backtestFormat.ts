// 回测域的展示辅助（纯函数，便于单测）
import { colors } from '../theme/tokens';

// 周期 → 中文标签（undefined/自定义区间以外的兜底为近1年）
export const periodLabel = (p?: string) => (p === '3y' ? '近3年' : p === '5y' ? '近5年' : '近1年');

// 策略 → 图表系列色（与 DESIGN.md 图表色约定一致）
export const STRATEGY_COLORS: Record<string, string> = {
  linear: colors.primary,
  nonlinear: colors.chartPurple,
  ma_cross: colors.chartCyan,
  macd: colors.chartOrange,
};

// 参数对象 → "x=1, y=2" 展示串
export const fmtParams = (p: Record<string, number>) =>
  Object.entries(p || {}).map(([k, v]) => `${k}=${v}`).join(', ');

// 寻优排序指标中文名
export const METRIC_LABEL: Record<string, string> = {
  sharpe: '夏普比率',
  total_return: '总收益%',
  annual_return: '年化收益%',
  max_drawdown: '最大回撤%',
  win_rate: '胜率%',
  trade_count: '交易次数',
};

/**
 * 格式化回测权益曲线 Tooltip 单行（兼容折线标量 value 与 B/S 散点 [date, equity] 数组 value，彻底消除 $NaN）
 */
export const formatBacktestTooltipLine = (p: any) => {
  const marker = p?.marker || '';
  const seriesName = p?.seriesName || '';
  const rawVal = Array.isArray(p?.value) ? p.value[1] : p?.value;
  const numVal = Number(rawVal);
  const eqStr = Number.isFinite(numVal) ? `$${numVal.toLocaleString()}` : '--';
  const dataObj = p?.data || {};
  if (dataObj && typeof dataObj === 'object' && dataObj.price != null) {
    const pxStr = `$${Number(dataObj.price).toFixed(2)}`;
    if (dataObj.shares != null) {
      return `${marker}${seriesName}: 成交价 ${pxStr} (${dataObj.shares}股) · 总资产 ${eqStr}`;
    }
    if (dataObj.profit_pct != null) {
      const pct = Number(dataObj.profit_pct) * 100;
      const sign = pct >= 0 ? '+' : '';
      return `${marker}${seriesName}: 成交价 ${pxStr} (${sign}${pct.toFixed(2)}%) · 总资产 ${eqStr}`;
    }
    return `${marker}${seriesName}: 成交价 ${pxStr} · 总资产 ${eqStr}`;
  }
  return `${marker}${seriesName}: ${eqStr}`;
};
