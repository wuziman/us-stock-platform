import React, { useState, useEffect } from 'react';
import { Card, Table, Button, Modal, Form, Input, InputNumber, DatePicker, Tag, Space, Statistic, Row, Col, message, Popconfirm, Alert, Tabs, Tooltip } from 'antd';
import { PlusOutlined, DeleteOutlined, EditOutlined, ThunderboltFilled, WarningFilled, SettingOutlined, DownloadOutlined } from '@ant-design/icons';
import { portfolioApi } from '../services/api';
import type { Position, PositionSummary } from '../types/api';
import dayjs from 'dayjs';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import echarts from '../services/echarts';
import { colors } from '../theme/tokens';
import { calcPositionSize, currencySymbol, downloadCsv, resolvePositionsViewState } from '../utils/format';

const Portfolio: React.FC = () => {
  const [loading, setLoading] = useState(false);
  const [positions, setPositions] = useState<Position[]>([]);
  const [history, setHistory] = useState<Position[]>([]);
  const [summary, setSummary] = useState<PositionSummary | null>(null);
  const [capitalInput, setCapitalInput] = useState<number | null>(null);
  const [capitalSaving, setCapitalSaving] = useState(false);
  const [submitting, setSubmitting] = useState(false);   // 弹窗提交防重复点击
  const [loadErrors, setLoadErrors] = useState<{ detail?: boolean; positions?: boolean; history?: boolean; stats?: boolean }>({});

  const [sellModalVisible, setSellModalVisible] = useState(false);
  const [sellingPosition, setSellingPosition] = useState<Position | null>(null);
  const [editModalVisible, setEditModalVisible] = useState(false);
  const [editingPosition, setEditingPosition] = useState<Position | null>(null);
  const [activeTab, setActiveTab] = useState<string>('holding');
  const [sellForm] = Form.useForm();
  const [form] = Form.useForm();

  // ---------- 组合净值曲线与回撤 ----------
  const [curve, setCurve] = useState<Array<{ date: string; value: number }>>([]);
  const [ddStats, setDdStats] = useState<{
    current_drawdown_pct: number;
    max_drawdown_pct: number;
    peak_value: number;
  } | null>(null);

  const fetchData = async () => {
    setLoading(true);
    // 分区加载：任一接口失败只标记对应分区，不把整个页面变成假零值
    const [positionsRes, historyRes, summaryRes, capitalRes] = await Promise.allSettled([
      portfolioApi.getPositions(),
      portfolioApi.getHistory(),
      portfolioApi.getSummary(),
      portfolioApi.getTotalCapital()
    ]);
    const failed: { detail?: boolean; positions?: boolean; history?: boolean; stats?: boolean } = {};
    if (positionsRes.status === 'fulfilled') setPositions(positionsRes.value.data);
    else { failed.detail = true; failed.positions = true; }
    if (historyRes.status === 'fulfilled') setHistory(historyRes.value.data);
    else { failed.detail = true; failed.history = true; }
    if (summaryRes.status === 'fulfilled') setSummary(summaryRes.value.data);
    else failed.stats = true;
    if (capitalRes.status === 'fulfilled') {
      const cap = capitalRes.value.data.total_capital || 0;
      setCapitalInput(cap || null);
    }
    setLoadErrors(failed);
    setLoading(false);
  };

  useEffect(() => {
    fetchData();
    // 净值曲线加载失败不影响持仓主数据
    portfolioApi.getEquityCurve()
      .then((res) => {
        setCurve(res.data.curve || []);
        setDdStats(res.data);
      })
      .catch(() => {});
  }, []);

  // ---------- 止损止盈触发检测 ----------
  const stopAlerts = positions.filter(p =>
    p.stop_loss && p.current_price && p.current_price <= p.stop_loss
  );
  const tpAlerts = positions.filter(p =>
    p.take_profit && p.current_price && p.current_price >= p.take_profit
  );

  const handleSaveCapital = async () => {
    if (capitalInput === null || capitalInput <= 0) {
      message.warning('请输入有效的总资金');
      return;
    }
    setCapitalSaving(true);
    try {
      await portfolioApi.setTotalCapital(capitalInput);
      message.success('总资金已保存');
      fetchData();
    } catch {
      message.error('保存失败');
    } finally {
      setCapitalSaving(false);
    }
  };

  // ---------- 卖出 ----------
  const openSellModal = (record: Position) => {
    setSellingPosition(record);
    sellForm.setFieldsValue({
      sell_price: record.current_price ?? record.buy_price,
      sell_date: dayjs()
    });
    setSellModalVisible(true);
  };

  const handleSellSubmit = async () => {
    if (!sellingPosition || submitting) return;
    setSubmitting(true);
    try {
      const values = await sellForm.validateFields();
      const res = await portfolioApi.sellPosition(sellingPosition.id, {
        sell_price: values.sell_price,
        sell_date: values.sell_date.format('YYYY-MM-DD')
      });
      const sym = currencySymbol(sellingPosition.market);
      message.success(`卖出成功！已实现盈亏 ${res.data.realized_pnl >= 0 ? '+' : ''}${sym}${res.data.realized_pnl}（${res.data.realized_pnl_pct}%），持有${res.data.holding_days}天`);
      setSellModalVisible(false);
      fetchData();
    } catch (error) {
      if ((error as { errorFields?: unknown }).errorFields) return;
      message.error('卖出失败');
    } finally {
      setSubmitting(false);
    }
  };

  const handleAdd = () => {
    setEditingPosition(null);
    form.resetFields();
    setEditModalVisible(true);
  };

  // ---------- 修改持仓：复用添加弹窗，代码/名称锁定不可改 ----------
  const handleEdit = (record: Position) => {
    setEditingPosition(record);
    form.setFieldsValue({
      stock_code: record.stock_code,
      stock_name: record.stock_name,
      buy_price: record.buy_price,
      quantity: record.quantity,
      buy_date: dayjs(record.buy_date),
      stop_loss: record.stop_loss,
      take_profit: record.take_profit,
    });
    setEditModalVisible(true);
  };

  const handleDelete = async (id: number) => {
    try {
      await portfolioApi.deletePosition(id);
      message.success('删除成功');
      fetchData();
    } catch {
      message.error('删除失败');
    }
  };

  const handleSubmit = async () => {
    if (submitting) return;
    setSubmitting(true);
    try {
      const values = await form.validateFields();
      if (editingPosition) {
        await portfolioApi.updatePosition(editingPosition.id, {
          buy_price: values.buy_price,
          quantity: values.quantity,
          buy_date: values.buy_date.format('YYYY-MM-DD'),
          stop_loss: values.stop_loss ?? null,   // 清空=清除该价位
          take_profit: values.take_profit ?? null,
        });
        message.success('修改成功');
      } else {
        const data = {
          ...values,
          buy_date: values.buy_date.format('YYYY-MM-DD'),
          // market 由后端 detect_market 统一判定，前端不再复刻正则
        };
        await portfolioApi.addPosition(data);
        message.success('添加成功');
      }
      setEditModalVisible(false);
      fetchData();
    } catch (error) {
      if ((error as { errorFields?: unknown }).errorFields) return;
      message.error('操作失败');
    } finally {
      setSubmitting(false);
    }
  };

  const handleExportCsv = () => {
    if (activeTab === 'holding') {
      if (positions.length === 0) {
        message.info('当前暂无持仓可导出');
        return;
      }
      downloadCsv(
        'portfolio_holdings.csv',
        ['股票代码', '股票名称', '市场', '买入价', '数量', '现价', '浮动盈亏', '收益率(%)', '止损位', '止盈位', '持有天数'],
        positions.map((p) => [
          p.stock_code,
          p.stock_name,
          p.market,
          p.buy_price,
          p.quantity,
          p.current_price ?? '',
          p.profit_loss ?? '',
          p.profit_loss_pct ?? '',
          p.stop_loss ?? '',
          p.take_profit ?? '',
          p.holding_days ?? '',
        ])
      );
    } else {
      if (history.length === 0) {
        message.info('当前暂无历史交易可导出');
        return;
      }
      downloadCsv(
        'portfolio_history.csv',
        ['股票代码', '股票名称', '市场', '买入价', '数量', '卖出价', '买入日', '卖出日', '持有天数', '已实现盈亏', '已实现收益率(%)'],
        history.map((p) => [
          p.stock_code,
          p.stock_name,
          p.market,
          p.buy_price,
          p.quantity,
          p.sell_price ?? '',
          p.buy_date?.slice(0, 10) ?? '',
          p.sell_date?.slice(0, 10) ?? '',
          p.holding_days ?? '',
          p.realized_pnl ?? '',
          p.realized_pnl_pct ?? '',
        ])
      );
    }
  };

  // ---------- 表格列 ----------
  const holdingColumns = [
    { title: '代码', dataIndex: 'stock_code', key: 'stock_code', width: 90 },
    { title: '名称', dataIndex: 'stock_name', key: 'stock_name', width: 110 },
    {
      title: '市场', dataIndex: 'market', key: 'market', width: 70,
      render: (m: string) => <Tag color={m === 'A' ? 'red' : 'blue'}>{m === 'A' ? 'A股' : '美股'}</Tag>
    },
    { title: '买入价', dataIndex: 'buy_price', key: 'buy_price', width: 90, render: (p: number, r: Position) => `${currencySymbol(r.market)}${p.toFixed(2)}` },
    { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 70 },
    {
      title: '现价', dataIndex: 'current_price', key: 'current_price', width: 90,
      render: (p: number | null, r: Position) => p ? `${currencySymbol(r.market)}${p.toFixed(2)}` : '-'
    },
    {
      title: '浮动盈亏', dataIndex: 'profit_loss', key: 'profit_loss', width: 120,
      render: (profit: number | null, r: Position) => {
        if (profit === null) return '-';
        return (
          <span style={{ color: profit >= 0 ? colors.profit : colors.loss, fontWeight: 'bold' }}>
            {profit >= 0 ? '+' : ''}{currencySymbol(r.market)}{profit.toFixed(2)} ({r.profit_loss_pct?.toFixed(2)}%)
          </span>
        );
      }
    },
    {
      title: '持有天数', dataIndex: 'holding_days', key: 'holding_days', width: 80,
      render: (d: number | null) => d !== null ? `${d}天` : '-'
    },
    {
      title: '止损/止盈与补仓位', key: 'levels', width: 185,
      render: (_: unknown, r: Position) => {
        const nextAddPx = (r.buy_price * 0.95).toFixed(2);
        return (
          <Space size={4} wrap>
            <Tag color={r.current_price && r.stop_loss && r.current_price <= r.stop_loss ? 'red' : 'default'}>
              损{r.stop_loss ? `${currencySymbol(r.market)}${r.stop_loss}` : '-'}
            </Tag>
            <Tag color={r.current_price && r.take_profit && r.current_price >= r.take_profit ? 'green' : 'default'}>
              盈{r.take_profit ? `${currencySymbol(r.market)}${r.take_profit}` : '-'}
            </Tag>
            <Tooltip title="按V2仓位方案：较买入均价回撤5%为第一档阶梯补仓位">
              <Tag color="blue" style={{ fontSize: 11 }}>
                补{currencySymbol(r.market)}{nextAddPx}
              </Tag>
            </Tooltip>
          </Space>
        );
      }
    },
    {
      title: '操作', key: 'action', width: 170,
      render: (_: unknown, r: Position) => (
        <Space>
          <Tooltip title="修改买入价/数量/止损止盈">
            <Button size="small" aria-label="修改持仓" icon={<EditOutlined />} onClick={() => handleEdit(r)} />
          </Tooltip>
          <Tooltip title="卖出">
            <Button type="primary" size="small" danger icon={<ThunderboltFilled />}
                    onClick={() => openSellModal(r)}>
              卖出
            </Button>
          </Tooltip>
          <Popconfirm title="确定删除该记录吗？" onConfirm={() => handleDelete(r.id)}>
            <Button type="link" size="small" danger aria-label="删除持仓" icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      )
    }
  ];

  const historyColumns = [
    { title: '代码', dataIndex: 'stock_code', key: 'stock_code', width: 90 },
    { title: '名称', dataIndex: 'stock_name', key: 'stock_name', width: 110 },
    { title: '买入价', dataIndex: 'buy_price', key: 'buy_price', width: 90, render: (p: number, r: Position) => `${currencySymbol(r.market)}${p.toFixed(2)}` },
    { title: '数量', dataIndex: 'quantity', key: 'quantity', width: 70 },
    { title: '卖出价', dataIndex: 'sell_price', key: 'sell_price', width: 90, render: (p: number | null, r: Position) => p ? `${currencySymbol(r.market)}${p.toFixed(2)}` : '-' },
    { title: '买入日', dataIndex: 'buy_date', key: 'buy_date', width: 100, render: (d: string) => d?.slice(0, 10) },
    { title: '卖出日', dataIndex: 'sell_date', key: 'sell_date', width: 100, render: (d: string | null) => d?.slice(0, 10) || '-' },
    { title: '持有天数', dataIndex: 'holding_days', key: 'hd', width: 80, render: (d: number | null) => d !== null && d !== undefined ? `${d}天` : '-' },
    {
      title: '已实现盈亏', dataIndex: 'realized_pnl', key: 'rp', width: 130,
      render: (pnl: number | null, r: Position) => {
        if (pnl === null) return '-';
        return (
          <span style={{ color: pnl >= 0 ? colors.profit : colors.loss, fontWeight: 'bold' }}>
            {pnl >= 0 ? '+' : ''}{currencySymbol(r.market)}{pnl.toFixed(2)} ({r.realized_pnl_pct?.toFixed(2)}%)
          </span>
        );
      }
    },
    {
      title: '操作', key: 'action', width: 70,
      render: (_: unknown, r: Position) => (
        <Popconfirm title="删除该历史记录？" onConfirm={() => handleDelete(r.id)}>
          <Button type="link" size="small" danger aria-label="删除历史记录" icon={<DeleteOutlined />} />
        </Popconfirm>
      )
    }
  ];

  return (
    <div>
      <h2>💼 持仓管理</h2>

      {/* ===== 止损止盈触发警报 ===== */}
      {stopAlerts.length > 0 && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message={`⛔ 止损触发（${stopAlerts.length}只）`}
          description={
            <div>
              {stopAlerts.map(a => (
                <div key={a.id}>
                  <b>{a.stock_name}({a.stock_code})</b>：现价{currencySymbol(a.market)}{a.current_price} 已跌破止损位{currencySymbol(a.market)}{a.stop_loss}
                  ，当前浮亏{a.profit_loss_pct}% —— 严格执行止损纪律！
                </div>
              ))}
            </div>
          }
        />
      )}
      {tpAlerts.length > 0 && (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 8 }}
          message={`🎯 止盈达成（${tpAlerts.length}只）`}
          description={
            <div>
              {tpAlerts.map(a => (
                <div key={a.id}>
                  <b>{a.stock_name}({a.stock_code})</b>：现价{currencySymbol(a.market)}{a.current_price} 已达止盈位{currencySymbol(a.market)}{a.take_profit}
                  ，当前盈利+{a.profit_loss_pct}% —— 考虑分批止盈落袋
                </div>
              ))}
            </div>
          }
        />
      )}

      {/* ===== 仓位集中度预警 ===== */}
      {summary?.warnings && summary.warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          icon={<WarningFilled />}
          style={{ marginBottom: 8 }}
          message={`⚠️ 仓位风控预警（${summary.warnings.length}项）`}
          description={
            <div>
              {summary.warnings.map((w, i) => <div key={i}>• {w.message}</div>)}
            </div>
          }
        />
      )}

      {/* ===== 加载失败提示（分区，不与真实零值混淆） ===== */}
      {loadErrors.stats && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message="持仓统计加载失败——下方总览数字暂不可用，不代表真实持仓状况"
          action={<Button size="small" danger onClick={fetchData}>重新加载</Button>}
        />
      )}
      {loadErrors.detail && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message="持仓明细加载失败"
          action={<Button size="small" danger onClick={fetchData}>重新加载</Button>}
        />
      )}

      {/* ===== 总览统计（统计接口失败时整体隐藏，避免渲染成假零值） ===== */}
      {!loadErrors.stats && (
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={12} md={8} lg={4}>
          <Card><Statistic title="持仓数量" value={summary?.total_positions || 0} /></Card>
        </Col>
        <Col xs={12} md={8} lg={5}>
          <Card><Statistic title="持仓市值" value={summary?.total_value || 0} precision={2} prefix="$" /></Card>
        </Col>
        <Col xs={12} md={8} lg={5}>
          <Card>
            <Statistic
              title="浮动盈亏"
              value={summary?.total_profit || 0} precision={2} prefix="$"
              valueStyle={{ color: (summary?.total_profit || 0) > 0 ? colors.profit : (summary?.total_profit || 0) < 0 ? colors.loss : colors.textSecondary }}
            />
          </Card>
        </Col>
        <Col xs={12} md={8} lg={5}>
          <Card>
            <Statistic
              title="收益率"
              value={summary?.total_profit_pct || 0} precision={2} suffix="%"
              valueStyle={{ color: (summary?.total_profit_pct || 0) > 0 ? colors.profit : (summary?.total_profit_pct || 0) < 0 ? colors.loss : colors.textSecondary }}
            />
          </Card>
        </Col>
        <Col xs={12} md={8} lg={5}>
          <Card>
            <Statistic
              title="现金比例"
              value={summary?.cash_pct ?? '—'}
              precision={summary?.cash_pct != null ? 1 : undefined}
              suffix={summary?.cash_pct != null ? '%' : ''}
              valueStyle={{
                color: summary?.cash_pct == null ? undefined :
                  summary.cash_pct < 15 ? colors.loss : summary.cash_pct > 25 ? colors.warning : colors.profit
              }}
            />
          </Card>
        </Col>
      </Row>
      )}

      {/* ===== 组合净值曲线（每日快照） ===== */}
      <Card
        title="📈 组合净值曲线"
        style={{ marginBottom: 16 }}
        extra={
          ddStats && ddStats.max_drawdown_pct > 0 ? (
            <Space size={16}>
              <span style={{ fontSize: 13 }}>
                历史峰值 <b>${ddStats.peak_value?.toLocaleString()}</b>
              </span>
              <span style={{ fontSize: 13 }}>
                当前回撤{' '}
                <b style={{ color: ddStats.current_drawdown_pct >= 20 ? colors.loss : colors.profit }}>
                  -{ddStats.current_drawdown_pct}%
                </b>
                {' '}（历史最大 -{ddStats.max_drawdown_pct}%）
              </span>
            </Space>
          ) : null
        }
      >
        {curve.length >= 3 ? (
          <>
            {ddStats && ddStats.current_drawdown_pct >= 20 && (
              <Alert
                type="error"
                showIcon
                style={{ marginBottom: 8 }}
                message={`⛔ 当前回撤 -${ddStats.current_drawdown_pct}%，已超过20%组合风控线，请审视整体仓位`}
              />
            )}
            <ReactEChartsCore
              echarts={echarts}
              option={{
                tooltip: { trigger: 'axis' },
                grid: { left: 70, right: 20, top: 20, bottom: 30 },
                xAxis: { type: 'category', data: curve.map(p => p.date) },
                yAxis: { type: 'value', scale: true },
                series: [{
                  name: '组合市值',
                  type: 'line',
                  data: curve.map(p => p.value),
                  smooth: true,
                  showSymbol: false,
                  lineStyle: { color: colors.primary, width: 2 },
                  areaStyle: { color: colors.primary, opacity: 0.12 },
                }],
              }}
              style={{ height: 260 }}
            />
            <div style={{ fontSize: 12, color: colors.textSecondary }}>
              快照由后台监控任务在每个交易时段自动写入（同日取最新值）
            </div>
          </>
        ) : curve.length > 0 ? (
          <div style={{ textAlign: 'center', padding: 30, color: colors.textSecondary }}>
            已积累 {curve.length} 天快照——满 3 天后绘制曲线（样本过少，两点连线容易夸大波动）
          </div>
        ) : (
          <div style={{ textAlign: 'center', padding: 30, color: colors.textSecondary }}>
            暂无快照数据——后台监控任务会在交易时段自动记录每日组合市值，累积几天后这里会出现曲线
          </div>
        )}
      </Card>

      <Card
        title="持仓明细"
        extra={
          <Space wrap>
            <Tooltip title="设置总资金后启用单票40%上限与现金比例预警">
              <InputNumber
                prefix={<SettingOutlined />}
                placeholder="总资金"
                min={0}
                step={10000}
                value={capitalInput}
                onChange={(v) => setCapitalInput(v)}
                formatter={value => `$ ${value}`.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}
                parser={value => Number(value?.replace(/\$\s?|(,*)/g, '') || 0)}
                style={{ width: 150 }}
              />
            </Tooltip>
            <Button onClick={handleSaveCapital} loading={capitalSaving}>保存</Button>
            <Button icon={<DownloadOutlined />} onClick={handleExportCsv}>导出CSV</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={handleAdd}>添加持仓</Button>
          </Space>
        }
      >
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            {
              key: 'holding',
              label: `当前持仓 (${loadErrors.positions ? '加载失败' : positions.length})`,
              children: resolvePositionsViewState(loading, !!loadErrors.positions, positions.length) === 'error' ? (
                <Alert
                  type="error"
                  showIcon
                  message="当前持仓请求失败——无法读取持仓列表（并非暂无持仓）"
                  description="请检查后端服务或网络连接后点击右侧按钮重试。"
                  action={<Button size="small" danger onClick={fetchData}>重新加载</Button>}
                />
              ) : (
                <Table
                  columns={holdingColumns}
                  dataSource={positions}
                  rowKey="id"
                  loading={loading}
                  pagination={false}
                  scroll={{ x: 1100 }}
                  locale={{ emptyText: '暂无持仓，点击右上角添加' }}
                />
              )
            },
            {
              key: 'history',
              label: `历史交易 (${loadErrors.history ? '加载失败' : history.length})`,
              children: resolvePositionsViewState(loading, !!loadErrors.history, history.length) === 'error' ? (
                <Alert
                  type="error"
                  showIcon
                  message="历史交易请求失败——无法读取历史交易记录"
                  action={<Button size="small" danger onClick={fetchData}>重新加载</Button>}
                />
              ) : (
                <Table
                  columns={historyColumns}
                  dataSource={history}
                  rowKey="id"
                  loading={loading}
                  pagination={false}
                  scroll={{ x: 1100 }}
                  locale={{ emptyText: '暂无历史交易' }}
                />
              )
            }
          ]}
        />
      </Card>

      {/* ===== 卖出弹窗 ===== */}
      <Modal
        title={`卖出 ${sellingPosition?.stock_name || ''} (${sellingPosition?.stock_code || ''})`}
        open={sellModalVisible}
        onOk={handleSellSubmit}
        confirmLoading={submitting}
        onCancel={() => setSellModalVisible(false)}
        okText="确认卖出"
        width="92%"
        style={{ maxWidth: 520 }}
      >
        {sellingPosition && (
          <Alert
            type="info"
            style={{ marginBottom: 16 }}
            message={`买入价${currencySymbol(sellingPosition.market)}${sellingPosition.buy_price} × ${sellingPosition.quantity}股 | 现价${currencySymbol(sellingPosition.market)}${sellingPosition.current_price ?? '-'} | 浮动盈亏${sellingPosition.profit_loss_pct ?? '-'}%`}
          />
        )}
        <Form form={sellForm} layout="vertical">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="sell_price" label={`卖出价格 (${currencySymbol(sellingPosition?.market)})`} rules={[{ required: true, message: '请输入卖出价格' }]}>
                <InputNumber prefix={currencySymbol(sellingPosition?.market)} style={{ width: '100%' }} min={0} step={0.01} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="sell_date" label="卖出日期" rules={[{ required: true, message: '请选择日期' }]}>
                <DatePicker style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      {/* ===== 添加/修改持仓弹窗 ===== */}
      <Modal
        title={editingPosition ? `修改持仓 ${editingPosition.stock_name} (${editingPosition.stock_code})` : '添加持仓'}
        open={editModalVisible}
        onOk={handleSubmit}
        confirmLoading={submitting}
        onCancel={() => setEditModalVisible(false)}
        width="92%"
        style={{ maxWidth: 600 }}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="stock_code" label="股票代码" rules={[{ required: true, message: '请输入股票代码' }]}>
            <Input placeholder="如：AAPL, MU, 000001" disabled={!!editingPosition} />
          </Form.Item>
          <Form.Item name="stock_name" label="股票名称" rules={[{ required: true, message: '请输入股票名称' }]}>
            <Input placeholder="如：苹果, 美光科技" disabled={!!editingPosition} />
          </Form.Item>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="buy_price" label="买入价格" rules={[{ required: true, message: '请输入买入价格' }]}>
                <InputNumber style={{ width: '100%' }} min={0} step={0.01} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="quantity" label="数量" rules={[{ required: true, message: '请输入数量' }]}>
                <InputNumber style={{ width: '100%' }} min={1} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="buy_date" label="买入日期" rules={[{ required: true, message: '请选择买入日期' }]}>
            <DatePicker style={{ width: '100%' }} />
          </Form.Item>
          <div style={{ marginBottom: 12 }}>
            <Space size="small" wrap>
              <span style={{ fontSize: 12, color: colors.textSecondary }}>⚡ 快捷策略与仓位：</span>
              <Button
                size="small"
                onClick={() => {
                  const bp = Number(form.getFieldValue('buy_price'));
                  if (!bp || bp <= 0) {
                    message.warning('请先填写买入价格');
                    return;
                  }
                  form.setFieldsValue({
                    stop_loss: Number((bp * 0.92).toFixed(2)),
                    take_profit: Number((bp * 1.15).toFixed(2)),
                  });
                }}
              >
                按线性策略 (-8%损/+15%盈)
              </Button>
              <Button
                size="small"
                onClick={() => {
                  const bp = Number(form.getFieldValue('buy_price'));
                  if (!bp || bp <= 0) {
                    message.warning('请先填写买入价格');
                    return;
                  }
                  form.setFieldsValue({
                    stop_loss: Number((bp * 0.92).toFixed(2)),
                    take_profit: Number((bp * 1.46).toFixed(2)),
                  });
                }}
              >
                按非线性策略 (-8%损/+46%盈)
              </Button>
              <Button
                size="small"
                type="dashed"
                onClick={() => {
                  const bp = Number(form.getFieldValue('buy_price'));
                  if (!bp || bp <= 0) {
                    message.warning('请先填写买入价格');
                    return;
                  }
                  const sl = Number(form.getFieldValue('stop_loss')) || Number((bp * 0.92).toFixed(2));
                  const cap = capitalInput && capitalInput > 0 ? capitalInput : 100000;
                  const ps = calcPositionSize(cap, bp, sl);
                  if (ps.recommendedShares > 0) {
                    form.setFieldsValue({
                      quantity: ps.recommendedShares,
                      stop_loss: sl,
                    });
                    message.success(`按总资金 $${cap.toLocaleString()} 的 2% 定额风险法则，建议建仓 ${ps.recommendedShares} 股（首仓可先入 ${ps.firstTrancheShares} 股）`);
                  }
                }}
              >
                🧮 2%风控算仓位
              </Button>
            </Space>
          </div>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                name="stop_loss"
                label="止损位"
                dependencies={['buy_price']}
                rules={[({ getFieldValue }) => ({
                  validator(_, value) {
                    const buy = getFieldValue('buy_price');
                    if (value == null || !buy) return Promise.resolve();
                    return value < buy ? Promise.resolve() : Promise.reject(new Error('止损位应低于买入价'));
                  },
                })]}
              >
                <InputNumber style={{ width: '100%' }} min={0} step={0.01} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                name="take_profit"
                label="止盈位"
                dependencies={['buy_price']}
                rules={[({ getFieldValue }) => ({
                  validator(_, value) {
                    const buy = getFieldValue('buy_price');
                    if (value == null || !buy) return Promise.resolve();
                    return value > buy ? Promise.resolve() : Promise.reject(new Error('止盈位应高于买入价'));
                  },
                })]}
              >
                <InputNumber style={{ width: '100%' }} min={0} step={0.01} />
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>
    </div>
  );
};

export default Portfolio;
