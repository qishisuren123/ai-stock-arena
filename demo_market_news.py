"""A 股行情 + 财经新闻抓取 Demo

免费无认证，纯 requests 实现，可直接运行查看效果。

数据源:
  - 大盘指数 / 个股行情: 腾讯财经 HTTP API (qt.gtimg.cn)
  - 财联社电报快讯: cls.cn (带重要性分级 A/B/C)
  - 东方财富财经新闻: eastmoney.com (标题 + 摘要)

用法:
  pip install requests
  python demo_market_news.py
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

# ============================================================
# 配置：如需代理可在此设置，不需要就留空字典
# ============================================================
PROXIES = {
    # "http": "http://your-proxy:port",
    # "https": "http://your-proxy:port",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ============================================================
# 1. 大盘指数行情（腾讯财经 API）
# ============================================================
def get_market_overview() -> str:
    """获取上证、深证、创业板三大指数实时行情

    API: http://qt.gtimg.cn/q=sh000001,sz399001,sz399006
    返回格式: v_sh000001="1~上证指数~000001~3350.50~..." 以 ~ 分隔
    关键字段索引:
      [1] 名称  [3] 当前价  [32] 涨跌幅%  [37] 成交额(万)
    """
    codes = ["sh000001", "sz399001", "sz399006"]
    labels = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}

    url = f"http://qt.gtimg.cn/q={','.join(codes)}"
    resp = requests.get(url, proxies=PROXIES, timeout=10)
    resp.encoding = "gbk"

    lines = []
    for raw_line in resp.text.strip().split(";"):
        raw_line = raw_line.strip()
        if not raw_line or "=" not in raw_line:
            continue
        key = raw_line.split("=")[0].split("_")[-1]  # 提取 sh000001
        fields = raw_line.split('"')[1].split("~")
        if len(fields) < 45:
            continue

        label = labels.get(key, fields[1])
        price = float(fields[3]) if fields[3] else 0
        change_pct = float(fields[32]) if fields[32] else 0
        amount = float(fields[37]) if fields[37] else 0  # 单位：万元

        lines.append(
            f"{label}: {price:.2f}  涨跌幅: {change_pct:+.2f}%  "
            f"成交额: {amount / 1e4:.0f}亿"
        )

    return "\n".join(lines) if lines else "未获取到指数数据"


# ============================================================
# 2. 涨幅榜 - 主流股票行情（腾讯财经 API）
# ============================================================
def get_hot_stocks(top_n: int = 10) -> str:
    """获取一批主流股票实时行情，按涨跌幅排序

    选取了 25 只覆盖各行业的代表性股票，实时抓取后排序。
    股票代码规则: 6 开头 → sh(上交所), 其余 → sz(深交所)
    """
    watch_list = [
        # 白酒/消费
        "sh600519", "sz000858", "sh600809", "sz000568",
        # 金融
        "sh601318", "sh600036", "sh601166", "sz000001", "sh600030",
        # 新能源/制造
        "sz000333", "sz002594", "sz300750", "sh601012",
        # 科技/医药
        "sz002475", "sh688981", "sz002230", "sz300059",
        "sh688012", "sz300760", "sh600276",
        # 资源/旅游
        "sh601899", "sh600900", "sh601888", "sz002714", "sh603259",
    ]

    url = f"http://qt.gtimg.cn/q={','.join(watch_list)}"
    resp = requests.get(url, proxies=PROXIES, timeout=10)
    resp.encoding = "gbk"

    stocks = []
    for raw_line in resp.text.strip().split(";"):
        raw_line = raw_line.strip()
        if not raw_line or "=" not in raw_line:
            continue
        fields = raw_line.split('"')[1].split("~")
        if len(fields) < 45:
            continue
        stocks.append({
            "name": fields[1],
            "code": fields[2],
            "price": float(fields[3]) if fields[3] else 0,
            "change_pct": float(fields[32]) if fields[32] else 0,
            "turnover_rate": float(fields[38]) if fields[38] else 0,
        })

    # 按涨跌幅降序
    stocks.sort(key=lambda x: x["change_pct"], reverse=True)

    lines = []
    for i, s in enumerate(stocks[:top_n], 1):
        lines.append(
            f"{i:2d}. {s['name']}({s['code']}) "
            f"现价:{s['price']:.2f}  涨幅:{s['change_pct']:+.2f}%  "
            f"换手:{s['turnover_rate']:.2f}%"
        )
    return "\n".join(lines) if lines else "未获取到行情数据"


# ============================================================
# 3. 财联社电报快讯 (cls.cn)
# ============================================================
def get_cls_telegraph(limit: int = 10) -> str:
    """抓取财联社电报，优先返回重要级别(A/B)的新闻

    API: https://www.cls.cn/nodeapi/updateTelegraphList
    参数: app=CailianpressWeb, sv=7.7.5, rn=条数, os=web
    返回 JSON: data.roll_data[] 每条含 level(A/B/C), title, content
    """
    url = (
        f"https://www.cls.cn/nodeapi/updateTelegraphList"
        f"?app=CailianpressWeb&sv=7.7.5&rn={limit * 3}&os=web"
    )
    headers = {**HEADERS, "Referer": "https://www.cls.cn/telegraph"}

    resp = requests.get(url, headers=headers, proxies=PROXIES, timeout=10)
    data = resp.json()
    items = data.get("data", {}).get("roll_data", [])
    if not items:
        return ""

    # 按重要性分级
    important, normal = [], []
    for item in items:
        level = item.get("level", "C")
        title = item.get("title", "")
        content = item.get("content", "")
        # 优先用 title，没有则用 content 并去除 HTML 标签
        text = title if title else re.sub(r"<[^>]+>", "", content)
        text = text.strip()
        if not text:
            continue
        if level in ("A", "B"):
            important.append(f"[重要] {text}")
        else:
            normal.append(text)

    # 重要新闻优先，不足补普通新闻
    selected = important[:limit]
    if len(selected) < limit:
        selected.extend(normal[:limit - len(selected)])

    return "\n".join(selected)


# ============================================================
# 4. 东方财富财经新闻 (eastmoney.com)
# ============================================================
def get_eastmoney_news(limit: int = 10) -> str:
    """抓取东方财富财经要闻，返回标题 + 摘要

    API: https://np-listapi.eastmoney.com/comm/web/getNewsByColumns
    参数: column=350(财经要闻), limit=条数
    返回 JSON: data.list[] 每条含 title, digest
    """
    url = (
        f"https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
        f"?client=web&biz=web&column=350&limit={limit}&source=web&req_trace=1"
    )
    resp = requests.get(url, headers=HEADERS, proxies=PROXIES, timeout=10)
    data = resp.json()
    items = data.get("data", {}).get("list", [])
    if not items:
        return ""

    lines = []
    for item in items[:limit]:
        title = item.get("title", "").strip()
        digest = item.get("digest", "").strip()
        if not title:
            continue
        if digest:
            digest = digest[:100] + ("..." if len(digest) > 100 else "")
            lines.append(f"{title} - {digest}")
        else:
            lines.append(title)

    return "\n".join(lines)


# ============================================================
# 5. 统一入口：并行获取所有数据
# ============================================================
def get_all_market_info() -> str:
    """一次性获取大盘 + 涨幅榜 + 双源新闻，并行请求"""
    results = {}

    def _fetch(name, func, *args):
        try:
            results[name] = func(*args)
        except Exception as e:
            results[name] = f"[获取失败: {e}]"

    # 4 个请求并行
    with ThreadPoolExecutor(max_workers=4) as pool:
        pool.submit(_fetch, "overview", get_market_overview)
        pool.submit(_fetch, "hot", get_hot_stocks, 10)
        pool.submit(_fetch, "cls", get_cls_telegraph, 10)
        pool.submit(_fetch, "east", get_eastmoney_news, 10)

    # 组装输出
    sections = [
        f"{'=' * 50}",
        f"【大盘指数】",
        results.get("overview", ""),
        "",
        f"【涨幅榜 TOP10】",
        results.get("hot", ""),
    ]

    cls = results.get("cls", "")
    east = results.get("east", "")
    if cls:
        sections += ["", f"【财联社快讯】", cls]
    if east:
        sections += ["", f"【财经要闻】", east]
    if not cls and not east:
        sections += ["", "暂无新闻数据"]

    sections.append(f"{'=' * 50}")
    return "\n".join(sections)


# ============================================================
# 直接运行
# ============================================================
if __name__ == "__main__":
    t0 = time.time()
    print(get_all_market_info())
    print(f"\n总耗时: {time.time() - t0:.2f}s")

    # 深度数据 demo（融资融券 + PE分位）
    print(f"\n{'=' * 50}")
    print("【深度数据 Demo】")
    print(f"{'=' * 50}")
    from market_data import get_stock_deep_info
    demo_codes = ["600519", "002594", "300750"]
    t1 = time.time()
    deep = get_stock_deep_info(demo_codes)
    print(deep if deep else "未获取到深度数据")
    print(f"\n深度数据耗时: {time.time() - t1:.2f}s")
