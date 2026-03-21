"""A股智能投顾助手 - 主入口"""

import sys
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, IntPrompt
from rich.markdown import Markdown
from rich import print as rprint

import market_data
import ai_advisor
import trade_recorder

console = Console()


def show_menu():
    """显示主菜单"""
    console.print(Panel(
        "[1] 获取今日买卖建议\n"
        "[2] 查询个股分析\n"
        "[3] 记录我的操作\n"
        "[4] 查看交易历史和盈亏\n"
        "[5] 退出",
        title="A股智能投顾助手",
        border_style="cyan",
    ))


def do_daily_advice():
    """获取今日市场建议"""
    with console.status("[bold green]正在获取市场数据..."):
        overview = market_data.get_market_overview()
        sectors = market_data.get_hot_stocks()

    console.print(Panel(overview, title="大盘指数", border_style="blue"))
    console.print(Panel(sectors, title="涨幅榜 TOP15", border_style="yellow"))

    with console.status("[bold green]AI 正在分析，请稍候..."):
        data_text = f"【大盘指数】\n{overview}\n\n【涨幅榜】\n{sectors}"
        advice = ai_advisor.get_advice(data_text)

    console.print(Panel(Markdown(advice), title="AI 投资建议", border_style="green"))


def do_stock_analysis():
    """查询个股深度分析"""
    code = Prompt.ask("请输入股票代码（如 600519）")
    with console.status("[bold green]正在获取个股数据..."):
        kline = market_data.get_stock_kline(code)
        info = market_data.get_stock_info(code)

    console.print(Panel(info, title=f"个股信息 [{code}]", border_style="blue"))

    with console.status("[bold green]AI 正在分析，请稍候..."):
        stock_text = f"【基本面】\n{info}\n\n【近20日K线】\n{kline}"
        analysis = ai_advisor.analyze_stock(stock_text)

    console.print(Panel(Markdown(analysis), title="AI 个股分析", border_style="green"))


def do_record_trade():
    """记录交易操作"""
    code = Prompt.ask("股票代码")
    name = Prompt.ask("股票名称")
    action = Prompt.ask("操作类型", choices=["buy", "sell", "hold"])
    price = float(Prompt.ask("价格"))
    quantity = IntPrompt.ask("数量（股）")
    reason = Prompt.ask("备注/理由", default="")

    trade = trade_recorder.add_trade(code, name, action, price, quantity, reason)
    action_cn = {"buy": "买入", "sell": "卖出", "hold": "持仓"}
    console.print(
        f"[green]已记录: {action_cn.get(action, action)} "
        f"{name}({code}) {price}元 x {quantity}股[/green]"
    )


def do_show_history():
    """查看交易历史和盈亏"""
    trades = trade_recorder.get_trades()
    if not trades:
        console.print("[yellow]暂无交易记录[/yellow]")
        return

    action_cn = {"buy": "买入", "sell": "卖出", "hold": "持仓"}
    console.print(Panel("交易历史", border_style="blue"))
    for t in trades:
        console.print(
            f"  {t['date']} {t['time']} | "
            f"{action_cn.get(t['action'], t['action'])} "
            f"{t['name']}({t['code']}) "
            f"{t['price']}元 x {t['quantity']}股"
            f"{' | ' + t['reason'] if t.get('reason') else ''}"
        )

    pnl = trade_recorder.calc_pnl()
    if pnl:
        console.print(Panel("持仓盈亏", border_style="green"))
        for p in pnl:
            console.print(
                f"  {p['name']}({p['code']}) | "
                f"均价: {p['avg_cost']} | "
                f"持仓: {p['holding_qty']}股 | "
                f"已实现盈亏: {p['realized_pnl']}元"
            )


def main():
    console.print("[bold cyan]A股智能投顾助手[/bold cyan] v1.0\n")
    while True:
        show_menu()
        choice = Prompt.ask("请选择", choices=["1", "2", "3", "4", "5"])
        console.print()
        try:
            if choice == "1":
                do_daily_advice()
            elif choice == "2":
                do_stock_analysis()
            elif choice == "3":
                do_record_trade()
            elif choice == "4":
                do_show_history()
            elif choice == "5":
                console.print("[cyan]再见！投资有风险，入市需谨慎。[/cyan]")
                sys.exit(0)
        except KeyboardInterrupt:
            console.print("\n[yellow]操作已取消[/yellow]")
        except Exception as e:
            console.print(f"[red]出错了: {e}[/red]")
        console.print()


if __name__ == "__main__":
    main()
