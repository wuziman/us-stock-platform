// 回测页三面板：对比 / 参数寻优 / Walk-Forward。
// 自包含设计——各自持有结果状态与请求逻辑，只依赖父组件传入的股票代码与主表单参数，
// 消除此前"寻优/WF 静默继承顶部表单"的隐式耦合（参数经 getParams 显式传入并回显）。
import React, { useState } from 'react';
import { Card, Select, Button, InputNumber, Table, Tag, Space, Descriptions, message } from 'antd';
import { AimOutlined, PlayCircleOutlined, RocketOutlined } from '@ant-design/icons';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import echarts from '../services/echarts';
import { backtestApi } from '../services/api';
import type { CompareResult, OptimizeResult, OptimizeRow, Strategy, WFSegment, WFResult } from '../types/api';
import { colors } from '../theme/tokens';
import { METRIC_LABEL, STRATEGY_COLORS, fmtParams, periodLabel } from '../utils/backtestFormat';
import { pctColor } from '../utils/format';

const tooltipFormatter = (params: unknown) => {
  const arr = Array.isArray(params) ? params : [params];
  const lines = (arr as Array<{ marker?: string; seriesName?: string; value?: unknown }>).map(
    p => `${p.marker}${p.seriesName}: $${Number(p.value).toLocaleString()}`);
  return [(arr[0] as { name?: string })?.name, ...lines].join('<br/>');
};

// ⚡ 4策略对比 vs 买入持有（纯展示：请求由主表单区的一键对比按钮发起）
export const ComparePanel: React.FC<{ result: CompareResult | null }> = ({ result }) => {
  if (!result) return null;

  const strategyList = [
    { key: 'linear', name: '线性' },
    { key: 'nonlinear', name: '非线性' },
    { key: 'ma_cross', name: '双均线交叉' },
    { key: 'macd', name: 'MACD' }
  ];
  // 4策略同日期轴，取第一个有数据的作为x轴
  const first = strategyList
    .map(s => result.strategies[s.key])
    .find(r => r?.equity_curve && r.equity_curve.length > 0);

  const compareColumns = [
    {
      title: '策略',
      dataIndex: 'name',
      key: 'name',
      render: (name: string, record: { key: string }) =>
        record.key === 'buy_hold'
          ? <Tag color="default">{name}（基准）</Tag>
          : <b>{name}</b>
    },
    {
      title: '总收益', dataIndex: 'total_return', key: 'total_return',
      render: (v: number) => <span style={{ color: pctColor(v) }}>{v >= 0 ? '+' : ''}{v.toFixed(2)}%</span>
    },
    {
      title: '年化收益', dataIndex: 'annual_return', key: 'annual_return',
      render: (v: number) => <span style={{ color: pctColor(v) }}>{v >= 0 ? '+' : ''}{v.toFixed(2)}%</span>
    },
    {
      title: '最大回撤', dataIndex: 'max_drawdown', key: 'max_drawdown',
      render: (v: number) => <span style={{ color: colors.loss }}>{v.toFixed(2)}%</span>
    },
    {
      title: '夏普比率', dataIndex: 'sharpe_ratio', key: 'sharpe_ratio',
      render: (v: number) => v.toFixed(2)
    },
    {
      title: '胜率', dataIndex: 'win_rate', key: 'win_rate',
      render: (v: number) => `${v.toFixed(2)}%`
    },
    { title: '交易次数', dataIndex: 'trade_count', key: 'trade_count' },
    {
      title: '总手续费', dataIndex: 'total_fees', key: 'total_fees',
      render: (v: number) => `$${v.toFixed(2)}`
    },
    {
      title: '相对持有超额', dataIndex: 'excess_vs_buy_hold', key: 'excess_vs_buy_hold',
      render: (v: number) => (
        <span style={{ color: pctColor(v), fontWeight: 600 }}>
          {v > 0 ? '+' : ''}{v.toFixed(2)}%
        </span>
      )
    }
  ];

  const series: any[] = first ? strategyList.map(s => ({
    name: s.name,
    type: 'line',
    data: result.strategies[s.key]?.equity_curve.map(d => d.value) ?? [],
    smooth: true,
    showSymbol: false,
    lineStyle: { width: 2, color: STRATEGY_COLORS[s.key] },
    itemStyle: { color: STRATEGY_COLORS[s.key] }
  })) : [];

  // 买入持有基准：灰色虚线
  series.push({
    name: '买入持有',
    type: 'line',
    data: result.buy_hold.equity_curve.map(d => d.value),
    smooth: true,
    showSymbol: false,
    lineStyle: { width: 2, color: colors.chartNeutral, type: 'dashed' },
    itemStyle: { color: colors.chartNeutral }
  });

  return (
    <Card title={`⚡ 4策略对比 vs 买入持有 · ${result.stock_code}`} style={{ marginTop: 16 }}>
      <ReactEChartsCore
        echarts={echarts}
        option={{
          tooltip: { trigger: 'axis', formatter: tooltipFormatter },
          legend: { top: 0 },
          grid: { top: 40, left: 70, right: 20, bottom: 50 },
          xAxis: { type: 'category', data: first ? first.equity_curve.map(d => d.date) : [] },
          yAxis: { type: 'value', name: '总资产 ($)', scale: true },
          dataZoom: [{ type: 'inside' }, { type: 'slider', height: 20, bottom: 0 }],
          series
        }}
        style={{ height: 380 }}
      />
      <Table
        style={{ marginTop: 16, whiteSpace: 'nowrap' }}
        columns={compareColumns}
        dataSource={result.comparison}
        rowKey="key"
        pagination={false}
        size="small"
        scroll={{ x: 'max-content' }}
      />
    </Card>
  );
};

interface RunPanelProps {
  stockCode: string | null;
  strategies: Strategy[];
  /** 点击运行时从主表单取周期/资金（显式传参，面板标题回显实际口径） */
  getParams: () => { period?: string; initial_capital: number };
  /** 一键将寻优得到的最优参数带入主回测引擎执行 */
  onApplyBestParams?: (strategy: string, bestParams: Record<string, number>) => void;
}

// 🎯 参数网格寻优面板
export const OptimizePanel: React.FC<RunPanelProps> = ({ stockCode, strategies, getParams, onApplyBestParams }) => {
  const [optStrategy, setOptStrategy] = useState('linear');
  const [optResult, setOptResult] = useState<OptimizeResult | null>(null);
  const [optLoading, setOptLoading] = useState(false);

  const handleRunOptimize = async () => {
    if (!stockCode) {
      message.warning('请先选择股票');
      return;
    }
    try {
      setOptLoading(true);
      const params = getParams();
      const response = await backtestApi.optimize({
        stock_code: stockCode,
        strategy: optStrategy,
        period: params.period || '1y',
        initial_capital: params.initial_capital
      });
      setOptResult(response.data);
      message.success('寻优完成');
    } catch {
      message.error('参数寻优失败');
    } finally {
      setOptLoading(false);
    }
  };

  // 🎯 寻优热力图：x/y=两维参数，visualMap颜色映射metric值
  const getHeatmapOption = () => {
    if (!optResult?.heatmap) return {};
    const hm = optResult.heatmap;
    const data: Array<[number, number, number]> = [];
    hm.z.forEach((row, yi) =>
      row.forEach((v, xi) => {
        if (v !== null && v !== undefined) data.push([xi, yi, v]);
      })
    );
    if (data.length === 0) return {};
    const vals = data.map(d => d[2]);
    const metricName = METRIC_LABEL[optResult.metric] || optResult.metric;
    return {
      tooltip: {
        position: 'top',
        formatter: (p: { value: [number, number, number] }) =>
          `${hm.x_name}=${hm.x_values[p.value[0]]}<br/>${hm.y_name}=${hm.y_values[p.value[1]]}<br/>${metricName}: ${Number(p.value[2]).toFixed(2)}`
      },
      grid: { top: 40, left: 70, right: 100, bottom: 50 },
      xAxis: { type: 'category', name: hm.x_name, data: hm.x_values.map(String) },
      yAxis: { type: 'category', name: hm.y_name, data: hm.y_values.map(String) },
      visualMap: {
        min: Math.min(...vals),
        max: Math.max(...vals),
        calculable: true,
        orient: 'vertical',
        right: 10,
        top: 'center',
        inRange: { color: [colors.loss, colors.warning, colors.profit] }
      },
      series: [{
        type: 'heatmap',
        data,
        label: { show: true, formatter: (p: { value: [number, number, number] }) => Number(p.value[2]).toFixed(2) },
        emphasis: { itemStyle: { shadowBlur: 8, shadowColor: 'rgba(0,0,0,0.4)' } }
      }]
    };
  };

  const optimizeColumns = [
    {
      title: '#', key: 'rank', width: 40,
      render: (_: unknown, __: OptimizeRow, idx: number) => idx + 1
    },
    {
      title: '参数组合', dataIndex: 'params', key: 'params',
      render: (p: Record<string, number>) => <code>{fmtParams(p)}</code>
    },
    {
      title: '总收益', dataIndex: 'total_return', key: 'total_return',
      render: (v: number) => <span style={{ color: pctColor(v) }}>{v >= 0 ? '+' : ''}{v.toFixed(2)}%</span>
    },
    {
      title: '年化收益', dataIndex: 'annual_return', key: 'annual_return',
      render: (v: number) => <span style={{ color: pctColor(v) }}>{v >= 0 ? '+' : ''}{v.toFixed(2)}%</span>
    },
    {
      title: '夏普比率', dataIndex: 'sharpe_ratio', key: 'sharpe_ratio',
      render: (v: number) => v.toFixed(2)
    },
    {
      title: '最大回撤', dataIndex: 'max_drawdown', key: 'max_drawdown',
      render: (v: number) => <span style={{ color: colors.loss }}>{v.toFixed(2)}%</span>
    },
    { title: '交易次数', dataIndex: 'trade_count', key: 'trade_count' }
  ];

  return (
    <Card title={`🎯 参数寻优${optResult ? ` · ${optResult.stock_code} · ${periodLabel(optResult.period)} · 资金$${(optResult.initial_capital ?? 0).toLocaleString()}` : ''}`} style={{ marginTop: 16 }}>
      <Space wrap style={{ marginBottom: 16 }}>
        <Select value={optStrategy} onChange={setOptStrategy} style={{ width: 240 }} optionLabelProp="title">
          {strategies.map(s => (
            <Select.Option key={s.id} value={s.id} title={s.name}>
              {s.name} - {s.description}
            </Select.Option>
          ))}
        </Select>
        <Button
          type="primary"
          ghost
          icon={<AimOutlined />}
          loading={optLoading}
          disabled={!stockCode}
          onClick={handleRunOptimize}
        >
          开始寻优
        </Button>
      </Space>

      {optResult && (
        <>
          <div style={{ marginBottom: 12 }}>
            <Space wrap>
              <Tag color="gold">
                最优参数：{fmtParams(optResult.best.params)}
                （{METRIC_LABEL[optResult.metric] || optResult.metric} {Number(optResult.best.sharpe_ratio).toFixed(2)}）
              </Tag>
              {onApplyBestParams && (
                <Button
                  size="small"
                  type="primary"
                  icon={<RocketOutlined />}
                  onClick={() => onApplyBestParams(optResult.strategy, optResult.best.params)}
                >
                  以此最优参数运行回测
                </Button>
              )}
            </Space>
          </div>
          <ReactEChartsCore echarts={echarts} option={getHeatmapOption()} style={{ height: 320 }} />
          <div style={{ margin: '8px 0 4px', color: colors.textSecondary }}>
            热力图：x={optResult.heatmap.x_name}，y={optResult.heatmap.y_name}，颜色={METRIC_LABEL[optResult.metric] || optResult.metric}；下表为Top5最优参数组合
          </div>
          <Table
            style={{ whiteSpace: 'nowrap' }}
            columns={optimizeColumns}
            dataSource={optResult.results.slice(0, 5)}
            rowKey={(r) => fmtParams(r.params)}
            pagination={false}
            size="small"
            scroll={{ x: 'max-content' }}
          />
        </>
      )}
    </Card>
  );
};

// 🔬 Walk-Forward 滚动验证面板
export const WalkForwardPanel: React.FC<RunPanelProps> = ({ stockCode, strategies, getParams }) => {
  const [wfStrategy, setWfStrategy] = useState('linear');
  const [wfPeriod, setWfPeriod] = useState('5y');
  const [wfSegments, setWfSegments] = useState(2);
  const [wfResult, setWfResult] = useState<WFResult | null>(null);
  const [wfLoading, setWfLoading] = useState(false);

  const handleRunWalkForward = async () => {
    if (!stockCode) {
      message.warning('请先选择股票');
      return;
    }
    try {
      setWfLoading(true);
      const params = getParams();
      const response = await backtestApi.walkForward({
        stock_code: stockCode,
        strategy: wfStrategy,
        period: wfPeriod,
        initial_capital: params.initial_capital,
        segments: wfSegments
      });
      setWfResult(response.data);
      message.success('Walk-Forward验证完成');
    } catch {
      message.error('Walk-Forward验证失败');
    } finally {
      setWfLoading(false);
    }
  };

  // 🔬 拼接OOS净值 vs OOS买入持有（灰色虚线），按日期对齐
  const getWalkForwardChartOption = () => {
    if (!wfResult || !wfResult.stitched_oos_curve.length) return {};
    const curve = wfResult.stitched_oos_curve;
    const dates = curve.map(d => d.date);
    const bhMap = new Map(wfResult.oos_buy_hold_curve.map(p => [p.date, p.value]));

    const series: any[] = [
      {
        name: '拼接OOS净值',
        type: 'line',
        data: curve.map(d => d.value),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: colors.primary },
        areaStyle: { opacity: 0.12, color: colors.primary }
      },
      {
        name: 'OOS买入持有',
        type: 'line',
        data: dates.map(d => bhMap.get(d) ?? null),
        smooth: true,
        showSymbol: false,
        connectNulls: true,
        lineStyle: { width: 2, color: colors.chartNeutral, type: 'dashed' },
        itemStyle: { color: colors.chartNeutral }
      }
    ];

    return {
      tooltip: { trigger: 'axis', formatter: tooltipFormatter },
      legend: { top: 0 },
      grid: { top: 40, left: 70, right: 20, bottom: 50 },
      xAxis: { type: 'category', data: dates },
      yAxis: { type: 'value', name: '复合净值 ($)', scale: true },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 20, bottom: 0 }],
      series
    };
  };

  const wfColumns = [
    { title: '步骤', dataIndex: 'step', key: 'step', width: 60 },
    {
      title: '训练区间', dataIndex: 'train_range', key: 'train_range',
      render: (r: [string, string]) => `${r[0]} ~ ${r[1]}`
    },
    {
      title: '测试区间', dataIndex: 'test_range', key: 'test_range',
      render: (r: [string, string]) => `${r[0]} ~ ${r[1]}`
    },
    {
      title: '最优参数（样本内夏普）', dataIndex: 'best_params', key: 'best_params',
      render: (p: Record<string, number>, record: WFSegment) => (
        <span><code>{fmtParams(p)}</code>（IS夏普 {record.is_sharpe.toFixed(2)}）</span>
      )
    },
    {
      title: '样本外收益', dataIndex: 'oos_return', key: 'oos_return',
      render: (v: number) => (
        <span style={{ color: pctColor(v), fontWeight: 600 }}>
          {v >= 0 ? '+' : ''}{v.toFixed(2)}%
        </span>
      )
    },
    {
      title: 'OOS夏普', dataIndex: 'oos_sharpe', key: 'oos_sharpe',
      render: (v: number) => v.toFixed(2)
    },
    {
      title: 'OOS回撤', dataIndex: 'oos_max_drawdown', key: 'oos_max_drawdown',
      render: (v: number) => <span style={{ color: colors.loss }}>{v.toFixed(2)}%</span>
    },
    {
      title: '跑赢持有', dataIndex: 'beats_buy_hold', key: 'beats_buy_hold',
      render: (ok: boolean, record: WFSegment) => (
        <>
          <Tag color={ok ? 'green' : 'red'}>{ok ? '✓ 跑赢' : '✗ 跑输'}</Tag>
          <span style={{ fontSize: 12, color: colors.textSecondary }}>持有{record.oos_buy_hold_return >= 0 ? '+' : ''}{record.oos_buy_hold_return.toFixed(2)}%</span>
        </>
      )
    }
  ];

  return (
    <Card title={`🔬 Walk-Forward验证${wfResult ? ` · ${wfResult.stock_code} · ${periodLabel(wfResult.period)} · 资金$${(wfResult.initial_capital ?? 0).toLocaleString()}` : ''}`} style={{ marginTop: 16 }}>
      <Space wrap style={{ marginBottom: 16 }}>
        <Select value={wfStrategy} onChange={setWfStrategy} style={{ width: 200 }} optionLabelProp="title">
          {strategies.map(s => (
            <Select.Option key={s.id} value={s.id} title={s.name}>
              {s.name} - {s.description}
            </Select.Option>
          ))}
        </Select>
        <Select
          value={wfPeriod}
          onChange={setWfPeriod}
          style={{ width: 96 }}
          options={[
            { value: '1y', label: '近1年' },
            { value: '3y', label: '近3年' },
            { value: '5y', label: '近5年' },
          ]}
        />
        <span>段数：</span>
        <InputNumber
          min={1}
          max={4}
          value={wfSegments}
          onChange={(v) => setWfSegments(Number(v) || 2)}
        />
        <Button
          type="primary"
          ghost
          icon={<PlayCircleOutlined />}
          loading={wfLoading}
          disabled={!stockCode}
          onClick={handleRunWalkForward}
        >
          开始验证
        </Button>
      </Space>

      {wfResult && (
        <>
          <Descriptions size="small" column={3} style={{ marginBottom: 12 }}>
            <Descriptions.Item label="平均样本外收益">
              <span style={{ color: pctColor(wfResult.summary.avg_oos_return), fontWeight: 600 }}>
                {wfResult.summary.avg_oos_return >= 0 ? '+' : ''}{wfResult.summary.avg_oos_return.toFixed(2)}%
              </span>
            </Descriptions.Item>
            <Descriptions.Item label="平均OOS夏普">
              {wfResult.summary.avg_oos_sharpe.toFixed(2)}
            </Descriptions.Item>
            <Descriptions.Item label="跑赢段数">
              {wfResult.summary.win_segments}/{wfResult.summary.total_segments}
            </Descriptions.Item>
          </Descriptions>
          <ReactEChartsCore echarts={echarts} option={getWalkForwardChartOption()} style={{ height: 320 }} />
          <Table
            style={{ marginTop: 16, whiteSpace: 'nowrap' }}
            columns={wfColumns}
            dataSource={wfResult.segments}
            rowKey="step"
            pagination={false}
            size="small"
            scroll={{ x: 'max-content' }}
          />
        </>
      )}
    </Card>
  );
};
