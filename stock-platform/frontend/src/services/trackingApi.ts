import api from './api';

// 单条评分记录（含至今收益）
export interface TrackingRecord {
  date: string;
  total_score: number;
  entry_price: number;
  current_price: number;
  forward_return_pct: number;
}

// 评分分桶统计
export interface TrackingBucket {
  bucket: string;
  count: number;
  avg_return: number | null;
}

// 评分追踪响应
export interface TrackingResult {
  stock_code: string;
  count: number;
  records: TrackingRecord[];
  buckets: TrackingBucket[];
  correlation: number | null;
  interpretation: string;
}

// 四维权重体检报告
export interface DimensionCorrelationItem {
  dimension: string;
  label: string;
  base_weight: number;
  correlation: number | null;
  variance_zero: boolean;
  effective_rank_score: number;
  status: string;
}

export interface PerturbationSensitivityItem {
  dimension: string;
  label: string;
  delta: string;
  perturbed_weights: Record<string, number>;
  base_portfolio_return_pct: number;
  perturbed_portfolio_return_pct: number;
  return_impact_pct: number;
}

export interface WeightHealthReport {
  window_days: number;
  forward_days: number;
  mode: string;
  sample_count: number;
  base_weights: Record<string, number>;
  base_portfolio_return_pct: number;
  dimension_correlations: Record<string, DimensionCorrelationItem>;
  swimming_naked_dimension: {
    dimension: string;
    label: string;
    correlation: number | null;
    base_weight: number;
    reason: string;
    conclusion: string;
  };
  diagnosis: string;
  perturbation_sensitivity: PerturbationSensitivityItem[];
  policy_note: string;
}

// 评分追踪API
export const trackingApi = {
  getTracking: (code: string) =>
    api.get<TrackingResult>('/analysis/tracking', { params: { stock_code: code } }),

  getWeightHealth: (params?: { stock_code?: string; window_days?: number; forward_days?: number; mode?: string }) =>
    api.get<WeightHealthReport>('/analysis/weight-health', { params }),
};

export default trackingApi;
