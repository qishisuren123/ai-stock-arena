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
from model_config import MODELS, get_safe_name
from simulator import SimAccount, INITIAL_CASH

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
    console.print("[green]所有状态已保存，再见！[/green]")
    sys.exit(0)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


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
        advice = ai_advisor.get_structured_advice_multi(
            runner.cfg, market_text, portfolio_info
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
    """并行查询所有模型，总超时 120s"""
    console.print(f"[dim]正在并行查询 {len(runners)} 个模型...[/dim]")
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_query_single_model, r, market_text, prices): r
            for r in runners
        }
        for future in as_completed(futures, timeout=120):
            try:
                future.result()
            except Exception as e:
                r = futures[future]
                r.status = "超时"
                r.error = str(e)
                r.advice = {"analysis": f"超时: {e}", "actions": []}


def execute_trades(prices: dict[str, float]):
    """对每个模型独立执行交易（先卖后买）"""
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

        # 先卖
        for act in actions:
            if act.get("action") != "sell":
                continue
            code = act["code"]
            sell_price = local_prices.get(code)
            if sell_price:
                r.account.sell(code, sell_price)

        # 后买
        for act in actions:
            if act.get("action") != "buy":
                continue
            code = act["code"]
            name = act.get("name", code)
            ratio = act.get("ratio", 0.2)
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
        hot = market_data.get_hot_stocks(10)
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

    market_text = f"【大盘指数】\n{overview}\n\n【涨幅榜TOP10】\n{hot}\n\n{news}"
    if deep_info:
        market_text += f"\n\n{deep_info}"

    # 4. 并行查询所有模型
    query_all_models(market_text, prices)

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


def main():
    """主循环"""
    console.print(Panel(
        f"[bold]多模型对比自动交易系统[/bold]\n"
        f"模型数量: {len(runners)} | 初始资金: 10,000 元/模型\n"
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

    while _running:
        if is_trading_time():
            try:
                run_trading_cycle()
            except Exception as e:
                console.print(f"[red]交易周期异常: {e}[/red]")
                for r in runners:
                    r.account.save()

            # 等待到下一个整点
            now = datetime.now()
            next_hour = (now + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )
            wait_secs = (next_hour - now).total_seconds()
            console.print(
                f"[dim]下次执行: {next_hour.strftime('%H:%M')}"
                f"（等待 {int(wait_secs // 60)} 分钟）[/dim]"
            )

            end_time = time.time() + wait_secs
            while _running and time.time() < end_time:
                time.sleep(1)
        else:
            now_ts = time.time()
            if now_ts - last_heartbeat >= heartbeat_interval:
                next_win = next_trading_window()
                console.print(
                    f"[dim]{datetime.now().strftime('%H:%M')} "
                    f"非交易时间，下一交易窗口: {next_win}[/dim]"
                )
                last_heartbeat = now_ts
            time.sleep(10)


if __name__ == "__main__":
    main()
