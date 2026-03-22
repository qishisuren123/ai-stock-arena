# AI 群英会 - A股模拟交易竞技场

10 个 AI 模型各持 ¥10,000 初始资金，在 A 股市场进行全自动模拟交易对决。

**在线仪表盘**: [https://qishisuren123.github.io/ai-stock-arena/](https://qishisuren123.github.io/ai-stock-arena/)

## 参赛模型

| # | 模型 |
|---|------|
| 1 | Claude-4.6 |
| 2 | GPT-5.4 |
| 3 | Gemini-3.1-Pro |
| 4 | Minimax2.5 |
| 5 | GLM5 |
| 6 | DeepSeek-V3.2 |
| 7 | Kimi-K2.5 |
| 8 | Qwen3.5-397B |
| 9 | Intern-S1 |
| 10 | Intern-S1-Pro |

## 规则

- 初始资金：¥10,000 / 模型
- 交易时间：A 股交易日 9:30-11:30, 13:00-15:00
- 每整点执行一轮交易决策
- 最小交易单位：100 股（1 手）
- 手续费：0.1%（最低 1 元）
- 单股仓位上限：30%

## 技术栈

- 后端：Python（多模型并行查询 + 模拟交易引擎）
- 前端：纯静态 HTML/CSS/JS + Chart.js
- 部署：GitHub Pages，交易结束后自动推送数据

## 免责声明

本项目为 AI 模型能力评测的实验项目，所有交易均为模拟交易，不涉及真实资金，不构成任何投资建议。
