import React, { useState, useEffect } from 'react';
import {
  Card, Button, Tag, Space, Alert, Modal, message,
  Descriptions, Spin, Tooltip, Row, Col
} from 'antd';
import {
  ReloadOutlined, ThunderboltOutlined,
  RocketOutlined, StarOutlined, LineChartOutlined, TrophyOutlined
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { aiPickApi, stockApi, errDetail } from '../services/api';
import type { PickRecord, StatusInfo, WeeklyAlphaResponse, WeeklyHistoryResponse, AIPickPerformanceResponse } from '../types/api';
import { colors } from '../theme/tokens';

const CONF_COLOR: Record<string, string> = { high: 'red', medium: 'orange', low: 'default' };
const CONF_LABEL: Record<string, string> = { high: '高确信', medium: '中等', low: '低' };
const TAG_TEXT_FIX = {
  orange: { color: '#873800' },
  green: { color: '#135200' },
} as const;

const AIPick: React.FC = () => {
  const navigate = useNavigate();
  // 周度美股硬科技选股 · Alpha TOP 5 猛禽池
  const [weeklyAlpha, setWeeklyAlpha] = useState<WeeklyAlphaResponse | null>(null);
  const [weeklyHistory, setWeeklyHistory] = useState<WeeklyHistoryResponse | null>(null);
  const [loadingWeekly, setLoadingWeekly] = useState(true);
  const [scanningWeekly, setScanningWeekly] = useState(false);

  const [status, setStatus] = useState<StatusInfo | null>(null);
  const [history, setHistory] = useState<PickRecord[]>([]);
  const [performance, setPerformance] = useState<AIPickPerformanceResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(true);

  const fetchWeeklyAlpha = async () => {
    setLoadingWeekly(true);
    try {
      const [top5Res, histRes] = await Promise.allSettled([
        aiPickApi.getWeeklyAlphaTop5(),
        aiPickApi.getWeeklyAlphaHistory(),
      ]);
      if (top5Res.status === 'fulfilled') setWeeklyAlpha(top5Res.value.data);
      if (histRes.status === 'fulfilled') setWeeklyHistory(histRes.value.data);
    } catch {
      // 容错静默
    } finally {
      setLoadingWeekly(false);
    }
  };

  const handleScanWeekly = async () => {
    setScanningWeekly(true);
    message.loading({ content: '正在扫描全美35-40支顶级硬科技与算力龙头，约需10-15秒...', key: 'weekly-scan', duration: 0 });
    try {
      const res = await aiPickApi.triggerWeeklyAlphaScan();
      setWeeklyAlpha(res.data);
      message.success({ content: '周度 Alpha TOP 5 猛禽池选股已完成！', key: 'weekly-scan' });
      aiPickApi.getWeeklyAlphaHistory().then((r) => setWeeklyHistory(r.data)).catch(() => {});
    } catch (err) {
      message.error({ content: errDetail(err, '选股扫描失败'), key: 'weekly-scan' });
    } finally {
      setScanningWeekly(false);
    }
  };

  const handleAddWatch = async (code: string, name: string) => {
    try {
      await stockApi.addToWatchlist({ stock_code: code, stock_name: name, market: 'US' });
      message.success(`已将 ${code} (${name}) 加入自选股`);
    } catch (err) {
      message.error(errDetail(err, '添加自选失败'));
    }
  };

  const fetchStatus = async () => {
    try {
      const res = await aiPickApi.getStatus();
      setStatus(res.data);
    } catch {
      // 状态读取失败不阻塞页面
    }
  };

  const fetchHistory = async () => {
    setLoadingHistory(true);
    try {
      const [res, perfRes] = await Promise.allSettled([
        aiPickApi.getHistory(30),
        aiPickApi.getPerformance(100),
      ]);
      if (res.status === 'fulfilled') setHistory(res.value.data);
      if (perfRes.status === 'fulfilled') setPerformance(perfRes.value.data);
    } catch {
      message.error('历史记录加载失败');
    } finally {
      setLoadingHistory(false);
    }
  };

  useEffect(() => {
    fetchWeeklyAlpha();
    fetchStatus();
    fetchHistory();
  }, []);

  const handleRun = () => {
    if (!status?.ai_configured) {
      message.warning('请先在 config/config.json 配置 ai_provider（base_url/api_key/model）');
      return;
    }
    Modal.confirm({
      title: '运行全产业链AI选股？',
      content: '将拉取候选池行情与新闻、结合产业链卡点逻辑调用大模型深度分析，耗时约1-3分钟。',
      okText: '开始分析',
      onOk: async () => {
        setRunning(true);
        try {
          const res = await aiPickApi.run();
          message.success(`AI选股完成：${res.data.picks.length}只入选`);
          await Promise.all([fetchStatus(), fetchHistory()]);
        } catch (error) {
          const detail = errDetail(error, 'AI选股失败');
          message.error(String(detail).slice(0, 120));
        } finally {
          setRunning(false);
        }
      }
    });
  };

  const grouped = history.reduce<Record<string, PickRecord[]>>((acc, r) => {
    (acc[r.run_date] = acc[r.run_date] || []).push(r);
    return acc;
  }, {});

  return (
    <div>
      <h2>🦅 精选标的 · 硬科技量化 TOP 5</h2>

      {/* ===== 周度美股硬科技选股 · Alpha TOP 5 猛禽池 ===== */}
      <Card
        title={
          <Space align="center" wrap>
            <RocketOutlined style={{ color: colors.primary, fontSize: 18 }} />
            <span style={{ fontSize: 16, fontWeight: 'bold' }}>🦅 周度美股硬科技选股 · Alpha TOP 5 猛禽池</span>
            {weeklyAlpha?.scan_date && (
              <Tag color="cyan">扫描时间：{weeklyAlpha.scan_date} (全美 {weeklyAlpha.total_scanned} 支硬科技龙头)</Tag>
            )}
          </Space>
        }
        extra={
          <Button
            type="primary"
            icon={<ReloadOutlined />}
            onClick={handleScanWeekly}
            loading={scanningWeekly}
          >
            一键全市场量化扫描
          </Button>
        }
        style={{ marginBottom: 20, borderColor: colors.primary, borderRadius: 8 }}
      >
        {weeklyHistory?.cumulative && weeklyHistory.cumulative.total_weeks > 0 && (
          <Alert
            type="success"
            icon={<TrophyOutlined />}
            showIcon
            style={{ marginBottom: 14 }}
            message={
              <Space wrap>
                <b>🏆 猛禽池跨周累积实盘战绩：</b>
                <Tag color="blue">累计复盘 {weeklyHistory.cumulative.total_weeks} 期 ({weeklyHistory.cumulative.total_picks} 支次)</Tag>
                <Tag color="green">总胜率 {weeklyHistory.cumulative.overall_win_rate}% ({weeklyHistory.cumulative.total_winners}胜)</Tag>
                <Tag color={weeklyHistory.cumulative.avg_return_pct >= 0 ? 'gold' : 'red'}>
                  单周平均收益 {weeklyHistory.cumulative.avg_return_pct >= 0 ? '+' : ''}{weeklyHistory.cumulative.avg_return_pct}%
                </Tag>
              </Space>
            }
          />
        )}

        {weeklyAlpha?.ai_thesis && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message="🧠 AI 投研总监核心逻辑研判"
            description={<div style={{ whiteSpace: 'pre-wrap', fontSize: 13, lineHeight: 1.6 }}>{weeklyAlpha.ai_thesis}</div>}
          />
        )}

        {weeklyAlpha?.top5 && weeklyAlpha.top5.length > 0 ? (
          <Row gutter={[16, 16]}>
            {weeklyAlpha.top5.map((item, idx) => {
              const rankMedals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣'];
              const rankColors = ['gold', 'geekblue', 'volcano', 'blue', 'cyan'];
              return (
                <Col xs={24} sm={12} lg={8} key={item.code} style={{ minWidth: 220 }}>
                  <Card
                    size="small"
                    title={
                      <Space>
                        <span style={{ fontSize: 16 }}>{rankMedals[idx] || `#${idx + 1}`}</span>
                        <b>{item.code}</b>
                        <Tag color={rankColors[idx]}>{item.name}</Tag>
                      </Space>
                    }
                    extra={
                      <Space size={2}>
                        <Tooltip title="跳转5数操盘卡">
                          <Button
                            type="text"
                            size="small"
                            icon={<LineChartOutlined />}
                            onClick={() => navigate(`/analysis?code=${item.code}&name=${encodeURIComponent(item.name)}&market=US`)}
                          />
                        </Tooltip>
                        <Tooltip title="加入自选股">
                          <Button
                            type="text"
                            size="small"
                            icon={<StarOutlined />}
                            onClick={() => handleAddWatch(item.code, item.name)}
                          />
                        </Tooltip>
                      </Space>
                    }
                    style={{ height: '100%', borderRadius: 8, boxShadow: '0 1px 4px rgba(0,0,0,0.05)' }}
                  >
                    <div style={{ marginBottom: 8 }}>
                      <Tag color="purple" style={{ fontSize: 11 }}>{item.sector}</Tag>
                    </div>

                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
                      <span style={{ color: colors.textSecondary, fontSize: 12 }}>当前真实价</span>
                      <span style={{ fontSize: 18, fontWeight: 'bold' }}>${item.current_price}</span>
                    </div>

                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                      <span style={{ color: colors.textSecondary, fontSize: 12 }}>量化总分</span>
                      <Tag color={item.total_score >= 80 ? 'green' : 'blue'} style={{ fontWeight: 'bold', fontSize: 13 }}>
                        {item.total_score} 分
                      </Tag>
                    </div>

                    <div style={{ background: colors.bgLight, padding: '8px 10px', borderRadius: 6, fontSize: 12, marginBottom: 8 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                        <span style={{ color: colors.textSecondary }}>动量得分</span>
                        <b>{item.momentum_score}</b>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                        <span style={{ color: colors.textSecondary }}>买入甜点区</span>
                        <b>{item.sweetspot_score}</b>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <span style={{ color: colors.textSecondary }}>回测胜率</span>
                        <b style={{ color: colors.profit }}>{item.win_rate}%</b>
                      </div>
                    </div>

                    <div style={{ fontSize: 12, borderTop: `1px dashed ${colors.border}`, paddingTop: 6 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <span style={{ color: colors.textSecondary }}>建议买点:</span>
                        <b style={{ color: colors.primary }}>${item.linear_buy}</b>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <span style={{ color: colors.textSecondary }}>止盈目标:</span>
                        <b style={{ color: colors.profit }}>${item.linear_profit}</b>
                      </div>
                    </div>
                  </Card>
                </Col>
              );
            })}
          </Row>
        ) : (
          <div style={{ textAlign: 'center', padding: 40, color: colors.textSecondary }}>
            {loadingWeekly ? '正在拉取猛禽池数据...' : '暂无数据，请点击右上角「一键全市场量化扫描」'}
          </div>
        )}
      </Card>

      {/* ===== 上周实盘复盘战报 ===== */}
      {weeklyAlpha?.battle_report && weeklyAlpha.battle_report.picks && (
        <Card
          title={
            <Space wrap>
              <span>⚔️ 上期猛禽池战绩复盘</span>
              <Tag color={weeklyAlpha.battle_report.avg_return_pct >= 0 ? 'green' : 'red'}>
                平均收益: {weeklyAlpha.battle_report.avg_return_pct >= 0 ? '+' : ''}{weeklyAlpha.battle_report.avg_return_pct}%
              </Tag>
              <Tag color={weeklyAlpha.battle_report.win_rate >= 50 ? 'green' : 'orange'}>
                胜率: {weeklyAlpha.battle_report.win_rate}% ({weeklyAlpha.battle_report.win_count}胜{weeklyAlpha.battle_report.loss_count}负)
              </Tag>
            </Space>
          }
          style={{ marginBottom: 20, borderRadius: 8 }}
        >
          <div style={{ marginBottom: 12, fontSize: 12, color: colors.textSecondary }}>
            入选日期: {weeklyAlpha.battle_report.prev_scan_date} → 复盘日期: {weeklyAlpha.battle_report.review_date}
          </div>
          <Row gutter={[12, 12]}>
            {weeklyAlpha.battle_report.picks.map((p) => (
              <Col xs={24} sm={12} lg={8} key={p.code}>
                <div style={{
                  display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                  padding: '8px 12px', background: colors.bgLight, borderRadius: 6,
                  borderLeft: `3px solid ${p.is_winner ? '#52c41a' : '#ff4d4f'}`
                }}>
                  <div>
                    <b>{p.code}</b> <span style={{ fontSize: 12, color: colors.textSecondary }}>{p.name}</span>
                    <div style={{ fontSize: 11, color: colors.textSecondary }}>
                      ${p.entry_price} → ${p.review_price}
                    </div>
                  </div>
                  <Tag color={p.is_winner ? 'green' : 'red'} style={{ fontWeight: 'bold', fontSize: 13 }}>
                    {p.weekly_return_pct >= 0 ? '+' : ''}{p.weekly_return_pct}%
                  </Tag>
                </div>
              </Col>
            ))}
          </Row>
          <div style={{ marginTop: 12, fontSize: 12, color: colors.textSecondary }}>
            🏆 最强: <b style={{ color: '#52c41a' }}>{weeklyAlpha.battle_report.best_pick.code} ({weeklyAlpha.battle_report.best_pick.return_pct >= 0 ? '+' : ''}{weeklyAlpha.battle_report.best_pick.return_pct}%)</b>
            {' | '}最弱: <b style={{ color: '#ff4d4f' }}>{weeklyAlpha.battle_report.worst_pick.code} ({weeklyAlpha.battle_report.worst_pick.return_pct >= 0 ? '+' : ''}{weeklyAlpha.battle_report.worst_pick.return_pct}%)</b>
          </div>
        </Card>
      )}

      {/* ===== AI 产业链深度投研 ===== */}
      {status && !status.ai_configured && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message="AI大模型未配置"
          description={
            <div style={{ fontSize: 13 }}>
              请在 <b>config/config.json</b> 中配置 ai_provider 字段以启用深度投研分析。
            </div>
          }
        />
      )}
      <Card title="🧠 AI 产业链深度投研选股" style={{ marginBottom: 16 }}>
        <Space size="large" wrap align="center">
          <Button
            type="primary"
            size="large"
            icon={<ThunderboltOutlined />}
            onClick={handleRun}
            loading={running}
            disabled={!status?.ai_configured}
          >
            {running ? 'AI产业链深度分析中…（约1-3分钟）' : '运行全产业链AI选股'}
          </Button>
          <Descriptions column={1} size="small" style={{ flex: 1, minWidth: 320 }}>
            <Descriptions.Item label="AI投研模型">
              {status?.ai_configured
                ? <Tag color="green" style={TAG_TEXT_FIX.green}>{status.ai_model}</Tag>
                : <Tag color="red">未配置</Tag>}
            </Descriptions.Item>
            <Descriptions.Item label="候选标的池">
              全美35-40支顶级AI算力、半导体与光通信龙头
            </Descriptions.Item>
            <Descriptions.Item label="上次运行">
              {status?.last_run_date || '从未运行'}
            </Descriptions.Item>
          </Descriptions>
        </Space>
        <div style={{ color: colors.textSecondary, fontSize: 12, marginTop: 12 }}>
          分析框架：产业链卡点 → 定价权 → 验证链 → 压力测试。输出仅供研究参考，不构成投资建议。
        </div>
      </Card>

      {/* ===== 历史战绩 ===== */}
      {performance && performance.total_picks > 0 && (
        <Card
          title={
            <Space wrap>
              <span>📊 历史战绩（20 / 60 交易日复盘 · 按置信度分层）</span>
              <Tag color="blue">累计记录 {performance.total_picks} 笔</Tag>
              {performance.overall?.d20?.win_rate != null && (
                <Tag color={performance.overall.d20.win_rate >= 50 ? 'green' : 'orange'}>
                  20日胜率 {performance.overall.d20.win_rate}% (均收益 {performance.overall.d20.avg_return_pct ?? 0}%)
                </Tag>
              )}
              {performance.overall?.d60?.win_rate != null && (
                <Tag color={performance.overall.d60.win_rate >= 50 ? 'green' : 'orange'}>
                  60日胜率 {performance.overall.d60.win_rate}% (均收益 {performance.overall.d60.avg_return_pct ?? 0}%)
                </Tag>
              )}
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          <Row gutter={16}>
            {(['high', 'medium', 'low'] as const).map((tierKey) => {
              const t = performance.by_confidence?.[tierKey];
              if (!t || t.total_picks === 0) return null;
              return (
                <Col xs={24} md={8} key={tierKey}>
                  <Card size="small" type="inner" title={<Tag color={CONF_COLOR[tierKey]}>{CONF_LABEL[tierKey]}组 ({t.total_picks}只)</Tag>}>
                    <p style={{ margin: '4px 0' }}>
                      <b>20日:</b> 胜率 {t.d20?.win_rate != null ? `${t.d20.win_rate}%` : '待满期'}
                      {' '}(均收益 {t.d20?.avg_return_pct != null ? `${t.d20.avg_return_pct}%` : '--'})
                    </p>
                    <p style={{ margin: '4px 0 0' }}>
                      <b>60日:</b> 胜率 {t.d60?.win_rate != null ? `${t.d60.win_rate}%` : '待满期'}
                      {' '}(均收益 {t.d60?.avg_return_pct != null ? `${t.d60.avg_return_pct}%` : '--'})
                    </p>
                  </Card>
                </Col>
              );
            })}
          </Row>
        </Card>
      )}

      {/* ===== 历史选股记录 ===== */}
      <h3 style={{ marginTop: 24, marginBottom: 12 }}>📋 历期选股结果</h3>
      {loadingHistory ? (
        <div style={{ textAlign: 'center', padding: 50 }}><Spin size="large" /></div>
      ) : Object.keys(grouped).length === 0 ? (
        <Card><div style={{ textAlign: 'center', padding: 30, color: colors.textSecondary }}>暂无历史选股记录</div></Card>
      ) : (
        Object.entries(grouped).map(([runDate, picks]) => {
          const tracked = picks.filter((p) => p.return_pct != null);
          const winCount = tracked.filter((p) => (p.return_pct || 0) > 0).length;
          const avgRet = tracked.length > 0
            ? Math.round((tracked.reduce((sum, p) => sum + (p.return_pct || 0), 0) / tracked.length) * 10) / 10
            : null;
          return (
            <Card
              key={runDate}
              title={
                <Space wrap>
                  <span>📋 {runDate} 选股结果</span>
                  {avgRet !== null && (
                    <Tag color={avgRet >= 0 ? 'green' : 'red'}>
                      至今等权 {avgRet >= 0 ? '+' : ''}{avgRet}% (胜率 {winCount}/{tracked.length})
                    </Tag>
                  )}
                </Space>
              }
              style={{ marginBottom: 16 }}
              extra={<Tooltip title="按rank排序"><Tag>{picks.length}只</Tag></Tooltip>}
            >
              {picks.map((p) => (
                <Card
                  key={p.id}
                  type="inner"
                  title={
                    <Space wrap>
                      <b>#{p.rank} {p.stock_code}</b>
                      <span>{p.stock_name}</span>
                      <Tag
                        color={CONF_COLOR[p.confidence]}
                        style={CONF_COLOR[p.confidence] === 'orange' ? TAG_TEXT_FIX.orange : undefined}
                      >
                        {CONF_LABEL[p.confidence]}
                      </Tag>
                      {p.price_at_pick != null && <span style={{ color: colors.textSecondary }}>入选@${p.price_at_pick}</span>}
                      {p.current_price != null && <span style={{ color: colors.textSecondary }}>→ 现价${p.current_price}</span>}
                      {p.return_pct != null && (
                        <Tag color={p.return_pct >= 0 ? 'green' : 'red'} style={{ fontWeight: 'bold' }}>
                          至今 {p.return_pct >= 0 ? '+' : ''}{p.return_pct}%
                        </Tag>
                      )}
                    </Space>
                  }
                  extra={
                    <Space size={4}>
                      <Button
                        size="small"
                        type="link"
                        icon={<LineChartOutlined />}
                        onClick={() => navigate(`/analysis?code=${p.stock_code}&name=${encodeURIComponent(p.stock_name)}&market=US`)}
                      >
                        5数操盘卡
                      </Button>
                      <Button
                        size="small"
                        type="link"
                        icon={<StarOutlined />}
                        onClick={() => handleAddWatch(p.stock_code, p.stock_name)}
                      >
                        加自选
                      </Button>
                    </Space>
                  }
                  style={{ marginBottom: 12 }}
                >
                  <p><b>💡 核心论点：</b>{p.thesis}</p>
                  <p><b>🔗 卡点：</b>{p.bottlenecks}</p>
                  <p><b>⚠️ 反方压力测试：</b>{p.risks}</p>
                  <p style={{ marginBottom: 0 }}><b>🚀 催化剂：</b>{p.catalysts}</p>
                </Card>
              ))}
            </Card>
          );
        })
      )}
    </div>
  );
};

export default AIPick;
