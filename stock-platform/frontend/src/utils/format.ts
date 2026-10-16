// 通用格式化与风控/导出纯函数（便于单测）
import { colors } from '../theme/tokens';

// 根据市场代码返回币种符号（A股为 ¥，美股及默认为 $）
export const currencySymbol = (market?: string) => (market === 'A' ? '¥' : '$');

// 三策略点位：价格两位小数；后端指标不足时字段为 null，显示占位符
export const fmtPx = (v: number | null, market?: string) =>
  v == null ? '--' : `${currencySymbol(market)}${v.toFixed(2)}`;

// 距离百分比：带正负号
export const fmtDist = (v: number | null) => (v == null ? '--' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`);

// 涨跌三色：正绿/负红/零与无值灰（DESIGN.md 盈亏语义约定）
export const pctColor = (v: number) => (v > 0 ? colors.profit : v < 0 ? colors.loss : colors.textSecondary);

// 2%定额风险法则与单票40%上限科学建仓计算器（与后端 calculate_recommended_position 保持一致）
export const calcPositionSize = (
  capital: number,
  buyPrice: number | null | undefined,
  stopLoss?: number | null,
  riskPct: number = 0.02,
  maxPosPct: number = 0.40
) => {
  const effCap = capital && capital > 0 ? capital : 100000;
  if (!buyPrice || buyPrice <= 0) {
    return {
      recommendedShares: 0,
      firstTrancheShares: 0,
      positionValue: 0,
      positionPct: 0,
      maxLossAmount: 0,
      stopLoss: 0,
      addPrice1: 0,
      cappedBy40Pct: false,
    };
  }
  const effStop = stopLoss && stopLoss > 0 && stopLoss < buyPrice
    ? stopLoss
    : Number((buyPrice * 0.92).toFixed(2));
  const riskAmount = effCap * riskPct;
  const perShareRisk = Math.abs(buyPrice - effStop);
  const riskShares = perShareRisk > 0 ? Math.floor(riskAmount / perShareRisk) : 0;
  const maxSharesByCap = Math.floor((effCap * maxPosPct) / buyPrice);
  const cappedBy40Pct = riskShares > maxSharesByCap;
  const recommendedShares = Math.max(0, Math.min(riskShares, maxSharesByCap));
  const positionValue = Number((recommendedShares * buyPrice).toFixed(2));
  const positionPct = Number(((positionValue / effCap) * 100).toFixed(1));
  return {
    recommendedShares,
    firstTrancheShares: Math.max(0, Math.floor(recommendedShares / 2)),
    positionValue,
    positionPct,
    maxLossAmount: Number((recommendedShares * perShareRisk).toFixed(2)),
    stopLoss: effStop,
    addPrice1: Number((buyPrice * 0.95).toFixed(2)),
    cappedBy40Pct,
  };
};

// 生成带 UTF-8 BOM 的 CSV 文本（Excel 直接双击打开中文不乱码）
export const buildCsvContent = (
  headers: string[],
  rows: Array<Array<string | number | null | undefined>>
) => {
  const escapeCell = (val: string | number | null | undefined) => {
    if (val === null || val === undefined) return '';
    const s = String(val);
    if (s.includes(',') || s.includes('"') || s.includes('\n') || s.includes('\r')) {
      return `"${s.replace(/"/g, '""')}"`;
    }
    return s;
  };
  const lines = [
    headers.map(escapeCell).join(','),
    ...rows.map((r) => r.map(escapeCell).join(',')),
  ];
  return '\uFEFF' + lines.join('\r\n');
};

// 浏览器端一键触发 CSV 下载
export const downloadCsv = (
  filename: string,
  headers: string[],
  rows: Array<Array<string | number | null | undefined>>
) => {
  if (typeof window === 'undefined' || typeof document === 'undefined') return;
  const csv = buildCsvContent(headers, rows);
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.setAttribute('download', filename.endsWith('.csv') ? filename : `${filename}.csv`);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
};
