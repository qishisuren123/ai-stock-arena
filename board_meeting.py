"""AI 股票董事会 - 9 个模型共同管理公共基金账户

核心机制：
  1. 提案：复用各模型个人账户的 advice，零额外 API 调用
  2. 投票：9 模型并行投票（加权），超过 50% 通过
  3. 进化：根据交易结果更新基因组（影响力/准确率）
  4. GEP 兼容：输出 genes.json / capsules.json / events.jsonl
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import ai_advisor
from simulator import SimAccount, INITIAL_CASH

console = Console()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATES_DIR = os.path.join(BASE_DIR, "multi_states")

# GEP 兼容数据文件路径
GENES_FILE = os.path.join(STATES_DIR, "board_genes.json")
CAPSULES_FILE = os.path.join(STATES_DIR, "board_capsules.json")
EVENTS_FILE = os.path.join(STATES_DIR, "board_events.jsonl")
BOARD_STATE_FILE = os.path.join(STATES_DIR, "sim_state_board_fund.json")
BOARD_RULES_FILE = os.path.join(STATES_DIR, "board_rules.json")

# 影响力范围（默认值，运行时从 BoardRuleset 读取）
INFLUENCE_MIN = 0.3
INFLUENCE_MAX = 3.0


# ============================================================
# 董事会规则集 — 可进化的参数
# ============================================================

class BoardRuleset:
    """持久化的董事会规则集，所有原本硬编码的参数都从这里读取"""

    DEFAULTS = {
        "generation": 0,
        "pass_threshold": 0.50,
        "proposal_weight": 1.0,
        "vote_weight": 0.5,
        "co_proposal_bonus": 0.0,
        "max_position_ratio": 0.30,
        "max_positions": 3,
        "influence_range": [0.3, 3.0],
        "recent_trades": [],
        "fitness": 0.0,
        "fitness_prev": 0.0,
        "trades_since_evolution": 0,
        "evolution_interval": 5,
        "amendment_interval": 20,
        "trades_since_amendment": 0,
        "rule_history": [],
    }

    # 参数钳位范围
    CLAMP = {
        "pass_threshold": (0.30, 0.80),
        "proposal_weight": (0.3, 3.0),
        "vote_weight": (0.1, 2.0),
        "co_proposal_bonus": (0.0, 0.15),
        "max_position_ratio": (0.10, 0.50),
        "max_positions": (1, 5),
    }

    def __init__(self, path: str = BOARD_RULES_FILE):
        self._path = path
        self._data = dict(self.DEFAULTS)
        # 深拷贝列表类型默认值
        self._data["recent_trades"] = []
        self._data["influence_range"] = [0.3, 3.0]
        self._data["rule_history"] = []
        self._load()

    def _load(self):
        """从文件加载规则，不存在则用默认值"""
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    self._data[k] = v
            except (json.JSONDecodeError, ValueError):
                pass

    def save(self):
        """持久化到文件"""
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._data:
            return self._data[name]
        raise AttributeError(f"BoardRuleset has no attribute '{name}'")

    def _clamp(self, key: str, value):
        """将参数钳位到合法范围"""
        if key in self.CLAMP:
            lo, hi = self.CLAMP[key]
            if isinstance(value, float):
                return round(max(lo, min(hi, value)), 4)
            return max(lo, min(hi, value))
        return value

    def record_trade(self, pnl: float, cost: float):
        """记录一笔交易结果，递增计数器"""
        self._data["recent_trades"].append({
            "pnl": round(pnl, 2),
            "cost": round(cost, 2),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        # 只保留最近 50 笔
        if len(self._data["recent_trades"]) > 50:
            self._data["recent_trades"] = self._data["recent_trades"][-50:]
        self._data["trades_since_evolution"] += 1
        self._data["trades_since_amendment"] += 1
        self.save()

    def should_auto_evolve(self) -> bool:
        """是否触发绩效反馈自适应"""
        return self._data["trades_since_evolution"] >= self._data["evolution_interval"]

    def auto_evolve(self):
        """轨道1：绩效反馈自适应 — 计算 fitness，微调参数"""
        trades = self._data["recent_trades"]
        if not trades:
            return

        # 计算适应度
        total = len(trades)
        profitable = sum(1 for t in trades if t["pnl"] > 0)
        win_rate = profitable / total if total > 0 else 0
        pnl_pcts = [t["pnl"] / max(abs(t["cost"]), 1) for t in trades]
        avg_pnl_pct = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0
        fitness = win_rate * (1 + avg_pnl_pct)

        prev_fitness = self._data["fitness"]
        self._data["fitness_prev"] = prev_fitness
        self._data["fitness"] = round(fitness, 4)

        changes = {}
        if fitness < prev_fitness:
            # fitness 下降 → 收紧
            changes["pass_threshold"] = self._data["pass_threshold"] + 0.03
            changes["max_position_ratio"] = self._data["max_position_ratio"] - 0.02
        elif fitness > prev_fitness:
            # fitness 上升 → 放宽
            changes["pass_threshold"] = self._data["pass_threshold"] - 0.02
            changes["co_proposal_bonus"] = self._data["co_proposal_bonus"] + 0.01
        else:
            # 持平 → 微调权重
            changes["proposal_weight"] = self._data["proposal_weight"] + 0.05
            changes["vote_weight"] = self._data["vote_weight"] - 0.02

        # 应用钳位后的变更
        for k, v in changes.items():
            self._data[k] = self._clamp(k, v)

        self._data["generation"] += 1
        self._data["trades_since_evolution"] = 0

        # 记录 rule_history
        self._data["rule_history"].append({
            "generation": self._data["generation"],
            "trigger": "auto",
            "fitness": round(fitness, 4),
            "changes": {k: round(self._data[k], 4) if isinstance(self._data[k], float) else self._data[k] for k in changes},
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        if len(self._data["rule_history"]) > 50:
            self._data["rule_history"] = self._data["rule_history"][-50:]

        self.save()

        # 追加进化事件
        append_event({
            "type": "RuleEvolution",
            "trigger": "auto",
            "generation": self._data["generation"],
            "fitness": round(fitness, 4),
            "changes": changes,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

        console.print(
            f"  [bold #10b981]规则自适应[/bold #10b981] "
            f"G{self._data['generation']} fitness={fitness:.3f} "
            f"调整: {', '.join(f'{k}={self._data[k]}' for k in changes)}"
        )

        return changes

    def should_amend(self, pending_changes: dict = None) -> bool:
        """是否触发修宪投票"""
        if self._data["trades_since_amendment"] >= self._data["amendment_interval"]:
            return True
        if pending_changes:
            for k, v in pending_changes.items():
                current = self._data.get(k, 0)
                if current != 0 and abs(v - current) / abs(current) >= 0.15:
                    return True
        return False

    def generate_amendments(self) -> list:
        """基于数据分析生成修宪提案列表"""
        proposals = []
        trades = self._data["recent_trades"]
        if not trades:
            return proposals

        total = len(trades)
        profitable = sum(1 for t in trades if t["pnl"] > 0)
        win_rate = profitable / total if total > 0 else 0

        # 提案1：根据胜率调整 pass_threshold
        if win_rate < 0.3:
            proposals.append({
                "description": f"胜率仅 {win_rate:.0%}，建议提高通过门槛至 {min(0.8, self._data['pass_threshold'] + 0.05):.2f}",
                "changes": {"pass_threshold": self._clamp("pass_threshold", self._data["pass_threshold"] + 0.05)},
            })
        elif win_rate > 0.7:
            proposals.append({
                "description": f"胜率高达 {win_rate:.0%}，可适当降低门槛至 {max(0.3, self._data['pass_threshold'] - 0.05):.2f}",
                "changes": {"pass_threshold": self._clamp("pass_threshold", self._data["pass_threshold"] - 0.05)},
            })

        # 提案2：根据亏损幅度调整仓位上限
        losses = [t for t in trades if t["pnl"] < 0]
        if losses:
            avg_loss_pct = sum(t["pnl"] / max(abs(t["cost"]), 1) for t in losses) / len(losses)
            if avg_loss_pct < -0.05:
                proposals.append({
                    "description": f"平均亏损 {avg_loss_pct:.1%}，建议降低仓位上限至 {max(0.1, self._data['max_position_ratio'] - 0.05):.2f}",
                    "changes": {"max_position_ratio": self._clamp("max_position_ratio", self._data["max_position_ratio"] - 0.05)},
                })

        # 提案3：调整投票/提案权重平衡
        if self._data["proposal_weight"] > self._data["vote_weight"] * 3:
            proposals.append({
                "description": "提案权重过高，建议平衡投票权重",
                "changes": {
                    "proposal_weight": self._clamp("proposal_weight", self._data["proposal_weight"] - 0.1),
                    "vote_weight": self._clamp("vote_weight", self._data["vote_weight"] + 0.1),
                },
            })

        return proposals[:3]

    def apply_amendment(self, changes: dict):
        """应用修宪结果"""
        for k, v in changes.items():
            if k in self._data:
                self._data[k] = self._clamp(k, v)

        self._data["generation"] += 1
        self._data["trades_since_amendment"] = 0

        self._data["rule_history"].append({
            "generation": self._data["generation"],
            "trigger": "amendment",
            "changes": {k: round(v, 4) if isinstance(v, float) else v for k, v in changes.items()},
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        if len(self._data["rule_history"]) > 50:
            self._data["rule_history"] = self._data["rule_history"][-50:]

        self.save()

        append_event({
            "type": "RuleEvolution",
            "trigger": "amendment",
            "generation": self._data["generation"],
            "changes": changes,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

        console.print(
            f"  [bold #d4a017]修宪通过[/bold #d4a017] "
            f"G{self._data['generation']} "
            f"变更: {', '.join(f'{k}={self._data[k]}' for k in changes)}"
        )


# ============================================================
# 基因组管理
# ============================================================

def _default_genome(model_name: str) -> dict:
    """创建默认基因组"""
    return {
        "model_name": model_name,
        "influence": 1.0,
        "proposal_accuracy": 0.0,
        "vote_accuracy": 0.0,
        "proposals_total": 0,
        "proposals_profitable": 0,
        "votes_correct": 0,
        "votes_total": 0,
        "generation": 0,
        "personality": {
            "risk_tolerance": 0.5,
            "creativity": 0.5,
            "obedience": 0.8,
        },
    }


def load_genomes() -> dict:
    """加载基因组，返回 {model_name: genome}"""
    if not os.path.exists(GENES_FILE):
        return {}
    try:
        with open(GENES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {g["model"]: g for g in data.get("genes", [])}
    except (json.JSONDecodeError, KeyError):
        return {}


def save_genomes(genomes: dict):
    """保存基因组为 GEP 兼容格式"""
    genes_list = []
    for name, g in genomes.items():
        safe = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        genes_list.append({
            "type": "Gene",
            "id": f"gene_board_{safe}",
            "model": name,
            "influence": round(g.get("influence", 1.0), 3),
            "proposal_accuracy": round(g.get("proposal_accuracy", 0.0), 3),
            "vote_accuracy": round(g.get("vote_accuracy", 0.0), 3),
            "proposals_total": g.get("proposals_total", 0),
            "proposals_profitable": g.get("proposals_profitable", 0),
            "votes_correct": g.get("votes_correct", 0),
            "votes_total": g.get("votes_total", 0),
            "generation": g.get("generation", 0),
            "personality": g.get("personality", {}),
        })
    data = {"version": 1, "genes": genes_list}
    with open(GENES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# Capsule / Event 管理
# ============================================================

def save_capsule(capsule: dict):
    """追加一个成功策略 Capsule"""
    capsules = {"version": 1, "capsules": []}
    if os.path.exists(CAPSULES_FILE):
        try:
            with open(CAPSULES_FILE, "r", encoding="utf-8") as f:
                capsules = json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    capsules["capsules"].append(capsule)
    # 保留最近 100 个
    if len(capsules["capsules"]) > 100:
        capsules["capsules"] = capsules["capsules"][-100:]
    with open(CAPSULES_FILE, "w", encoding="utf-8") as f:
        json.dump(capsules, f, ensure_ascii=False, indent=2)


def append_event(event: dict):
    """追加一行进化事件到 events.jsonl"""
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ============================================================
# BoardFund 类 — 公共基金账户
# ============================================================

class BoardFund(SimAccount):
    """董事会公共基金账户，继承 SimAccount 并扩展 pending_trades"""

    def __init__(self):
        super().__init__(state_file=BOARD_STATE_FILE)
        # pending_trades: 记录每笔买入的归因信息
        self.pending_trades: dict = {}
        # 最近一轮决议记录（供导出）
        self.last_decisions: list = []
        self._load_extra()

    def _load_extra(self):
        """加载额外的 pending_trades 数据"""
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.pending_trades = data.get("pending_trades", {})
            self.last_decisions = data.get("last_decisions", [])
        except (json.JSONDecodeError, KeyError):
            pass

    def save(self):
        """保存状态（含 pending_trades）"""
        state_dir = os.path.dirname(self.state_file)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        data = {
            "cash": round(self.cash, 2),
            "positions": self.positions,
            "trade_log": self.trade_log,
            "realized_pnl": round(self.realized_pnl, 2),
            "pending_trades": self.pending_trades,
            "last_decisions": self.last_decisions,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def buy_with_attribution(self, code: str, name: str, price: float,
                             ratio: float, prices: dict,
                             proposer: str, vote_score: float,
                             voters_approve: list, voters_reject: list) -> str | None:
        """买入并记录归因信息"""
        result = self.buy(code, name, price, ratio, prices)
        if result:
            self.pending_trades[code] = {
                "proposer": proposer,
                "buy_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "vote_score": round(vote_score, 3),
                "voters_approve": voters_approve,
                "voters_reject": voters_reject,
            }
        return result

    def sell_with_evolution(self, code: str, price: float,
                           genomes: dict, market_summary: str = "",
                           ruleset: "BoardRuleset" = None) -> str | None:
        """卖出并触发进化更新"""
        if code not in self.positions:
            return None

        # 获取归因信息
        attribution = self.pending_trades.pop(code, None)

        # 计算盈亏（卖出前）— 使用与 simulator.py 一致的佣金
        pos = self.positions[code]
        income = pos["qty"] * price
        commission = max(income * 0.00025, 5.0)  # 万2.5 最低5元
        stamp_tax = income * 0.0005  # 印花税 0.05%
        net_income = income - commission - stamp_tax
        pnl = net_income - pos["total_cost"]
        cost = pos["total_cost"]
        profitable = pnl > 0

        # 执行卖出
        result = self.sell(code, price)

        # 进化更新
        if attribution and result:
            evolve_genomes(genomes, attribution, profitable, pnl, market_summary,
                           ruleset=ruleset)
            # 记录交易到 ruleset
            if ruleset:
                ruleset.record_trade(pnl, cost)

        return result


# ============================================================
# 投票逻辑
# ============================================================

VOTE_SYSTEM_PROMPT = """你是 AI 股票董事会的一位董事成员，正在参与投票。

你需要对每个交易提案投票（approve 或 reject），并给出简短理由。

你必须严格以 JSON 格式返回，不要输出任何其他内容（不要 markdown 代码块标记）。

JSON 格式：
[
    {"proposal_id": "prop_001", "vote": "approve", "reason": "一句话理由"},
    {"proposal_id": "prop_002", "vote": "reject", "reason": "一句话理由"}
]

投票原则：
1. 独立思考，基于市场数据和公共基金状况做判断
2. 倾向于保守和谨慎。宁可错过机会，也不要亏损。默认倾向投 reject。
3. 强烈反对以下行为（必须投 reject）：
   - 买入后不足 3 个交易日就卖出（频繁交易）
   - 买入已经连续涨 3 天以上的股票（追涨）
   - 单笔仓位超过 25%（过度集中）
   - 买入市值低于 100 亿的小盘股或概念炒作股（历史证明小盘股贡献了 98% 的亏损）
   - 买入当日涨幅超过 5% 的股票（可能追高被套）
   - 没有明确理由的交易提案
4. 支持的行为：
   - 触发止损（亏损 > 5%）的卖出提案
   - 买入沪深300/中证500成分股，有清晰基本面依据
   - 适度分散的持仓（2-3只，每只 10-20%）
   - 银行、电力、消费等稳健蓝筹股
5. 注意每笔交易的真实成本：佣金最低 5 元 + 卖出印花税，小额交易不划算
6. 公共基金目标：稳健增值，月度收益 1-3%，最大回撤 < 5%"""


def _collect_proposals(runners) -> list:
    """从各模型个人交易的 advice 中收集提案，并过滤反模式"""
    proposals = []
    seen_actions = {}  # (code, action) -> proposal，用于去重

    # 获取公共基金持仓信息（用于持仓期检查）
    fund_positions = board_fund.positions
    # 获取最近交易记录（用于防止刚卖又买）
    recent_sells = set()
    for log in board_fund.trade_log[-10:]:
        if log.get("action") == "sell":
            recent_sells.add(log.get("code", ""))

    for r in runners:
        if not r.advice:
            continue
        actions = r.advice.get("actions", [])
        for act in actions:
            code = act.get("code", "")
            action = act.get("action", "")
            if not code or action not in ("buy", "sell"):
                continue

            # --- 反模式过滤 ---
            # 1. 卖出检查：持仓不足 3 天不允许卖出（除非止损）
            if action == "sell" and code in fund_positions:
                pos = fund_positions[code]
                buy_date = pos.get("buy_date", "")
                if buy_date:
                    try:
                        from datetime import datetime as _dt
                        bd = _dt.strptime(buy_date, "%Y-%m-%d")
                        hold_days = (_dt.now() - bd).days
                        if hold_days < 3:
                            # 检查是否为止损（亏损 > 5%）
                            avg_cost = pos.get("avg_cost", 0)
                            # 粗略判断（没有实时价格时跳过过滤）
                            console.print(
                                f"  [dim]过滤: {r.name} 提议卖出 {code}，"
                                f"持仓仅 {hold_days} 天，不足 3 天最低持仓期[/dim]"
                            )
                            continue
                    except (ValueError, ImportError):
                        pass

            # 2. 买入检查：刚卖出的股票不能立即买回
            if action == "buy" and code in recent_sells:
                console.print(
                    f"  [dim]过滤: {r.name} 提议买入 {code}，"
                    f"该股票近期刚被卖出，避免反复交易[/dim]"
                )
                continue

            key = (code, action)
            if key in seen_actions:
                # 多个模型推荐同一操作，记录共同推荐
                existing = seen_actions[key]
                existing["co_proposers"].append(r.name)
                continue

            prop_id = f"prop_{len(proposals) + 1:03d}"
            proposal = {
                "proposal_id": prop_id,
                "proposer": r.name,
                "co_proposers": [],
                "code": code,
                "name": act.get("name", code),
                "action": action,
                "ratio": min(act.get("ratio", 0.2), 0.25),  # 强制上限 25%
                "reasoning": r.advice.get("analysis", "")[:200],
            }
            proposals.append(proposal)
            seen_actions[key] = proposal

    return proposals


def _build_vote_prompt(proposals: list, fund: BoardFund, prices: dict) -> str:
    """构造投票 prompt，包含基金状态、最近交易记录和提案"""
    # 公共基金状态
    fund_summary = fund.get_portfolio_summary(prices)

    # 最近交易记录（帮助模型了解持仓期和交易频率）
    trade_history_lines = []
    for log in fund.trade_log[-8:]:
        action_cn = "买入" if log.get("action") == "buy" else "卖出"
        pnl_text = f"，盈亏{log.get('pnl', 0):+.2f}" if log.get("action") == "sell" else ""
        trade_history_lines.append(
            f"  {log.get('time','')} {action_cn} {log.get('name','')}({log.get('code','')}) "
            f"{log.get('qty',0)}股 @ {log.get('price',0):.2f}{pnl_text}"
        )
    trade_history = "\n".join(trade_history_lines) if trade_history_lines else "暂无交易记录"

    # 提案列表（隐藏提案者，避免偏见）
    prop_lines = []
    for p in proposals:
        co_count = len(p.get("co_proposers", []))
        co_text = f"（{co_count + 1} 位董事共同推荐）" if co_count > 0 else ""
        prop_lines.append(
            f"- {p['proposal_id']}: {p['action'].upper()} {p['name']}({p['code']}) "
            f"仓位比例 {p['ratio']:.0%}{co_text}\n"
            f"  理由: {p['reasoning']}"
        )
    proposals_text = "\n".join(prop_lines)

    return (
        f"【公共基金状态】\n{fund_summary}\n\n"
        f"【最近交易记录】\n{trade_history}\n\n"
        f"【待投票提案】\n{proposals_text}\n\n"
        f"请对以上每个提案投票（纯 JSON 数组）。记住：倾向保守，默认 reject。"
    )


def _query_single_vote(runner, vote_prompt: str) -> tuple:
    """查询单个模型的投票结果"""
    try:
        raw = ai_advisor.call_model_api(
            runner.cfg, VOTE_SYSTEM_PROMPT, vote_prompt, max_retries=2
        )
        if not raw:
            return runner.name, []

        # 清理输出
        text = re.sub(r"<think>[\s\S]*?</think>\s*", "", raw).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        votes = json.loads(text)
        if not isinstance(votes, list):
            return runner.name, []
        return runner.name, votes
    except Exception as e:
        console.print(f"  [dim]{runner.name} 投票解析失败: {e}[/dim]")
        return runner.name, []


def _vote_all(runners, vote_prompt: str) -> dict:
    """并行收集所有模型的投票"""
    all_votes = {}  # model_name -> [vote_records]
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_query_single_vote, r, vote_prompt): r
            for r in runners
        }
        try:
            for future in as_completed(futures, timeout=120):
                try:
                    name, votes = future.result()
                    all_votes[name] = votes
                except Exception:
                    r = futures[future]
                    all_votes[r.name] = []
        except TimeoutError:
            for future, r in futures.items():
                if not future.done():
                    all_votes[r.name] = []
                    console.print(f"  [yellow]{r.name} 投票超时[/yellow]")

    return all_votes


def _tally_votes(proposals: list, all_votes: dict, genomes: dict,
                  ruleset: "BoardRuleset" = None) -> list:
    """计票，返回带得票结果的提案列表。使用 ruleset 中的 pass_threshold 和 co_proposal_bonus"""
    results = []
    threshold = ruleset.pass_threshold if ruleset else 0.5
    co_bonus = ruleset.co_proposal_bonus if ruleset else 0.0

    for prop in proposals:
        pid = prop["proposal_id"]
        approve_weight = 0.0
        reject_weight = 0.0
        voters_approve = []
        voters_reject = []

        for model_name, votes in all_votes.items():
            genome = genomes.get(model_name, _default_genome(model_name))
            influence = genome.get("influence", 1.0)

            vote_record = None
            for v in votes:
                if v.get("proposal_id") == pid:
                    vote_record = v
                    break

            if vote_record:
                if vote_record.get("vote") == "approve":
                    approve_weight += influence
                    voters_approve.append(model_name)
                else:
                    reject_weight += influence
                    voters_reject.append(model_name)

        # 共同推荐加分
        co_count = len(prop.get("co_proposers", []))
        if co_count > 0 and co_bonus > 0:
            approve_weight += co_count * co_bonus

        total_weight = approve_weight + reject_weight
        score = approve_weight / total_weight if total_weight > 0 else 0
        approved = score > threshold

        result = dict(prop)
        result["vote_score"] = round(score, 3)
        result["approved"] = approved
        result["voters_approve"] = voters_approve
        result["voters_reject"] = voters_reject
        results.append(result)

    return results


# ============================================================
# 进化引擎
# ============================================================

def evolve_genomes(genomes: dict, attribution: dict, profitable: bool,
                   pnl: float, market_summary: str = "",
                   ruleset: "BoardRuleset" = None):
    """根据交易结果更新基因组，使用 ruleset 中的 proposal_weight/vote_weight/influence_range"""
    proposer = attribution.get("proposer", "")
    voters_approve = attribution.get("voters_approve", [])
    voters_reject = attribution.get("voters_reject", [])

    # 从 ruleset 读取权重参数
    prop_w = ruleset.proposal_weight if ruleset else 1.0
    vote_w = ruleset.vote_weight if ruleset else 0.5
    inf_range = ruleset.influence_range if ruleset else [INFLUENCE_MIN, INFLUENCE_MAX]
    inf_min, inf_max = inf_range[0], inf_range[1]

    # 更新提案者的 proposal_accuracy
    if proposer in genomes:
        g = genomes[proposer]
        g["proposals_total"] = g.get("proposals_total", 0) + 1
        if profitable:
            g["proposals_profitable"] = g.get("proposals_profitable", 0) + 1

    # 更新投票者的 vote_accuracy
    for voter in voters_approve:
        if voter in genomes:
            g = genomes[voter]
            g["votes_total"] = g.get("votes_total", 0) + 1
            if profitable:
                g["votes_correct"] = g.get("votes_correct", 0) + 1

    for voter in voters_reject:
        if voter in genomes:
            g = genomes[voter]
            g["votes_total"] = g.get("votes_total", 0) + 1
            if not profitable:
                # 正确的反对也算
                g["votes_correct"] = g.get("votes_correct", 0) + 1

    # 重算所有参与者的影响力（使用 ruleset 权重）
    involved = set([proposer] + voters_approve + voters_reject)
    for name in involved:
        if name not in genomes:
            continue
        g = genomes[name]
        accuracy = g.get("proposals_profitable", 0) / max(g.get("proposals_total", 0), 1)
        vote_acc = g.get("votes_correct", 0) / max(g.get("votes_total", 0), 1)
        influence = 1.0 + accuracy * prop_w + vote_acc * vote_w
        g["influence"] = round(max(inf_min, min(inf_max, influence)), 3)
        g["proposal_accuracy"] = round(accuracy, 3)
        g["vote_accuracy"] = round(vote_acc, 3)
        g["generation"] = g.get("generation", 0) + 1

    # 保存基因组
    save_genomes(genomes)

    # 创建 Capsule（成功策略）或 Event
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    if profitable:
        capsule_id = f"cap_{datetime.now().strftime('%Y%m%d')}_{int(time.time()) % 10000:04d}"
        capsule = {
            "type": "Capsule",
            "id": capsule_id,
            "proposer": proposer,
            "proposal": {
                "code": attribution.get("code", ""),
                "action": "buy",
                "vote_score": attribution.get("vote_score", 0),
            },
            "outcome": {"pnl": round(pnl, 2), "status": "profitable"},
            "context_summary": market_summary[:200] if market_summary else "",
            "timestamp": now_str,
        }
        save_capsule(capsule)

    # 追加事件
    event = {
        "type": "EvolutionEvent",
        "timestamp": now_str,
        "proposer": proposer,
        "profitable": profitable,
        "pnl": round(pnl, 2),
        "voters_approve": voters_approve,
        "voters_reject": voters_reject,
        "genome_changes": {
            name: {
                "influence": genomes[name].get("influence", 1.0),
                "generation": genomes[name].get("generation", 0),
            }
            for name in involved if name in genomes
        },
    }
    append_event(event)


# ============================================================
# 主流程: conduct_board_meeting
# ============================================================

# 全局单例
board_fund = BoardFund()
board_ruleset = BoardRuleset()


# ============================================================
# 修宪投票
# ============================================================

AMENDMENT_SYSTEM_PROMPT = """你是 AI 股票董事会的一位董事成员，正在参与修宪投票。

你需要对每个规则修改提案投票（approve 或 reject），并给出简短理由。

你必须严格以 JSON 格式返回，不要输出任何其他内容（不要 markdown 代码块标记）。

JSON 格式：
[
    {"proposal_id": "amend_001", "vote": "approve", "reason": "一句话理由"},
    {"proposal_id": "amend_002", "vote": "reject", "reason": "一句话理由"}
]

投票原则：
1. 审慎判断规则变更的合理性
2. 考虑变更对基金整体运作的影响
3. 过于激进的变更应投反对票
4. 基于绩效数据做出判断"""


def conduct_rule_amendment(runners, ruleset: BoardRuleset):
    """执行修宪投票流程"""
    amendments = ruleset.generate_amendments()
    if not amendments:
        console.print("  [dim]无修宪提案[/dim]")
        return

    console.print(f"  [bold #d4a017]修宪投票[/bold #d4a017] 共 {len(amendments)} 条提案")

    # 构造投票 prompt
    prop_lines = []
    for i, a in enumerate(amendments):
        pid = f"amend_{i + 1:03d}"
        a["proposal_id"] = pid
        changes_text = ", ".join(f"{k}={v}" for k, v in a["changes"].items())
        prop_lines.append(f"- {pid}: {a['description']}\n  具体变更: {changes_text}")

    current_rules = (
        f"pass_threshold={ruleset.pass_threshold}, "
        f"proposal_weight={ruleset.proposal_weight}, "
        f"vote_weight={ruleset.vote_weight}, "
        f"co_proposal_bonus={ruleset.co_proposal_bonus}, "
        f"max_position_ratio={ruleset.max_position_ratio}"
    )

    vote_prompt = (
        f"【当前规则】\n{current_rules}\n"
        f"【当前适应度】fitness={ruleset.fitness:.4f}\n"
        f"【规则代数】G{ruleset.generation}\n\n"
        f"【修宪提案】\n" + "\n".join(prop_lines) + "\n\n"
        f"请对以上每个提案投票（纯 JSON 数组）。"
    )

    # 并行投票（复用投票框架）
    all_votes = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_query_single_vote_amendment, r, vote_prompt): r
            for r in runners
        }
        try:
            for future in as_completed(futures, timeout=120):
                try:
                    name, votes = future.result()
                    all_votes[name] = votes
                except Exception:
                    r = futures[future]
                    all_votes[r.name] = []
        except TimeoutError:
            pass

    # 计票（用 pass_threshold 自身作门槛）
    genomes = load_genomes()
    for a in amendments:
        pid = a["proposal_id"]
        approve_w = 0.0
        reject_w = 0.0
        for model_name, votes in all_votes.items():
            genome = genomes.get(model_name, _default_genome(model_name))
            influence = genome.get("influence", 1.0)
            for v in votes:
                if v.get("proposal_id") == pid:
                    if v.get("vote") == "approve":
                        approve_w += influence
                    else:
                        reject_w += influence
                    break

        total_w = approve_w + reject_w
        score = approve_w / total_w if total_w > 0 else 0
        passed = score > ruleset.pass_threshold

        if passed:
            ruleset.apply_amendment(a["changes"])
            console.print(
                f"    [{pid}] [bold green]通过[/bold green] "
                f"({score:.0%}) {a['description']}"
            )
        else:
            console.print(
                f"    [{pid}] [dim]否决[/dim] "
                f"({score:.0%}) {a['description']}"
            )


def _query_single_vote_amendment(runner, vote_prompt: str) -> tuple:
    """查询单个模型的修宪投票（复用投票解析逻辑）"""
    try:
        raw = ai_advisor.call_model_api(
            runner.cfg, AMENDMENT_SYSTEM_PROMPT, vote_prompt, max_retries=2
        )
        if not raw:
            return runner.name, []
        text = re.sub(r"<think>[\s\S]*?</think>\s*", "", raw).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        votes = json.loads(text)
        if not isinstance(votes, list):
            return runner.name, []
        return runner.name, votes
    except Exception as e:
        console.print(f"  [dim]{runner.name} 修宪投票解析失败: {e}[/dim]")
        return runner.name, []


def conduct_board_meeting(runners, market_text: str, prices: dict):
    """执行董事会会议：提案 → 投票 → 决议 → 进化 → 规则自适应"""
    import market_data

    console.rule("[bold #d4a017]AI 董事会会议[/bold #d4a017]")

    # 加载/初始化基因组和规则集
    genomes = load_genomes()
    ruleset = board_ruleset
    for r in runners:
        if r.name not in genomes:
            genomes[r.name] = _default_genome(r.name)

    # --- 阶段 1: 收集提案 ---
    proposals = _collect_proposals(runners)
    if not proposals:
        console.print("[dim]本轮无交易提案，董事会休会[/dim]")
        board_fund.last_decisions = []
        board_fund.save()
        save_genomes(genomes)
        return

    console.print(f"[dim]收集到 {len(proposals)} 个提案[/dim]")

    # --- 阶段 2: 投票 ---
    # 获取公共基金持仓的最新价格
    fund_held = list(board_fund.positions.keys())
    fund_prices = dict(prices)
    if fund_held:
        fresh = market_data.get_realtime_prices(fund_held)
        fund_prices.update(fresh)

    vote_prompt = _build_vote_prompt(proposals, board_fund, fund_prices)
    console.print(f"[dim]正在并行投票（{len(runners)} 位董事）...[/dim]")
    t0 = time.time()
    all_votes = _vote_all(runners, vote_prompt)
    vote_time = time.time() - t0
    console.print(f"[dim]投票完成，耗时 {vote_time:.1f}s[/dim]")

    # --- 阶段 3: 计票 & 决议（使用 ruleset 参数）---
    results = _tally_votes(proposals, all_votes, genomes, ruleset=ruleset)

    # 刷新买入标的价格
    buy_codes = [r["code"] for r in results if r["approved"] and r["action"] == "buy"]
    if buy_codes:
        fresh_buy = market_data.get_realtime_prices(buy_codes)
        fund_prices.update(fresh_buy)

    # 先卖（传递 ruleset，含 T+1 守卫）
    today = datetime.now().strftime("%Y-%m-%d")
    for r in results:
        if not r["approved"] or r["action"] != "sell":
            continue
        code = r["code"]
        # T+1 守卫：当日买入不允许卖出
        if code in board_fund.positions:
            pos = board_fund.positions[code]
            buy_date = pos.get("buy_date", "")
            if buy_date == today:
                console.print(f"  [dim]董事会: 跳过卖出 {code}（T+1，今日买入）[/dim]")
                continue
        sell_price = fund_prices.get(code)
        if sell_price:
            msg = board_fund.sell_with_evolution(
                code, sell_price, genomes, market_text[:200],
                ruleset=ruleset,
            )
            if msg:
                console.print(f"  [green]卖出决议执行: {msg}[/green]")

    # 后买（使用 ruleset.max_position_ratio）
    for r in results:
        if not r["approved"] or r["action"] != "buy":
            continue
        code = r["code"]
        buy_price = fund_prices.get(code)
        if buy_price:
            ratio = min(r["ratio"], ruleset.max_position_ratio)
            msg = board_fund.buy_with_attribution(
                code, r["name"], buy_price, ratio, fund_prices,
                proposer=r["proposer"],
                vote_score=r["vote_score"],
                voters_approve=r["voters_approve"],
                voters_reject=r["voters_reject"],
            )
            if msg:
                console.print(f"  [red]买入决议执行: {msg}[/red]")

    # 保存决议记录
    board_fund.last_decisions = [
        {
            "code": r["code"],
            "name": r["name"],
            "action": r["action"],
            "proposer": r["proposer"],
            "vote_score": r["vote_score"],
            "approved": r["approved"],
        }
        for r in results
    ]
    board_fund.save()
    save_genomes(genomes)

    # --- 阶段 4: 规则自进化 ---
    if ruleset.should_auto_evolve():
        changes = ruleset.auto_evolve()
        # 检查是否需要修宪
        if ruleset.should_amend(pending_changes=changes):
            conduct_rule_amendment(runners, ruleset)
    elif ruleset.should_amend():
        conduct_rule_amendment(runners, ruleset)

    # --- 打印决议面板 ---
    _print_board_table(results, fund_prices, genomes)


def _print_board_table(results: list, prices: dict, genomes: dict):
    """打印董事会决议表格"""
    # 决议表
    table = Table(
        title="董事会决议",
        border_style="#d4a017",
        show_lines=True,
    )
    table.add_column("提案", width=6)
    table.add_column("操作", width=5)
    table.add_column("标的", width=16)
    table.add_column("提案人", width=14)
    table.add_column("得票率", justify="right", width=8)
    table.add_column("结果", justify="center", width=6)

    for r in results:
        action_color = "red" if r["action"] == "buy" else "green"
        action_text = f"[{action_color}]{r['action'].upper()}[/{action_color}]"
        score_text = f"{r['vote_score']:.0%}"
        result_text = "[bold green]通过[/bold green]" if r["approved"] else "[dim]否决[/dim]"
        table.add_row(
            r["proposal_id"], action_text,
            f"{r['name']}({r['code']})", r["proposer"],
            score_text, result_text,
        )

    console.print(table)

    # 公共基金摘要
    total = board_fund.total_value(prices)
    pnl_pct = (total - INITIAL_CASH) / INITIAL_CASH * 100
    pnl_color = "red" if pnl_pct > 0 else ("green" if pnl_pct < 0 else "white")
    console.print(
        f"  公共基金: [bold]¥{total:.2f}[/bold] "
        f"[{pnl_color}]({pnl_pct:+.2f}%)[/{pnl_color}]"
    )

    # 影响力排行（前3）
    sorted_g = sorted(genomes.items(), key=lambda x: x[1].get("influence", 1.0), reverse=True)
    top3 = sorted_g[:3]
    top3_text = " > ".join(f"{name}({g['influence']:.2f})" for name, g in top3)
    console.print(f"  [dim]影响力排名: {top3_text}[/dim]")


def get_board_summary_for_report(prices: dict) -> str:
    """生成董事会摘要文本，供战报使用"""
    total = board_fund.total_value(prices)
    pnl_pct = (total - INITIAL_CASH) / INITIAL_CASH * 100

    decisions = board_fund.last_decisions
    if not decisions:
        return ""

    total_proposals = len(decisions)
    approved_count = sum(1 for d in decisions if d.get("approved"))
    approved_list = [
        f"{d['action'].upper()} {d['name']}({d['code']})"
        for d in decisions if d.get("approved")
    ]

    genomes = load_genomes()
    sorted_g = sorted(genomes.items(), key=lambda x: x[1].get("influence", 1.0), reverse=True)
    top3 = [name for name, _ in sorted_g[:3]]

    lines = [
        f"董事会会议: 本轮收到 {total_proposals} 个提案，投票通过 {approved_count} 个。",
    ]
    if approved_list:
        lines.append(f"通过的提案: {', '.join(approved_list)}")
    lines.append(f"当前最有影响力的董事: {', '.join(top3)}")
    lines.append(f"公共基金收益: {pnl_pct:+.2f}%")

    return "\n".join(lines)
