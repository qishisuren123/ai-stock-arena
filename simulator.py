"""模拟交易引擎 - 管理虚拟账户的现金余额和持仓"""

import json
import os
from datetime import datetime

STATE_FILE = os.path.join(os.path.dirname(__file__), "sim_state.json")

# 交易参数
INITIAL_CASH = 10000.0  # 初始资金
COMMISSION_RATE = 0.00025  # 佣金费率 万2.5（主流券商费率）
MIN_COMMISSION = 5.0  # 最低佣金 5 元
STAMP_TAX_RATE = 0.0005  # 印花税 0.05%（仅卖出收取）
MIN_LOT = 100  # 最小交易单位（1手=100股）
MAX_POSITION_RATIO = 0.25  # 单股仓位上限 25%（从30%降低）


class SimAccount:
    """模拟交易账户"""

    def __init__(self, state_file=None):
        self.state_file = state_file or STATE_FILE
        self.cash = INITIAL_CASH
        # 持仓: {code: {name, qty, avg_cost, total_cost}}
        self.positions: dict[str, dict] = {}
        # 交易日志
        self.trade_log: list[dict] = []
        # 累计已实现盈亏
        self.realized_pnl = 0.0
        self._load()

    def _load(self):
        """从文件加载账户状态"""
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.cash = data.get("cash", INITIAL_CASH)
            self.positions = data.get("positions", {})
            self.trade_log = data.get("trade_log", [])
            self.realized_pnl = data.get("realized_pnl", 0.0)
        except (json.JSONDecodeError, KeyError):
            pass

    def save(self):
        """持久化账户状态到文件"""
        # 确保状态文件所在目录存在
        state_dir = os.path.dirname(self.state_file)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        data = {
            "cash": round(self.cash, 2),
            "positions": self.positions,
            "trade_log": self.trade_log,
            "realized_pnl": round(self.realized_pnl, 2),
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def total_value(self, prices: dict[str, float]) -> float:
        """计算账户总资产（现金 + 持仓市值）"""
        market_value = sum(
            prices.get(code, pos["avg_cost"]) * pos["qty"]
            for code, pos in self.positions.items()
        )
        return self.cash + market_value

    def buy(self, code: str, name: str, price: float, target_ratio: float,
            prices: dict[str, float]) -> str | None:
        """按目标仓位比例买入，返回操作描述或 None（未操作）"""
        total = self.total_value(prices)
        target_value = total * min(target_ratio, MAX_POSITION_RATIO)

        # 已有持仓的市值
        current_value = 0
        if code in self.positions:
            current_value = self.positions[code]["qty"] * price

        # 需要增加的金额
        need_value = target_value - current_value
        if need_value < price * MIN_LOT:
            return None  # 不够买 1 手，跳过

        # 计算可买股数（取整到100股）
        lots = int(need_value / (price * MIN_LOT))
        if lots <= 0:
            return None
        qty = lots * MIN_LOT
        cost = qty * price
        commission = max(cost * COMMISSION_RATE, MIN_COMMISSION)
        total_cost = cost + commission

        if total_cost > self.cash:
            # 资金不足，尽量买
            lots = int(self.cash / ((price * MIN_LOT) * (1 + COMMISSION_RATE) + MIN_COMMISSION / MIN_LOT))
            if lots <= 0:
                return None
            qty = lots * MIN_LOT
            cost = qty * price
            commission = max(cost * COMMISSION_RATE, MIN_COMMISSION)
            total_cost = cost + commission

        # 执行买入
        self.cash -= total_cost
        today = datetime.now().strftime("%Y-%m-%d")
        if code in self.positions:
            pos = self.positions[code]
            old_total = pos["avg_cost"] * pos["qty"]
            pos["qty"] += qty
            pos["avg_cost"] = round((old_total + cost) / pos["qty"], 4)
            pos["total_cost"] = round(old_total + cost, 2)
            # 加仓时更新 buy_date 为最近一次
            pos["buy_date"] = today
        else:
            self.positions[code] = {
                "name": name,
                "qty": qty,
                "avg_cost": round(price, 4),
                "total_cost": round(cost, 2),
                "buy_date": today,
            }

        # 记录日志
        msg = f"买入 {name}({code}) {qty}股 @ {price:.2f}，花费 {total_cost:.2f}（含手续费 {commission:.2f}）"
        self._log("buy", code, name, qty, price, commission)
        return msg

    def sell(self, code: str, price: float) -> str | None:
        """卖出全部持仓，返回操作描述或 None（无持仓）"""
        if code not in self.positions:
            return None

        pos = self.positions[code]
        qty = pos["qty"]
        name = pos["name"]
        income = qty * price
        commission = max(income * COMMISSION_RATE, MIN_COMMISSION)
        stamp_tax = income * STAMP_TAX_RATE  # 印花税（仅卖出）
        net_income = income - commission - stamp_tax

        # 计算盈亏
        pnl = net_income - pos["total_cost"]
        self.realized_pnl += pnl
        self.cash += net_income

        # 删除持仓
        del self.positions[code]

        pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
        msg = f"卖出 {name}({code}) {qty}股 @ {price:.2f}，盈亏 {pnl_str}（佣金 {commission:.2f} + 印花税 {stamp_tax:.2f}）"
        self._log("sell", code, name, qty, price, commission + stamp_tax, pnl)
        return msg

    def _log(self, action: str, code: str, name: str, qty: int,
             price: float, commission: float, pnl: float = 0):
        """追加交易日志"""
        self.trade_log.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "action": action,
            "code": code,
            "name": name,
            "qty": qty,
            "price": price,
            "commission": round(commission, 2),
            "pnl": round(pnl, 2),
        })

    def get_portfolio_summary(self, prices: dict[str, float] | None = None) -> str:
        """生成持仓摘要文本，供 AI 参考"""
        total = self.total_value(prices or {})
        lines = [
            f"账户总资产: {total:.2f} 元",
            f"可用现金: {self.cash:.2f} 元",
            f"累计已实现盈亏: {self.realized_pnl:+.2f} 元",
            f"总收益率: {(total - INITIAL_CASH) / INITIAL_CASH * 100:+.2f}%",
            "",
        ]
        if self.positions:
            lines.append("当前持仓:")
            for code, pos in self.positions.items():
                current_price = (prices or {}).get(code, pos["avg_cost"])
                market_val = current_price * pos["qty"]
                unrealized = market_val - pos["total_cost"]
                pct = unrealized / pos["total_cost"] * 100 if pos["total_cost"] else 0
                ratio = market_val / total * 100 if total else 0
                # 显示持仓天数
                buy_date = pos.get("buy_date", "")
                hold_days = ""
                if buy_date:
                    try:
                        bd = datetime.strptime(buy_date, "%Y-%m-%d")
                        days = (datetime.now() - bd).days
                        hold_days = f" | 持仓{days}天"
                    except ValueError:
                        pass
                lines.append(
                    f"  {pos['name']}({code}): {pos['qty']}股 | "
                    f"成本 {pos['avg_cost']:.2f} | 现价 {current_price:.2f} | "
                    f"浮盈 {unrealized:+.2f}({pct:+.1f}%) | "
                    f"仓位 {ratio:.1f}%{hold_days}"
                )
        else:
            lines.append("当前空仓")
        return "\n".join(lines)
