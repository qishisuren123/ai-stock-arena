"""多模型对比自动交易系统 - 10 个模型独立账户并行交易

用法:
    conda activate stock
    cd /mnt/shared-storage-user/renyiming/tonghuashun
    python auto_trader_multi.py

建议在 tmux 中运行，Ctrl+C 优雅退出并保存所有模型状态。
"""

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import ai_advisor
import market_data
from board_meeting import board_fund, conduct_board_meeting, get_board_summary_for_report
from model_config import MODELS, get_safe_name
from simulator import SimAccount, INITIAL_CASH
import market_intel

console = Console()

# 状态文件目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATES_DIR = os.path.join(BASE_DIR, "multi_states")
os.makedirs(STATES_DIR, exist_ok=True)

# 优雅退出标志
_running = True


class ModelRunner:
    """单个模型的运行器：管理配置 + 独立账户 + 本轮结果"""

    def __init__(self, model_cfg: dict):
        self.cfg = model_cfg
        self.name = model_cfg["name"]
        safe = get_safe_name(self.name)
        state_file = os.path.join(STATES_DIR, f"sim_state_{safe}.json")
        self.account = SimAccount(state_file=state_file)
        # 本轮结果
        self.advice = None
        self.elapsed = 0.0
        self.status = "待机"
        self.error = None


# 初始化所有模型运行器
runners: list[ModelRunner] = [ModelRunner(cfg) for cfg in MODELS]


def _signal_handler(sig, frame):
    global _running
    _running = False
    console.print("\n[yellow]收到退出信号，正在保存所有模型状态...[/yellow]")
    for r in runners:
        r.account.save()
    board_fund.save()
    console.print("[green]所有状态已保存，再见！[/green]")
    sys.exit(0)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def save_thinking_data():
    """保存各模型本轮思考过程到 _thinking.json"""
    thinking = {}
    for r in runners:
        safe = get_safe_name(r.name)
        analysis = ""
        actions = []
        if r.advice:
            analysis = r.advice.get("analysis", "")
            actions = r.advice.get("actions", [])
        thinking[safe] = {
            "name": r.name,
            "analysis": analysis,
            "actions": actions,
            "elapsed": round(r.elapsed, 1),
            "status": r.status,
        }
    path = os.path.join(STATES_DIR, "_thinking.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(thinking, f, ensure_ascii=False, indent=2)


def save_hot_codes(top_codes: list):
    """保存当前热门股票代码到 _hot_codes.json"""
    path = os.path.join(STATES_DIR, "_hot_codes.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(top_codes, f, ensure_ascii=False)


def generate_battle_report(overview: str, prices: dict):
    """调用 Kimi 生成本轮战报解说"""
    # 汇总本轮各模型操作摘要
    lines = []
    for r in runners:
        total = r.account.total_value(prices)
        pnl_pct = (total - INITIAL_CASH) / INITIAL_CASH * 100
        actions = r.advice.get("actions", []) if r.advice else []
        if actions:
            ops = ", ".join(
                f"{a.get('action','?')}{a.get('name', a.get('code',''))}"
                for a in actions
            )
        else:
            ops = "观望"
        lines.append(f"- {r.name}: 收益{pnl_pct:+.2f}%, 操作: {ops}")
    summary = "\n".join(lines)

    system_prompt = (
        "你是一位风趣幽默的 A 股解说员，负责为 AI 炒股竞技场写战报。"
        "请用 200 字以内写一段生动有趣的解说，点评各模型本轮表现，可以适当调侃。"
        "不要用 markdown 格式，直接输出纯文本。"
    )
    user_msg = f"大盘概况：\n{overview}\n\n各模型本轮表现：\n{summary}"

    # 追加董事会信息
    board_summary = get_board_summary_for_report(prices)
    if board_summary:
        user_msg += f"\n\n{board_summary}"

    # 从 model_config.py 中取 Minimax 配置用于战报生成（model_config.py 已在 .gitignore）
    reporter_cfg = None
    for cfg in MODELS:
        if cfg["name"] == "Minimax2.5":
            reporter_cfg = cfg
            break
    if not reporter_cfg:
        console.print("[yellow]未找到战报模型配置，跳过[/yellow]")
        return

    report_text = ai_advisor.call_model_api(reporter_cfg, system_prompt, user_msg)
    # 过滤掉模型可能输出的 <think>...</think> 思考过程
    import re as _re
    report_text = _re.sub(r"<think>[\s\S]*?</think>\s*", "", report_text).strip()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 保存当前战报
    current = {"timestamp": now_str, "report": report_text.strip()}
    cur_path = os.path.join(STATES_DIR, "_battle_report.json")
    with open(cur_path, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)

    # 追加到历史战报（上限 50 条）
    hist_path = os.path.join(STATES_DIR, "_battle_reports_history.json")
    history = []
    if os.path.exists(hist_path):
        try:
            with open(hist_path, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, ValueError):
            history = []
    history.append(current)
    if len(history) > 50:
        history = history[-50:]
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    console.print(f"[dim]战报已生成: {report_text[:60]}...[/dim]")


def is_trading_time() -> bool:
    """判断当前是否为 A 股交易时间"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return (930 <= t <= 1130) or (1300 <= t <= 1500)


def next_trading_window() -> str:
    """返回下一个交易窗口描述"""
    now = datetime.now()
    t = now.hour * 100 + now.minute
    if now.weekday() >= 5:
        days = 7 - now.weekday()
        return f"周一 09:30"
    if t < 930:
        return "今日 09:30"
    elif 1130 < t < 1300:
        return "今日 13:00"
    else:
        if now.weekday() == 4:
            return "下周一 09:30"
        return "明日 09:30"


def _query_single_model(runner: ModelRunner, market_text: str,
                        prices: dict[str, float]) -> ModelRunner:
    """查询单个模型（在线程池中执行）"""
    t0 = time.time()
    try:
        portfolio_info = runner.account.get_portfolio_summary(prices)
        # 构建最近交易记录（供模型参考持仓期）
        trade_history = ""
        recent = runner.account.trade_log[-5:]
        if recent:
            lines = []
            for log in recent:
                action_cn = "买入" if log.get("action") == "buy" else "卖出"
                pnl_text = f" 盈亏{log.get('pnl',0):+.2f}" if log.get("action") == "sell" else ""
                lines.append(
                    f"{log.get('time','')} {action_cn} {log.get('name','')}({log.get('code','')}) "
                    f"{log.get('qty',0)}股 @ {log.get('price',0):.2f}{pnl_text}"
                )
            trade_history = "\n".join(lines)
        advice = ai_advisor.get_structured_advice_multi(
            runner.cfg, market_text, portfolio_info, trade_history
        )
        runner.advice = advice
        runner.status = "成功"
    except Exception as e:
        runner.advice = {"analysis": str(e), "actions": []}
        runner.status = "失败"
        runner.error = str(e)
    runner.elapsed = time.time() - t0
    return runner


def query_all_models(market_text: str, prices: dict[str, float]):
    """并行查询所有模型，总超时 180s，超时的模型标记跳过"""
    console.print(f"[dim]正在并行查询 {len(runners)} 个模型...[/dim]")
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_query_single_model, r, market_text, prices): r
            for r in runners
        }
        try:
            for future in as_completed(futures, timeout=180):
                try:
                    future.result()
                except Exception as e:
                    r = futures[future]
                    r.status = "失败"
                    r.error = str(e)
                    r.advice = {"analysis": f"查询失败: {e}", "actions": []}
        except TimeoutError:
            # 标记未完成的模型
            for future, r in futures.items():
                if not future.done():
                    r.status = "超时"
                    r.advice = {"analysis": "查询超时（180s）", "actions": []}
                    console.print(f"  [yellow]{r.name} 查询超时[/yellow]")


def execute_trades(prices: dict[str, float]):
    """对每个模型独立执行交易（先卖后买），带持仓期守卫"""
    for r in runners:
        if not r.advice:
            continue
        actions = r.advice.get("actions", [])
        if not actions:
            continue

        # 获取需要买入的股票价格
        buy_codes = [a["code"] for a in actions if a.get("action") == "buy"]
        local_prices = dict(prices)  # 复制一份
        if buy_codes:
            new_prices = market_data.get_realtime_prices(buy_codes)
            local_prices.update(new_prices)

        # 先卖（带持仓期检查）
        for act in actions:
            if act.get("action") != "sell":
                continue
            code = act["code"]
            # 持仓期守卫：持仓不足 1 天不允许卖（T+1）
            if code in r.account.positions:
                pos = r.account.positions[code]
                buy_date = pos.get("buy_date", "")
                if buy_date:
                    try:
                        bd = datetime.strptime(buy_date, "%Y-%m-%d")
                        hold_days = (datetime.now() - bd).days
                        if hold_days < 1:
                            console.print(
                                f"  [dim]{r.name}: 跳过卖出 {code}（T+1，今日买入）[/dim]"
                            )
                            continue
                    except ValueError:
                        pass
            sell_price = local_prices.get(code)
            if sell_price:
                r.account.sell(code, sell_price)

        # 后买
        for act in actions:
            if act.get("action") != "buy":
                continue
            code = act["code"]
            name = act.get("name", code)
            ratio = min(act.get("ratio", 0.2), 0.25)  # 强制上限 25%
            buy_price = local_prices.get(code)
            if buy_price:
                r.account.buy(code, name, buy_price, ratio, local_prices)

        r.account.save()


def print_leaderboard(prices: dict[str, float]):
    """Rich 排行榜：排名/模型/总资产/收益率/持仓/耗时/状态"""
    # 计算各模型总资产
    data = []
    for r in runners:
        # 更新持仓价格
        held_codes = list(r.account.positions.keys())
        local_prices = dict(prices)
        if held_codes:
            fresh = market_data.get_realtime_prices(held_codes)
            local_prices.update(fresh)
        total = r.account.total_value(local_prices)
        pnl_pct = (total - INITIAL_CASH) / INITIAL_CASH * 100
        # 持仓摘要
        if r.account.positions:
            pos_list = [f"{p['name']}({c})" for c, p in r.account.positions.items()]
            pos_str = ", ".join(pos_list)
        else:
            pos_str = "空仓"
        data.append((r.name, total, pnl_pct, pos_str, r.elapsed, r.status))

    # 按总资产排序
    data.sort(key=lambda x: x[1], reverse=True)

    table = Table(title=f"模型排行榜 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                  border_style="cyan", show_lines=True)
    table.add_column("#", justify="center", style="bold", width=3)
    table.add_column("模型", style="bold cyan", width=16)
    table.add_column("总资产", justify="right", width=10)
    table.add_column("收益率", justify="right", width=9)
    table.add_column("持仓", width=30)
    table.add_column("耗时", justify="right", width=7)
    table.add_column("状态", justify="center", width=6)

    for i, (name, total, pnl_pct, pos_str, elapsed, status) in enumerate(data, 1):
        # 收益率颜色
        if pnl_pct > 0:
            pnl_color = "green"
        elif pnl_pct < 0:
            pnl_color = "red"
        else:
            pnl_color = "white"

        # 状态颜色
        if status == "成功":
            status_styled = f"[green]{status}[/green]"
        elif status == "失败" or status == "超时":
            status_styled = f"[red]{status}[/red]"
        else:
            status_styled = f"[dim]{status}[/dim]"

        table.add_row(
            str(i),
            name,
            f"{total:.2f}",
            f"[{pnl_color}]{pnl_pct:+.2f}%[/{pnl_color}]",
            pos_str,
            f"{elapsed:.1f}s",
            status_styled,
        )

    console.print(table)


def run_trading_cycle():
    """执行一轮多模型交易周期"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.rule(f"[bold cyan]多模型交易周期 {now_str}[/bold cyan]")

    # 1. 获取市场数据（共享）
    console.print("[dim]正在获取市场数据...[/dim]")
    try:
        overview = market_data.get_market_overview()
        hot = market_data.get_hot_stocks(15)
        losers = market_data.get_losers(10)
        sectors = market_data.get_sector_overview()
        concepts = market_data.get_concept_hot()
        breadth = market_data.get_market_breadth()
    except Exception as e:
        console.print(f"[red]获取市场数据失败: {e}[/red]")
        return

    console.print(Panel(overview, title="大盘指数", border_style="blue"))

    # 2. 获取所有模型持仓股票的价格
    all_held_codes = set()
    for r in runners:
        all_held_codes.update(r.account.positions.keys())
    prices = market_data.get_realtime_prices(list(all_held_codes)) if all_held_codes else {}

    # 3. 获取财经新闻
    news = market_data.get_financial_news()

    # 4. 获取深度数据（融资融券 + PE分位）
    top_codes = market_data.get_hot_stock_codes(5)
    deep_codes = list(set(list(all_held_codes) + top_codes))
    deep_info = market_data.get_stock_deep_info(deep_codes) if deep_codes else ""

    market_text = f"【大盘指数】\n{overview}"
    if breadth:
        market_text += f"\n{breadth}"

    # 新增：获取北向资金
    try:
        northbound = market_data.get_northbound_flow()
        if northbound:
            market_text += f"\n\n【北向资金】\n{northbound}"
    except Exception:
        pass

    if sectors:
        market_text += f"\n\n【行业板块】\n{sectors}"
    if concepts:
        market_text += f"\n\n【热门概念】\n{concepts}"
    market_text += f"\n\n【涨幅榜TOP15】\n{hot}"
    if losers:
        market_text += f"\n\n【跌幅榜TOP10】\n{losers}"
    market_text += f"\n\n{news}"
    if deep_info:
        market_text += f"\n\n{deep_info}"

    # 保存热门股票代码
    save_hot_codes(top_codes)

    # ★★★ 战略情报收集 — 7 模型竞聘上岗 + 绩效考核 ★★★
    try:
        # 提取上证指数点位，供情报模块做回溯验证
        index_level = 0.0
        try:
            idx_data = market_data._qq_fetch(["sh000001"])
            if "sh000001" in idx_data:
                index_level = idx_data["sh000001"]["price"]
        except Exception:
            pass

        intel_briefing = market_intel.gather_intelligence(
            market_text, index_level=index_level,
        )
        briefing_text = intel_briefing.get("briefing_text", "")
        if briefing_text:
            # 将情报简报注入到 market_text 的最前面
            market_text = f"{briefing_text}\n\n{market_text}"
    except Exception as e:
        console.print(f"[yellow]情报收集异常（不影响交易）: {e}[/yellow]")

    # 4. 并行查询所有模型（现在 market_text 包含情报简报）
    query_all_models(market_text, prices)

    # 保存各模型思考过程
    save_thinking_data()

    # 5. 打印各模型 AI 分析摘要
    for r in runners:
        analysis = r.advice.get("analysis", "") if r.advice else ""
        actions_count = len(r.advice.get("actions", [])) if r.advice else 0
        status_icon = "✓" if r.status == "成功" else "✗"
        console.print(
            f"  {status_icon} [bold]{r.name}[/bold] ({r.elapsed:.1f}s) "
            f"操作数:{actions_count} | {analysis[:60]}"
        )

    # 6. 执行交易
    execute_trades(prices)

    # 7. 刷新价格并打印排行榜
    all_held_codes = set()
    for r in runners:
        all_held_codes.update(r.account.positions.keys())
    if all_held_codes:
        prices = market_data.get_realtime_prices(list(all_held_codes))
    print_leaderboard(prices)

    # 生成 Kimi 战报解说
    try:
        generate_battle_report(overview, prices)
    except Exception as e:
        console.print(f"[yellow]战报生成失败（不影响交易）: {e}[/yellow]")

    # 董事会会议：投票 → 决议 → 进化
    try:
        conduct_board_meeting(runners, market_text, prices)
    except Exception as e:
        console.print(f"[yellow]董事会会议异常: {e}[/yellow]")

    console.print("[dim]所有模型状态已保存[/dim]")

    # 保存最新价格快照并推送到 GitHub
    try:
        prices_file = os.path.join(STATES_DIR, "_latest_prices.json")
        with open(prices_file, "w", encoding="utf-8") as f:
            json.dump(prices, f, ensure_ascii=False, indent=2)
        sync_script = os.path.join(BASE_DIR, "sync_to_github.sh")
        if os.path.exists(sync_script):
            console.print("[dim]正在同步到 GitHub...[/dim]")
            result = subprocess.run(
                ["bash", sync_script],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                console.print("[green]GitHub 同步完成[/green]")
            else:
                console.print(f"[yellow]GitHub 同步失败: {result.stderr[:200]}[/yellow]")
    except Exception as e:
        console.print(f"[yellow]同步异常（不影响交易）: {e}[/yellow]")


def _sync_now():
    """手动触发同步：导出数据并推送 GitHub"""
    console.print("[cyan]手动同步中...[/cyan]")
    try:
        prices_file = os.path.join(STATES_DIR, "_latest_prices.json")
        # 刷新价格
        all_held_codes = set()
        for r in runners:
            all_held_codes.update(r.account.positions.keys())
        prices = market_data.get_realtime_prices(list(all_held_codes)) if all_held_codes else {}
        with open(prices_file, "w", encoding="utf-8") as f:
            json.dump(prices, f, ensure_ascii=False, indent=2)
        sync_script = os.path.join(BASE_DIR, "sync_to_github.sh")
        if os.path.exists(sync_script):
            result = subprocess.run(["bash", sync_script], capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                console.print("[green]手动同步完成[/green]")
            else:
                console.print(f"[yellow]同步失败: {result.stderr[:200]}[/yellow]")
    except Exception as e:
        console.print(f"[yellow]同步异常: {e}[/yellow]")


def _wait_with_input(seconds: float):
    """等待指定秒数，期间按回车立即触发同步，输入 q 退出"""
    import select
    console.print("[dim]按回车立即同步网站，输入 q 退出[/dim]")
    end_time = time.time() + seconds
    while _running and time.time() < end_time:
        # 检查 stdin 是否有输入（非阻塞，等 1 秒）
        ready, _, _ = select.select([sys.stdin], [], [], 1.0)
        if ready:
            line = sys.stdin.readline().strip().lower()
            if line == "q":
                console.print("[yellow]收到退出指令[/yellow]")
                for r in runners:
                    r.account.save()
                sys.exit(0)
            else:
                _sync_now()


def main():
    """主循环"""
    console.print(Panel(
        f"[bold]多模型对比自动交易系统[/bold]\n"
        f"模型数量: {len(runners)} | 初始资金: 10,000 元/模型\n"
        f"董事会公共基金: 10,000 元（共同管理）\n"
        f"交易时间: 周一至周五 9:30-11:30, 13:00-15:00\n"
        f"按 Ctrl+C 退出并保存所有状态",
        title="启动",
        border_style="cyan",
    ))

    # 打印模型列表
    model_table = Table(title="注册模型", border_style="blue")
    model_table.add_column("#", justify="center", width=3)
    model_table.add_column("模型", width=16)
    model_table.add_column("API 格式", width=10)
    model_table.add_column("Base URL", width=50)
    for i, r in enumerate(runners, 1):
        model_table.add_row(str(i), r.name, r.cfg["api_format"], r.cfg["base_url"])
    console.print(model_table)

    # 启动时打印排行榜
    all_held_codes = set()
    for r in runners:
        all_held_codes.update(r.account.positions.keys())
    prices = market_data.get_realtime_prices(list(all_held_codes)) if all_held_codes else {}
    print_leaderboard(prices)

    heartbeat_interval = 600  # 非交易时间每 10 分钟心跳
    last_heartbeat = 0

    # 交易时间点：每天只在 10:00 和 14:00 各执行一次（减少频繁交易）
    TRADE_HOURS = [10, 14]
    last_trade_key = ""  # "YYYY-MM-DD-HH" 防止同一时段重复执行

    while _running:
        if is_trading_time():
            now = datetime.now()
            trade_key = f"{now.strftime('%Y-%m-%d')}-{now.hour}"

            # 只在指定时间点（10:xx 或 14:xx）执行，且每个时段只执行一次
            if now.hour in TRADE_HOURS and trade_key != last_trade_key:
                last_trade_key = trade_key
                try:
                    run_trading_cycle()
                except Exception as e:
                    console.print(f"[red]交易周期异常: {e}[/red]")
                    for r in runners:
                        r.account.save()

            # 等待到下一个整点
            next_hour = (now + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )
            wait_secs = (next_hour - now).total_seconds()
            # 找到下一个交易时间点
            next_trade = "无"
            for h in TRADE_HOURS:
                if now.hour < h:
                    next_trade = f"今日 {h}:00"
                    break
            if next_trade == "无":
                next_trade = "明日 10:00"

            console.print(
                f"[dim]下次交易: {next_trade}"
                f"（等待 {int(wait_secs // 60)} 分钟）[/dim]"
            )

            _wait_with_input(wait_secs)
        else:
            now_ts = time.time()
            if now_ts - last_heartbeat >= heartbeat_interval:
                next_win = next_trading_window()
                console.print(
                    f"[dim]{datetime.now().strftime('%H:%M')} "
                    f"非交易时间，下一交易窗口: {next_win}[/dim]"
                )
                last_heartbeat = now_ts
            _wait_with_input(10)


if __name__ == "__main__":
    main()
