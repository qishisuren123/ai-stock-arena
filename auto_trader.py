"""自动模拟交易主循环 - tmux 挂机入口

用法:
    conda activate stock
    cd /mnt/shared-storage-user/renyiming/tonghuashun
    python auto_trader.py

建议在 tmux 中运行，Ctrl+C 优雅退出并保存状态。
"""

import signal
import sys
import time
from datetime import datetime, timedelta

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import market_data
import ai_advisor
from simulator import SimAccount

console = Console()

# 全局账户实例
account = SimAccount()

# 优雅退出标志
_running = True


def _signal_handler(sig, frame):
    global _running
    _running = False
    console.print("\n[yellow]收到退出信号，正在保存状态...[/yellow]")
    account.save()
    console.print("[green]状态已保存，再见！[/green]")
    sys.exit(0)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def is_trading_time() -> bool:
    """判断当前是否为 A 股交易时间（周一至周五 9:30-11:30, 13:00-15:00）"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周六日
        return False
    t = now.hour * 100 + now.minute
    return (930 <= t <= 1130) or (1300 <= t <= 1500)


def next_trading_window() -> str:
    """返回距离下一个交易窗口的描述"""
    now = datetime.now()
    t = now.hour * 100 + now.minute

    if now.weekday() >= 5:
        # 周末，计算到周一 9:30
        days = 7 - now.weekday()
        target = now.replace(hour=9, minute=30, second=0) + timedelta(days=days)
        return f"周一 09:30（{(target - now).seconds // 3600}h后）"

    if t < 930:
        return "今日 09:30"
    elif 1130 < t < 1300:
        return "今日 13:00"
    else:
        # 收盘后，明天
        if now.weekday() == 4:  # 周五
            return "下周一 09:30"
        return "明日 09:30"


def print_portfolio(prices: dict[str, float]):
    """Rich 美化打印持仓状态"""
    total = account.total_value(prices)
    pnl_total = total - 10000.0
    pnl_color = "green" if pnl_total >= 0 else "red"

    # 摘要面板
    summary = (
        f"总资产: [bold]{total:.2f}[/bold] 元  |  "
        f"现金: {account.cash:.2f} 元  |  "
        f"收益: [{pnl_color}]{pnl_total:+.2f} ({pnl_total/100:+.2f}%)[/{pnl_color}]  |  "
        f"已实现盈亏: {account.realized_pnl:+.2f}"
    )
    console.print(Panel(summary, title="账户概览", border_style="cyan"))

    # 持仓表格
    if account.positions:
        table = Table(title="当前持仓", border_style="blue")
        table.add_column("股票", style="bold")
        table.add_column("数量", justify="right")
        table.add_column("成本", justify="right")
        table.add_column("现价", justify="right")
        table.add_column("市值", justify="right")
        table.add_column("浮盈", justify="right")
        table.add_column("仓位", justify="right")

        for code, pos in account.positions.items():
            cur_price = prices.get(code, pos["avg_cost"])
            mkt_val = cur_price * pos["qty"]
            unrealized = mkt_val - pos["total_cost"]
            pct = unrealized / pos["total_cost"] * 100 if pos["total_cost"] else 0
            ratio = mkt_val / total * 100 if total else 0
            color = "green" if unrealized >= 0 else "red"

            table.add_row(
                f"{pos['name']}({code})",
                str(pos["qty"]),
                f"{pos['avg_cost']:.2f}",
                f"{cur_price:.2f}",
                f"{mkt_val:.2f}",
                f"[{color}]{unrealized:+.2f}({pct:+.1f}%)[/{color}]",
                f"{ratio:.1f}%",
            )
        console.print(table)
    else:
        console.print("[dim]当前空仓[/dim]")


def run_trading_cycle():
    """执行一轮交易：获取数据 → AI 分析 → 先卖后买 → 打印持仓"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.rule(f"[bold cyan]交易周期 {now_str}[/bold cyan]")

    # 1. 获取市场数据
    console.print("[dim]正在获取市场数据...[/dim]")
    try:
        overview = market_data.get_market_overview()
        hot = market_data.get_hot_stocks(10)
    except Exception as e:
        console.print(f"[red]获取市场数据失败: {e}[/red]")
        return

    console.print(Panel(overview, title="大盘指数", border_style="blue"))

    # 2. 获取持仓股票的实时价格
    held_codes = list(account.positions.keys())
    prices = market_data.get_realtime_prices(held_codes) if held_codes else {}

    # 3. 获取财经新闻
    news = market_data.get_financial_news()

    # 4. 获取深度数据（融资融券 + PE分位）
    top_codes = market_data.get_hot_stock_codes(5)
    deep_codes = list(set(held_codes + top_codes))
    deep_info = market_data.get_stock_deep_info(deep_codes) if deep_codes else ""

    # 5. 生成持仓摘要给 AI
    portfolio_info = account.get_portfolio_summary(prices)
    market_text = f"【大盘指数】\n{overview}\n\n【涨幅榜TOP10】\n{hot}\n\n{news}"
    if deep_info:
        market_text += f"\n\n{deep_info}"

    # 6. 调用 AI 获取结构化指令
    console.print("[dim]正在请求 AI 分析...[/dim]")
    try:
        advice = ai_advisor.get_structured_advice(market_text, portfolio_info)
    except Exception as e:
        console.print(f"[red]AI 分析失败: {e}[/red]")
        return

    # 打印 AI 分析
    analysis = advice.get("analysis", "无分析")
    console.print(Panel(analysis, title="AI 分析", border_style="green"))

    actions = advice.get("actions", [])
    if not actions:
        console.print("[dim]AI 建议: 本轮不操作[/dim]")
        print_portfolio(prices)
        account.save()
        return

    # 7. 执行交易：先卖后买
    # 收集需要买入的股票代码，提前获取价格
    buy_codes = [a["code"] for a in actions if a.get("action") == "buy"]
    if buy_codes:
        new_prices = market_data.get_realtime_prices(buy_codes)
        prices.update(new_prices)

    # 先执行卖出
    for act in actions:
        if act.get("action") != "sell":
            continue
        code = act["code"]
        sell_price = prices.get(code)
        if not sell_price:
            console.print(f"[yellow]跳过卖出 {code}: 无法获取价格[/yellow]")
            continue
        result = account.sell(code, sell_price)
        if result:
            console.print(f"[red]  ↓ {result}[/red]")

    # 再执行买入
    for act in actions:
        if act.get("action") != "buy":
            continue
        code = act["code"]
        name = act.get("name", code)
        ratio = act.get("ratio", 0.2)
        buy_price = prices.get(code)
        if not buy_price:
            console.print(f"[yellow]跳过买入 {name}({code}): 无法获取价格[/yellow]")
            continue
        result = account.buy(code, name, buy_price, ratio, prices)
        if result:
            console.print(f"[green]  ↑ {result}[/green]")

    # 8. 更新价格并打印持仓
    all_codes = list(account.positions.keys())
    if all_codes:
        prices = market_data.get_realtime_prices(all_codes)
    print_portfolio(prices)
    account.save()
    console.print("[dim]状态已保存[/dim]")


def main():
    """主循环：交易时间每小时执行，非交易时间等待"""
    console.print(Panel(
        "[bold]自动模拟交易系统[/bold]\n"
        "初始资金: 10,000 元 | 手续费: 0.1% | 最小单位: 100 股\n"
        "交易时间: 周一至周五 9:30-11:30, 13:00-15:00\n"
        "按 Ctrl+C 退出并保存",
        title="启动",
        border_style="cyan",
    ))

    # 启动时打印当前持仓
    held_codes = list(account.positions.keys())
    prices = market_data.get_realtime_prices(held_codes) if held_codes else {}
    print_portfolio(prices)

    heartbeat_interval = 600  # 非交易时间每 10 分钟打印心跳
    last_heartbeat = 0

    while _running:
        if is_trading_time():
            try:
                run_trading_cycle()
            except Exception as e:
                console.print(f"[red]交易周期异常: {e}[/red]")
                account.save()

            # 等待到下一个整点
            now = datetime.now()
            # 下一个整点（或半小时对齐）
            next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            wait_secs = (next_hour - now).total_seconds()
            console.print(f"[dim]下次执行: {next_hour.strftime('%H:%M')}（等待 {int(wait_secs//60)} 分钟）[/dim]")

            # 分段 sleep，每秒检查退出标志
            end_time = time.time() + wait_secs
            while _running and time.time() < end_time:
                time.sleep(1)
        else:
            # 非交易时间
            now_ts = time.time()
            if now_ts - last_heartbeat >= heartbeat_interval:
                next_win = next_trading_window()
                console.print(
                    f"[dim]{datetime.now().strftime('%H:%M')} "
                    f"非交易时间，下一交易窗口: {next_win}[/dim]"
                )
                last_heartbeat = now_ts
            time.sleep(10)  # 非交易时间轻量级轮询


if __name__ == "__main__":
    main()
