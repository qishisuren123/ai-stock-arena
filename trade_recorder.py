"""交易记录模块 - 读写 JSON 格式的交易历史"""

import json
import os
from datetime import datetime

TRADES_FILE = os.path.join(os.path.dirname(__file__), "trades.json")


def _load() -> dict:
    """加载交易记录"""
    if not os.path.exists(TRADES_FILE):
        return {"trades": []}
    with open(TRADES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict):
    """保存交易记录"""
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_trade(code: str, name: str, action: str, price: float,
              quantity: int, reason: str = "") -> dict:
    """添加一条交易记录"""
    now = datetime.now()
    trade = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "code": code,
        "name": name,
        "action": action,  # buy / sell / hold
        "price": price,
        "quantity": quantity,
        "reason": reason,
    }
    data = _load()
    data["trades"].append(trade)
    _save(data)
    return trade


def get_trades() -> list:
    """获取所有交易记录"""
    return _load()["trades"]


def calc_pnl() -> list:
    """按股票代码计算简单盈亏（先进先出）"""
    trades = get_trades()
    # 按股票分组
    holdings: dict[str, list] = {}
    results = []
    for t in trades:
        code = t["code"]
        if code not in holdings:
            holdings[code] = {"name": t["name"], "buys": [], "sells": []}
        if t["action"] == "buy":
            holdings[code]["buys"].append(t)
        elif t["action"] == "sell":
            holdings[code]["sells"].append(t)

    for code, info in holdings.items():
        total_buy_cost = sum(b["price"] * b["quantity"] for b in info["buys"])
        total_buy_qty = sum(b["quantity"] for b in info["buys"])
        total_sell_income = sum(s["price"] * s["quantity"] for s in info["sells"])
        total_sell_qty = sum(s["quantity"] for s in info["sells"])
        holding_qty = total_buy_qty - total_sell_qty
        avg_cost = total_buy_cost / total_buy_qty if total_buy_qty else 0
        realized_pnl = total_sell_income - avg_cost * total_sell_qty if total_sell_qty else 0
        results.append({
            "code": code,
            "name": info["name"],
            "avg_cost": round(avg_cost, 2),
            "holding_qty": holding_qty,
            "realized_pnl": round(realized_pnl, 2),
        })
    return results
