"""导出多模型交易数据到 docs/data/ 供 GitHub Pages 仪表盘使用

用法:
    python export_data.py
"""

import json
import os
import re
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATES_DIR = os.path.join(BASE_DIR, "multi_states")
DOCS_DATA_DIR = os.path.join(BASE_DIR, "docs", "data")
PRICES_FILE = os.path.join(STATES_DIR, "_latest_prices.json")
THINKING_FILE = os.path.join(STATES_DIR, "_thinking.json")
HOT_CODES_FILE = os.path.join(STATES_DIR, "_hot_codes.json")
BATTLE_REPORT_FILE = os.path.join(STATES_DIR, "_battle_report.json")
BATTLE_REPORTS_HIST_FILE = os.path.join(STATES_DIR, "_battle_reports_history.json")

INITIAL_CASH = 10000.0
HISTORY_MAX = 720  # 历史记录上限（每小时1条，约30天）

# 硬编码模型列表（避免 import model_config.py 暴露敏感信息）
MODELS = [
    {"name": "Claude-4.6"},
    {"name": "Gemini-3.1-Pro"},
    {"name": "Minimax2.5"},
    {"name": "GLM5"},
    {"name": "DeepSeek-V3.2"},
    {"name": "Kimi-K2.5"},
    {"name": "Qwen3.5-397B"},
    {"name": "Intern-S1"},
    {"name": "Intern-S1-Pro"},
]


def _safe_name(name: str) -> str:
    """将模型名称转为安全文件名（小写、替换特殊字符为下划线）"""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _load_json(path: str, default=None):
    """安全加载 JSON 文件"""
    if default is None:
        default = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return default


def load_prices() -> dict:
    """加载最新价格快照"""
    return _load_json(PRICES_FILE, {})


def compute_advanced_metrics(state: dict, name: str,
                             history: list, prices: dict) -> dict:
    """计算多维度高级指标：最大回撤、Sharpe、连胜/连败、平均持仓天数、持仓集中度"""
    safe = _safe_name(name)
    metrics = {
        "max_drawdown": 0.0,
        "sharpe_ratio": 0.0,
        "win_streak": 0,
        "lose_streak": 0,
        "avg_hold_days": 0.0,
        "concentration_hhi": 0.0,
    }

    # --- 最大回撤：从 history 中取该模型的 total_value 序列 ---
    values = []
    for snap in history:
        for m in snap.get("models", []):
            if m.get("name") == name:
                values.append(m.get("total_value", INITIAL_CASH))
                break
    if len(values) >= 2:
        peak = values[0]
        max_dd = 0.0
        for v in values[1:]:
            if v > peak:
                peak = v
            dd = (peak - v) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        metrics["max_drawdown"] = round(max_dd, 2)

    # --- Sharpe 比率：基于逐期收益率 ---
    if len(values) >= 3:
        returns = []
        for i in range(1, len(values)):
            if values[i - 1] > 0:
                returns.append((values[i] - values[i - 1]) / values[i - 1])
        if returns:
            mean_r = sum(returns) / len(returns)
            var_r = sum((r - mean_r) ** 2 for r in returns) / len(returns)
            std_r = var_r ** 0.5
            # 年化（假设每小时一条，交易日 4 小时，约 250 天）
            annualize = (250 * 4) ** 0.5
            metrics["sharpe_ratio"] = round(
                (mean_r / std_r * annualize) if std_r > 0 else 0, 2
            )

    # --- 连胜/连败：遍历 sell trades 的 pnl ---
    trade_log = state.get("trade_log", [])
    sell_trades = [t for t in trade_log if t.get("action") == "sell"]
    if sell_trades:
        cur_win = 0
        cur_lose = 0
        max_win = 0
        max_lose = 0
        for t in sell_trades:
            pnl = t.get("pnl", 0)
            if pnl > 0:
                cur_win += 1
                cur_lose = 0
            elif pnl < 0:
                cur_lose += 1
                cur_win = 0
            else:
                cur_win = 0
                cur_lose = 0
            max_win = max(max_win, cur_win)
            max_lose = max(max_lose, cur_lose)
        metrics["win_streak"] = max_win
        metrics["lose_streak"] = max_lose

    # --- 平均持仓天数：buy/sell 配对计算时间差 ---
    buy_times = {}  # code -> 最近 buy 时间
    hold_days_list = []
    for t in trade_log:
        code = t.get("code", "")
        ts_str = t.get("time", "")
        if not ts_str:
            continue
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if t.get("action") == "buy":
            buy_times[code] = ts
        elif t.get("action") == "sell" and code in buy_times:
            delta = (ts - buy_times[code]).total_seconds() / 86400
            hold_days_list.append(delta)
            del buy_times[code]
    if hold_days_list:
        metrics["avg_hold_days"] = round(
            sum(hold_days_list) / len(hold_days_list), 1
        )

    # --- 持仓集中度：HHI 指数 ---
    positions = state.get("positions", {})
    if positions:
        total_mv = 0.0
        mvs = []
        for code, pos in positions.items():
            price = prices.get(code, pos.get("avg_cost", 0))
            mv = price * pos.get("qty", 0)
            mvs.append(mv)
            total_mv += mv
        if total_mv > 0:
            hhi = sum((mv / total_mv * 100) ** 2 for mv in mvs)
            metrics["concentration_hhi"] = round(hhi, 0)

    return metrics


def compute_style_tags(state: dict, name: str,
                       history: list, hot_codes: list) -> list:
    """根据交易行为生成风格画像标签"""
    trade_log = state.get("trade_log", [])
    positions = state.get("positions", {})
    sell_trades = [t for t in trade_log if t.get("action") == "sell"]
    buy_trades = [t for t in trade_log if t.get("action") == "buy"]
    total_trades = len(sell_trades) + len(buy_trades)

    # 数据不足
    if total_trades < 2:
        return ["新手上路"]

    tags = []

    # 交易频率 → 激进/保守（基于历史条数和交易次数的比值）
    history_len = max(len(history), 1)
    trade_freq = total_trades / history_len
    if trade_freq > 0.5:
        tags.append("激进派")
    elif trade_freq < 0.15:
        tags.append("保守派")

    # 买入股票是否热门 → 追涨型/抄底型
    if buy_trades and hot_codes:
        hot_set = set(hot_codes)
        hot_buys = sum(1 for t in buy_trades if t.get("code") in hot_set)
        ratio = hot_buys / len(buy_trades)
        if ratio > 0.5:
            tags.append("追涨型")
        elif ratio < 0.2:
            tags.append("抄底型")

    # 平均持仓天数 → 长线/短线
    buy_times = {}
    hold_days_list = []
    for t in trade_log:
        code = t.get("code", "")
        ts_str = t.get("time", "")
        if not ts_str:
            continue
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if t.get("action") == "buy":
            buy_times[code] = ts
        elif t.get("action") == "sell" and code in buy_times:
            delta = (ts - buy_times[code]).total_seconds() / 86400
            hold_days_list.append(delta)
            del buy_times[code]
    if hold_days_list:
        avg_days = sum(hold_days_list) / len(hold_days_list)
        if avg_days >= 3:
            tags.append("长线选手")
        elif avg_days < 1:
            tags.append("短线选手")

    # 当前是否空仓
    if not positions:
        tags.append("观望派")

    # 持仓数量 → 分散/集中
    pos_count = len(positions)
    if pos_count >= 3:
        tags.append("分散持仓")
    elif pos_count == 1:
        tags.append("集中持仓")

    return tags if tags else ["稳健型"]


def export():
    """读取所有模型状态，生成 latest.json 和追加 history.json"""
    os.makedirs(DOCS_DATA_DIR, exist_ok=True)
    prices = load_prices()
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    # 预加载共享数据
    thinking_data = _load_json(THINKING_FILE, {})
    hot_codes = _load_json(HOT_CODES_FILE, [])
    battle_report_data = _load_json(BATTLE_REPORT_FILE, {})
    battle_reports_hist = _load_json(BATTLE_REPORTS_HIST_FILE, [])

    # 预加载 history（compute_advanced_metrics 和 compute_style_tags 需要）
    history_file = os.path.join(DOCS_DATA_DIR, "history.json")
    history = _load_json(history_file, [])

    models_data = []
    for m in MODELS:
        safe = _safe_name(m["name"])
        state_file = os.path.join(STATES_DIR, f"sim_state_{safe}.json")

        # 默认值
        record = {
            "name": m["name"],
            "cash": INITIAL_CASH,
            "positions": [],
            "total_value": INITIAL_CASH,
            "return_pct": 0.0,
            "realized_pnl": 0.0,
            "trade_count": 0,
            "win_count": 0,
            "win_rate": 0.0,
            "last_update": None,
            "thinking": None,
            "style_tags": ["新手上路"],
            "metrics": {},
        }

        if not os.path.exists(state_file):
            models_data.append(record)
            continue

        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        cash = state.get("cash", INITIAL_CASH)
        positions = state.get("positions", {})
        trade_log = state.get("trade_log", [])
        realized_pnl = state.get("realized_pnl", 0.0)

        # 计算持仓市值
        market_value = 0.0
        pos_list = []
        for code, pos in positions.items():
            current_price = prices.get(code, pos.get("avg_cost", 0))
            qty = pos.get("qty", 0)
            mv = current_price * qty
            market_value += mv
            cost = pos.get("total_cost", pos.get("avg_cost", 0) * qty)
            unrealized = mv - cost
            pos_list.append({
                "code": code,
                "name": pos.get("name", code),
                "qty": qty,
                "avg_cost": pos.get("avg_cost", 0),
                "current_price": round(current_price, 2),
                "market_value": round(mv, 2),
                "unrealized_pnl": round(unrealized, 2),
            })

        total_value = cash + market_value
        return_pct = (total_value - INITIAL_CASH) / INITIAL_CASH * 100

        # 统计胜率（仅计算卖出交易）
        sell_trades = [t for t in trade_log if t.get("action") == "sell"]
        trade_count = len(sell_trades)
        win_count = len([t for t in sell_trades if t.get("pnl", 0) > 0])
        win_rate = (win_count / trade_count * 100) if trade_count > 0 else 0.0

        # 思考过程
        thinking = None
        if safe in thinking_data:
            td = thinking_data[safe]
            analysis = td.get("analysis", "")
            # 截断到 500 字符
            if len(analysis) > 500:
                analysis = analysis[:500] + "..."
            thinking = {
                "analysis": analysis,
                "actions": td.get("actions", []),
                "status": td.get("status", ""),
            }

        # 高级指标
        adv_metrics = compute_advanced_metrics(state, m["name"], history, prices)

        # 风格标签
        style_tags = compute_style_tags(state, m["name"], history, hot_codes)

        record.update({
            "cash": round(cash, 2),
            "positions": pos_list,
            "total_value": round(total_value, 2),
            "return_pct": round(return_pct, 2),
            "realized_pnl": round(realized_pnl, 2),
            "trade_count": trade_count,
            "win_count": win_count,
            "win_rate": round(win_rate, 1),
            "last_update": state.get("last_update"),
            "thinking": thinking,
            "style_tags": style_tags,
            "metrics": adv_metrics,
        })
        models_data.append(record)

    # 按总资产排序
    models_data.sort(key=lambda x: x["total_value"], reverse=True)

    # 生成 latest.json
    latest = {
        "timestamp": timestamp,
        "initial_cash": INITIAL_CASH,
        "models": models_data,
    }

    # 嵌入战报数据
    if battle_report_data:
        latest["battle_report"] = battle_report_data.get("report", "")
        latest["battle_report_time"] = battle_report_data.get("timestamp", "")
    # 最近 5 条历史战报
    if battle_reports_hist:
        latest["battle_reports"] = battle_reports_hist[-5:]

    latest_file = os.path.join(DOCS_DATA_DIR, "latest.json")
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)

    # 追加 history.json
    # 每条历史记录只保存精简数据
    snapshot = {
        "timestamp": timestamp,
        "models": [
            {
                "name": m["name"],
                "total_value": m["total_value"],
                "return_pct": m["return_pct"],
            }
            for m in models_data
        ],
    }
    history.append(snapshot)

    # 裁剪到上限
    if len(history) > HISTORY_MAX:
        history = history[-HISTORY_MAX:]

    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    print(f"[export_data] 导出完成 @ {timestamp}")
    print(f"  latest.json: {len(models_data)} 个模型")
    print(f"  history.json: {len(history)} 条记录")
    print(f"  thinking: {'有' if thinking_data else '无'}")
    print(f"  battle_report: {'有' if battle_report_data else '无'}")
    print(f"  style_tags: 已计算")


if __name__ == "__main__":
    export()
