"""市场战略情报模块 — 多模型竞聘上岗 + 绩效考核 + 动态换岗

核心机制：
  1. 角色定义与模型分离：7 个分析师角色只定义职责，不绑定模型
  2. 绩效考核(RoleFitness)：每轮回溯验证上轮预测 vs 实际市场，打分
  3. 动态换岗：每 5 轮审查，绩效差的模型被替换
  4. 竞聘上岗：首次运行时所有模型竞争所有角色，按表现分配
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
from model_config import MODELS

console = Console()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATES_DIR = os.path.join(BASE_DIR, "multi_states")
INTEL_FILE = os.path.join(STATES_DIR, "_intel_briefing.json")
INTEL_HISTORY_FILE = os.path.join(STATES_DIR, "_intel_history.json")
ROLE_FITNESS_FILE = os.path.join(STATES_DIR, "_role_fitness.json")

# 可用的免费内部模型
FREE_MODELS = [
    "Minimax2.5", "GLM5", "DeepSeek-V3.2", "Kimi-K2.5",
    "Qwen3.5-397B", "Intern-S1", "Intern-S1-Pro",
]

# ============================================================
# 角色定义（只定义职责和 prompt，不绑定模型）
# ============================================================

ROLE_DEFS = [
    {
        "role": "宏观策略师",
        "category": "macro",
        "system_prompt": """你是一位资深宏观策略分析师，服务于一个 AI 炒股基金。请基于提供的市场数据，从宏观角度分析：

1. 当前大盘处于什么阶段（上升趋势/震荡/下跌趋势）？判断依据是什么？
2. 成交量和成交额释放了什么信号？缩量还是放量？
3. 近期新闻中有什么重大政策或事件？对市场短期有何影响？
4. 未来 1-3 天的市场方向判断，给出置信度

你必须严格以 JSON 格式返回，不要输出任何其他内容（不要 markdown 代码块标记）：
{
    "market_phase": "上升趋势/震荡/下跌趋势",
    "volume_signal": "放量上攻/缩量调整/放量下跌/缩量反弹",
    "key_signals": ["信号1", "信号2"],
    "policy_impact": "50字以内的政策面总结",
    "outlook": "看多/看空/震荡",
    "confidence": 65,
    "summary": "50字内总结"
}""",
    },
    {
        "role": "行业轮动分析师",
        "category": "sector",
        "system_prompt": """你是一位行业轮动分析专家，服务于一个 AI 炒股基金。请基于行业板块和概念数据分析：

1. 今日领涨行业是什么？连续领涨还是新起的？
2. 资金正在从哪些板块流出、流入哪些板块？
3. 是否存在明确的行业轮动信号？（如科技→消费、大盘→小盘）
4. 推荐 2-3 个未来 3 天最值得关注的行业方向

你必须严格以 JSON 格式返回，不要输出任何其他内容（不要 markdown 代码块标记）：
{
    "hot_sectors": ["行业1", "行业2", "行业3"],
    "cold_sectors": ["行业1", "行业2"],
    "rotation_signal": "描述轮动方向，无则写无",
    "recommended_sectors": [
        {"sector": "行业名", "reason": "20字理由", "confidence": 70}
    ],
    "summary": "50字内总结"
}""",
    },
    {
        "role": "市场情绪分析师",
        "category": "sentiment",
        "system_prompt": """你是一位市场情绪分析专家，服务于一个 AI 炒股基金。请基于新闻、涨跌数据和市场宽度分析：

1. 当前市场情绪评分（0=极度恐慌，50=中性，100=极度贪婪）
2. 新闻面是利好主导还是利空主导？列出 2 条最重要的消息
3. 涨跌家数比和涨停跌停数量反映了什么？
4. 是否存在情绪极端化信号？（恐慌抛售 / 疯狂追涨 / 集体观望）

你必须严格以 JSON 格式返回，不要输出任何其他内容（不要 markdown 代码块标记）：
{
    "sentiment_score": 55,
    "sentiment_label": "偏乐观/中性/偏悲观/恐慌/贪婪",
    "news_sentiment": "利好/中性/利空",
    "major_news": ["最重要的消息1", "第二重要的消息2"],
    "crowd_behavior": "追涨/抛售/观望/分化",
    "extreme_signal": "无/有，简述",
    "summary": "50字内总结"
}""",
    },
    {
        "role": "资金流向分析师",
        "category": "flow",
        "system_prompt": """你是一位资金流向分析专家，服务于一个 AI 炒股基金。请基于成交额、涨跌幅榜和融资融券数据分析：

1. 今日资金主要流向：大盘蓝筹 or 中小盘 or 题材股？
2. 成交额排行榜上的个股在被抢筹还是出货？
3. 融资融券数据释放什么信号？杠杆资金方向？
4. 有没有异常资金活动？（板块成交额突增、个股天量等）

你必须严格以 JSON 格式返回，不要输出任何其他内容（不要 markdown 代码块标记）：
{
    "flow_direction": "大盘蓝筹/中小盘/题材股/均衡",
    "institutional_action": "抢筹/出货/观望",
    "margin_signal": "杠杆加仓/杠杆减仓/中性",
    "hot_money_targets": ["被资金追捧的股票1", "股票2"],
    "anomalies": ["异常信号1"],
    "summary": "50字内总结"
}""",
    },
    {
        "role": "风险预警分析师",
        "category": "risk",
        "system_prompt": """你是一位风险预警分析师，服务于一个 AI 炒股基金。你的职责是找风险、泼冷水。请分析：

1. 当前市场最大的 2-3 个风险因素是什么？
2. 新闻中有没有被忽视的利空信息？
3. 哪些板块或热门股可能面临回调？为什么？
4. 整体风险评级和防御建议

你必须严格以 JSON 格式返回，不要输出任何其他内容（不要 markdown 代码块标记）：
{
    "risk_level": "低/中等/高/极高",
    "top_risks": ["风险1", "风险2", "风险3"],
    "overlooked_negatives": ["被忽视的利空1"],
    "sectors_at_risk": ["可能回调的板块1"],
    "defensive_advice": "30字内防御建议",
    "summary": "50字内总结"
}""",
    },
    {
        "role": "热点题材分析师",
        "category": "theme",
        "system_prompt": """你是一位热点题材挖掘分析师，服务于一个 AI 炒股基金。请基于概念板块、新闻和涨幅数据分析：

1. 今天最核心的 1-2 条市场主线是什么？（AI/新能源/半导体/医药等）
2. 有什么新出现的催化剂？（政策发布/技术突破/事件驱动/业绩超预期）
3. 哪些题材有 3 天以上的持续性？哪些大概率是一日游？
4. 推荐最值得跟踪的 2 个题材，以及相关代表性个股

你必须严格以 JSON 格式返回，不要输出任何其他内容（不要 markdown 代码块标记）：
{
    "main_themes": ["主线1", "主线2"],
    "catalysts": ["催化剂1", "催化剂2"],
    "sustainable_themes": ["有持续性的题材"],
    "one_day_themes": ["一日游题材"],
    "recommended": [
        {"theme": "题材", "reason": "理由", "stocks": ["代码1(名称)"]}
    ],
    "summary": "50字内总结"
}""",
    },
    {
        "role": "选股分析师",
        "category": "stock",
        "system_prompt": """你是一位精选个股分析师，服务于一个 1 万元的 AI 炒股基金。请基于涨跌榜、成交额和估值数据分析：

1. 涨幅榜中哪些股票上涨有基本面支撑（不是纯炒作）？
2. 跌幅榜中有没有被错杀的优质股可以抄底？
3. 估值分位数据中哪些股票明显被低估？
4. 综合推荐 2-3 只买入候选，要求：股价 50 元以下、日成交额 > 1 亿、有明确逻辑

你必须严格以 JSON 格式返回，不要输出任何其他内容（不要 markdown 代码块标记）：
{
    "quality_gainers": [{"code": "000001", "name": "名称", "reason": "理由"}],
    "oversold_picks": [{"code": "000002", "name": "名称", "reason": "理由"}],
    "undervalued": [{"code": "000003", "name": "名称", "pe_percentile": "30%"}],
    "top_picks": [
        {"code": "600xxx", "name": "名称", "reason": "理由", "suggested_ratio": 0.15}
    ],
    "summary": "50字内总结"
}""",
    },
]

ROLE_NAMES = [r["role"] for r in ROLE_DEFS]


# ============================================================
# RoleFitness — 绩效考核 + 动态角色分配
# ============================================================

class RoleFitness:
    """基于绩效数据的动态角色分配器

    数据结构 (_role_fitness.json):
    {
        "assignments": {"宏观策略师": "DeepSeek-V3.2", ...},
        "scores": {
            "DeepSeek-V3.2": {
                "宏观策略师": {"hits": 3, "misses": 2, "api_fails": 0, "score": 0.60},
                "行业轮动分析师": {"hits": 0, "misses": 0, "api_fails": 0, "score": 0.50},
                ...
            },
            ...
        },
        "cycle_count": 12,
        "review_interval": 5,
        "last_review_cycle": 10,
        "swap_history": [
            {"cycle": 10, "role": "宏观策略师", "old": "X", "new": "Y", "reason": "..."}
        ],
        "prev_predictions": { ... }  # 上轮预测快照，用于回溯验证
    }
    """

    REVIEW_INTERVAL = 5  # 每 5 轮审查一次
    MIN_DATA_FOR_SWAP = 3  # 至少 3 轮数据才考虑换岗
    SWAP_THRESHOLD = 0.15  # 候选模型比当前持有者高出 15% 才换

    def __init__(self):
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(ROLE_FITNESS_FILE):
            try:
                with open(ROLE_FITNESS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                pass
        return self._init_default()

    def _init_default(self) -> dict:
        """首次运行，用合理的初始分配"""
        # 初始分配逻辑：根据模型特点的合理推测
        # 但所有模型的所有角色初始得分一样(0.50)，让绩效数据来决定
        assignments = {
            "宏观策略师": "DeepSeek-V3.2",
            "行业轮动分析师": "Qwen3.5-397B",
            "市场情绪分析师": "GLM5",
            "资金流向分析师": "Kimi-K2.5",
            "风险预警分析师": "Minimax2.5",
            "热点题材分析师": "Intern-S1-Pro",
            "选股分析师": "Intern-S1",
        }
        scores = {}
        for model in FREE_MODELS:
            scores[model] = {}
            for role in ROLE_NAMES:
                scores[model][role] = {
                    "hits": 0, "misses": 0, "api_fails": 0, "score": 0.50,
                }
        return {
            "assignments": assignments,
            "scores": scores,
            "cycle_count": 0,
            "review_interval": self.REVIEW_INTERVAL,
            "last_review_cycle": 0,
            "swap_history": [],
            "prev_predictions": {},
        }

    def save(self):
        os.makedirs(STATES_DIR, exist_ok=True)
        with open(ROLE_FITNESS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    @property
    def assignments(self) -> dict:
        return self._data["assignments"]

    @property
    def cycle_count(self) -> int:
        return self._data["cycle_count"]

    def get_model_for_role(self, role_name: str) -> str:
        return self._data["assignments"].get(role_name, FREE_MODELS[0])

    def get_score(self, model: str, role: str) -> float:
        return self._data["scores"].get(model, {}).get(role, {}).get("score", 0.5)

    def get_total_evals(self, model: str, role: str) -> int:
        s = self._data["scores"].get(model, {}).get(role, {})
        return s.get("hits", 0) + s.get("misses", 0)

    # ----- 绩效记录 -----

    def record_hit(self, model: str, role: str):
        """记录一次正确预测"""
        self._ensure_entry(model, role)
        s = self._data["scores"][model][role]
        s["hits"] += 1
        s["score"] = self._calc_score(s)

    def record_miss(self, model: str, role: str):
        """记录一次错误预测"""
        self._ensure_entry(model, role)
        s = self._data["scores"][model][role]
        s["misses"] += 1
        s["score"] = self._calc_score(s)

    def record_api_fail(self, model: str, role: str):
        """记录一次 API 失败（扣分但幅度小）"""
        self._ensure_entry(model, role)
        s = self._data["scores"][model][role]
        s["api_fails"] += 1
        # API 失败也算半个 miss
        s["score"] = self._calc_score(s)

    def _ensure_entry(self, model: str, role: str):
        if model not in self._data["scores"]:
            self._data["scores"][model] = {}
        if role not in self._data["scores"][model]:
            self._data["scores"][model][role] = {
                "hits": 0, "misses": 0, "api_fails": 0, "score": 0.50,
            }

    @staticmethod
    def _calc_score(s: dict) -> float:
        """计算综合得分: hits / (hits + misses + api_fails*0.5)"""
        total = s["hits"] + s["misses"] + s["api_fails"] * 0.5
        if total <= 0:
            return 0.50
        return round(s["hits"] / total, 3)

    # ----- 回溯验证 -----

    def store_predictions(self, briefing: dict, index_level: float,
                          pick_prices: dict):
        """存储本轮预测，供下轮验证"""
        self._data["prev_predictions"] = {
            "timestamp": briefing.get("timestamp", ""),
            "macro_outlook": briefing.get("macro_outlook", ""),
            "sentiment_score": briefing.get("sentiment_score", 50),
            "risk_level": briefing.get("risk_level", ""),
            "top_picks": briefing.get("top_picks", []),
            "hot_sectors": briefing.get("hot_sectors", []),
            "index_level": index_level,
            "pick_prices": pick_prices,
            "assignments_snapshot": dict(self._data["assignments"]),
        }

    def validate_previous(self, current_index_level: float,
                          current_pick_prices: dict):
        """对比上轮预测与当前实际市场，更新绩效"""
        prev = self._data.get("prev_predictions", {})
        if not prev or not prev.get("index_level"):
            return  # 无上轮数据

        prev_index = prev["index_level"]
        assignments = prev.get("assignments_snapshot", {})
        index_change_pct = (current_index_level - prev_index) / prev_index * 100

        console.print(f"[dim]回溯验证: 指数变化 {index_change_pct:+.2f}%[/dim]")

        # 1. 验证宏观策略师：方向预测
        macro_model = assignments.get("宏观策略师", "")
        outlook = prev.get("macro_outlook", "震荡")
        if macro_model:
            if outlook == "看多" and index_change_pct > 0.2:
                self.record_hit(macro_model, "宏观策略师")
                console.print(f"  [green]✓[/green] 宏观策略师({macro_model}): 看多→涨{index_change_pct:+.1f}%")
            elif outlook == "看空" and index_change_pct < -0.2:
                self.record_hit(macro_model, "宏观策略师")
                console.print(f"  [green]✓[/green] 宏观策略师({macro_model}): 看空→跌{index_change_pct:+.1f}%")
            elif outlook == "震荡" and abs(index_change_pct) <= 0.5:
                self.record_hit(macro_model, "宏观策略师")
                console.print(f"  [green]✓[/green] 宏观策略师({macro_model}): 震荡→波动{index_change_pct:+.1f}%")
            else:
                self.record_miss(macro_model, "宏观策略师")
                console.print(f"  [red]✗[/red] 宏观策略师({macro_model}): {outlook}→实际{index_change_pct:+.1f}%")

        # 2. 验证情绪分析师：情绪方向与市场一致性
        sent_model = assignments.get("市场情绪分析师", "")
        sent_score = prev.get("sentiment_score", 50)
        if sent_model:
            # 情绪 > 60 视为乐观，< 40 视为悲观
            if (sent_score > 60 and index_change_pct > 0) or \
               (sent_score < 40 and index_change_pct < 0) or \
               (40 <= sent_score <= 60 and abs(index_change_pct) < 1.0):
                self.record_hit(sent_model, "市场情绪分析师")
                console.print(f"  [green]✓[/green] 情绪分析师({sent_model}): 情绪{sent_score}→市场{index_change_pct:+.1f}%")
            else:
                self.record_miss(sent_model, "市场情绪分析师")
                console.print(f"  [red]✗[/red] 情绪分析师({sent_model}): 情绪{sent_score}→市场{index_change_pct:+.1f}%")

        # 3. 验证风险分析师：高风险预警时市场确实下跌
        risk_model = assignments.get("风险预警分析师", "")
        risk_level = prev.get("risk_level", "中等")
        if risk_model:
            if risk_level in ("高", "极高") and index_change_pct < -0.5:
                self.record_hit(risk_model, "风险预警分析师")
                console.print(f"  [green]✓[/green] 风险分析师({risk_model}): 预警{risk_level}→跌{index_change_pct:+.1f}%")
            elif risk_level in ("低",) and index_change_pct > -0.5:
                self.record_hit(risk_model, "风险预警分析师")
                console.print(f"  [green]✓[/green] 风险分析师({risk_model}): 低风险→稳定{index_change_pct:+.1f}%")
            elif risk_level in ("高", "极高") and index_change_pct > 0.5:
                self.record_miss(risk_model, "风险预警分析师")
                console.print(f"  [red]✗[/red] 风险分析师({risk_model}): 预警{risk_level}→但涨{index_change_pct:+.1f}%")
            else:
                # 中等风险不好评判，算 0.5
                pass

        # 4. 验证选股分析师：推荐股票的涨跌
        stock_model = assignments.get("选股分析师", "")
        picks = prev.get("top_picks", [])
        prev_prices = prev.get("pick_prices", {})
        if stock_model and picks and prev_prices:
            gains = []
            for p in picks:
                code = p.get("code", "") if isinstance(p, dict) else ""
                if code and code in prev_prices and code in current_pick_prices:
                    before = prev_prices[code]
                    after = current_pick_prices[code]
                    if before > 0:
                        gains.append((after - before) / before * 100)
            if gains:
                avg_gain = sum(gains) / len(gains)
                # 推荐的股票平均涨了算 hit，跌了算 miss
                if avg_gain > 0:
                    self.record_hit(stock_model, "选股分析师")
                    console.print(f"  [green]✓[/green] 选股分析师({stock_model}): 推荐股均涨{avg_gain:+.1f}%")
                else:
                    self.record_miss(stock_model, "选股分析师")
                    console.print(f"  [red]✗[/red] 选股分析师({stock_model}): 推荐股均跌{avg_gain:+.1f}%")

        # 递增周期计数
        self._data["cycle_count"] += 1

    # ----- 角色审查和换岗 -----

    def should_review(self) -> bool:
        """是否到了审查周期"""
        cycle = self._data["cycle_count"]
        last_review = self._data.get("last_review_cycle", 0)
        return cycle - last_review >= self.REVIEW_INTERVAL

    def review_and_swap(self):
        """审查所有角色分配，绩效差的换人"""
        console.print("[bold #e67e22]═══ 角色绩效审查 ═══[/bold #e67e22]")
        swaps = []

        for role in ROLE_NAMES:
            current_model = self._data["assignments"].get(role, "")
            current_score = self.get_score(current_model, role)
            current_evals = self.get_total_evals(current_model, role)

            # 找其他空闲或表现更好的模型
            assigned_models = set(self._data["assignments"].values())
            best_alt = None
            best_alt_score = current_score

            for model in FREE_MODELS:
                if model == current_model:
                    continue
                alt_score = self.get_score(model, role)
                alt_evals = self.get_total_evals(model, role)

                # 如果候选模型在这个角色上有数据且明显更好
                # 或者当前模型表现太差(< 0.35)且候选尚未尝试过
                if alt_evals >= self.MIN_DATA_FOR_SWAP and \
                   alt_score > current_score + self.SWAP_THRESHOLD:
                    if alt_score > best_alt_score:
                        best_alt = model
                        best_alt_score = alt_score
                elif current_evals >= self.MIN_DATA_FOR_SWAP and \
                     current_score < 0.35 and alt_evals == 0:
                    # 当前太差，给新模型一个机会（如果它没被分配到其他角色）
                    if model not in assigned_models:
                        best_alt = model
                        best_alt_score = 0.50  # 新模型默认分

            if best_alt:
                swaps.append({
                    "role": role,
                    "old_model": current_model,
                    "old_score": current_score,
                    "new_model": best_alt,
                    "new_score": best_alt_score,
                })

        # 执行换岗（需要处理链式交换：A→B角色 同时 B→A角色）
        for swap in swaps:
            role = swap["role"]
            old_model = swap["old_model"]
            new_model = swap["new_model"]

            # 检查 new_model 是否已被分配到其他角色
            other_role = None
            for r, m in self._data["assignments"].items():
                if m == new_model:
                    other_role = r
                    break

            if other_role:
                # 交换两个模型的角色
                self._data["assignments"][role] = new_model
                self._data["assignments"][other_role] = old_model
                console.print(
                    f"  [yellow]⇄[/yellow] {role}: "
                    f"{old_model}({swap['old_score']:.0%}) → {new_model}({swap['new_score']:.0%})"
                )
                console.print(
                    f"  [yellow]⇄[/yellow] {other_role}: "
                    f"{new_model} → {old_model}（互换）"
                )
            else:
                self._data["assignments"][role] = new_model
                console.print(
                    f"  [yellow]→[/yellow] {role}: "
                    f"{old_model}({swap['old_score']:.0%}) → {new_model}({swap['new_score']:.0%})"
                )

            # 记录换岗历史
            self._data["swap_history"].append({
                "cycle": self._data["cycle_count"],
                "role": role,
                "old": old_model,
                "new": new_model,
                "old_score": swap["old_score"],
                "new_score": swap["new_score"],
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })

        if not swaps:
            console.print("  [dim]本轮无换岗，当前分配维持不变[/dim]")

        self._data["last_review_cycle"] = self._data["cycle_count"]
        # 保留最近 30 条换岗记录
        if len(self._data["swap_history"]) > 30:
            self._data["swap_history"] = self._data["swap_history"][-30:]

    def print_scoreboard(self):
        """打印角色绩效看板"""
        table = Table(
            title=f"分析师绩效看板 (第{self.cycle_count}轮)",
            border_style="#e67e22",
        )
        table.add_column("角色", width=14)
        table.add_column("当前模型", width=16)
        table.add_column("得分", justify="right", width=6)
        table.add_column("正确", justify="right", width=4)
        table.add_column("错误", justify="right", width=4)
        table.add_column("评估次数", justify="right", width=6)

        for role in ROLE_NAMES:
            model = self._data["assignments"].get(role, "?")
            s = self._data["scores"].get(model, {}).get(role, {})
            score = s.get("score", 0.5)
            hits = s.get("hits", 0)
            misses = s.get("misses", 0)
            total = hits + misses

            # 颜色标注
            if total >= 3 and score >= 0.6:
                score_text = f"[green]{score:.0%}[/green]"
            elif total >= 3 and score < 0.4:
                score_text = f"[red]{score:.0%}[/red]"
            else:
                score_text = f"{score:.0%}"

            table.add_row(role, model, score_text, str(hits), str(misses), str(total))

        console.print(table)


# ============================================================
# 核心查询逻辑
# ============================================================

def _get_model_cfg(model_name: str) -> dict | None:
    """从 MODELS 列表中查找指定名称的模型配置"""
    for cfg in MODELS:
        if cfg["name"] == model_name:
            return cfg
    return None


def _query_analyst(role_def: dict, model_name: str, market_text: str) -> dict:
    """查询单个分析师模型"""
    model_cfg = _get_model_cfg(model_name)
    if not model_cfg:
        return {"role": role_def["role"], "model": model_name,
                "error": f"模型 {model_name} 不存在"}
    try:
        raw = ai_advisor.call_model_api(
            model_cfg, role_def["system_prompt"], market_text, max_retries=2,
        )
        if not raw:
            return {"role": role_def["role"], "model": model_name,
                    "error": "返回为空"}

        text = re.sub(r"<think>[\s\S]*?</think>\s*", "", raw).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        result = json.loads(text)
        result["role"] = role_def["role"]
        result["model"] = model_name
        return result

    except json.JSONDecodeError:
        return {
            "role": role_def["role"], "model": model_name,
            "summary": (raw or "")[:200], "parse_error": True,
        }
    except Exception as e:
        return {"role": role_def["role"], "model": model_name,
                "error": str(e)[:120]}


def gather_intelligence(market_text: str,
                        index_level: float = 0.0) -> dict:
    """并行调用分析师模型，汇总生成战略情报简报

    Args:
        market_text: 完整的市场数据文本
        index_level: 当前上证指数点位（用于回溯验证）
    Returns:
        briefing dict，核心字段为 briefing_text
    """
    fitness = RoleFitness()

    # --- 回溯验证上轮预测 ---
    if index_level > 0 and fitness.cycle_count > 0:
        # 获取上轮推荐的股票价格（用于验证选股）
        prev_preds = fitness._data.get("prev_predictions", {})
        pick_codes = []
        for p in prev_preds.get("top_picks", []):
            if isinstance(p, dict) and p.get("code"):
                pick_codes.append(p["code"])
        current_pick_prices = {}
        if pick_codes:
            try:
                import market_data
                current_pick_prices = market_data.get_realtime_prices(pick_codes)
            except Exception:
                pass
        fitness.validate_previous(index_level, current_pick_prices)

    # --- 角色审查 ---
    if fitness.should_review():
        fitness.review_and_swap()

    # --- 打印当前分配 ---
    console.rule("[bold #3b82f6]战略情报收集[/bold #3b82f6]")
    if fitness.cycle_count > 0:
        fitness.print_scoreboard()

    assignments = fitness.assignments
    console.print("[dim]角色分配:[/dim]")
    for role, model in assignments.items():
        score = fitness.get_score(model, role)
        evals = fitness.get_total_evals(model, role)
        info = f"得分{score:.0%}" if evals > 0 else "新任"
        console.print(f"  {role} → {model} [{info}]")

    # --- 并行查询 ---
    reports: dict[str, dict] = {}
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = {}
        for role_def in ROLE_DEFS:
            model_name = assignments.get(role_def["role"], FREE_MODELS[0])
            futures[executor.submit(
                _query_analyst, role_def, model_name, market_text
            )] = role_def

        try:
            for future in as_completed(futures, timeout=150):
                try:
                    result = future.result()
                    role_name = result.get("role", "未知")
                    model_name = result.get("model", "")
                    reports[role_name] = result

                    if "error" in result:
                        console.print(
                            f"  [red]✗[/red] {role_name}({model_name}): "
                            f"{result['error'][:60]}"
                        )
                        # API 失败扣分
                        fitness.record_api_fail(model_name, role_name)
                    else:
                        summary = result.get("summary", "完成")
                        if isinstance(summary, str):
                            summary = summary[:50]
                        console.print(
                            f"  [green]✓[/green] {role_name}({model_name}): "
                            f"{summary}"
                        )
                except Exception as e:
                    role_def = futures[future]
                    role_name = role_def["role"]
                    reports[role_name] = {"role": role_name, "error": str(e)[:80]}
        except TimeoutError:
            for future, role_def in futures.items():
                if not future.done():
                    reports[role_def["role"]] = {
                        "role": role_def["role"], "error": "超时(150s)",
                    }

    elapsed = time.time() - t0
    ok_count = sum(1 for r in reports.values() if "error" not in r)
    console.print(
        f"[dim]情报收集完成: {ok_count}/{len(ROLE_DEFS)} 成功, "
        f"耗时 {elapsed:.1f}s[/dim]"
    )

    # --- 汇总情报 ---
    briefing = _synthesize(reports)

    # --- 保存预测快照供下轮验证 ---
    pick_prices_now = {}
    picks = briefing.get("top_picks", [])
    if picks:
        pick_codes = [p.get("code") for p in picks
                      if isinstance(p, dict) and p.get("code")]
        if pick_codes:
            try:
                import market_data
                pick_prices_now = market_data.get_realtime_prices(pick_codes)
            except Exception:
                pass
    fitness.store_predictions(briefing, index_level, pick_prices_now)
    fitness.save()

    # --- 打印简报面板 ---
    console.print(Panel(
        briefing.get("briefing_text", "（无）"),
        title="[bold]战略情报简报[/bold]",
        border_style="#3b82f6",
        padding=(0, 1),
    ))

    # --- 持久化 ---
    _save_intel(briefing, reports, fitness)
    return briefing


# ============================================================
# 汇总逻辑（与之前基本相同）
# ============================================================

def _synthesize(reports: dict) -> dict:
    """将各分析师报告汇总为结构化情报简报"""
    briefing = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ok_count": sum(1 for r in reports.values() if "error" not in r),
        "total": len(reports),
    }

    macro = reports.get("宏观策略师", {})
    if "error" not in macro:
        briefing["market_phase"] = macro.get("market_phase", "未知")
        briefing["volume_signal"] = macro.get("volume_signal", "")
        briefing["macro_outlook"] = macro.get("outlook", "未知")
        briefing["macro_confidence"] = macro.get("confidence", 50)
        briefing["macro_signals"] = macro.get("key_signals", [])
        briefing["policy_impact"] = macro.get("policy_impact", "")

    sent = reports.get("市场情绪分析师", {})
    if "error" not in sent:
        briefing["sentiment_score"] = sent.get("sentiment_score", 50)
        briefing["sentiment_label"] = sent.get("sentiment_label", "中性")
        briefing["news_sentiment"] = sent.get("news_sentiment", "中性")
        briefing["major_news"] = sent.get("major_news", [])
        briefing["crowd_behavior"] = sent.get("crowd_behavior", "")
        briefing["extreme_signal"] = sent.get("extreme_signal", "无")

    risk = reports.get("风险预警分析师", {})
    if "error" not in risk:
        briefing["risk_level"] = risk.get("risk_level", "中等")
        briefing["top_risks"] = risk.get("top_risks", [])
        briefing["overlooked_negatives"] = risk.get("overlooked_negatives", [])
        briefing["defensive_advice"] = risk.get("defensive_advice", "")

    sector = reports.get("行业轮动分析师", {})
    if "error" not in sector:
        briefing["hot_sectors"] = sector.get("hot_sectors", [])
        briefing["cold_sectors"] = sector.get("cold_sectors", [])
        briefing["recommended_sectors"] = sector.get("recommended_sectors", [])
        briefing["rotation_signal"] = sector.get("rotation_signal", "")

    money = reports.get("资金流向分析师", {})
    if "error" not in money:
        briefing["flow_direction"] = money.get("flow_direction", "")
        briefing["institutional_action"] = money.get("institutional_action", "")
        briefing["margin_signal"] = money.get("margin_signal", "")
        briefing["hot_money_targets"] = money.get("hot_money_targets", [])
        briefing["flow_anomalies"] = money.get("anomalies", [])

    theme = reports.get("热点题材分析师", {})
    if "error" not in theme:
        briefing["main_themes"] = theme.get("main_themes", [])
        briefing["catalysts"] = theme.get("catalysts", [])
        briefing["sustainable_themes"] = theme.get("sustainable_themes", [])
        briefing["one_day_themes"] = theme.get("one_day_themes", [])
        briefing["recommended_themes"] = theme.get("recommended", [])

    stock = reports.get("选股分析师", {})
    if "error" not in stock:
        briefing["top_picks"] = stock.get("top_picks", [])
        briefing["oversold_picks"] = stock.get("oversold_picks", [])
        briefing["quality_gainers"] = stock.get("quality_gainers", [])

    briefing["composite_signal"] = _compute_composite_signal(briefing)
    briefing["briefing_text"] = _format_text(briefing)
    return briefing


def _compute_composite_signal(b: dict) -> str:
    score = 0
    outlook = b.get("macro_outlook", "震荡")
    conf = min(b.get("macro_confidence", 50), 100) / 100
    if outlook == "看多":
        score += int(30 * conf)
    elif outlook == "看空":
        score -= int(30 * conf)
    sent = b.get("sentiment_score", 50)
    score += int((sent - 50) * 0.5)
    risk_map = {"低": 15, "中等": 0, "高": -15, "极高": -25}
    score += risk_map.get(b.get("risk_level", "中等"), 0)
    inst = b.get("institutional_action", "")
    if inst == "抢筹":
        score += 10
    elif inst == "出货":
        score -= 10
    margin = b.get("margin_signal", "")
    if "加仓" in margin:
        score += 8
    elif "减仓" in margin:
        score -= 8
    if score >= 40:
        return "强烈看多"
    elif score >= 15:
        return "偏多"
    elif score > -15:
        return "中性震荡"
    elif score > -40:
        return "偏空"
    return "强烈看空"


def _format_text(b: dict) -> str:
    """生成纯文本情报简报"""
    lines = ["═══ AI 多模型战略情报简报 ═══"]
    sig = b.get("composite_signal", "中性震荡")
    lines.append(f"★ 综合信号: {sig}")

    phase = b.get("market_phase", "")
    outlook = b.get("macro_outlook", "")
    vol_sig = b.get("volume_signal", "")
    conf = b.get("macro_confidence", 50)
    if phase:
        line = f"▎宏观: {phase}"
        if outlook:
            line += f"，短期{outlook}(置信{conf}%)"
        if vol_sig:
            line += f"，{vol_sig}"
        lines.append(line)
    signals = b.get("macro_signals", [])
    if signals:
        lines.append(f"  信号: {'; '.join(str(s) for s in signals[:3])}")
    policy = b.get("policy_impact", "")
    if policy:
        lines.append(f"  政策: {policy}")

    score = b.get("sentiment_score")
    label = b.get("sentiment_label", "")
    if score is not None:
        lines.append(f"▎情绪: {label}({score}/100)")
    crowd = b.get("crowd_behavior", "")
    extreme = b.get("extreme_signal", "无")
    if crowd:
        line = f"  散户: {crowd}"
        if extreme and extreme != "无":
            line += f" ⚠ {extreme}"
        lines.append(line)
    news = b.get("major_news", [])
    for n in news[:2]:
        lines.append(f"  📰 {n}")

    risk = b.get("risk_level", "")
    if risk:
        lines.append(f"▎风险: {risk}")
    risks = b.get("top_risks", [])
    if risks:
        lines.append(f"  风险: {'; '.join(str(r) for r in risks[:3])}")
    advice = b.get("defensive_advice", "")
    if advice:
        lines.append(f"  防御: {advice}")

    hot = b.get("hot_sectors", [])
    if hot:
        lines.append(f"▎热门行业: {', '.join(str(s) for s in hot[:5])}")
    recs = b.get("recommended_sectors", [])
    for r in recs[:2]:
        if isinstance(r, dict):
            lines.append(f"  → {r.get('sector','')}({r.get('reason','')}) {r.get('confidence','')}%")

    flow = b.get("flow_direction", "")
    inst = b.get("institutional_action", "")
    margin = b.get("margin_signal", "")
    if flow or inst:
        parts = [p for p in [flow, f"机构{inst}" if inst else "", margin] if p]
        lines.append(f"▎资金: {', '.join(parts)}")
    targets = b.get("hot_money_targets", [])
    if targets:
        lines.append(f"  追捧: {', '.join(str(t) for t in targets[:4])}")

    themes = b.get("main_themes", [])
    if themes:
        lines.append(f"▎主线: {', '.join(str(t) for t in themes[:3])}")
    sustainable = b.get("sustainable_themes", [])
    if sustainable:
        lines.append(f"  持续: {', '.join(str(s) for s in sustainable[:3])}")
    for tr in b.get("recommended_themes", [])[:2]:
        if isinstance(tr, dict):
            stocks = tr.get("stocks", [])
            st = f" | {', '.join(str(s) for s in stocks[:3])}" if stocks else ""
            lines.append(f"  → {tr.get('theme','')}: {tr.get('reason','')}{st}")

    picks = b.get("top_picks", [])
    if picks:
        lines.append("▎买入推荐:")
        for p in picks[:3]:
            if isinstance(p, dict):
                ratio = p.get("suggested_ratio", p.get("target_ratio", ""))
                rt = f" 仓位{ratio:.0%}" if isinstance(ratio, (int, float)) else ""
                lines.append(f"  ★ {p.get('name','')}({p.get('code','')}): {p.get('reason','')}{rt}")
    oversold = b.get("oversold_picks", [])
    if oversold:
        lines.append("▎超跌候选:")
        for p in oversold[:2]:
            if isinstance(p, dict):
                lines.append(f"  ○ {p.get('name','')}({p.get('code','')}): {p.get('reason','')}")

    lines.append("")
    if sig == "强烈看空":
        lines.append("⚠️ 多维度看空共振: 建议空仓观望，严禁新开仓！")
    elif sig == "偏空":
        lines.append("⚠️ 偏空信号: 不建议新买入，持仓逢高减仓")
    elif sig == "强烈看多":
        lines.append("✅ 多维度看多共振: 可积极建仓，优先选择情报推荐标的")
    elif sig == "偏多":
        lines.append("✅ 偏多信号: 可适度建仓(10-15%)，关注推荐方向")
    else:
        lines.append("➡️ 中性震荡: 保持现有仓位，非必要不操作")
    lines.append("═══════════════════════════")
    return "\n".join(lines)


# ============================================================
# 持久化
# ============================================================

def _save_intel(briefing: dict, reports: dict, fitness: RoleFitness):
    """保存情报数据"""
    os.makedirs(STATES_DIR, exist_ok=True)

    data = {
        "briefing": briefing,
        "reports": {},
        "assignments": fitness.assignments,
    }
    for role_name, report in reports.items():
        if "error" not in report:
            data["reports"][role_name] = report
    with open(INTEL_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 追加历史
    history = []
    if os.path.exists(INTEL_HISTORY_FILE):
        try:
            with open(INTEL_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, ValueError):
            history = []
    history.append({
        "timestamp": briefing.get("timestamp", ""),
        "composite_signal": briefing.get("composite_signal", ""),
        "market_phase": briefing.get("market_phase", ""),
        "sentiment_score": briefing.get("sentiment_score"),
        "risk_level": briefing.get("risk_level", ""),
        "hot_sectors": briefing.get("hot_sectors", [])[:3],
        "main_themes": briefing.get("main_themes", [])[:2],
        "ok_count": briefing.get("ok_count", 0),
    })
    if len(history) > 30:
        history = history[-30:]
    with open(INTEL_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_latest_briefing_text() -> str:
    """读取最新情报简报文本（2 小时内有效）"""
    if not os.path.exists(INTEL_FILE):
        return ""
    try:
        with open(INTEL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        briefing = data.get("briefing", {})
        ts = briefing.get("timestamp", "")
        if not ts:
            return ""
        intel_time = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        if (datetime.now() - intel_time).total_seconds() / 3600 > 2:
            return ""
        return briefing.get("briefing_text", "")
    except Exception:
        return ""


def get_latest_briefing() -> dict:
    """读取最新完整情报 dict"""
    if not os.path.exists(INTEL_FILE):
        return {}
    try:
        with open(INTEL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("briefing", {})
    except Exception:
        return {}
