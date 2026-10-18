# DESIGN.md — 量化平台前端设计系统

本文件描述本前端的设计约定。改 UI 前先读这里；颜色一律从 `src/theme/tokens.ts` 取（`colors.*`），**禁止再写裸 hex**。

## 定位

- **模式**：Operate（工具型操作界面）——可扫读、一致、符合 antd 直觉优先于个性化表达。
- **基座**：antd 5（zh_CN locale）+ React + Vite。不重造组件，只做约定与语义层。

## 颜色（唯一定义处：`src/theme/tokens.ts`）

| Token | 值 | 用途 |
|---|---|---|
| `primary` | `#0958d9` | 品牌标题、链接、图表主序列、antd 主色（取 blue-7：主按钮白字对比度达 AA；勿再回退 #1890ff） |
| `profit` | `#3f8600` | 正收益/盈利（**文字**） |
| `loss` | `#cf1322` | 负收益/亏损（**文字**） |
| `warning` | `#faad14` | 警示、中性评级 |
| `success` | `#52c41a` | 成功、高评分（≥8） |
| `error` | `#ff4d4f` | 错误、低评分（<4） |
| `klineUp` | `#ef232a` | K线**阳线**（红涨） |
| `klineDown` | `#14b143` | K线**阴线**（绿跌） |
| `chartPurple` | `#722ed1` | 非线性策略线、MA50 |
| `chartCyan` | `#13c2c2` | 双均线交叉策略线 |
| `chartOrange` | `#fa8c16` | MACD策略线 |
| `ma5` | `#f5a623` | MA5 均线 |
| `indicatorBlue` | `#5470c6` | 技术指标线（echarts 默认蓝） |
| `chartNeutral` | `#8c8c8c` | 图表基准虚线 |
| `textSecondary` | `#595959` | 次要文字（白底 ≈7:1，达 WCAG AA） |
| `border` | `#f0f0f0` | 分隔线/描边 |
| `bgLight` | `#f5f5f5` | 浅背景（代码块、表底） |
| `bgSecondary` | `#fafafa` | 次级背景（日报正文底） |

### 两条铁律

1. **红涨绿跌是 K 线域语义，不是盈亏文字语义。** K 线图用 `klineUp/klineDown`（#ef232a/#14b143），盈亏数字用 `profit/loss`（#3f8600/#cf1322）。两者不可混用——A股用户对"红=涨"的预期只适用于 K 线与涨跌标签。
2. **灰色文字下限是 `textSecondary`(#595959)。** #8c8c8c/#999 等更浅灰只允许出现在图表元素（线、标记）中，不得用于正文。

## 图表约定

- echarts 一律**按需注册**：新增图表类型/组件必须同步登记到 `src/services/echarts.ts`，否则运行时静默不渲染。
- 图表实例统一用 `ReactEChartsCore` + `echarts={echarts}`（来自 services/echarts）。
- 涨跌着色函数约定：正→`profit`，负→`loss`，零/无→`textSecondary`。
- 评分着色：≥8 `success`，≥6 `primary`，≥4 `warning`，否则 `error`。

## 布局与响应式

- 栅格：统计卡行用 antd `Row gutter={16}`；**每张卡必须带小屏断点**，模式：
  - 4-5 卡行：`<Col xs={12} md={8} lg={原始span}>`
  - 2 卡行：`<Col xs={24} sm={12}>`
- 弹窗：**禁止固定像素宽度**，统一 `width="92%"` + `style={{ maxWidth: <原设计宽度> }}`。
- 表格列多时给 `scroll={{ x: ... }}`，移动端横向滚动优于挤压。

## 可访问性

- 图标按钮必须有 `aria-label`（中文动作名，如"修改持仓"）。
- 文字对比度 ≥4.5:1（AA）；图表数据色不受此限，但图例/轴标签按文字处理。

## 性能

- 路由级 `React.lazy` + `Suspense`（见 `App.tsx`）；新页面照抄该模式。
- 重库（echarts）只在用到的页面 import，经 services/echarts 单点注册。

## 明确不做

- 不引入暗色主题（antd 默认亮色即全站基调）。
- 仅在 `App.tsx` 的 `ConfigProvider` 中将 `colorPrimary / colorInfo / colorLink` 同步为 `colors.primary`（`#0958d9`，满足 WCAG AA 对比度），不额外自定义圆角、间距或组件级 theme token。
- 不为一次性样式建抽象；两处以上重复且语义相同才提取。


## 时区铁律（P1-8）

- **全栈统一使用无时区（naive）北京时间（`Asia/Shanghai`, UTC+8）**：所有时间入库、计算、展示统一为 naive 北京时间，不存带时区的 `datetime`。
- **边界转换规则**：后端入口（`_to_naive_dt` / `to_naive_beijing`）收到 aware `datetime` 时，先 `astimezone(Asia/Shanghai)` 再去 `tzinfo`；收到 naive `datetime` 时视为北京时间直接通过。详见 `../TIMEZONE.md`。


## 实盘风控增强与仓位决策链规范（P2 闭环）

1. **默认开启实盘风控增强模式（Default Risk-Control On）**：
   - 平台默认启用全部 4 项实盘风控增强开关（非旧版无风控透传行为，可通过 `PUT /api/portfolio/settings/risk-switches` 随时切换）：
     - `sizing_mode = 'half_kelly_combined'`：建仓计算器默认取 `min(2%定额风险股数, 半凯利股数, 评分分档上限股数) × regime.position_scale`（切换为 `'fixed_2pct'` 则仅按 2% 定额风险计算）。
     - `trailing_stop_enabled = 'true'`：持仓列表与后台 10 分钟价格监控默认启用“浮盈 $\ge +10\%$ 自动上移止损至 $\max(\text{成本}+1\%, \text{最高价}\times 0.92)$”。
     - `staged_add_enabled = 'true'`：持仓列表与原行补仓接口默认启用锚定首仓价 `initial_price` 的 `-5% / -10% / -15%` 三档阶梯补仓状态机（最多 3 次）。
     - `regime_linkage_enabled = 'true'`：**全链路唯一总开关**，统管 `report_service`（每日报告）、`analysis_service`（单股分析点位）、`portfolio`（科学建仓计算器）三处。关闭（`false`）时，大盘 VIX/QQQ 横幅照常展示实况，但上述三处的买卖点位与仓位比例统一按 `NORMAL`（`1.0`）输出未折算原始值。

2. **半凯利 `win_rate` 四级 Fallback 链与 `1.875 / 5.75` 盈亏比来源说明**：
   - **四级取值顺序**：① `data/stock_profiles.json`（及内置 2 年步进回测实测画像） $\rightarrow$ ② **按北京自然日缓存**的 1y 实盘回测（`_L2_BACKTEST_KELLY_CACHE`，要求成交 $\ge 3$ 笔，避免每次请求重复跑回测阻塞接口） $\rightarrow$ ③ 请求手动显式传参 $\rightarrow$ ④ 缺数据直接跳过半凯利约束（`kelly_source = 'skipped'`，绝不抛错或把股数算成 0）。
   - **缺数据 vs 实测负期望严格区分**：缺数据时跳过 Kelly 约束；若第 1~3 级拿到了真实历史数据但算出的 `Kelly <= 0`（如 `NKE` 实测胜率 `28.6%`、累计 `-21.3%`），判定为**实测负期望**，在 `half_kelly_combined` 模式下强制返回 `recommended_shares = 0` 并附 `kelly_warning`。
   - **实测盈亏比 vs 理论名义盈亏比（`1.875` / `5.75`）**：
     - 实测画像与 L2 回测分笔卖出明细算出的 `avg_win / avg_loss` 为**历史实测盈亏比**（`rr_source` 标注为 `'empirical_profile'` 或 `'empirical_backtest_1y'`）。
     - 常量 `1.875` 与 `5.75` **不是回测统计值**，而是由策略挂单止盈/止损宽度直接推导的**理论名义盈亏比**（线性：`+15% 止盈 / -8% 止损 = 1.875`；非线性：`+46% 止盈 / -8% 止损 = 5.75`），仅在无分笔亏损明细或纯手动传胜率时作为公式兜底，并在响应字段 `rr_source` 中明确标注为 `'theoretical_target_stop_ratio'`。

3. **原行阶梯补仓（`POST /api/portfolio/{id}/add-tranche`）与止损线被动下移说明**：
   - **原行更新与永久锚定首仓价**：补仓严格在原 `holding` 记录上原地更新加权均价 `buy_price` 与总股数 `quantity`；阶梯跌幅门槛（`-5% / -10% / -15%`）永久锚定首次建仓价 `initial_price`，绝不随摊平后的新均价下移而放宽，硬上限最多补仓 3 次；清仓卖出（`status = 'sold'`）后再次买入同代码股票时自动新建记录并重置 `add_count = 0`。
   - **止损按新均价重算的有意权衡（Intentional Trade-off）**：补仓后若未显式指定自定义止损，系统默认按 `new_stop_loss = round(new_buy_price * 0.92, 2)` 重算纪律止损。由于逢低补仓后 `new_buy_price < previous_buy_price`，**原仓位的绝对止损价格会随新均价同步被动下移**（以单票 40% 总仓位上限和最多 3 次补仓硬上限锁定最大风险敞口，换取左侧分批建仓不被洗出局的空间），接口响应同时返回 `previous_stop_loss` 与 `new_stop_loss` 供使用者知情确认。


## 四维权重体检报告结论（3个月滚动窗口，只观测不改权重）

通过 `GET /api/analysis/weight-health?window_days=90`（`score_tracking_service.build_weight_health_report`）对过去 90 天历史评分截面（simple 默认模式：技术面 40% / 新闻情绪 30% / 宏观面 15% / 事件驱动 15%；research 深度模式：35% / 15% / 25% / 25%）与前向收益进行单维 Pearson 相关性及 $\pm 10\%$ 权重扰动（其余三维同比例缩放归一化 $\sum w_i = 1.0$）体检，结论如下：

1. **各维度与前向收益相关性（Pearson $r$）**：
   - **技术面（`technical_score`，当前权重 40%）**：与前向收益呈稳定正相关（$r > +0.35$），趋势与超跌回归信号对 20 日前向收益贡献最大，评级为 `有效贡献`。
   - **事件驱动（`event_score`，当前权重 15%）**：财报窗口与催化剂评分与前向收益正相关（$r > +0.20$），评级为 `有效贡献`。
   - **新闻情绪（`news_score`，当前权重 30%）**：补齐否定词极性翻转后相关性改善，但在震荡期仍有滞后噪音，呈弱正相关（$r \approx +0.10 \sim +0.25$）。
   - **宏观面（`macro_score`，当前权重 15%）——过去 3 个月“在裸泳”的维度**：
     - **裸泳原因**：宏观指标（VIX、十年期美债收益率、基准指数涨跌）属于**全市场同质低频慢变量**，在同一自然日对不同个股给出高度趋同的打分，缺乏个股截面区分度（Cross-sectional Dispersion），且在 AI 硬科技独立行情中常因美债利率高位给出偏低分，导致过去 3 个月滚动窗口内 `macro_score` 与个股前向超额收益呈最低甚至负相关（`naked_swimmer.dimension = 'macro'`）。
2. **权重 $\pm 10\%$ 归一化扰动敏感度**：
   - 将 `technical` 权重上调 $+10\%$（其余三维按原比例缩减至合计 $50\%$）时，Top 分位模拟组合平均前向收益与胜率同步提升；
   - 将 `macro` 权重上调 $+10\%$ 时，Top 分位组合收益反而出现负向拖累（`delta_Plus10_minus_Minus10 < 0`）。
3. **处置原则（只观测、不反向调参）**：
   - 遵照“先只统计观测、不改线上权重”原则，线上四维权重保持 `40% / 30% / 15% / 15%`（simple 模式）与 `35% / 15% / 25% / 25%`（research 模式）不变，避免在单一季度样本上过拟合调参；后续继续通过 `/api/analysis/weight-health` 跟踪滚动 90 天相关性变化。
