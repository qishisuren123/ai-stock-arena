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

# 影响力范围
INFLUENCE_MIN = 0.3
INFLUENCE_MAX = 3.0


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
                           genomes: dict, market_summary: str = "") -> str | None:
        """卖出并触发进化更新"""
        if code not in self.positions:
            return None

        # 获取归因信息
        attribution = self.pending_trades.pop(code, None)

        # 计算盈亏（卖出前）
        pos = self.positions[code]
        income = pos["qty"] * price
        commission = max(income * 0.001, 1.0)
        net_income = income - commission
        pnl = net_income - pos["total_cost"]
        profitable = pnl > 0

        # 执行卖出
        result = self.sell(code, price)

        # 进化更新
        if attribution and result:
            evolve_genomes(genomes, attribution, profitable, pnl, market_summary)

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
2. 不要盲目跟风或反对，给出合理的理由
3. 如果提案的标的已在公共基金持仓中，谨慎投票
4. 注意公共基金的整体仓位和风险控制"""


def _collect_proposals(runners) -> list:
    """从各模型个人交易的 advice 中收集提案"""
    proposals = []
    seen_actions = {}  # (code, action) -> proposal，用于去重

    for r in runners:
        if not r.advice:
            continue
        actions = r.advice.get("actions", [])
        for act in actions:
            code = act.get("code", "")
            action = act.get("action", "")
            if not code or action not in ("buy", "sell"):
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
                "ratio": act.get("ratio", 0.2),
                "reasoning": r.advice.get("analysis", "")[:200],
            }
            proposals.append(proposal)
            seen_actions[key] = proposal

    return proposals


def _build_vote_prompt(proposals: list, fund: BoardFund, prices: dict) -> str:
    """构造投票 prompt"""
    # 公共基金状态
    fund_summary = fund.get_portfolio_summary(prices)

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
        f"【待投票提案】\n{proposals_text}\n\n"
        f"请对以上每个提案投票（纯 JSON 数组）。"
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


def _tally_votes(proposals: list, all_votes: dict, genomes: dict) -> list:
    """计票，返回带得票结果的提案列表"""
    results = []
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

        total_weight = approve_weight + reject_weight
        score = approve_weight / total_weight if total_weight > 0 else 0
        approved = score > 0.5

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
                   pnl: float, market_summary: str = ""):
    """根据交易结果更新基因组"""
    proposer = attribution.get("proposer", "")
    voters_approve = attribution.get("voters_approve", [])
    voters_reject = attribution.get("voters_reject", [])

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

    # 重算所有参与者的影响力
    involved = set([proposer] + voters_approve + voters_reject)
    for name in involved:
        if name not in genomes:
            continue
        g = genomes[name]
        accuracy = g.get("proposals_profitable", 0) / max(g.get("proposals_total", 0), 1)
        vote_acc = g.get("votes_correct", 0) / max(g.get("votes_total", 0), 1)
        influence = 1.0 + accuracy * 1.0 + vote_acc * 0.5
        g["influence"] = round(max(INFLUENCE_MIN, min(INFLUENCE_MAX, influence)), 3)
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


def conduct_board_meeting(runners, market_text: str, prices: dict):
    """执行董事会会议：提案 → 投票 → 决议 → 进化"""
    import market_data

    console.rule("[bold #d4a017]AI 董事会会议[/bold #d4a017]")

    # 加载/初始化基因组
    genomes = load_genomes()
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

    # --- 阶段 3: 计票 & 决议 ---
    results = _tally_votes(proposals, all_votes, genomes)

    # 刷新买入标的价格
    buy_codes = [r["code"] for r in results if r["approved"] and r["action"] == "buy"]
    if buy_codes:
        fresh_buy = market_data.get_realtime_prices(buy_codes)
        fund_prices.update(fresh_buy)

    # 先卖
    for r in results:
        if not r["approved"] or r["action"] != "sell":
            continue
        code = r["code"]
        sell_price = fund_prices.get(code)
        if sell_price:
            msg = board_fund.sell_with_evolution(
                code, sell_price, genomes, market_text[:200]
            )
            if msg:
                console.print(f"  [green]卖出决议执行: {msg}[/green]")

    # 后买
    for r in results:
        if not r["approved"] or r["action"] != "buy":
            continue
        code = r["code"]
        buy_price = fund_prices.get(code)
        if buy_price:
            msg = board_fund.buy_with_attribution(
                code, r["name"], buy_price, r["ratio"], fund_prices,
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
