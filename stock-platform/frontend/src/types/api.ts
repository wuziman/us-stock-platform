// 全站 API 类型唯一定义处。后端改字段名时同步这里，tsc 会在编译期报出所有失配点。

// ===== 通用 =====
export interface EquityPoint {
  date: string;
  value: number;
}

export interface KlineBar {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number;
  ma5: number | null;
  ma20: number | null;
  ma50: number | null;
  bb_upper: number | null;
  bb_mid: number | null;
  bb_lower: number | null;
  rsi?: number | null;
  macd?: number | null;
  macd_signal?: number | null;
  macd_hist?: number | null;
}

export interface PositionSizeInfo {
  capital: number;
  buy_price: number;
  stop_loss: number;
  risk_per_trade_pct: number;
  max_loss_amount: number;
  recommended_shares: number;
  risk_2pct_shares?: number;
  half_kelly_shares?: number | null;
  score_tier_shares?: number | null;
  position_value: number;
  position_pct: number;
  first_tranche_shares: number;
  add_price_1: number;
  add_price_2?: number;
  add_price_3?: number;
  capped_by_40pct: boolean;
  half_kelly_pct?: number | null;
  win_rate_used?: number | null;
  reward_risk_ratio_used?: number;
  kelly_source?: string;
  rr_source?: string;
  kelly_warning?: string | null;
  sizing_mode?: string;
  regime_status?: string;
  regime_scale?: number;
}

// ===== 股票 / 自选股 =====
export interface SearchResult {
  code: string;
  name: string;
  market: string;
}

export interface WatchlistItem {
  id: number;
  stock_code: string;
  stock_name: string;
  market: string;
  created_at?: string;
}

export interface QuoteItem {
  stock_code: string;
  price: number | null;
  change_pct: number | null;
}

export interface EarningsItem {
  stock_code: string;
  stock_name: string;
  market: string;
  earnings_date: string | null;
  days_away: number | null;
}

// ===== 持仓 =====
export interface HoldingAddSignal {
  should_add: boolean;
  add_count: number;
  max_adds: number;
  initial_price: number;
  drop_from_initial_pct: number;
  next_tier: number | null;
  next_tier_drop_pct: number | null;
  next_trigger_price: number | null;
  suggested_add_amount: number;
  suggested_add_shares: number;
}

export interface RiskSwitchesInfo {
  sizing_mode: 'half_kelly_combined' | 'fixed_2pct' | string;
  trailing_stop_enabled: boolean;
  staged_add_enabled: boolean;
  regime_linkage_enabled: boolean;
}

export interface AddTrancheResponse {
  message: string;
  position_id: number;
  stock_code: string;
  initial_price: number;
  previous_buy_price: number;
  new_buy_price: number;
  previous_quantity: number;
  added_quantity: number;
  new_quantity: number;
  add_count: number;
  remaining_adds: number;
  previous_stop_loss: number | null;
  new_stop_loss: number;
  stop_loss_shift_note: string;
}

export interface Position {
  id: number;
  stock_code: string;
  stock_name: string;
  market: string;
  buy_price: number;
  initial_price?: number | null;
  add_count?: number;
  highest_price?: number | null;
  quantity: number;
  buy_date: string;
  stop_loss: number | null;
  take_profit: number | null;
  trailing_stop_price?: number | null;
  effective_stop_loss?: number | null;
  add_position_signal?: HoldingAddSignal | null;
  status: string;
  sell_price?: number | null;
  sell_date?: string | null;
  current_price?: number | null;
  profit_loss?: number | null;
  profit_loss_pct?: number | null;
  realized_pnl?: number | null;
  realized_pnl_pct?: number | null;
  holding_days?: number | null;
}

export interface PositionWarning {
  level: 'error' | 'warning' | 'info' | string;
  code: string;
  name: string;
  weight: number;
  message: string;
}

export interface PositionSummary {
  total_positions: number;
  total_value: number;
  total_cost: number;
  total_profit: number;
  total_profit_pct: number;
  total_capital: number;
  cash_pct: number | null;
  usd_cny_rate?: number;
  currency?: 'USD' | 'CNY' | string;
  warnings: PositionWarning[];
}

export interface SellResponse {
  message: string;
  partial?: boolean;
  sold_quantity?: number;
  remaining_quantity?: number;
  realized_pnl: number;
  realized_pnl_pct: number;
  holding_days: number;
}

export interface EquityCurveResponse {
  curve: EquityPoint[];
  current_drawdown_pct: number;
  max_drawdown_pct: number;
  peak_value: number;
}

// ===== 分析 =====
export interface NewsItem {
  title: string;
  source?: string;
  datetime?: string;
  sentiment?: number;
  url?: string;
}

export interface EventItem {
  name: string;
  date?: string | null;
  days_away: number | null;
  impact: string;
}

export interface AnalysisDetails {
  technical: Record<string, unknown>;
  news: {
    news_count: number;
    positive_count: number;
    negative_count: number;
    sentiment: string;
    sources?: string[];
    news: NewsItem[];
  };
  macro: {
    indicators?: Record<string, string | number | null>;
    interpretations?: string[];
  };
  event: {
    events?: EventItem[];
  };
}

export interface MarketRegimeResponse {
  vix: number;
  qqq_change: number;
  status: 'NORMAL' | 'CAUTION' | 'CIRCUIT_BREAKER' | string;
  banner: string;
  advice: string;
}

export interface EarningsRadar {
  date: string | null;
  days_away: number | null;
  tag: string;
  is_imminent: boolean;
}

export interface AnalysisResult {
  stock_code: string;
  stock_name: string;
  market: string;
  scores: {
    technical: number;
    news: number;
    macro: number;
    event: number;
    total: number;
  };
  recommendation: {
    level: string;
    action: string;
    confidence: string;
  };
  earnings_radar?: EarningsRadar;
  regime_adaptive?: {
    regime: string;
    mode_desc: string;
    weights: {
      technical: number;
      news: number;
      macro: number;
      event: number;
    };
    adaptive_total_score: number;
    static_total_score: number;
    score_diff: number;
  };
  action_card?: {
    action_type: string;
    action_label: string;
    current_price: number;
    buy_price: number;
    position_pct: string;
    take_profit_price: number;
    stop_loss_price: number;
    one_sentence_reason: string;
    risk_reward_ratio: string;
  };
  price_levels: {
    current_price: number | null;
    atr?: number | null;
    linear: PriceLevels;
    nonlinear: PriceLevels;
    macd?: {
      state: 'golden' | 'death' | 'unknown';
      days_in_state: number;
      hist: number;
      add_price: number | null;
      watch_price: number | null;
      stop: number | null;
      note: string;
    };
  };
  details: AnalysisDetails;
}

export interface PriceLevels {
  buy: number | null;
  stop: number | null;
  profit: number | null;
  distance: number | null;
  atr_stop?: number | null;
  atr_profit?: number | null;
  breakeven_trigger?: number | null;
  breakeven_stop?: number | null;
}

export interface HistoryItem {
  id: number;
  stock_code: string;
  stock_name: string;
  market: string;
  total_score: number | null;
  recommendation: string | null;
  created_at: string | null;
}

// ===== 回测 =====
export interface Strategy {
  id: string;
  name: string;
  description: string;
}

export interface BacktestResult {
  stock_code: string;
  strategy: string;
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe_ratio: number;
  win_rate: number;
  trade_count: number;
  initial_capital: number;
  final_value: number;
  equity_curve: EquityPoint[];
  period: string;
  date_range?: string;   // 自定义起止日期时由后端返回实际窗口
  trades: Array<{
    date: string;
    action: string;
    price: number;
    shares: number;
    profit_pct?: number;
    fee?: number;
  }>;
  total_fees?: number;
  commission_per_trade?: number;
  buy_hold_curve?: EquityPoint[];
  buy_hold_return?: number;   // 买入持有基准收益（后端随单次回测返回）
}

export interface CompareRow {
  name: string;
  key: string;
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe_ratio: number;
  win_rate: number;
  trade_count: number;
  total_fees: number;
  excess_vs_buy_hold: number;
}

export interface CompareResult {
  stock_code: string;
  period: string;
  initial_capital: number;
  strategies: Record<string, BacktestResult>;
  buy_hold: {
    total_return: number;
    annual_return: number;
    max_drawdown: number;
    sharpe_ratio: number;
    equity_curve: EquityPoint[];
  };
  comparison: CompareRow[];
}

// 🎯 参数寻优
export interface OptimizeRow {
  params: Record<string, number>;
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe_ratio: number;
  win_rate: number;
  trade_count: number;
  final_value: number;
}

export interface HeatmapData {
  x_name: string;
  y_name: string;
  x_values: number[];
  y_values: number[];
  z: Array<Array<number | null>>;
}

export interface OptimizeResult {
  stock_code: string;
  strategy: string;
  metric: string;
  period?: string;             // 回显实际回测口径
  initial_capital?: number;
  best: OptimizeRow;
  results: OptimizeRow[];
  heatmap: HeatmapData;
  in_sample_warning?: string;
}

// 🔬 Walk-Forward验证
export interface WFSegment {
  step: number;
  train_range: [string, string];
  test_range: [string, string];
  best_params: Record<string, number>;
  is_sharpe: number;
  oos_return: number;
  oos_sharpe: number;
  oos_max_drawdown: number;
  oos_buy_hold_return: number;
  beats_buy_hold: boolean;
}

export interface WFParamStability {
  mode_params: Record<string, number>;
  mode_count: number;
  mode_ratio: number;
  mode_ratio_pct: number;
  unique_combos: number;
  total_segments: number;
  stability_rating: '高' | '中' | '低' | string;
  per_param?: Record<string, { mode: number; mode_count: number; mode_ratio: number }>;
}

export interface WFResult {
  stock_code: string;
  strategy: string;
  period?: string;
  initial_capital?: number;
  train_ratio: number;
  total_segments: number;
  segments: WFSegment[];
  stitched_oos_curve: EquityPoint[];
  oos_buy_hold_curve: EquityPoint[];
  param_stability?: WFParamStability;
  summary: {
    avg_oos_return: number;
    avg_oos_sharpe: number;
    win_segments: number;
    total_segments: number;
    mode_ratio?: number;
    unique_combos?: number;
    stability_rating?: string;
    param_stability?: WFParamStability;
  };
}

// ===== 日报 / 调度 =====
export interface ReportPreview {
  report: string;
  char_count: number;
}

export interface ReportSend {
  sent: boolean;
  message?: string;
  report?: string;
  char_count?: number;
  error?: string;
}

export interface ScheduleInfo {
  enabled: boolean;
  hour: number;
  minute: number;
  last_sent_date: string | null;
}

// ===== AI选股 =====
export interface PickRecord {
  id: number;
  run_date: string;
  created_at: string | null;
  rank: number;
  stock_code: string;
  stock_name: string;
  confidence: string;
  thesis: string;
  bottlenecks: string;
  risks: string;
  catalysts: string;
  market_commentary: string;
  price_at_pick: number | null;
  current_price?: number | null;
  return_pct?: number | null;
}

export interface StatusInfo {
  ai_configured: boolean;
  ai_model: string;
  xhs_cookie_set: boolean;
  bloggers: Array<{ name: string; url: string }>;
  cached_posts: number;
  last_run_date: string | null;
}

export interface XhsSummaryRow {
  blogger_name: string;
  summary_text: string;
  posts_count: number | null;
  period_start: string | null;
  period_end: string | null;
  created_at: string | null;
}

export interface WeeklyAlphaItem {
  code: string;
  name: string;
  sector: string;
  current_price: number;
  rsi: number;
  m20_return: number;
  win_rate: number;
  momentum_score: number;
  sweetspot_score: number;
  backtest_score: number;
  total_score: number;
  earnings: {
    date: string | null;
    days: number | null;
    tag: string;
    is_imminent: boolean;
  };
  linear_buy: number;
  nonlinear_buy: number;
  linear_profit: number;
  linear_stop: number;
  forward_pe?: number | null;
  rev_growth_pct?: number | null;
  target_price?: number | null;
  upside_pct?: number | null;
  atr_stop?: number;
  breakeven_trigger?: number;
}

export interface WeeklyAlphaResponse {
  scan_date: string | null;
  total_scanned: number;
  top5: WeeklyAlphaItem[];
  ai_thesis: string;
  battle_report?: BattleReport | null;
}

export interface BattlePickReview {
  code: string;
  name: string;
  sector: string;
  entry_price: number;
  review_price: number;
  weekly_return_pct: number;
  is_winner: boolean;
}

export interface BattleReport {
  prev_scan_date: string;
  review_date: string;
  picks: BattlePickReview[];
  win_count: number;
  loss_count: number;
  win_rate: number;
  avg_return_pct: number;
  best_pick: { code: string; name: string; return_pct: number };
  worst_pick: { code: string; name: string; return_pct: number };
}

export interface WeeklyHistoryResponse {
  weeks: Array<{
    scan_date: string;
    top5: Array<{ code: string; name: string; sector: string; total_score: number; current_price: number }>;
    battle_report?: BattleReport | null;
  }>;
  cumulative: {
    total_weeks: number;
    total_picks: number;
    overall_win_rate: number;
    avg_return_pct: number;
    total_winners: number;
  } | null;
}

export interface AIPickConfidenceWindowStat {
  matured_count: number;
  pending_count: number;
  win_count: number;
  win_rate: number | null;
  avg_return_pct: number | null;
}

export interface AIPickConfidenceTier {
  total_picks: number;
  d20: AIPickConfidenceWindowStat;
  d60: AIPickConfidenceWindowStat;
}

export interface AIPickPerformanceItem {
  id: number | null;
  run_date: string;
  stock_code: string;
  stock_name: string;
  confidence: 'high' | 'medium' | 'low' | string;
  entry_price: number | null;
  status_20d: 'matured' | 'pending';
  price_20d: number | null;
  return_20d_pct: number | null;
  status_60d: 'matured' | 'pending';
  price_60d: number | null;
  return_60d_pct: number | null;
}

export interface AIPickPerformanceResponse {
  total_picks: number;
  overall: {
    d20: AIPickConfidenceWindowStat;
    d60: AIPickConfidenceWindowStat;
  };
  by_confidence: {
    high: AIPickConfidenceTier;
    medium: AIPickConfidenceTier;
    low: AIPickConfidenceTier;
    [key: string]: AIPickConfidenceTier;
  };
  picks: AIPickPerformanceItem[];
  note?: string;
}

// ===== 买入雷达 =====
export interface RadarItem {
  stock_code: string;
  stock_name: string;
  market: string;
  current_price: number;
  change_percent: number;
  buy_price: number;
  distance_pct: number;
  position_pct: string;
  status: 'TRIGGERED' | 'APPROACHING' | 'WAITING';
  status_label: string;
  status_color: string;
}

// ===== 模拟实战记账 =====
export interface PaperTradeOpenPayload {
  stock_code: string;
  stock_name: string;
  market?: string;
  buy_price: number;
  position_pct?: string;
  take_profit_price: number;
  stop_loss_price: number;
}

export interface PaperTradeItem {
  id: number;
  stock_code: string;
  stock_name?: string;
  market: string;
  buy_price: number;
  buy_date: string;
  position_pct: string;
  take_profit_price: number;
  stop_loss_price: number;
  status: 'OPEN' | 'PROFIT' | 'LOSS' | 'CLOSED';
  current_price?: number | null;
  unrealized_pnl_pct?: number | null;
  exit_price?: number | null;
  exit_date?: string | null;
  exit_reason?: string | null;
  pnl_pct?: number | null;
  created_at?: string | null;
}

export interface PaperTradeSummary {
  total_settled_trades: number;
  win_trades: number;
  loss_trades: number;
  win_rate_pct: number;
  total_realized_pnl_pct: number;
  open_trades_count: number;
}
