"""行情数据模块 - 通过腾讯/新浪财经 API 获取 A 股市场数据"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# 加载代理配置
_config_path = os.path.join(os.path.dirname(__file__), "config.json")
_proxies = {}
if os.path.exists(_config_path):
    with open(_config_path, "r") as _f:
        _proxy_cfg = json.load(_f).get("proxy", {})
        if _proxy_cfg.get("http"):
            _proxies["http"] = _proxy_cfg["http"]
        if _proxy_cfg.get("https"):
            _proxies["https"] = _proxy_cfg["https"]


def _qq_fetch(codes: list[str]) -> dict:
    """通过腾讯财经 API 获取实时行情
    返回 {code: {name, price, change_pct, volume, amount, ...}}
    """
    url = f"http://qt.gtimg.cn/q={','.join(codes)}"
    resp = requests.get(url, proxies=_proxies, timeout=10)
    resp.encoding = "gbk"
    result = {}
    for line in resp.text.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        # v_sh000001="1~上证指数~000001~..."
        key = line.split("=")[0].split("_")[-1]
        data = line.split('"')[1].split("~")
        if len(data) < 45:
            continue
        result[key] = {
            "name": data[1],
            "code": data[2],
            "price": float(data[3]) if data[3] else 0,
            "yesterday_close": float(data[4]) if data[4] else 0,
            "open": float(data[5]) if data[5] else 0,
            "volume": float(data[36]) if data[36] else 0,  # 成交量（手）
            "amount": float(data[37]) if data[37] else 0,  # 成交额（万）
            "change_pct": float(data[32]) if data[32] else 0,  # 涨跌幅%
            "high": float(data[33]) if data[33] else 0,
            "low": float(data[34]) if data[34] else 0,
            "pe": data[39] if len(data) > 39 else "N/A",
            "pb": data[46] if len(data) > 46 else "N/A",
            "total_mv": data[45] if len(data) > 45 else "N/A",  # 总市值（亿）
            "turnover_rate": float(data[38]) if data[38] else 0,  # 换手率%
        }
    return result


def get_market_overview() -> str:
    """获取主要指数行情（上证、深证、创业板）"""
    try:
        data = _qq_fetch(["sh000001", "sz399001", "sz399006"])
        names = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}
        lines = []
        for code, label in names.items():
            d = data.get(code)
            if not d:
                continue
            lines.append(
                f"{label}: {d['price']:.2f}  "
                f"涨跌幅: {d['change_pct']:.2f}%  "
                f"成交额: {d['amount'] / 1e4:.0f}亿"
            )
        return "\n".join(lines) if lines else "未获取到指数数据"
    except Exception as e:
        return f"获取指数数据失败: {e}"


def get_hot_stocks(top_n: int = 15) -> str:
    """获取一批主流股票行情，按涨跌幅排序"""
    try:
        # 覆盖各行业的代表性股票
        watch_list = [
            "sh600519", "sh601318", "sh600036", "sh600900", "sh601012",
            "sz000858", "sz000333", "sz002594", "sz300750", "sz002475",
            "sh688981", "sh601899", "sh600030", "sh601166", "sz000001",
            "sz002230", "sz300059", "sh603259", "sh600809", "sz000568",
            "sh601888", "sz002714", "sh688012", "sz300760", "sh600276",
        ]
        data = _qq_fetch(watch_list)
        # 按涨跌幅排序
        sorted_items = sorted(data.values(), key=lambda x: x["change_pct"], reverse=True)
        lines = []
        for i, d in enumerate(sorted_items[:top_n], 1):
            lines.append(
                f"{i:2d}. {d['name']}({d['code']}) "
                f"现价:{d['price']:.2f} "
                f"涨幅:{d['change_pct']:.2f}% "
                f"换手:{d['turnover_rate']:.2f}%"
            )
        return "\n".join(lines) if lines else "未获取到行情数据"
    except Exception as e:
        return f"获取行情数据失败: {e}"


def get_stock_kline(code: str, days: int = 20) -> str:
    """获取个股近 N 日 K 线（腾讯日K接口）"""
    try:
        # 判断市场前缀
        prefix = "sh" if code.startswith("6") else "sz"
        url = (
            f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={prefix}{code},day,,,{days},qfq"
        )
        resp = requests.get(url, proxies=_proxies, timeout=10)
        data = resp.json()
        klines = data["data"][f"{prefix}{code}"]["qfqday"]
        lines = []
        for k in klines[-days:]:
            # [日期, 开, 收, 高, 低, 成交量]
            lines.append(
                f"{k[0]} | 开:{k[1]} 收:{k[2]} "
                f"高:{k[3]} 低:{k[4]} 量:{k[5]}"
            )
        return "\n".join(lines) if lines else "无K线数据"
    except Exception as e:
        return f"获取K线数据失败: {e}"


def get_realtime_prices(codes: list[str]) -> dict[str, float]:
    """批量获取实时价格，接受纯数字代码列表，返回 {code: price}
    例: get_realtime_prices(["600519", "000858"]) -> {"600519": 1680.0, "000858": 150.5}
    """
    if not codes:
        return {}
    # 纯数字代码 → 带市场前缀
    qq_codes = []
    for c in codes:
        prefix = "sh" if c.startswith("6") else "sz"
        qq_codes.append(f"{prefix}{c}")
    try:
        data = _qq_fetch(qq_codes)
        result = {}
        for info in data.values():
            if info["price"] > 0:
                result[info["code"]] = info["price"]
        return result
    except Exception:
        return {}


def get_stock_info(code: str) -> str:
    """获取个股实时信息"""
    try:
        prefix = "sh" if code.startswith("6") else "sz"
        data = _qq_fetch([f"{prefix}{code}"])
        d = list(data.values())[0] if data else None
        if not d:
            return "未找到该股票"
        parts = [
            f"股票名称: {d['name']}",
            f"当前价格: {d['price']:.2f}",
            f"涨跌幅: {d['change_pct']:.2f}%",
            f"今开: {d['open']:.2f}  最高: {d['high']:.2f}  最低: {d['low']:.2f}",
            f"昨收: {d['yesterday_close']:.2f}",
            f"成交额: {d['amount'] / 1e4:.1f}亿",
            f"市盈率: {d['pe']}",
            f"市净率: {d['pb']}",
            f"总市值: {d['total_mv']}亿",
        ]
        return "\n".join(parts)
    except Exception as e:
        return f"获取个股信息失败: {e}"


def get_hot_stock_codes(top_n: int = 5) -> list[str]:
    """获取涨幅榜前 N 只股票的纯数字代码"""
    try:
        watch_list = [
            "sh600519", "sh601318", "sh600036", "sh600900", "sh601012",
            "sz000858", "sz000333", "sz002594", "sz300750", "sz002475",
            "sh688981", "sh601899", "sh600030", "sh601166", "sz000001",
            "sz002230", "sz300059", "sh603259", "sh600809", "sz000568",
            "sh601888", "sz002714", "sh688012", "sz300760", "sh600276",
        ]
        data = _qq_fetch(watch_list)
        sorted_items = sorted(data.values(), key=lambda x: x["change_pct"], reverse=True)
        return [d["code"] for d in sorted_items[:top_n]]
    except Exception:
        return []


def get_margin_data(codes: list[str]) -> str:
    """获取融资融券数据（东方财富 datacenter API）"""
    if not codes:
        return ""

    def _fetch_one(code: str) -> str:
        try:
            url = (
                "https://datacenter-web.eastmoney.com/api/data/v1/get"
                f"?reportName=RPTA_WEB_RZRQ_GGMX&columns=ALL"
                f"&filter=(SCODE=%22{code}%22)"
                f"&sortColumns=DATE&sortTypes=-1&pageSize=1"
            )
            resp = requests.get(url, timeout=10)
            data = resp.json()
            items = data.get("result", {}).get("data", [])
            if not items:
                return ""
            d = items[0]
            name = d.get("SECNAME", code)
            # 融资余额（元→亿）
            rzye = d.get("RZYE", 0) or 0
            rzye_yi = rzye / 1e8
            # 融资净买入（元→亿）
            rzjmr = d.get("RZJME", 0) or 0
            rzjmr_yi = rzjmr / 1e8
            # 融券余额（元→亿）
            rqye = d.get("RQYE", 0) or 0
            rqye_yi = rqye / 1e8
            sign = "+" if rzjmr_yi >= 0 else ""
            return (
                f"{name}({code}): 融资余额 {rzye_yi:.1f}亿 | "
                f"融资净买入 {sign}{rzjmr_yi:.1f}亿 | "
                f"融券余额 {rqye_yi:.1f}亿"
            )
        except Exception:
            return ""

    # 并行请求
    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_one, c): c for c in codes}
        for future in as_completed(futures):
            text = future.result()
            if text:
                results.append(text)
    return "\n".join(results)


def get_pe_analysis(codes: list[str]) -> str:
    """获取历史PE分位分析（东方财富 datacenter API）"""
    if not codes:
        return ""

    def _fetch_one(code: str) -> str:
        try:
            all_pe = []
            page = 1
            while True:
                url = (
                    "https://datacenter-web.eastmoney.com/api/data/v1/get"
                    f"?reportName=RPT_VALUEANALYSIS_DET"
                    f"&columns=SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,PE_TTM"
                    f"&filter=(SECURITY_CODE=%22{code}%22)"
                    f"&pageSize=500&pageNumber={page}"
                    f"&sortColumns=TRADE_DATE&sortTypes=-1"
                )
                resp = requests.get(url, timeout=15)
                data = resp.json()
                items = data.get("result", {}).get("data", [])
                if not items:
                    break
                for item in items:
                    pe = item.get("PE_TTM")
                    if pe is not None and pe > 0:
                        all_pe.append(pe)
                # 最多拉4页
                if page >= 4 or len(items) < 500:
                    break
                page += 1

            if not all_pe:
                return ""

            current_pe = all_pe[0]  # 最新一条
            sorted_pe = sorted(all_pe)
            rank = sum(1 for x in sorted_pe if x <= current_pe)
            percentile = rank / len(sorted_pe) * 100
            pe_min = sorted_pe[0]
            pe_median = sorted_pe[len(sorted_pe) // 2]
            pe_max = sorted_pe[-1]

            # 获取股票名称
            name = code
            try:
                url2 = (
                    "https://datacenter-web.eastmoney.com/api/data/v1/get"
                    f"?reportName=RPT_VALUEANALYSIS_DET"
                    f"&columns=SECURITY_NAME_ABBR"
                    f"&filter=(SECURITY_CODE=%22{code}%22)"
                    f"&pageSize=1&sortColumns=TRADE_DATE&sortTypes=-1"
                )
                resp2 = requests.get(url2, timeout=5)
                name = resp2.json()["result"]["data"][0].get("SECURITY_NAME_ABBR", code)
            except Exception:
                pass

            return (
                f"{name}({code}): PE(TTM) {current_pe:.1f} | "
                f"历史分位 {percentile:.0f}% | "
                f"区间 [{pe_min:.1f} / {pe_median:.1f} / {pe_max:.1f}]"
            )
        except Exception:
            return ""

    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_one, c): c for c in codes}
        for future in as_completed(futures):
            text = future.result()
            if text:
                results.append(text)
    return "\n".join(results)


def get_stock_deep_info(codes: list[str]) -> str:
    """统一入口：并行获取融资融券 + 估值分析"""
    if not codes:
        return ""
    margin_text = ""
    pe_text = ""
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_margin = executor.submit(get_margin_data, codes)
        f_pe = executor.submit(get_pe_analysis, codes)
        try:
            margin_text = f_margin.result(timeout=30)
        except Exception:
            pass
        try:
            pe_text = f_pe.result(timeout=30)
        except Exception:
            pass

    sections = []
    if margin_text:
        sections.append(f"【融资融券】\n{margin_text}")
    if pe_text:
        sections.append(f"【估值分析】\n{pe_text}")
    return "\n\n".join(sections)


# ============================================================
# 财经新闻数据源
# ============================================================

def get_cls_telegraph(limit: int = 10) -> str:
    """获取财联社电报快讯，优先返回重要级别(A/B)的新闻"""
    try:
        url = (
            f"https://www.cls.cn/nodeapi/updateTelegraphList"
            f"?app=CailianpressWeb&sv=7.7.5&rn={limit * 3}&os=web"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.cls.cn/telegraph",
        }
        resp = requests.get(url, headers=headers, proxies=_proxies, timeout=10)
        data = resp.json()
        items = data.get("data", {}).get("roll_data", [])
        if not items:
            return ""

        # 按重要性分级：A/B 级优先
        important = []
        normal = []
        for item in items:
            level = item.get("level", "C")
            # 提取内容：优先用 title，没有则用 content 并去除 HTML 标签
            title = item.get("title", "")
            content = item.get("content", "")
            text = title if title else re.sub(r"<[^>]+>", "", content)
            text = text.strip()
            if not text:
                continue
            if level in ("A", "B"):
                important.append(f"[重要] {text}")
            else:
                normal.append(text)

        # 优先取重要新闻，不足则补普通新闻
        selected = important[:limit]
        if len(selected) < limit:
            selected.extend(normal[:limit - len(selected)])

        return "\n".join(selected) if selected else ""
    except Exception as e:
        return ""


def get_eastmoney_news(limit: int = 10) -> str:
    """获取东方财富财经新闻"""
    try:
        url = (
            f"https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
            f"?client=web&biz=web&column=350&limit={limit}&source=web&req_trace=1"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
        }
        resp = requests.get(url, headers=headers, proxies=_proxies, timeout=10)
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
                # 摘要截取前100字
                digest = digest[:100] + ("..." if len(digest) > 100 else "")
                lines.append(f"{title} - {digest}")
            else:
                lines.append(title)

        return "\n".join(lines) if lines else ""
    except Exception as e:
        return ""


def get_financial_news() -> str:
    """统一新闻入口：并行获取财联社 + 东方财富新闻，合并返回"""
    cls_text = ""
    east_text = ""

    # 并行请求两个数据源
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_cls = executor.submit(get_cls_telegraph, 10)
        future_east = executor.submit(get_eastmoney_news, 10)

        try:
            cls_text = future_cls.result(timeout=15)
        except Exception:
            pass
        try:
            east_text = future_east.result(timeout=15)
        except Exception:
            pass

    # 合并输出
    sections = []
    if cls_text:
        sections.append(f"【财联社快讯】\n{cls_text}")
    if east_text:
        sections.append(f"【财经要闻】\n{east_text}")

    if sections:
        return "\n\n".join(sections)
    return "暂无新闻数据"
