# Archive 归档目录（v1 历史脚本与废弃实现）

本目录存放已完成向 `stock-platform/backend` 收敛并正式退役的 v1 独立脚本与目录副本，保留仅供历史审计与口径溯源对比（除 `archive/` 自身外，全仓库其余模块对本目录零引用）：

1. `tech_stock_screener_v1_archived.py`
   - **原位置**：仓库根目录 `tech_stock_screener.py`
   - **归档原因**：原 GitHub Actions 在临时容器中运行后未入库；现已迁入 `stock-platform/backend/app/services/screener_service.py` 并持久化至 `WeeklyTop5Snapshot` 表。

2. `verify_nonlinear_winrate_v1_archived.py`
   - **原位置**：仓库根目录 `verify_nonlinear_winrate.py`
   - **归档原因**：原脚本存在 30/60 天 `TIME_EXIT` 超时强平虚增胜率、`RSI>=30` 时买点退化为 `MA20`、100% 满仓零佣金复利 3 处偏乐观口径；现已统一收敛至 `BacktestService.evaluate_df_profile` 严口径。

3. `utils_indicators_v1_archived.py`
   - **原位置**：仓库根目录 `utils/indicators.py`
   - **归档原因**：消除根目录与 `stock-platform/backend/app/utils/indicators.py` 的双轨重复指标实现。

4. `strategies/`（`trend_following.py`、`mean_reversion.py`、`momentum.py`、`multi_factor.py`）
   - **原位置**：仓库根目录 `strategies/`
   - **归档原因**：早期独立实验策略脚本，未被线上 FastAPI 平台（`BacktestService`）或日报引擎调用；线上 4 大回测策略与参数寻优/Walk-Forward 已统一内置于 `stock-platform/backend/app/services/backtest_service.py`。

5. `risk/`（`position_sizing.py`、`stop_loss.py`）
   - **原位置**：仓库根目录 `risk/`
   - **归档原因**：早期独立风控草稿模块；其中半凯利公式（`calculate_position_size_kelly`）与 2% 定额风险已收敛至 `position_manager.py` 与 `stock-platform/backend/app/routers/portfolio.py`，移动止损已收敛至 `stock-platform/backend/app/utils/price_levels.py::compute_trailing_stop`。

6. `position_plan_v2.py`
   - **原位置**：仓库根目录 `position_plan_v2.py`
   - **归档原因**：早期一次性静态打印脚本（硬编码特定日期买点），其 `-5% / -10% / -15%` 三档分批补仓与首仓永久锚定逻辑已收敛至 `position_manager.py` 与 `POST /api/portfolio/{id}/add-tranche`。

7. `legacy_root_scripts/`（`backtest_comparison.py`、`backtest_hybrid.py`、`calculate_nonlinear_points.py`、`compare_versions.py`）
   - **原位置**：仓库根目录
   - **归档原因**：早期命令行对比实验与静态买点测算脚本，核心算法已全部收敛至 `stock-platform/backend/app/services/backtest_service.py` 与 `app/utils/price_levels.py`。

8. `legacy_backend_manual_tests/`（8 个早期手动 `print` 调试脚本 `test_*.py`）
   - **原位置**：`stock-platform/backend/test_*.py`
   - **归档原因**：早期无 `assert` 的手动联网打印脚本，现已由 `stock-platform/backend/tests/` 下基于 `pytest` 的 243 个自动化回归测试全面替代。

9. `stock_analysis_v1_archived.py`
   - **原位置**：仓库根目录 `utils/stock_analysis.py`（1081行 v1 旧实现）
   - **归档原因**：全套四维分析服务已收敛至 `stock-platform/backend/app/services/analysis_service.py`，根目录 `utils/stock_analysis.py` 已转为轻量向后兼容垫片。

10. `visualization_v1_archived.py`
   - **原位置**：仓库根目录 `utils/visualization.py`
   - **归档原因**：早期 Matplotlib 静态绘图模块，平台已全面升级为 React + ECharts 交互式图表系统，全仓库除自身外零引用。
