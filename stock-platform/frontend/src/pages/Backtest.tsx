import React, { useRef, useState, useEffect } from 'react';
import { Card, Input, Select, Button, Row, Col, Form, InputNumber, DatePicker, Table, Tag, Statistic, Space, message, Descriptions, Divider, Checkbox } from 'antd';
import { SearchOutlined, ExperimentOutlined, DownloadOutlined } from '@ant-design/icons';
import { ComparePanel, OptimizePanel, WalkForwardPanel } from '../components/BacktestPanels';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import echarts from '../services/echarts';
import { stockApi, backtestApi } from '../services/api';
import { colors } from '../theme/tokens';
import { downloadCsv } from '../utils/format';
import { formatBacktestTooltipLine } from '../utils/backtestFormat';
import type { BacktestResult, CompareResult, SearchResult, Strategy } from '../types/api';

const { Search } = Input;
const { Option } = Select;

const Backtest: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [selectedStock, setSelectedStock] = useState<SearchResult | null>(null);
  const [backtestResult, setBacktestResult] = useState<BacktestResult | null>(null);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareResult, setCompareResult] = useState<CompareResult | null>(null);
  // 🎯 参数寻优
  // 🔬 Walk-Forward验证

  // 切股请求序号：切换后丢弃在途的回测/对比/寻优响应，防止旧股票结果重新写入
  const selectSeqRef = useRef(0);

  // 切换股票时清空旧结果，避免不同股票的回测/对比/寻优结果混排一页
  const handleSelectStock = (stock: SearchResult) => {
    selectSeqRef.current += 1;
    setSelectedStock(stock);
    setBacktestResult(null);
    setCompareResult(null);
  };
  const [form] = Form.useForm();
  const dateRangeWatch = Form.useWatch('date_range', form);   // 自定义时间段优先于周期

  const fetchStrategies = async () => {
    try {
      const response = await backtestApi.getStrategies();
      setStrategies(response.data.strategies);
    } catch {
      console.error('获取策略列表失败');
    }
  };

  useEffect(() => {
    fetchStrategies();
  }, []);

  const handleSearch = async (value: string) => {
    if (!value.trim()) return;

    try {
      const response = await stockApi.search(value);
      setSearchResults(response.data.results);
    } catch {
      message.error('搜索失败');
    }
  };

  const handleRunBacktest = async (overrideStrategy?: string, customParams?: Record<string, number>) => {
    try {
      const values = await form.validateFields();

      if (!selectedStock) {
        message.warning('请先选择股票');
        return;
      }

      setLoading(true);
      const seq = selectSeqRef.current;

      const targetStrategy = overrideStrategy || values.strategy;
      if (overrideStrategy) {
        form.setFieldValue('strategy', overrideStrategy);
      }

      const response = await backtestApi.runBacktest({
        stock_code: selectedStock.code,
        strategy: targetStrategy,
        period: values.period,
        start_date: values.date_range?.[0]?.format('YYYY-MM-DD'),
        end_date: values.date_range?.[1]?.format('YYYY-MM-DD'),
        initial_capital: values.initial_capital,
        commission_per_trade: values.commission_per_trade ?? 1.0,
        trailing_stop: !!values.trailing_stop,
        staged_entry: !!values.staged_entry,
        params: customParams,
      });

      if (seq !== selectSeqRef.current) return; // 等待期间已切股，丢弃过期结果
      setBacktestResult(response.data);
      message.success(customParams ? '已按寻优最优参数完成回测' : '回测完成');
    } catch (error) {
      if ((error as { errorFields?: unknown }).errorFields) {
        return;
      }
      message.error('回测失败');
    } finally {
      setLoading(false);
    }
  };

  // ⚡ 一键对比4策略 + 买入持有基准
  const handleRunCompare = async () => {
    if (!selectedStock) {
      message.warning('请先选择股票');
      return;
    }

    try {
      setCompareLoading(true);
      const seq = selectSeqRef.current;
      const values = form.getFieldsValue();
      const response = await backtestApi.compare({
        stock_code: selectedStock.code,
        period: values.period || '1y',
        initial_capital: values.initial_capital || 100000,
        commission_per_trade: values.commission_per_trade ?? 1.0,
        trailing_stop: !!values.trailing_stop,
        staged_entry: !!values.staged_entry,
      });
      if (seq !== selectSeqRef.current) return; // 等待期间已切股，丢弃过期结果
      setCompareResult(response.data);
      message.success('对比完成');
    } catch {
      message.error('策略对比失败');
    } finally {
      setCompareLoading(false);
    }
  };

  const getEquityCurveOption = () => {
    if (!backtestResult) return {};
    // 使用后端返回的每日总资产曲线（现金+持仓市值）
    const curve = backtestResult.equity_curve;
    if (!curve || curve.length === 0) return {};

    const dateToEquity = new Map(curve.map(d => [d.date, d.value]));
    const buyMarkers = (backtestResult.trades || [])
      .filter(t => t.action === 'buy' && dateToEquity.has(t.date))
      .map(t => ({ value: [t.date, dateToEquity.get(t.date)], price: t.price, shares: t.shares }));
    const sellMarkers = (backtestResult.trades || [])
      .filter(t => t.action === 'sell' && dateToEquity.has(t.date))
      .map(t => ({ value: [t.date, dateToEquity.get(t.date)], price: t.price, profit_pct: t.profit_pct }));

    const series: any[] = [
      {
        name: '总资产',
        type: 'line',
        data: curve.map(d => d.value),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: colors.primary },
        areaStyle: {
          opacity: 0.15,
          color: colors.primary
        }
      },
      ...(buyMarkers.length > 0 ? [{
        name: '买入点 (B)',
        type: 'scatter',
        data: buyMarkers,
        symbol: 'triangle',
        symbolSize: 12,
        itemStyle: { color: colors.profit },
        z: 10
      }] : []),
      ...(sellMarkers.length > 0 ? [{
        name: '卖出点 (S)',
        type: 'scatter',
        data: sellMarkers,
        symbol: 'triangle',
        symbolRotate: 180,
        symbolSize: 12,
        itemStyle: { color: colors.loss },
        z: 10
      }] : [])
    ];

    // 叠加买入持有基准（灰色虚线）
    if (backtestResult.buy_hold_curve && backtestResult.buy_hold_curve.length > 0) {
      series.push({
        name: '买入持有基准',
        type: 'line',
        data: backtestResult.buy_hold_curve.map(d => d.value),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: colors.chartNeutral, type: 'dashed' },
        itemStyle: { color: colors.chartNeutral }
      });
    }

    return {
      tooltip: {
        trigger: 'axis',
        formatter: (params: any) => {
          const arr = Array.isArray(params) ? params : [params];
          const lines = arr.map((p: any) => formatBacktestTooltipLine(p));
          return [arr[0]?.name, ...lines].join('<br/>');
        }
      },
      legend: { top: 0 },
      xAxis: {
        type: 'category',
        data: curve.map(d => d.date)
      },
      yAxis: {
        type: 'value',
        name: '总资产 ($)',
        scale: true
      },
      dataZoom: [
        { type: 'inside' },
        { type: 'slider', height: 20, bottom: 0 }
      ],
      series
    };
  };

  const tradeColumns = [
    {
      title: '日期',
      dataIndex: 'date',
      key: 'date',
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      render: (action: string) => (
        <Tag color={action === 'buy' ? 'green' : 'red'}>
          {action === 'buy' ? '买入' : '卖出'}
        </Tag>
      )
    },
    {
      title: '价格',
      dataIndex: 'price',
      key: 'price',
      render: (price: number) => `$${price.toFixed(2)}`
    },
    {
      title: '数量',
      dataIndex: 'shares',
      key: 'shares',
    },
    {
      title: '收益率',
      dataIndex: 'profit_pct',
      key: 'profit_pct',
      render: (pct: number | undefined) => {
        if (pct === undefined) return '-';
        return (
          <span style={{ color: pct >= 0 ? colors.profit : colors.loss }}>
            {pct >= 0 ? '+' : ''}{(pct * 100).toFixed(2)}%
          </span>
        );
      }
    }
  ];

  return (
    <div>
      <h2>🧪 策略回测</h2>

      <Row gutter={16}>
        <Col xs={24} lg={16}>
          <Card title="回测配置" style={{ marginBottom: 16 }}>
            <Form
              form={form}
              layout="vertical"
              initialValues={{
                strategy: 'linear',
                period: '1y',
                initial_capital: 100000,
                commission_per_trade: 1.0
              }}
            >
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="stock_code"
                    label="股票代码"
                    rules={[{ required: true, message: '请输入股票代码' }]}
                  >
                    <Search
                      placeholder="输入股票代码搜索"
                      onSearch={handleSearch}
                      enterButton={<><SearchOutlined /> 搜索</>}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="strategy"
                    label="选择策略"
                    rules={[{ required: true, message: '请选择策略' }]}
                  >
                    {/* 选中态只显示策略名（title），下拉项保留完整描述——长标签不再溢出选择框 */}
                    <Select placeholder="选择回测策略" optionLabelProp="title">
                      {strategies.map(s => (
                        <Option key={s.id} value={s.id} title={s.name}>
                          {s.name} - {s.description}
                        </Option>
                      ))}
                    </Select>
                  </Form.Item>
                </Col>
              </Row>

              {searchResults.length > 0 && (
                <Form.Item label="选择股票">
                  <Space wrap>
                    {searchResults.map((stock) => (
                      <Tag
                        key={stock.code}
                        color={selectedStock?.code === stock.code ? 'blue' : 'default'}
                        style={{ cursor: 'pointer' }}
                        onClick={() => handleSelectStock(stock)}
                      >
                        {stock.code} {stock.name}
                      </Tag>
                    ))}
                  </Space>
                </Form.Item>
              )}

              <Row gutter={16}>
                <Col xs={24} md={6}>
                  <Form.Item name="period" label="回测周期">
                    <Select
                      disabled={!!dateRangeWatch}
                      options={[
                        { value: '1y', label: '近1年' },
                        { value: '3y', label: '近3年' },
                        { value: '5y', label: '近5年' },
                      ]}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={6}>
                  <Form.Item name="date_range" label="自定义时间段（可选，优先于周期）">
                    <DatePicker.RangePicker style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={6}>
                  <Form.Item
                    name="initial_capital"
                    label="初始资金"
                    rules={[{ required: true }]}
                  >
                    <InputNumber<number>
                      style={{ width: '100%' }}
                      min={10000}
                      step={10000}
                      formatter={value => `$ ${value}`.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}
                      parser={value => Number(value?.replace(/\$\s?|(,*)/g, '') || 0)}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={6}>
                  <Form.Item
                    name="commission_per_trade"
                    label="单笔手续费 ($)"
                    tooltip="买卖每笔扣除的固定交易佣金（默认 $1/笔）"
                  >
                    <InputNumber<number>
                      style={{ width: '100%' }}
                      min={0}
                      max={100}
                      step={0.5}
                      precision={2}
                    />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16} style={{ marginBottom: 8 }}>
                <Col xs={24} md={12}>
                  <Form.Item name="trailing_stop" valuePropName="checked" style={{ marginBottom: 4 }}>
                    <Checkbox>🛡️ 启用移动止盈保本（浮盈+10%自动上移止损至成本+1%，并按最高点-8%追踪止盈）</Checkbox>
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item name="staged_entry" valuePropName="checked" style={{ marginBottom: 4 }}>
                    <Checkbox>📐 启用分批金字塔建仓（首仓50%资金，回撤5%补仓剩余50%摊低成本）</Checkbox>
                  </Form.Item>
                </Col>
              </Row>

              <Form.Item>
                <Space size="middle">
                  <Button
                    type="primary"
                    icon={<ExperimentOutlined />}
                    onClick={() => handleRunBacktest()}
                    loading={loading}
                    size="large"
                  >
                    开始回测
                  </Button>
                  <Button
                    onClick={handleRunCompare}
                    loading={compareLoading}
                    disabled={!selectedStock}
                    size="large"
                  >
                    ⚡ 一键对比4策略
                  </Button>
                </Space>
              </Form.Item>
            </Form>
          </Card>

          {backtestResult && (
            <Card title="📊 权益曲线">
              <ReactEChartsCore echarts={echarts} option={getEquityCurveOption()} style={{ height: 300 }} />
            </Card>
          )}

          <ComparePanel result={compareResult} />

          {/* 🎯 参数寻优 / 🔬 Walk-Forward：自包含面板，见 BacktestPanels.tsx */}
          <OptimizePanel
            stockCode={selectedStock?.code ?? null}
            strategies={strategies}
            getParams={() => {
              const v = form.getFieldsValue();
              return { period: v.period || '1y', initial_capital: v.initial_capital || 100000, commission_per_trade: v.commission_per_trade ?? 1.0 };
            }}
            onApplyBestParams={(strat, bestParams) => handleRunBacktest(strat, bestParams)}
          />

          <WalkForwardPanel
            stockCode={selectedStock?.code ?? null}
            strategies={strategies}
            getParams={() => {
              const v = form.getFieldsValue();
              return { period: v.period || '1y', initial_capital: v.initial_capital || 100000, commission_per_trade: v.commission_per_trade ?? 1.0 };
            }}
          />
        </Col>

        <Col xs={24} lg={8}>
          {backtestResult ? (
            <>
              <Card title="📈 回测结果" style={{ marginBottom: 16 }}>
                <Descriptions column={1}>
                  <Descriptions.Item label="股票代码">
                    {backtestResult.stock_code}
                  </Descriptions.Item>
                  <Descriptions.Item label="策略">
                    {strategies.find(s => s.id === backtestResult.strategy)?.name}
                  </Descriptions.Item>
                  <Descriptions.Item label="回测周期">
                    {backtestResult.date_range
                      ? backtestResult.date_range
                      : backtestResult.period === '3y' ? '近3年' : backtestResult.period === '5y' ? '近5年' : '近1年'}
                  </Descriptions.Item>
                </Descriptions>

                <Divider />

                <Row gutter={16}>
                  <Col span={12}>
                    <Statistic
                      title="总收益"
                      value={backtestResult.total_return}
                      precision={2}
                      suffix="%"
                      valueStyle={{
                        color: backtestResult.total_return >= 0 ? colors.profit : colors.loss
                      }}
                    />
                  </Col>
                  <Col span={12}>
                    <Statistic
                      title="年化收益"
                      value={backtestResult.annual_return}
                      precision={2}
                      suffix="%"
                      valueStyle={{
                        color: backtestResult.annual_return >= 0 ? colors.profit : colors.loss
                      }}
                    />
                  </Col>
                </Row>

                {backtestResult.buy_hold_return != null && (() => {
                  const excess = backtestResult.total_return - backtestResult.buy_hold_return!;
                  return (
                    <Statistic
                      title="相对买入持有（超额）"
                      value={excess}
                      precision={2}
                      suffix="%"
                      prefix={excess > 0 ? '+' : ''}
                      style={{ marginTop: 16 }}
                      valueStyle={{ color: excess > 0 ? colors.profit : excess < 0 ? colors.loss : colors.textSecondary }}
                    />
                  );
                })()}

                <Row gutter={16} style={{ marginTop: 16 }}>
                  <Col span={12}>
                    <Statistic
                      title="最大回撤"
                      value={backtestResult.max_drawdown}
                      precision={2}
                      suffix="%"
                      valueStyle={{ color: colors.loss }}
                    />
                  </Col>
                  <Col span={12}>
                    <Statistic
                      title="夏普比率"
                      value={backtestResult.sharpe_ratio}
                      precision={2}
                    />
                  </Col>
                </Row>

                <Row gutter={16} style={{ marginTop: 16 }}>
                  <Col span={12}>
                    <Statistic
                      title="胜率"
                      value={backtestResult.win_rate}
                      precision={2}
                      suffix="%"
                    />
                  </Col>
                  <Col span={12}>
                    <Statistic
                      title="交易次数"
                      value={backtestResult.trade_count}
                    />
                  </Col>
                </Row>

                <Divider />

                <Descriptions column={1}>
                  <Descriptions.Item label="初始资金">
                    ${backtestResult.initial_capital?.toLocaleString() ?? '-'}
                  </Descriptions.Item>
                  <Descriptions.Item label="最终资金">
                    ${backtestResult.final_value?.toLocaleString() ?? '-'}
                  </Descriptions.Item>
                </Descriptions>
              </Card>

              <Card
                title="📝 交易记录"
                extra={
                  backtestResult.trades && backtestResult.trades.length > 0 ? (
                    <Button
                      size="small"
                      icon={<DownloadOutlined />}
                      onClick={() => {
                        downloadCsv(
                          `backtest_${backtestResult.stock_code}_${backtestResult.strategy}.csv`,
                          ['日期', '操作', '价格', '数量', '收益率(%)'],
                          backtestResult.trades.map((t) => [
                            t.date,
                            t.action === 'buy' ? '买入' : '卖出',
                            t.price,
                            t.shares,
                            t.profit_pct !== undefined ? (t.profit_pct * 100).toFixed(2) : '',
                          ])
                        );
                      }}
                    >
                      导出CSV
                    </Button>
                  ) : null
                }
              >
                <Table
                  style={{ whiteSpace: 'nowrap' }}
                  columns={tradeColumns}
                  dataSource={backtestResult.trades}
                  rowKey={(record) => `${record.date}-${record.action}-${record.price}`}
                  pagination={false}
                  size="small"
                  scroll={{ x: 'max-content', y: 300 }}
                />
              </Card>
            </>
          ) : (
            <Card>
              <div style={{ textAlign: 'center', padding: 50, color: colors.textSecondary }}>
                配置参数后点击"开始回测"
              </div>
            </Card>
          )}
        </Col>
      </Row>
    </div>
  );
};

export default Backtest;
