import axios from 'axios';
import type {
  AnalysisResult,
  BacktestResult,
  CompareResult,
  EarningsItem,
  EquityCurveResponse,
  HistoryItem,
  KlineBar,
  MarketRegimeResponse,
  OptimizeResult,
  Position,
  PositionSizeInfo,
  PositionSummary,
  QuoteItem,
  ReportPreview,
  ReportSend,
  SearchResult,
  SellResponse,
  Strategy,
  WatchlistItem,
  WFResult,
  PickRecord,
  StatusInfo,
  XhsSummaryRow,
  WeeklyAlphaResponse,
  WeeklyHistoryResponse,
  AIPickPerformanceResponse,
} from '../types/api';

const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
});

// 从 axios 错误中提取后端 detail/message，取不到用 fallback（全站统一错误文案出口）
export function errDetail(e: unknown, fallback = '请求失败'): string {
  const resp = (e as { response?: { data?: { detail?: string; message?: string } } })?.response;
  return resp?.data?.detail || resp?.data?.message || fallback;
}

// 股票相关API
export const stockApi = {
  search: (query: string, market: string = 'all') =>
    api.get<{ results: SearchResult[] }>('/stocks/search', { params: { q: query, market } }),

  getHistory: (code: string, market: string = 'US', period: string = '3mo') =>
    api.get<{ data: KlineBar[] }>(`/stocks/${code}/history`, { params: { market, period } }),

  getWatchlist: () =>
    api.get<WatchlistItem[]>('/stocks/watchlist'),

  // 自选股实时行情（并行端点，供首页驾驶舱表格渐进填充）
  getWatchlistQuotes: () =>
    api.get<QuoteItem[]>('/stocks/watchlist/quotes'),

  // 自选股财报日历（美股yfinance；A股拿不到则日期为空）
  getEarningsCalendar: () =>
    api.get<{ items: EarningsItem[]; no_data: Array<{ stock_code: string; stock_name: string }> }>('/stocks/earnings-calendar'),

  // 大盘宏观波动率与黑天鹅熔断态势 (VIX + QQQ)
  getMarketRegime: () =>
    api.get<MarketRegimeResponse>('/stocks/market-regime'),

  addToWatchlist: (data: { stock_code: string; stock_name: string; market: string }) =>
    api.post<WatchlistItem>('/stocks/watchlist', data),

  removeFromWatchlist: (id: number) =>
    api.delete<{ message: string }>(`/stocks/watchlist/${id}`),
};

// 分析相关API
export const analysisApi = {
  analyze: (data: { stock_code: string; stock_name: string; mode?: string }) =>
    api.post<AnalysisResult>('/analysis', data),

  // 分析历史（market 由后端 detect_market 统一判定）
  getHistory: (stockCode?: string, limit: number = 20) =>
    api.get<HistoryItem[]>('/analysis/history', { params: { stock_code: stockCode, limit } }),
};

// 回测相关API
export const backtestApi = {
  getStrategies: () =>
    api.get<{ strategies: Strategy[] }>('/backtest/strategies'),

  runBacktest: (data: {
    stock_code: string;
    strategy: string;
    period?: string;
    start_date?: string;
    end_date?: string;
    initial_capital?: number;
    commission_per_trade?: number;
    trailing_stop?: boolean;
    staged_entry?: boolean;
    params?: Record<string, number>;
  }) => api.post<BacktestResult>('/backtest', data),

  // 一键对比4策略 + 买入持有基准
  compare: (data: {
    stock_code: string;
    period?: string;
    initial_capital?: number;
    commission_per_trade?: number;
    trailing_stop?: boolean;
    staged_entry?: boolean;
  }) => api.post<CompareResult>('/backtest/compare', data),

  // 🎯 参数网格寻优
  optimize: (data: {
    stock_code: string;
    strategy: string;
    period?: string;
    initial_capital?: number;
    commission_per_trade?: number;
    metric?: string;
  }) => api.post<OptimizeResult>('/backtest/optimize', data),

  // 🔬 Walk-Forward滚动验证
  walkForward: (data: {
    stock_code: string;
    strategy: string;
    period?: string;
    initial_capital?: number;
    commission_per_trade?: number;
    train_ratio?: number;
    segments?: number;
  }) => api.post<WFResult>('/backtest/walkforward', data),
};

// 持仓相关API
export const portfolioApi = {
  getPositions: () =>
    api.get<Position[]>('/portfolio'),

  getHistory: () =>
    api.get<Position[]>('/portfolio/history'),

  sellPosition: (id: number, data: { sell_price: number; sell_date: string }) =>
    api.post<SellResponse>(`/portfolio/${id}/sell`, data),

  addPosition: (data: {
    stock_code: string;
    stock_name: string;
    market?: string;
    buy_price: number;
    quantity: number;
    buy_date: string;
    stop_loss?: number;
    take_profit?: number;
  }) => api.post<Position>('/portfolio', data),

  updatePosition: (id: number, data: {
    buy_price?: number;
    quantity?: number;
    buy_date?: string;
    stop_loss?: number | null;
    take_profit?: number | null;
  }) => api.put<{ message: string }>(`/portfolio/${id}`, data),

  deletePosition: (id: number) =>
    api.delete<{ message: string }>(`/portfolio/${id}`),

  getSummary: () =>
    api.get<PositionSummary>('/portfolio/summary'),

  // 组合净值曲线 + 回撤统计（每日快照由后台监控任务写入）
  getEquityCurve: () =>
    api.get<EquityCurveResponse>('/portfolio/equity-curve'),

  getTotalCapital: () =>
    api.get<{ total_capital: number }>('/portfolio/settings/total_capital'),

  setTotalCapital: (total_capital: number) =>
    api.put<{ message: string; total_capital: number }>(`/portfolio/settings/total_capital?total_capital=${total_capital}`),

  getPositionSize: (params: {
    buy_price: number;
    stop_loss?: number;
    total_capital?: number;
    risk_per_trade?: number;
    win_rate?: number;
  }) => api.get<PositionSizeInfo>('/portfolio/position-size', { params }),
};

// 日报API
export const reportApi = {
  preview: () => api.get<ReportPreview>('/report/preview'),
  send: (dryRun: boolean) => api.post<ReportSend>('/report/send', { dry_run: dryRun }),
};

// AI选股API
export const aiPickApi = {
  getStatus: () => api.get<StatusInfo>('/ai-pick/status'),

  // 运行一次完整AI选股（LLM分析耗时较长）
  run: () => api.post<{ picks: PickRecord[] }>('/ai-pick/run', null, { timeout: 300000 }),

  getHistory: (limit: number = 30) =>
    api.get<PickRecord[]>('/ai-pick/history', { params: { limit } }),

  getPerformance: (limit: number = 100) =>
    api.get<AIPickPerformanceResponse>('/ai-pick/performance', { params: { limit } }),

  getXhsConfig: () => api.get<{ bloggers?: Array<{ name: string; url: string }> }>('/ai-pick/xhs-config'),

  saveXhsConfig: (data: { cookie?: string | null; bloggers?: Array<{ name: string; url: string }> | null }) =>
    api.put<{ message?: string }>('/ai-pick/xhs-config', data),

  refreshXhs: () =>
    api.post<{ bloggers?: Array<{ count?: number }>; new_posts: number }>('/ai-pick/xhs-refresh', null, { timeout: 120000 }),

  getXhsSummaries: () => api.get<XhsSummaryRow[]>('/ai-pick/xhs-summaries'),

  generateXhsSummaries: () =>
    api.post<{ summaries?: XhsSummaryRow[]; errors?: unknown[] }>('/ai-pick/xhs-summaries', null, { timeout: 300000 }),

  // 周度美股硬科技选股 · Alpha TOP 5 猛禽池
  getWeeklyAlphaTop5: () =>
    api.get<WeeklyAlphaResponse>('/ai-pick/top5'),

  triggerWeeklyAlphaScan: () =>
    api.post<WeeklyAlphaResponse>('/ai-pick/top5/scan', null, { timeout: 180000 }),

  getWeeklyAlphaHistory: () =>
    api.get<WeeklyHistoryResponse>('/ai-pick/top5/history'),
};

export default api;
