"""市场战略情报模块 — 多模型并行分析，生成情报简报

每个交易周期前，7 个免费内部模型各扮演一个分析师角色:
  宏观策略师(DeepSeek) / 行业轮动(Qwen) / 情绪分析(GLM5) /
  资金流向(Kimi) / 风险预警(Minimax) / 题材挖掘(Intern-S1-Pro) / 选股(Intern-S1)

并行分析后汇总成结构化情报简报，注入到所有模型的交易提示中。
"""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from rich.console import Console
from rich.panel import Panel

import ai_advisor
from model_config import MODELS

console = Console()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATES_DIR = os.path.join(BASE_DIR, "multi_states")
INTEL_FILE = os.path.join(STATES_DIR, "_intel_briefing.json")
INTEL_HISTORY_FILE = os.path.join(STATES_DIR, "_intel_history.json")

# ============================================================
# 分析师角色定义
# ============================================================

ANALYST_ROLES = [
    {
        "role": "宏观策略师",
        "model_name": "DeepSeek-V3.2",
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
        "model_name": "Qwen3.5-397B",
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
        "model_name": "GLM5",
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
        "model_name": "Kimi-K2.5",
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
        "model_name": "Minimax2.5",
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
        "model_name": "Intern-S1-Pro",
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
        "model_name": "Intern-S1",
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


# ============================================================
# 核心逻辑
# ============================================================

def _get_model_cfg(model_name: str) -> dict | None:
    """从 MODELS 列表中查找指定名称的模型配置"""
    for cfg in MODELS:
        if cfg["name"] == model_name:
            return cfg
    return None


def _query_analyst(role_cfg: dict, market_text: str) -> dict:
    """查询单个分析师模型，返回解析后的 dict"""
    model_cfg = _get_model_cfg(role_cfg["model_name"])
    if not model_cfg:
        return {"role": role_cfg["role"], "error": f"模型 {role_cfg['model_name']} 不存在"}

    try:
        raw = ai_advisor.call_model_api(
            model_cfg, role_cfg["system_prompt"], market_text, max_retries=2,
        )
        if not raw:
            return {"role": role_cfg["role"], "model": role_cfg["model_name"],
                    "error": "返回为空"}

        # 清理 <think> 和 markdown 代码块
        text = re.sub(r"<think>[\s\S]*?</think>\s*", "", raw).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        result = json.loads(text)
        result["role"] = role_cfg["role"]
        result["model"] = role_cfg["model_name"]
        return result

    except json.JSONDecodeError:
        # JSON 解析失败，把原始文本截取作为 summary 保留
        return {
            "role": role_cfg["role"],
            "model": role_cfg["model_name"],
            "summary": (raw or "")[:200],
            "parse_error": True,
        }
    except Exception as e:
        return {"role": role_cfg["role"], "model": role_cfg["model_name"],
                "error": str(e)[:120]}


def gather_intelligence(market_text: str) -> dict:
    """并行调用 7 个分析师模型，汇总生成战略情报简报

    Args:
        market_text: 完整的市场数据文本（指数+行业+新闻+涨跌榜等）
    Returns:
        briefing dict，核心字段为 briefing_text（可直接注入交易 prompt）
    """
    console.rule("[bold #3b82f6]战略情报收集[/bold #3b82f6]")

    reports: dict[str, dict] = {}
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = {
            executor.submit(_query_analyst, role, market_text): role
            for role in ANALYST_ROLES
        }
        try:
            for future in as_completed(futures, timeout=150):
                try:
                    result = future.result()
                    role_name = result.get("role", "未知")
                    reports[role_name] = result
                    # 打印每位分析师结果
                    if "error" in result:
                        console.print(
                            f"  [red]✗[/red] {role_name} "
                            f"[dim]({result.get('model','')})[/dim]: "
                            f"{result['error'][:60]}"
                        )
                    else:
                        summary = result.get("summary", "完成")
                        if isinstance(summary, str):
                            summary = summary[:50]
                        console.print(
                            f"  [green]✓[/green] {role_name} "
                            f"[dim]({result.get('model','')})[/dim]: "
                            f"{summary}"
                        )
                except Exception as e:
                    role = futures[future]
                    reports[role["role"]] = {
                        "role": role["role"], "error": str(e)[:80],
                    }
        except TimeoutError:
            for future, role in futures.items():
                if not future.done():
                    reports[role["role"]] = {
                        "role": role["role"], "error": "超时(150s)",
                    }
                    console.print(
                        f"  [yellow]⧖[/yellow] {role['role']}: 超时"
                    )

    elapsed = time.time() - t0
    ok_count = sum(1 for r in reports.values() if "error" not in r)
    console.print(
        f"[dim]情报收集完成: {ok_count}/{len(ANALYST_ROLES)} 成功, "
        f"耗时 {elapsed:.1f}s[/dim]"
    )

    # 汇总情报
    briefing = _synthesize(reports)

    # 打印简报面板
    console.print(Panel(
        briefing.get("briefing_text", "（无）"),
        title="[bold]战略情报简报[/bold]",
        border_style="#3b82f6",
        padding=(0, 1),
    ))

    # 持久化
    _save_intel(briefing, reports)
    return briefing


# ============================================================
# 汇总逻辑
# ============================================================

def _synthesize(reports: dict) -> dict:
    """将各分析师报告汇总为结构化情报简报"""

    briefing = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ok_count": sum(1 for r in reports.values() if "error" not in r),
        "total": len(reports),
    }

    # --- 1. 宏观 ---
    macro = reports.get("宏观策略师", {})
    if "error" not in macro:
        briefing["market_phase"] = macro.get("market_phase", "未知")
        briefing["volume_signal"] = macro.get("volume_signal", "")
        briefing["macro_outlook"] = macro.get("outlook", "未知")
        briefing["macro_confidence"] = macro.get("confidence", 50)
        briefing["macro_signals"] = macro.get("key_signals", [])
        briefing["policy_impact"] = macro.get("policy_impact", "")

    # --- 2. 情绪 ---
    sent = reports.get("市场情绪分析师", {})
    if "error" not in sent:
        briefing["sentiment_score"] = sent.get("sentiment_score", 50)
        briefing["sentiment_label"] = sent.get("sentiment_label", "中性")
        briefing["news_sentiment"] = sent.get("news_sentiment", "中性")
        briefing["major_news"] = sent.get("major_news", [])
        briefing["crowd_behavior"] = sent.get("crowd_behavior", "")
        briefing["extreme_signal"] = sent.get("extreme_signal", "无")

    # --- 3. 风险 ---
    risk = reports.get("风险预警分析师", {})
    if "error" not in risk:
        briefing["risk_level"] = risk.get("risk_level", "中等")
        briefing["top_risks"] = risk.get("top_risks", [])
        briefing["overlooked_negatives"] = risk.get("overlooked_negatives", [])
        briefing["defensive_advice"] = risk.get("defensive_advice", "")

    # --- 4. 行业 ---
    sector = reports.get("行业轮动分析师", {})
    if "error" not in sector:
        briefing["hot_sectors"] = sector.get("hot_sectors", [])
        briefing["cold_sectors"] = sector.get("cold_sectors", [])
        briefing["recommended_sectors"] = sector.get("recommended_sectors", [])
        briefing["rotation_signal"] = sector.get("rotation_signal", "")

    # --- 5. 资金 ---
    money = reports.get("资金流向分析师", {})
    if "error" not in money:
        briefing["flow_direction"] = money.get("flow_direction", "")
        briefing["institutional_action"] = money.get("institutional_action", "")
        briefing["margin_signal"] = money.get("margin_signal", "")
        briefing["hot_money_targets"] = money.get("hot_money_targets", [])
        briefing["flow_anomalies"] = money.get("anomalies", [])

    # --- 6. 题材 ---
    theme = reports.get("热点题材分析师", {})
    if "error" not in theme:
        briefing["main_themes"] = theme.get("main_themes", [])
        briefing["catalysts"] = theme.get("catalysts", [])
        briefing["sustainable_themes"] = theme.get("sustainable_themes", [])
        briefing["one_day_themes"] = theme.get("one_day_themes", [])
        briefing["recommended_themes"] = theme.get("recommended", [])

    # --- 7. 选股 ---
    stock = reports.get("选股分析师", {})
    if "error" not in stock:
        briefing["top_picks"] = stock.get("top_picks", [])
        briefing["oversold_picks"] = stock.get("oversold_picks", [])
        briefing["quality_gainers"] = stock.get("quality_gainers", [])

    # --- 8. 综合信号计算 ---
    briefing["composite_signal"] = _compute_composite_signal(briefing)

    # --- 9. 生成文本 ---
    briefing["briefing_text"] = _format_text(briefing)

    return briefing


def _compute_composite_signal(b: dict) -> str:
    """基于多维度信号计算综合交易信号"""
    score = 0  # -100(极度看空) ~ +100(极度看多)

    # 宏观方向: ±30
    outlook = b.get("macro_outlook", "震荡")
    conf = min(b.get("macro_confidence", 50), 100) / 100
    if outlook == "看多":
        score += int(30 * conf)
    elif outlook == "看空":
        score -= int(30 * conf)

    # 情绪: ±25
    sent = b.get("sentiment_score", 50)
    score += int((sent - 50) * 0.5)  # 0→-25, 50→0, 100→+25

    # 风险: ±25
    risk_map = {"低": 15, "中等": 0, "高": -15, "极高": -25}
    score += risk_map.get(b.get("risk_level", "中等"), 0)

    # 资金方向: ±10
    inst = b.get("institutional_action", "")
    if inst == "抢筹":
        score += 10
    elif inst == "出货":
        score -= 10

    # 杠杆: ±10
    margin = b.get("margin_signal", "")
    if "加仓" in margin:
        score += 8
    elif "减仓" in margin:
        score -= 8

    # 映射到信号文字
    if score >= 40:
        return "强烈看多"
    elif score >= 15:
        return "偏多"
    elif score > -15:
        return "中性震荡"
    elif score > -40:
        return "偏空"
    else:
        return "强烈看空"


def _format_text(b: dict) -> str:
    """生成纯文本情报简报，供注入交易 prompt"""
    lines = ["═══ AI 多模型战略情报简报 ═══"]

    # 综合信号（最重要，放在最前面）
    sig = b.get("composite_signal", "中性震荡")
    lines.append(f"★ 综合信号: {sig}")

    # 宏观
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

    # 情绪
    score = b.get("sentiment_score")
    label = b.get("sentiment_label", "")
    if score is not None:
        lines.append(f"▎情绪: {label}({score}/100)")
    crowd = b.get("crowd_behavior", "")
    extreme = b.get("extreme_signal", "无")
    if crowd:
        line = f"  散户行为: {crowd}"
        if extreme and extreme != "无":
            line += f" ⚠ 极端信号: {extreme}"
        lines.append(line)
    news = b.get("major_news", [])
    if news:
        for n in news[:2]:
            lines.append(f"  📰 {n}")

    # 风险
    risk = b.get("risk_level", "")
    if risk:
        lines.append(f"▎风险: {risk}")
    risks = b.get("top_risks", [])
    if risks:
        lines.append(f"  风险点: {'; '.join(str(r) for r in risks[:3])}")
    neg = b.get("overlooked_negatives", [])
    if neg:
        lines.append(f"  隐患: {'; '.join(str(n) for n in neg[:2])}")
    advice = b.get("defensive_advice", "")
    if advice:
        lines.append(f"  防御: {advice}")

    # 行业
    hot = b.get("hot_sectors", [])
    cold = b.get("cold_sectors", [])
    if hot:
        lines.append(f"▎热门行业: {', '.join(str(s) for s in hot[:5])}")
    if cold:
        lines.append(f"  冷门行业: {', '.join(str(s) for s in cold[:3])}")
    rotation = b.get("rotation_signal", "")
    if rotation and rotation != "无":
        lines.append(f"  轮动信号: {rotation}")
    recs = b.get("recommended_sectors", [])
    if recs:
        for r in recs[:2]:
            if isinstance(r, dict):
                lines.append(
                    f"  → 推荐: {r.get('sector','')} "
                    f"({r.get('reason','')}) "
                    f"置信{r.get('confidence','')}%"
                )

    # 资金
    flow = b.get("flow_direction", "")
    inst = b.get("institutional_action", "")
    margin = b.get("margin_signal", "")
    if flow or inst:
        parts = []
        if flow:
            parts.append(f"流向{flow}")
        if inst:
            parts.append(f"机构{inst}")
        if margin:
            parts.append(margin)
        lines.append(f"▎资金: {', '.join(parts)}")
    targets = b.get("hot_money_targets", [])
    if targets:
        lines.append(f"  资金追捧: {', '.join(str(t) for t in targets[:4])}")
    anomalies = b.get("flow_anomalies", [])
    if anomalies:
        lines.append(f"  异常: {'; '.join(str(a) for a in anomalies[:2])}")

    # 题材
    themes = b.get("main_themes", [])
    if themes:
        lines.append(f"▎今日主线: {', '.join(str(t) for t in themes[:3])}")
    catalysts = b.get("catalysts", [])
    if catalysts:
        lines.append(f"  催化剂: {'; '.join(str(c) for c in catalysts[:3])}")
    sustainable = b.get("sustainable_themes", [])
    oneday = b.get("one_day_themes", [])
    if sustainable:
        lines.append(f"  持续性: {', '.join(str(s) for s in sustainable[:3])}")
    if oneday:
        lines.append(f"  一日游: {', '.join(str(o) for o in oneday[:2])}")
    theme_recs = b.get("recommended_themes", [])
    if theme_recs:
        for tr in theme_recs[:2]:
            if isinstance(tr, dict):
                stocks = tr.get("stocks", [])
                stock_text = f" | 标的: {', '.join(str(s) for s in stocks[:3])}" if stocks else ""
                lines.append(
                    f"  → {tr.get('theme','')}: "
                    f"{tr.get('reason','')}{stock_text}"
                )

    # 选股推荐
    picks = b.get("top_picks", [])
    oversold = b.get("oversold_picks", [])
    if picks:
        lines.append("▎分析师买入推荐:")
        for p in picks[:3]:
            if isinstance(p, dict):
                ratio = p.get("suggested_ratio", p.get("target_ratio", ""))
                ratio_text = f" 建议仓位{ratio:.0%}" if isinstance(ratio, (int, float)) else ""
                lines.append(
                    f"  ★ {p.get('name','')}({p.get('code','')}): "
                    f"{p.get('reason','')}{ratio_text}"
                )
    if oversold:
        lines.append("▎超跌候选:")
        for p in oversold[:2]:
            if isinstance(p, dict):
                lines.append(
                    f"  ○ {p.get('name','')}({p.get('code','')}): "
                    f"{p.get('reason','')}"
                )

    # 综合操作建议（基于多维度共振）
    lines.append("")
    sig = b.get("composite_signal", "中性震荡")
    if sig == "强烈看空":
        lines.append("⚠️ 多维度看空共振: 建议空仓观望，严禁新开仓！")
    elif sig == "偏空":
        lines.append("⚠️ 偏空信号: 不建议新买入，持仓考虑逢高减仓")
    elif sig == "强烈看多":
        lines.append("✅ 多维度看多共振: 可积极建仓，优先选择情报推荐标的")
    elif sig == "偏多":
        lines.append("✅ 偏多信号: 可适度建仓(10-15%)，关注情报推荐方向")
    else:
        lines.append("➡️ 中性震荡: 保持现有仓位，非必要不操作")

    lines.append("═══════════════════════════")
    return "\n".join(lines)


# ============================================================
# 持久化 & 工具函数
# ============================================================

def _save_intel(briefing: dict, reports: dict):
    """保存情报数据到文件"""
    os.makedirs(STATES_DIR, exist_ok=True)

    # 当前情报
    data = {"briefing": briefing, "reports": {}}
    for role_name, report in reports.items():
        # 只保留成功的报告，节省空间
        if "error" not in report:
            data["reports"][role_name] = report
    with open(INTEL_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 追加历史（保留最近 30 条）
    history = []
    if os.path.exists(INTEL_HISTORY_FILE):
        try:
            with open(INTEL_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, ValueError):
            history = []

    # 历史只保留摘要字段
    history_entry = {
        "timestamp": briefing.get("timestamp", ""),
        "composite_signal": briefing.get("composite_signal", ""),
        "market_phase": briefing.get("market_phase", ""),
        "sentiment_score": briefing.get("sentiment_score"),
        "risk_level": briefing.get("risk_level", ""),
        "hot_sectors": briefing.get("hot_sectors", [])[:3],
        "main_themes": briefing.get("main_themes", [])[:2],
        "ok_count": briefing.get("ok_count", 0),
    }
    history.append(history_entry)
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
        age_hours = (datetime.now() - intel_time).total_seconds() / 3600
        if age_hours > 2:
            return ""
        return briefing.get("briefing_text", "")
    except Exception:
        return ""


def get_latest_briefing() -> dict:
    """读取最新完整情报 dict（供 export_data 使用）"""
    if not os.path.exists(INTEL_FILE):
        return {}
    try:
        with open(INTEL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("briefing", {})
    except Exception:
        return {}
