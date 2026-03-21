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

INITIAL_CASH = 10000.0
HISTORY_MAX = 720  # 历史记录上限（每小时1条，约30天）

# 硬编码模型列表（避免 import model_config.py 暴露敏感信息）
MODELS = [
    {"name": "Claude-Haiku",   "source": "商业API"},
    {"name": "GPT-5.4",        "source": "商业API"},
    {"name": "Gemini-3.1-Pro", "source": "商业API"},
    {"name": "Minimax2.5",     "source": "内部部署"},
    {"name": "GLM5",           "source": "内部部署"},
    {"name": "DeepSeek-V3.2",  "source": "内部部署"},
    {"name": "Kimi-K2.5",      "source": "内部部署"},
    {"name": "Qwen3.5-397B",   "source": "内部部署"},
    {"name": "Intern-S1",      "source": "内部部署"},
    {"name": "Intern-S1-Pro",  "source": "内部部署"},
]


def _safe_name(name: str) -> str:
    """将模型名称转为安全文件名（小写、替换特殊字符为下划线）"""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def load_prices() -> dict:
    """加载最新价格快照"""
    if os.path.exists(PRICES_FILE):
        with open(PRICES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def export():
    """读取所有模型状态，生成 latest.json 和追加 history.json"""
    os.makedirs(DOCS_DATA_DIR, exist_ok=True)
    prices = load_prices()
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    models_data = []
    for m in MODELS:
        safe = _safe_name(m["name"])
        state_file = os.path.join(STATES_DIR, f"sim_state_{safe}.json")

        # 默认值
        record = {
            "name": m["name"],
            "source": m["source"],
            "cash": INITIAL_CASH,
            "positions": [],
            "total_value": INITIAL_CASH,
            "return_pct": 0.0,
            "realized_pnl": 0.0,
            "trade_count": 0,
            "win_count": 0,
            "win_rate": 0.0,
            "last_update": None,
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
    latest_file = os.path.join(DOCS_DATA_DIR, "latest.json")
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=2)

    # 追加 history.json
    history_file = os.path.join(DOCS_DATA_DIR, "history.json")
    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, ValueError):
            history = []

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


if __name__ == "__main__":
    export()
