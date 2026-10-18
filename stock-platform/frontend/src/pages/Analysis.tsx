import React, { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { AnalysisResult, HistoryItem, KlineBar, SearchResult } from '../types/api';
import { calcPositionSize, currencySymbol, fmtDist, fmtPx } from '../utils/format';
import { Card, Input, Select, Button, Row, Col, Tag, message, Descriptions, Progress, Space, Divider, Tabs, Checkbox, Alert, Table, InputNumber } from 'antd';
import { SearchOutlined, StarOutlined, StarFilled, LineChartOutlined } from '@ant-design/icons';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import echarts from '../services/echarts';
import { stockApi, analysisApi, portfolioApi } from '../services/api';
import { patternApi, SignalsResult } from '../services/patternApi';
import { trackingApi, TrackingResult } from '../services/trackingApi';
import { colors } from '../theme/tokens';

const { Search } = Input;
const { Option } = Select;

const Analysis: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [selectedStock, setSelectedStock] = useState<SearchResult | null>(null);
  const [analysisResult, setAnalysisResult] = useState<AnalysisResult | null>(null);
  const [historyData, setHistoryData] = useState<KlineBar[]>([]);
  const [signals, setSignals] = useState<SignalsResult | null>(null);
  const [tracking, setTracking] = useState<TrackingResult | null>(null);
  const [isInWatchlist, setIsInWatchlist] = useState(false);
  const [showMA, setShowMA] = useState(true);
  const [showBOLL, setShowBOLL] = useState(false);
  const [showStrategyLines, setShowStrategyLines] = useState(true);
  const [chartPeriod, setChartPeriod] = useState<string>('3mo');
  const [subChartMode, setSubChartMode] = useState<'volume' | 'macd' | 'rsi'>('volume');
  const [riskCapital, setRiskCapital] = useState<number>(100000);
  const [searchMarket, setSearchMarket] = useState('all');
  const [analysisMode, setAnalysisMode] = useState<'simple' | 'research'>('simple');
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [newsExpanded, setNewsExpanded] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const [recentAnalyses, setRecentAnalyses] = useState<HistoryItem[]>([]);

  // 切股请求序号：只有最新一次选择的结果允许写入state，防止慢的旧响应覆盖新股票数据
  const selectSeqRef = useRef(0);
  const selectedStockRef = useRef<SearchResult | null>(null); // 在途响应返回时比对当前选中股票用

  const handleSearch = async (value: string) => {
    if (!value.trim()) return;

    setLoading(true);
    try {
      const response = await stockApi.search(value, searchMarket);
      setSearchResults(response.data.results);
    } catch {
      message.error('搜索失败');
    } finally {
      setLoading(false);
    }
  };

  // 拉取K线/信号/追踪三路数据；只有最新一次调用允许写入state。
  // 供切股、周期切换与"重新加载"复用——重试只重拉数据，不清已算好的分析结果
  const fetchChartData = async (stock: SearchResult, periodOverride?: string) => {
    const seq = ++selectSeqRef.current;
    const pd = periodOverride || chartPeriod;
    setHistoryLoading(true);
    setHistoryError(null);

    // 历史K线、技术信号、评分追踪并行拉取
    const [historyRes, signalsRes, trackingRes] = await Promise.allSettled([
      stockApi.getHistory(stock.code, stock.market, pd),
      patternApi.getSignals(stock.code, stock.market, pd),
      trackingApi.getTracking(stock.code),
    ]);
    if (seq !== selectSeqRef.current) return; // 期间已切到其他股票，丢弃过期结果
    setHistoryLoading(false);
    if (historyRes.status === 'fulfilled') {
      setHistoryData(historyRes.value.data.data);
    } else {
      console.error('获取历史数据失败');
      setHistoryError(`${stock.code} K线数据加载失败，请重试`);
    }
    if (signalsRes.status === 'fulfilled') {
      setSignals(signalsRes.value.data);
    }
    // 评分追踪：暂无记录(400)时静默忽略
    if (trackingRes.status === 'fulfilled') {
      setTracking(trackingRes.value.data);
    }
  };

  const handlePeriodChange = (newPeriod: string) => {
    setChartPeriod(newPeriod);
    if (selectedStock) {
      fetchChartData(selectedStock, newPeriod);
    }
  };

  const handleSelectStock = (stock: SearchResult) => {
    setSelectedStock(stock);
    selectedStockRef.current = stock;
    setAnalysisResult(null);
    setHistoryData([]);
    setSignals(null);
    setTracking(null);
    setIsInWatchlist(false); // 切换股票时重置，避免残留上一只的"已关注"
    // 选中状态写入URL：刷新/分享不丢（replace避免历史堆积）
    setSearchParams({ code: stock.code, name: stock.name, market: stock.market }, { replace: true });
    fetchChartData(stock);
  };

  // 从URL恢复选中（首页自选股行点击跳转入口）；拉取最近分析快捷列表与已设置的总资金（失败静默）
  useEffect(() => {
    const code = searchParams.get('code');
    if (code) {
      handleSelectStock({
        code,
        name: searchParams.get('name') || code,
        market: searchParams.get('market') || 'US',
      });
    }
    analysisApi.getHistory(undefined, 8)
      .then((res) => setRecentAnalyses(res.data || []))
      .catch(() => {});
    portfolioApi.getTotalCapital()
      .then((res) => {
        if (res.data?.total_capital && res.data.total_capital > 0) {
          setRiskCapital(res.data.total_capital);
        }
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const renderRecentAnalyses = () => {
    if (recentAnalyses.length === 0) {
      return <div style={{ textAlign: 'center', padding: 16, color: colors.textSecondary }}>暂无分析记录</div>;
    }
    return recentAnalyses.map((a) => (
      <div
        key={a.id}
        onClick={() => handleSelectStock({ code: a.stock_code, name: a.stock_name, market: a.market || 'US' })}
        style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          padding: '8px 4px', borderBottom: `1px solid ${colors.border}`, cursor: 'pointer'
        }}
      >
        <span><b>{a.stock_code}</b> {a.stock_name}</span>
        <span style={{ fontSize: 12, color: colors.textSecondary }}>
          {a.total_score != null ? `综合 ${a.total_score} · ` : ''}{(a.created_at || '').slice(0, 10)}
        </span>
      </div>
    ));
  };

  const handleAnalyze = async () => {
    if (!selectedStock) {
      message.warning('请先选择股票');
      return;
    }

    setLoading(true);
    const seq = selectSeqRef.current;
    try {
      const response = await analysisApi.analyze({
        stock_code: selectedStock.code,
        stock_name: selectedStock.name,
        mode: analysisMode
      });
      if (seq !== selectSeqRef.current) return; // 等待期间已切股，丢弃过期分析结果
      setAnalysisResult(response.data);
      setNewsExpanded(false); // 新结果出来时收起新闻列表，避免上一只股票的展开状态带过来
      message.success('分析完成');
      // 分析已自动落库，刷新评分追踪（失败静默忽略；在途期间已切股则丢弃）
      const seqAtRefresh = seq;
      if (selectedStock) {
        trackingApi.getTracking(selectedStock.code)
          .then((res) => {
            if (seqAtRefresh === selectSeqRef.current) setTracking(res.data);
          })
          .catch(() => {});
      }
    } catch {
      if (seq !== selectSeqRef.current) return; // 已取消/已切股，丢弃过期错误提示
      message.error('分析失败');
    } finally {
      setLoading(false);
    }
  };

  // 取消等待：作废在途结果（seq失效后响应被丢弃），立即还控制权；后端计算照常结束
  const handleCancelAnalyze = () => {
    selectSeqRef.current += 1;
    setLoading(false);
    message.info('已取消等待，本次结果将丢弃');
  };

  const handleAddToWatchlist = async () => {
    if (!selectedStock) return;
    const targetCode = selectedStock.code;

    try {
      await stockApi.addToWatchlist({
        stock_code: targetCode,
        stock_name: selectedStock.name,
        market: selectedStock.market
      });
      // 请求在途期间可能已切到别的股票，别把新股票错标成"已关注"
      if (selectedStockRef.current?.code === targetCode) {
        setIsInWatchlist(true);
        message.success('已添加到自选股');
      } else {
        message.info(`已将 ${targetCode} 加入自选股`);
      }
    } catch {
      message.error('添加失败');
    }
  };

  const getCandlestickOption = () => {
    if (historyData.length === 0) return {};

    const dates = historyData.map((d: KlineBar) => d.date);
    const ohlc = historyData.map((d: KlineBar) => [d.open, d.close, d.low, d.high]);
    const volumes = historyData.map((d: KlineBar) => d.volume);

    // 支撑/阻力位 markLine数据（绿=支撑，红=阻力）
    const srMarkData: any[] = [];
    if (signals?.support_resistance) {
      signals.support_resistance.supports.forEach((s) => {
        srMarkData.push({
          yAxis: s.price,
          lineStyle: { color: colors.klineDown, type: 'dashed', width: 1 },
          label: { formatter: `支 ${s.price}`, position: 'insideEndTop', fontSize: 10, color: colors.klineDown }
        });
      });
      signals.support_resistance.resistances.forEach((r) => {
        srMarkData.push({
          yAxis: r.price,
          lineStyle: { color: colors.klineUp, type: 'dashed', width: 1 },
          label: { formatter: `阻 ${r.price}`, position: 'insideEndBottom', fontSize: 10, color: colors.klineUp }
        });
      });
    }

    // 叠加当前策略挂单线（线性买点 / 非线性买点 / 止盈位 / 止损位）
    if (showStrategyLines && analysisResult?.price_levels) {
      const pl = analysisResult.price_levels;
      if (pl.linear?.buy) {
        srMarkData.push({
          yAxis: pl.linear.buy,
          lineStyle: { color: colors.primary, type: 'solid', width: 1.5 },
          label: { formatter: `线性买 ${pl.linear.buy}`, position: 'insideStartTop', fontSize: 11, color: colors.primary }
        });
      }
      if (pl.nonlinear?.buy && pl.nonlinear.buy !== pl.linear?.buy) {
        srMarkData.push({
          yAxis: pl.nonlinear.buy,
          lineStyle: { color: colors.chartPurple, type: 'solid', width: 1.5 },
          label: { formatter: `非线性买 ${pl.nonlinear.buy}`, position: 'insideStartBottom', fontSize: 11, color: colors.chartPurple }
        });
      }
      if (pl.linear?.profit) {
        srMarkData.push({
          yAxis: pl.linear.profit,
          lineStyle: { color: colors.profit, type: 'dotted', width: 1.2 },
          label: { formatter: `止盈 ${pl.linear.profit}`, position: 'insideStartTop', fontSize: 10, color: colors.profit }
        });
      }
      if (pl.linear?.stop) {
        srMarkData.push({
          yAxis: pl.linear.stop,
          lineStyle: { color: colors.loss, type: 'dotted', width: 1.2 },
          label: { formatter: `止损 ${pl.linear.stop}`, position: 'insideStartBottom', fontSize: 10, color: colors.loss }
        });
      }
    }

    // 形态标记scatter（bullish在K线下方绿▲，bearish上方红▼，neutral上方灰菱形）
    const patternPoints: any[] = [];
    if (signals?.patterns?.length) {
      const dateIndex = new Map(dates.map((d: string, idx: number) => [d, idx]));
      const highs = historyData.map((d: KlineBar) => d.high).filter((v): v is number => v != null);
      const lows = historyData.map((d: KlineBar) => d.low).filter((v): v is number => v != null);
      const priceRange = Math.max(...highs) - Math.min(...lows);
      const offset = priceRange > 0 ? priceRange * 0.03 : (historyData[0]?.close ?? 1) * 0.01;
      signals.patterns.forEach((p) => {
        const idx = dateIndex.get(p.date);
        if (idx === undefined) return;
        const d = historyData[idx];
        const low = d.low ?? d.close ?? 0;
        const high = d.high ?? d.close ?? 0;
        if (p.direction === 'bullish') {
          patternPoints.push({ value: [p.date, low - offset, p.pattern], symbolRotate: 0, itemStyle: { color: colors.klineDown } });
        } else if (p.direction === 'bearish') {
          patternPoints.push({ value: [p.date, high + offset, p.pattern], symbolRotate: 180, itemStyle: { color: colors.klineUp } });
        } else {
          patternPoints.push({ value: [p.date, high + offset, p.pattern], itemStyle: { color: '#999' } });
        }
      });
    }

    // 指标线序列（null值由ECharts断开）
    const series: any[] = [
      {
        name: 'K线',
        type: 'candlestick',
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: ohlc,
        itemStyle: { color: colors.klineUp, color0: colors.klineDown, borderColor: colors.klineUp, borderColor0: colors.klineDown },
        ...(srMarkData.length > 0 ? { markLine: { symbol: 'none', silent: true, data: srMarkData } } : {})
      }
    ];

    if (showMA) {
      series.push(
        // 系列级 color 必须与 lineStyle.color 一致，否则图例回落默认调色板（图例色≠线色）
        { name: 'MA5', type: 'line', xAxisIndex: 0, yAxisIndex: 0, color: colors.ma5, data: historyData.map((d: KlineBar) => d.ma5), symbol: 'none', lineStyle: { width: 1, color: colors.ma5 } },
        { name: 'MA20', type: 'line', xAxisIndex: 0, yAxisIndex: 0, color: colors.primary, data: historyData.map((d: KlineBar) => d.ma20), symbol: 'none', lineStyle: { width: 1.5, color: colors.primary } },
        { name: 'MA50', type: 'line', xAxisIndex: 0, yAxisIndex: 0, color: colors.chartPurple, data: historyData.map((d: KlineBar) => d.ma50), symbol: 'none', lineStyle: { width: 1.5, color: colors.chartPurple } },
      );
    }

    if (showBOLL) {
      series.push(
        { name: 'BOLL上轨', type: 'line', xAxisIndex: 0, yAxisIndex: 0, color: '#999', data: historyData.map((d: KlineBar) => d.bb_upper), symbol: 'none', lineStyle: { width: 1, type: 'dashed', color: '#999' } },
        { name: 'BOLL中轨', type: 'line', xAxisIndex: 0, yAxisIndex: 0, color: '#bbb', data: historyData.map((d: KlineBar) => d.bb_mid), symbol: 'none', lineStyle: { width: 1, type: 'dotted', color: '#bbb' } },
        { name: 'BOLL下轨', type: 'line', xAxisIndex: 0, yAxisIndex: 0, color: '#999', data: historyData.map((d: KlineBar) => d.bb_lower), symbol: 'none', lineStyle: { width: 1, type: 'dashed', color: '#999' } },
      );
    }

    if (patternPoints.length > 0) {
      series.push({
        name: '形态信号',
        type: 'scatter',
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: patternPoints,
        symbol: 'triangle',
        symbolSize: 11,
        z: 10
      });
    }

    const legendData = ['K线'];
    if (showMA) legendData.push('MA5', 'MA20', 'MA50');
    if (showBOLL) legendData.push('BOLL上轨', 'BOLL中轨', 'BOLL下轨');
    if (patternPoints.length > 0) legendData.push('形态信号');

    // 副图指标三选一：成交量 / MACD / RSI(14)
    if (subChartMode === 'macd') {
      series.push(
        {
          name: 'MACD柱',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: historyData.map((d: KlineBar) => ({
            value: d.macd_hist ?? 0,
            itemStyle: { color: (d.macd_hist ?? 0) >= 0 ? colors.klineUp : colors.klineDown }
          })),
        },
        {
          name: 'DIF',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 1,
          color: colors.primary,
          data: historyData.map((d: KlineBar) => d.macd),
          symbol: 'none',
          lineStyle: { width: 1.3, color: colors.primary }
        },
        {
          name: 'DEA',
          type: 'line',
          xAxisIndex: 1,
          yAxisIndex: 1,
          color: colors.ma5,
          data: historyData.map((d: KlineBar) => d.macd_signal),
          symbol: 'none',
          lineStyle: { width: 1.3, color: colors.ma5 }
        }
      );
      legendData.push('MACD柱', 'DIF', 'DEA');
    } else if (subChartMode === 'rsi') {
      series.push({
        name: 'RSI(14)',
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 1,
        color: colors.chartPurple,
        data: historyData.map((d: KlineBar) => d.rsi),
        symbol: 'none',
        lineStyle: { width: 1.5, color: colors.chartPurple },
        markLine: {
          symbol: 'none',
          silent: true,
          data: [
            { yAxis: 70, lineStyle: { color: colors.loss, type: 'dashed', width: 1 }, label: { formatter: '超买 70', fontSize: 10 } },
            { yAxis: 30, lineStyle: { color: colors.profit, type: 'dashed', width: 1 }, label: { formatter: '超卖 30', fontSize: 10 } },
          ]
        }
      });
      legendData.push('RSI(14)');
    } else {
      series.push({
        name: '成交量',
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumes,
        itemStyle: { color: colors.indicatorBlue, opacity: 0.7 }
      });
      legendData.push('成交量');
    }

    return {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: (params: any) => {
          const arr = Array.isArray(params) ? params : [params];
          if (arr.length === 0) return '';
          let html = arr[0].axisValueLabel || arr[0].name || '';
          for (const p of arr) {
            if (p.seriesType === 'scatter') {
              html += `<br/>${p.marker}${p.value[2]}`;
            } else if (p.seriesName === 'K线' && Array.isArray(p.value)) {
              html += `<br/>${p.marker}开:${p.value[0]} 收:${p.value[1]} 低:${p.value[2]} 高:${p.value[3]}`;
            } else if (p.value !== undefined && p.value !== null && !Array.isArray(p.value)) {
              const rawVal = typeof p.value === 'object' && p.value !== null ? p.value.value : p.value;
              html += `<br/>${p.marker}${p.seriesName}: ${rawVal}`;
            }
          }
          return html;
        }
      },
      legend: {
        data: legendData,
        top: 0
      },
      grid: [
        { left: '10%', right: '8%', top: '12%', height: '48%' },
        { left: '10%', right: '8%', top: '68%', height: '16%' }
      ],
      xAxis: [
        { type: 'category', data: dates, gridIndex: 0 },
        { type: 'category', data: dates, gridIndex: 1 }
      ],
      yAxis: [
        { scale: true, gridIndex: 0 },
        subChartMode === 'rsi'
          ? { min: 0, max: 100, gridIndex: 1, splitNumber: 2 }
          : { scale: true, gridIndex: 1, splitNumber: 2 }
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 40, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1], bottom: 0, height: 18, start: 40, end: 100 }
      ],
      series
    };
  };

  const getScoreColor = (score: number) => {
    if (score >= 8) return colors.success;
    if (score >= 6) return colors.primary;
    if (score >= 4) return colors.warning;
    return colors.error;
  };

  const getRecommendationColor = (level: string) => {
    if (level === '强烈推荐') return 'red';
    if (level === '推荐') return 'orange';
    if (level === '中性') return 'blue';
    if (level === '谨慎') return 'default';
    return 'volcano';
  };

  // 技术信号卡片：背离Alert + 近期形态 + 支撑阻力
  const renderSignalsCard = () => {
    if (!signals) return null;
    const dirMeta: Record<string, { color: string; label: string }> = {
      bullish: { color: 'green', label: '看涨' },
      bearish: { color: 'red', label: '看跌' },
      neutral: { color: 'default', label: '中性' }
    };
    const recentPatterns = signals.patterns.slice(-10).reverse();
    return (
      <Card title="🎯 技术信号" style={{ marginBottom: 16 }}>
        {signals.divergence.top_divergence && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message="⚠️ 检测到顶背离，注意回调风险"
            description={signals.divergence.detail}
          />
        )}
        {signals.divergence.bottom_divergence && (
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 12 }}
            message="📈 底背离，关注反弹机会"
            description={signals.divergence.detail}
          />
        )}

        <div style={{ fontWeight: 'bold', marginBottom: 6 }}>支撑位 / 阻力位</div>
        <Space wrap style={{ marginBottom: 8 }}>
          {signals.support_resistance.supports.map((s) => (
            <Tag key={`s-${s.price}`} color="green">支撑 {s.price} · {s.touches}次</Tag>
          ))}
          {signals.support_resistance.resistances.map((r) => (
            <Tag key={`r-${r.price}`} color="red">阻力 {r.price} · {r.touches}次</Tag>
          ))}
        </Space>

        <Divider style={{ margin: '8px 0' }} />
        <div style={{ fontWeight: 'bold', marginBottom: 6 }}>近期K线形态</div>
        {recentPatterns.length === 0 ? (
          <div style={{ color: colors.textSecondary, fontSize: 12 }}>近期未识别到明显形态</div>
        ) : (
          recentPatterns.map((p, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '3px 0', borderBottom: `1px solid ${colors.bgLight}` }}>
              <span style={{ color: colors.textSecondary, fontSize: 12 }}>{p.date}</span>
              <span style={{ fontSize: 13 }}>{p.pattern}</span>
              <Tag color={dirMeta[p.direction]?.color ?? 'default'} style={{ marginRight: 0 }}>
                {dirMeta[p.direction]?.label ?? p.direction}
              </Tag>
            </div>
          ))
        )}
      </Card>
    );
  };

  // 评分追踪散点图：x=评分，y=至今收益率(%)，y=0参考线
  const getTrackingOption = () => {
    if (!tracking || tracking.records.length === 0) return {};
    const data = tracking.records.map((r) => [r.total_score, r.forward_return_pct, r.date]);
    return {
      tooltip: {
        formatter: (p: any) => {
          const v = Array.isArray(p.value) ? p.value : [];
          const ret = typeof v[1] === 'number' ? v[1] : 0;
          return `${v[2]}<br/>评分 ${v[0]} · 至今 ${ret > 0 ? '+' : ''}${ret}%`;
        }
      },
      grid: { left: 55, right: 20, top: 30, bottom: 35 },
      xAxis: { name: '评分', nameLocation: 'middle', nameGap: 25, type: 'value', min: 0, max: 10 },
      yAxis: { name: '后续收益(%)', type: 'value', scale: true },
      series: [
        {
          name: '历史评分',
          type: 'scatter',
          symbolSize: 12,
          data,
          itemStyle: { color: colors.primary, opacity: 0.75 },
          markLine: {
            silent: true,
            symbol: 'none',
            data: [{ yAxis: 0 }],
            lineStyle: { color: '#999', type: 'dashed', width: 1 },
            label: { show: false }
          }
        }
      ]
    };
  };

  // 评分追踪卡片：验证历史评分的预测力（仅有记录时显示）
  const renderTrackingCard = () => {
    if (!tracking) return null;
    const corr = tracking.correlation;
    const corrColor = corr === null || corr === undefined ? 'default' : corr > 0 ? 'green' : corr < 0 ? 'red' : 'default';
    return (
      <Card title="📊 评分追踪" style={{ marginBottom: 16 }}>
        <Space wrap style={{ marginBottom: 8 }}>
          <Tag style={{ fontSize: 13, padding: '2px 10px' }}>累计评分 {tracking.count} 次</Tag>
          <Tag color={corrColor} style={{ fontSize: 13, padding: '2px 10px' }}>
            相关系数 {corr === null || corr === undefined ? '—' : corr} · {tracking.interpretation}
          </Tag>
        </Space>

        <ReactEChartsCore echarts={echarts} option={getTrackingOption()} style={{ height: 220 }} notMerge />

        <Divider style={{ margin: '8px 0' }} />
        <Table
          size="small"
          pagination={false}
          rowKey="bucket"
          dataSource={tracking.buckets}
          columns={[
            { title: '评分区间', dataIndex: 'bucket' },
            { title: '次数', dataIndex: 'count', width: 60 },
            {
              title: '平均后续收益',
              dataIndex: 'avg_return',
              render: (v: number | null) =>
                v === null || v === undefined ? (
                  <span style={{ color: colors.textSecondary }}>-</span>
                ) : (
                  <span style={{ color: v > 0 ? colors.success : v < 0 ? colors.error : colors.textSecondary, fontWeight: 'bold' }}>
                    {v > 0 ? '+' : ''}{v}%
                  </span>
                )
            }
          ]}
        />
      </Card>
    );
  };

  return (
    <div>
      <h2>🔍 股票分析</h2>

      <Card style={{ marginBottom: 16 }}>
        <Row gutter={16}>
          <Col xs={24} lg={16}>
            <Search
              placeholder="输入股票代码或名称搜索"
              enterButton={<><SearchOutlined /> 搜索</>}
              size="large"
              onSearch={handleSearch}
              loading={loading}
            />
          </Col>
          <Col xs={24} lg={8}>
            <Select
              placeholder="选择市场"
              style={{ width: '100%' }}
              value={searchMarket}
              onChange={(v) => setSearchMarket(v)}
              size="large"
            >
              <Option value="all">全部市场</Option>
              <Option value="A">A股</Option>
              <Option value="US">美股</Option>
            </Select>
          </Col>
        </Row>

        {searchResults.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <h4>搜索结果：</h4>
            <Space wrap>
              {searchResults.map((stock) => (
                <Tag
                  key={stock.code}
                  color={selectedStock?.code === stock.code ? 'blue' : 'default'}
                  style={{ cursor: 'pointer', padding: '4px 8px' }}
                  onClick={() => handleSelectStock(stock)}
                >
                  {stock.code} {stock.name}
                  <Tag color={stock.market === 'A' ? 'red' : 'blue'} style={{ marginLeft: 4 }}>
                    {stock.market === 'A' ? 'A股' : '美股'}
                  </Tag>
                </Tag>
              ))}
            </Space>
          </div>
        )}
      </Card>

      {/* 未选股时：最近分析作为驾驶舱入口全宽展示（此前整个内容区被 selectedStock 门控，空页只有搜索框） */}
      {!selectedStock && (
        <Row gutter={16}>
          <Col xs={24}>
            <Card title="最近分析">
              <div style={{ marginBottom: 8, color: colors.textSecondary, fontSize: 13 }}>
                搜索上方股票开始分析，或点击历史记录快速进入
              </div>
              {renderRecentAnalyses()}
            </Card>
          </Col>
        </Row>
      )}

      {selectedStock && (
        <Row gutter={16}>
          <Col xs={24} lg={16}>
            <Card
              title={
                <span>
                  📈 {selectedStock.code} {selectedStock.name}
                  <Tag color={selectedStock.market === 'A' ? 'red' : 'blue'} style={{ marginLeft: 8 }}>
                    {selectedStock.market === 'A' ? 'A股' : '美股'}
                  </Tag>
                </span>
              }
              extra={
                <Space wrap>
                  <Select
                    value={analysisMode}
                    onChange={(v: 'simple' | 'research') => setAnalysisMode(v)}
                    style={{ width: 185 }}
                    options={[
                      { value: 'simple', label: '标准模式 (40/30/15/15)' },
                      { value: 'research', label: '深度投研 (35/15/25/25)' },
                    ]}
                  />
                  <Button
                    icon={isInWatchlist ? <StarFilled /> : <StarOutlined />}
                    onClick={handleAddToWatchlist}
                    disabled={isInWatchlist}
                  >
                    {isInWatchlist ? '已关注' : '加入自选'}
                  </Button>
                  <Button
                    type="primary"
                    icon={<LineChartOutlined />}
                    onClick={handleAnalyze}
                    loading={loading}
                  >
                    开始分析
                  </Button>
                </Space>
              }
              style={{ marginBottom: 16 }}
            >
              {loading && (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message="分析进行中：拉取行情、四维数据源并综合评分，通常需要 20-40 秒"
                  action={<Button size="small" onClick={handleCancelAnalyze}>取消等待</Button>}
                />
              )}
              {historyData.length > 0 ? (
                <>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8, marginBottom: 8 }}>
                    <div>
                      <Checkbox checked={showMA} onChange={(e) => setShowMA(e.target.checked)}>均线 MA5/20/50</Checkbox>
                      <Checkbox checked={showBOLL} onChange={(e) => setShowBOLL(e.target.checked)} style={{ marginLeft: 12 }}>
                        布林带 BOLL
                      </Checkbox>
                      {analysisResult && (
                        <Checkbox checked={showStrategyLines} onChange={(e) => setShowStrategyLines(e.target.checked)} style={{ marginLeft: 12 }}>
                          策略买点/止盈止损标线
                        </Checkbox>
                      )}
                    </div>
                    <Space size="small" wrap>
                      <Select
                        size="small"
                        value={chartPeriod}
                        onChange={handlePeriodChange}
                        style={{ width: 92 }}
                        options={[
                          { value: '1mo', label: '近1个月' },
                          { value: '3mo', label: '近3个月' },
                          { value: '6mo', label: '近6个月' },
                          { value: '1y', label: '近1年' },
                          { value: '2y', label: '近2年' },
                        ]}
                      />
                      <Select
                        size="small"
                        value={subChartMode}
                        onChange={(v: 'volume' | 'macd' | 'rsi') => setSubChartMode(v)}
                        style={{ width: 108 }}
                        options={[
                          { value: 'volume', label: '副图: 成交量' },
                          { value: 'macd', label: '副图: MACD' },
                          { value: 'rsi', label: '副图: RSI(14)' },
                        ]}
                      />
                    </Space>
                  </div>
                  <ReactEChartsCore echarts={echarts} option={getCandlestickOption()} style={{ height: 440 }} notMerge />
                </>
              ) : historyError ? (
                <div style={{ textAlign: 'center', padding: 50 }}>
                  <div style={{ color: colors.loss, marginBottom: 12 }}>{historyError}</div>
                  <Button type="primary" onClick={() => fetchChartData(selectedStock)}>重新加载</Button>
                </div>
              ) : (
                <div style={{ textAlign: 'center', padding: 50, color: colors.textSecondary }}>
                  {historyLoading ? '加载中...' : '暂无数据'}
                </div>
              )}
            </Card>
          </Col>

          <Col xs={24} lg={8}>
            {analysisResult ? (
              <>
                {selectedStock.market !== 'A' && analysisResult.earnings_radar?.tag && (
                  <Alert
                    style={{ marginBottom: 16, borderRadius: 8 }}
                    type={analysisResult.earnings_radar.is_imminent ? 'error' : 'warning'}
                    showIcon
                    message={
                      <span style={{ fontWeight: 'bold' }}>
                        {analysisResult.earnings_radar.is_imminent ? '🚨 财报重大排雷警报' : '📅 财报日程提示'}
                      </span>
                    }
                    description={
                      <div style={{ marginTop: 4 }}>
                        <div style={{ fontWeight: 600 }}>{analysisResult.earnings_radar.tag}</div>
                        {analysisResult.earnings_radar.is_imminent && (
                          <div style={{ marginTop: 4, fontSize: 12, color: colors.textSecondary }}>
                            💡 <b>量化风控建议</b>：财报前夕期权隐含波动率（IV）极高，股价易出现双向剧烈跳空，开盲盒博弈期望值为负。建议严禁左侧重仓，待业绩靴子落地后再做右侧交易！
                          </div>
                        )}
                      </div>
                    }
                  />
                )}
                <Card title="📊 综合评分" style={{ marginBottom: 16 }}>
                  <div style={{ textAlign: 'center', marginBottom: 16 }}>
                    <Progress
                      type="dashboard"
                      percent={analysisResult.scores.total * 10}
                      strokeColor={getScoreColor(analysisResult.scores.total)}
                      format={() => (
                        <span style={{ fontSize: 24, fontWeight: 'bold' }}>
                          {analysisResult.scores.total}
                        </span>
                      )}
                    />
                    <div style={{ marginTop: 8 }}>
                      <Tag color={getRecommendationColor(analysisResult.recommendation.level)} style={{ fontSize: 16, padding: '4px 12px' }}>
                        {analysisResult.recommendation.level}
                      </Tag>
                    </div>
                  </div>

                  <Descriptions column={1} size="small">
                    <Descriptions.Item label="技术面">
                      <Progress percent={analysisResult.scores.technical * 10} size="small" strokeColor={getScoreColor(analysisResult.scores.technical)} />
                    </Descriptions.Item>
                    <Descriptions.Item label="消息面">
                      <Progress percent={analysisResult.scores.news * 10} size="small" strokeColor={getScoreColor(analysisResult.scores.news)} />
                    </Descriptions.Item>
                    <Descriptions.Item label="宏观面">
                      <Progress percent={analysisResult.scores.macro * 10} size="small" strokeColor={getScoreColor(analysisResult.scores.macro)} />
                    </Descriptions.Item>
                    <Descriptions.Item label="事件驱动">
                      <Progress percent={analysisResult.scores.event * 10} size="small" strokeColor={getScoreColor(analysisResult.scores.event)} />
                    </Descriptions.Item>
                  </Descriptions>
                </Card>

                <Card title="💰 三策略点位" style={{ marginBottom: 16 }}>
                  <div style={{ textAlign: 'center', marginBottom: 16 }}>
                    <span style={{ fontSize: 20, fontWeight: 'bold' }}>
                      当前价: {fmtPx(analysisResult.price_levels.current_price, analysisResult.market)}
                    </span>
                  </div>

                  <Tabs
                    defaultActiveKey="linear"
                    items={[
                      {
                        key: 'linear',
                        label: '📈 线性',
                        children: (
                          <Descriptions column={1} size="small">
                            <Descriptions.Item label="买入价">
                              <span style={{ color: colors.success, fontWeight: 'bold' }}>
                                {fmtPx(analysisResult.price_levels.linear.buy, analysisResult.market)}
                              </span>
                              <Tag color="green" style={{ marginLeft: 8 }}>
                                {fmtDist(analysisResult.price_levels.linear.distance)}
                              </Tag>
                            </Descriptions.Item>
                            <Descriptions.Item label="止盈位">
                              <span style={{ color: colors.primary }}>
                                {fmtPx(analysisResult.price_levels.linear.profit, analysisResult.market)}
                              </span>
                              <Tag color="blue" style={{ marginLeft: 8 }}>
                                +15%
                              </Tag>
                            </Descriptions.Item>
                            <Descriptions.Item label="止损位">
                              <span style={{ color: colors.error }}>
                                {fmtPx(analysisResult.price_levels.linear.stop, analysisResult.market)}
                              </span>
                              <Tag color="red" style={{ marginLeft: 8 }}>
                                -8%
                              </Tag>
                            </Descriptions.Item>
                            {analysisResult.price_levels.linear.atr_stop != null && (
                              <Descriptions.Item label="ATR自适应止损">
                                <span style={{ color: colors.warning, fontWeight: 600 }}>
                                  {fmtPx(analysisResult.price_levels.linear.atr_stop, analysisResult.market)}
                                </span>
                                <Tag color="orange" style={{ marginLeft: 8 }}>2×ATR动态</Tag>
                              </Descriptions.Item>
                            )}
                            {analysisResult.price_levels.linear.breakeven_trigger != null && (
                              <Descriptions.Item label="保本上移触发">
                                <span>{fmtPx(analysisResult.price_levels.linear.breakeven_trigger, analysisResult.market)}</span>
                                <Tag color="cyan" style={{ marginLeft: 8 }}>+10%上移保本</Tag>
                              </Descriptions.Item>
                            )}
                            {(() => {
                              const ps = calcPositionSize(
                                riskCapital,
                                analysisResult.price_levels.linear.buy,
                                analysisResult.price_levels.linear.atr_stop ?? analysisResult.price_levels.linear.stop
                              );
                              return ps.recommendedShares > 0 ? (
                                <Descriptions.Item label="2%风控建议仓位">
                                  <b>{ps.recommendedShares}股</b>
                                  <span style={{ fontSize: 12, color: colors.textSecondary, marginLeft: 6 }}>
                                    (首仓{ps.firstTrancheShares}股 · 占{ps.positionPct}%)
                                  </span>
                                </Descriptions.Item>
                              ) : null;
                            })()}
                          </Descriptions>
                        )
                      },
                      {
                        key: 'nonlinear',
                        label: '📊 非线性',
                        children: (
                          <Descriptions column={1} size="small">
                            <Descriptions.Item label="买入价">
                              <span style={{ color: colors.success, fontWeight: 'bold' }}>
                                {fmtPx(analysisResult.price_levels.nonlinear.buy, analysisResult.market)}
                              </span>
                              <Tag color="green" style={{ marginLeft: 8 }}>
                                {fmtDist(analysisResult.price_levels.nonlinear.distance)}
                              </Tag>
                            </Descriptions.Item>
                            <Descriptions.Item label="止盈位">
                              <span style={{ color: colors.primary }}>
                                {fmtPx(analysisResult.price_levels.nonlinear.profit, analysisResult.market)}
                              </span>
                              <Tag color="blue" style={{ marginLeft: 8 }}>
                                +46%
                              </Tag>
                            </Descriptions.Item>
                            <Descriptions.Item label="止损位">
                              <span style={{ color: colors.error }}>
                                {fmtPx(analysisResult.price_levels.nonlinear.stop, analysisResult.market)}
                              </span>
                              <Tag color="red" style={{ marginLeft: 8 }}>
                                -8%
                              </Tag>
                            </Descriptions.Item>
                            {analysisResult.price_levels.nonlinear.atr_stop != null && (
                              <Descriptions.Item label="ATR自适应止损">
                                <span style={{ color: colors.warning, fontWeight: 600 }}>
                                  {fmtPx(analysisResult.price_levels.nonlinear.atr_stop, analysisResult.market)}
                                </span>
                                <Tag color="orange" style={{ marginLeft: 8 }}>2.2×ATR动态</Tag>
                              </Descriptions.Item>
                            )}
                            {analysisResult.price_levels.nonlinear.breakeven_trigger != null && (
                              <Descriptions.Item label="保本上移触发">
                                <span>{fmtPx(analysisResult.price_levels.nonlinear.breakeven_trigger, analysisResult.market)}</span>
                                <Tag color="cyan" style={{ marginLeft: 8 }}>+10%上移保本</Tag>
                              </Descriptions.Item>
                            )}
                            {(() => {
                              const ps = calcPositionSize(
                                riskCapital,
                                analysisResult.price_levels.nonlinear.buy,
                                analysisResult.price_levels.nonlinear.atr_stop ?? analysisResult.price_levels.nonlinear.stop
                              );
                              return ps.recommendedShares > 0 ? (
                                <Descriptions.Item label="2%风控建议仓位">
                                  <b>{ps.recommendedShares}股</b>
                                  <span style={{ fontSize: 12, color: colors.textSecondary, marginLeft: 6 }}>
                                    (首仓{ps.firstTrancheShares}股 · 占{ps.positionPct}%)
                                  </span>
                                </Descriptions.Item>
                              ) : null;
                            })()}
                          </Descriptions>
                        )
                      },
                      {
                        key: 'macd',
                        label: '⚡ MACD',
                        children: (() => {
                          const m = analysisResult.price_levels.macd;
                          if (!m) return <div style={{ color: colors.textSecondary }}>暂无数据</div>;
                          const isGolden = m.state === 'golden';
                          return (
                            <div>
                              <div style={{ textAlign: 'center', marginBottom: 12 }}>
                                <Tag color={isGolden ? 'green' : m.state === 'death' ? 'red' : 'default'}
                                     style={{ fontSize: 15, padding: '4px 14px' }}>
                                  {isGolden ? '🟢 金叉' : m.state === 'death' ? '🔴 死叉' : '⚪ 未知'}
                                </Tag>
                                {m.state !== 'unknown' && (
                                  <span style={{ marginLeft: 8, color: '#666' }}>
                                    已持续 {m.days_in_state} 天
                                  </span>
                                )}
                              </div>
                              <Descriptions column={1} size="small">
                                {isGolden && m.add_price && (
                                  <Descriptions.Item label="加仓参考">
                                    <span style={{ color: colors.success, fontWeight: 'bold' }}>
                                      {currencySymbol(analysisResult.market)}{m.add_price}
                                    </span>
                                    <Tag color="green" style={{ marginLeft: 8 }}>回踩MA20</Tag>
                                  </Descriptions.Item>
                                )}
                                {!isGolden && m.watch_price && (
                                  <Descriptions.Item label="关注买点">
                                    <span style={{ color: colors.warning, fontWeight: 'bold' }}>
                                      {currencySymbol(analysisResult.market)}{m.watch_price}
                                    </span>
                                    <Tag color="orange" style={{ marginLeft: 8 }}>布林下轨</Tag>
                                  </Descriptions.Item>
                                )}
                                {m.state !== 'unknown' && (
                                  <Descriptions.Item label="离场信号">
                                    <span style={{ color: colors.primary }}>
                                      {isGolden ? 'MACD死叉（跟随趋势，不设固定止盈）' : 'MACD金叉'}
                                    </span>
                                  </Descriptions.Item>
                                )}
                                <Descriptions.Item label="纪律止损">
                                  <span style={{ color: colors.error }}>{currencySymbol(analysisResult.market)}{m.stop}</span>
                                  <Tag color="red" style={{ marginLeft: 8 }}>-8%</Tag>
                                </Descriptions.Item>
                              </Descriptions>
                              <div style={{ marginTop: 8, fontSize: 12, color: '#666', textAlign: 'center' }}>
                                {m.note}
                              </div>
                            </div>
                          );
                        })()
                      }
                    ]}
                  />

                  <Divider style={{ margin: '10px 0' }} />

                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 12, color: colors.textSecondary, marginBottom: 6 }}>
                    <span>🧮 仓位试算总资金 ({currencySymbol(analysisResult.market)})：</span>
                    <InputNumber<number>
                      size="small"
                      min={1000}
                      step={10000}
                      value={riskCapital}
                      onChange={(v) => setRiskCapital(v || 100000)}
                      style={{ width: 125 }}
                    />
                  </div>

                  <div style={{ textAlign: 'center', color: '#666', fontSize: 12 }}>
                    线性: 机会多 | 非线性: 胜率高 | MACD: 长期回测最优
                  </div>
                </Card>

                {renderSignalsCard()}

                {renderTrackingCard()}

                {analysisResult.details?.news?.news && analysisResult.details.news.news.length > 0 && (
                  <Card title={`📰 消息面 · ${analysisResult.details.news.sentiment}`} style={{ marginBottom: 16 }}>
                    <div style={{ marginBottom: 8, fontSize: 12, color: colors.textSecondary }}>
                      共{analysisResult.details.news.news_count}条新闻 | 利好{analysisResult.details.news.positive_count}条 / 利空{analysisResult.details.news.negative_count}条
                    </div>
                    {(newsExpanded
                      ? analysisResult.details.news.news
                      : analysisResult.details.news.news.slice(0, 8)
                    ).map((n: any, i: number) => (
                      <div key={i} style={{ marginBottom: 10, paddingBottom: 8, borderBottom: `1px solid ${colors.border}` }}>
                        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6 }}>
                          <Tag color={n.sentiment > 0 ? 'green' : n.sentiment < 0 ? 'red' : 'default'} style={{ flexShrink: 0 }}>
                            {n.sentiment > 0 ? '利好' : n.sentiment < 0 ? '利空' : '中性'}
                          </Tag>
                          {n.url ? (
                            <a
                              href={n.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              style={{ fontSize: 13, lineHeight: 1.5, color: 'inherit' }}
                              title="点击查看原文"
                            >
                              {n.title}
                            </a>
                          ) : (
                            <span style={{ fontSize: 13, lineHeight: 1.5 }}>{n.title}</span>
                          )}
                        </div>
                        <div style={{ fontSize: 11, color: colors.textSecondary, marginTop: 4, marginLeft: 44 }}>
                          {n.source} · {n.date}
                          {n.url && (
                            <a href={n.url} target="_blank" rel="noopener noreferrer" style={{ marginLeft: 6 }}>↗</a>
                          )}
                        </div>
                      </div>
                    ))}
                    {analysisResult.details.news.news.length > 8 && (
                      <div style={{ textAlign: 'center', marginTop: 4 }}>
                        <Button type="link" size="small" onClick={() => setNewsExpanded(!newsExpanded)}>
                          {newsExpanded
                            ? '收起'
                            : `▼ 显示更多 (还有${analysisResult.details.news.news.length - 8}条)`}
                        </Button>
                      </div>
                    )}
                  </Card>
                )}

                {((analysisResult.details?.macro?.interpretations?.length ?? 0) > 0 || (analysisResult.details?.event?.events?.length ?? 0) > 0) && (
                  <Card title="🌍 宏观 & 事件">
                    {analysisResult.details.macro?.indicators && Object.keys(analysisResult.details.macro.indicators).length > 0 && (
                      <div style={{ marginBottom: 8 }}>
                        {Object.entries({
                          fed_rate: '联邦利率',
                          cpi_yoy: 'CPI同比',
                          unemployment: '失业率'
                        }).map(([key, label]) => {
                          const val = analysisResult.details.macro.indicators?.[key];
                          return val != null ? (
                            <Tag key={key} style={{ marginBottom: 4 }}>{label}: {val}%</Tag>
                          ) : null;
                        })}
                      </div>
                    )}
                    {(analysisResult.details.macro?.interpretations || []).map((s: string, i: number) => (
                      <div key={i} style={{ fontSize: 13, color: '#555', marginBottom: 4 }}>• {s}</div>
                    ))}
                    <Divider style={{ margin: '8px 0' }} />
                    {(analysisResult.details.event?.events || []).map((e: any, i: number) => (
                      <div key={i} style={{ fontSize: 13, marginBottom: 6 }}>
                        <b>{e.name}</b>
                        {e.date ? ` (${e.date}${e.days_away !== null ? `, ${e.days_away > 0 ? `${e.days_away}天后` : '已发布'}` : ''})` : ''}
                        ：{e.impact}
                      </div>
                    ))}
                  </Card>
                )}
              </>
            ) : (
              <Card title="最近分析">
                {renderRecentAnalyses()}
              </Card>
            )}

            {!analysisResult && renderSignalsCard()}
            {!analysisResult && renderTrackingCard()}
          </Col>
        </Row>
      )}
    </div>
  );
};

export default Analysis;
