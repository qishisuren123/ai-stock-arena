# A股智能投顾助手 - 进展记录

## 2026-03-20 初始版本完成

### 完成内容
1. **环境搭建**: conda 环境 `stock`（Python 3.11），安装 anthropic、rich、akshare、httpx
2. **config.json**: API 密钥、模型名、代理配置
3. **trade_recorder.py**: 交易记录读写（JSON），支持添加/查看/盈亏计算
4. **market_data.py**: 行情数据获取（腾讯财经 API）
5. **ai_advisor.py**: Claude API 分析决策
6. **main.py**: 交互主循环，5个菜单项

### 遇到的问题及解决
- **akshare 东方财富 API 超时**: 集群网络不通东方财富域名（push2.eastmoney.com），改用腾讯财经 HTTP API（qt.gtimg.cn），通过集群代理可达
- **涨幅榜接口失效**: `rankA_hr` 返回空，改为预设 25 只主流股票列表，按涨跌幅排序
- **Anthropic SDK 520 错误**: 外部代理不稳定，弃用 SDK 改为 httpx 直接调用，加 5 次重试（间隔递增 5s/10s/15s/20s）
- **模型选择**: `claude-sonnet-4-20250514` 返回 "invalid claude code request"，改用 `claude-haiku-4-5-20251001`

### 运行方式
```bash
conda activate stock
cd /mnt/shared-storage-user/renyiming/tonghuashun
python main.py
```

## 2026-03-20 自动模拟交易模块

### 完成内容
1. **market_data.py**: 追加 `get_realtime_prices(codes)` 函数，接受纯数字代码列表返回 `{code: price}` 字典
2. **ai_advisor.py**: 追加结构化输出接口
   - `STRUCTURED_SYSTEM_PROMPT`: 要求 AI 返回 JSON 格式交易指令（analysis + actions）
   - `get_structured_advice(market_data, portfolio_info)`: 调用 API 并解析 JSON，带容错
3. **simulator.py**: 模拟交易引擎
   - `SimAccount` 类: 现金余额+持仓管理，状态持久化到 `sim_state.json`
   - 买入按目标仓位比例计算，取整到 100 股，扣手续费 0.1%
   - 卖出释放资金，计算单笔盈亏
   - 约束: 单股仓位上限 30%，最低 1 元手续费
4. **auto_trader.py**: 自动交易主循环（tmux 入口）
   - 交易时间判断（9:30-11:30, 13:00-15:00，周一至周五）
   - 每小时执行: 获取数据 → AI 分析 → 先卖后买 → 打印持仓
   - 非交易时间每 10 分钟打印心跳
   - Rich 美化输出（Panel/Table/颜色标记盈亏）
   - Ctrl+C / SIGTERM 优雅退出并保存

### 运行方式
```bash
# tmux 中运行
conda activate stock
cd /mnt/shared-storage-user/renyiming/tonghuashun
python auto_trader.py
```

## 2026-03-20 多模型对比交易系统

### 完成内容
1. **simulator.py 改造**: `SimAccount.__init__` 添加 `state_file` 参数，支持每个模型独立状态文件，默认仍为 `sim_state.json`（向后兼容）
2. **ai_advisor.py 扩展**: 追加多模型 API 调用
   - `_call_anthropic()`: Anthropic Messages 格式（Claude/GPT-5.4/Gemini 兼容）
   - `_call_openai()`: OpenAI Chat Completions 格式（pjlab 内部模型）
   - `call_model_api()`: 统一入口，按 `api_format` 分发，带 3 次重试
   - `get_structured_advice_multi()`: 多模型版结构化指令
3. **model_config.py**: 10 个模型的连接配置
   - 3 个 Anthropic 格式: Claude-Haiku, GPT-5.4, Gemini-3.1-Pro
   - 7 个 OpenAI 格式: Minimax2.5, GLM5, DeepSeek-V3.2, Kimi-K2.5, Qwen3.5-397B, Intern-S1, Intern-S1-Pro
4. **auto_trader_multi.py**: 多模型主入口
   - `ModelRunner` 类: 模型配置 + 独立 SimAccount + 本轮结果
   - 状态文件: `multi_states/sim_state_{safe_name}.json`
   - `query_all_models()`: ThreadPoolExecutor(10) 并行调用，总超时 120s
   - `execute_trades()`: 每个模型独立先卖后买
   - `print_leaderboard()`: Rich Table 排行榜（排名/模型/总资产/收益率/持仓/耗时/状态）
   - SIGINT/SIGTERM 保存所有模型状态后退出

### 运行方式
```bash
# tmux 中运行
conda activate stock
cd /mnt/shared-storage-user/renyiming/tonghuashun
python auto_trader_multi.py
```

## 2026-03-21 GitHub Pages 仪表盘

### 完成内容
（已有，此处省略具体记录）

## 2026-03-22 四大创意功能

### 完成内容
1. **模型思考过程展示**
   - `auto_trader_multi.py` 新增 `save_thinking_data()`：每轮查询后保存各模型的 analysis/actions/status 到 `_thinking.json`
   - `export_data.py` 读取并嵌入 `latest.json`（analysis 截断 500 字符）
   - 前端模型卡片底部可折叠展开"AI 思考过程"，显示分析文本 + 买卖意图标签

2. **多维度排行榜**
   - `export_data.py` 新增 `compute_advanced_metrics()`：计算最大回撤、Sharpe 比率、连胜/连败、平均持仓天数、HHI 集中度
   - 排行榜表头新增 3 列：最大回撤、Sharpe、连胜
   - 模型卡片也展示高级指标摘要

3. **自动风格画像**
   - `auto_trader_multi.py` 新增 `save_hot_codes()`：保存热门股代码到 `_hot_codes.json`
   - `export_data.py` 新增 `compute_style_tags()`：根据交易频率/热门股比例/持仓天数等生成标签（激进派/保守派/追涨型/抄底型/长线/短线/观望派 等）
   - 前端模型卡片名字下方显示彩色标签徽章

4. **Kimi 战报解说**
   - `auto_trader_multi.py` 新增 `generate_battle_report()`：硬编码 Kimi 配置，汇总各模型操作调用 Kimi 写 200 字解说
   - 保存当前战报 + 历史战报（上限 50 条）
   - `export_data.py` 嵌入最新战报 + 最近 5 条历史
   - 前端排行榜和走势图之间插入战报卡片，底部可展开历史战报

### 修改文件
- `auto_trader_multi.py`: +save_thinking_data(), +save_hot_codes(), +generate_battle_report(), 集成到 run_trading_cycle()
- `export_data.py`: +compute_advanced_metrics(), +compute_style_tags(), 读取 thinking/report/hot_codes, history 加载提前
- `docs/index.html`: +战报 section, 排行榜加 3 列
- `docs/css/style.css`: +思考过程折叠/风格标签/战报样式
- `docs/js/dashboard.js`: +renderBattleReport(), 扩展 renderLeaderboard() 和 renderModelGrid()

### 验证
- `python export_data.py` 通过，latest.json 含所有新字段
- 语法检查通过
