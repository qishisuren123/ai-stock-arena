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


def _eastmoney_rank(sort_field: str = "f3", ascending: bool = False,
                    count: int = 30) -> list[dict]:
    """通过东方财富 API 获取沪深A股实时排行
    sort_field: f3=涨跌幅 f6=成交额 f8=换手率
    返回 [{code, name, price, change_pct, amount, turnover_rate}, ...]
    """
    url = "http://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": count, "po": 0 if ascending else 1,
        "np": 1, "fltt": 2, "invt": 2, "fid": sort_field,
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f3,f5,f6,f8,f12,f14",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    data = resp.json()
    items = data.get("data", {}).get("diff", [])
    result = []
    for d in items:
        if not d.get("f2") or d["f2"] == "-":
            continue
        result.append({
            "code": str(d["f12"]),
            "name": d["f14"],
            "price": float(d["f2"]),
            "change_pct": float(d.get("f3", 0)),
            "amount": float(d.get("f6", 0)),
            "turnover_rate": float(d.get("f8", 0)),
        })
    return result


def get_hot_stocks(top_n: int = 15) -> str:
    """获取实时涨幅榜 + 成交额榜，合并去重后返回"""
    try:
        # 涨幅榜 TOP15 + 成交额榜 TOP10，给模型更广视野
        gainers = _eastmoney_rank("f3", ascending=False, count=top_n)
        volume_leaders = _eastmoney_rank("f6", ascending=False, count=10)

        # 合并去重
        seen = set()
        merged = []
        for d in gainers + volume_leaders:
            if d["code"] not in seen:
                seen.add(d["code"])
                merged.append(d)

        # 按涨幅排序
        merged.sort(key=lambda x: x["change_pct"], reverse=True)

        lines = []
        for i, d in enumerate(merged[:top_n + 5], 1):
            lines.append(
                f"{i:2d}. {d['name']}({d['code']}) "
                f"现价:{d['price']:.2f} "
                f"涨幅:{d['change_pct']:.2f}% "
                f"换手:{d['turnover_rate']:.2f}% "
                f"成交额:{d['amount'] / 1e8:.1f}亿"
            )
        return "\n".join(lines) if lines else "未获取到行情数据"
    except Exception as e:
        # 回退到固定列表
        return _get_hot_stocks_fallback(top_n)


def get_sector_overview() -> str:
    """获取行业板块涨跌幅排行，展示全局资金流向"""
    try:
        url = "http://push2.eastmoney.com/api/qt/clist/get"
        headers = {"User-Agent": "Mozilla/5.0"}
        # 行业板块按涨幅排序
        params = {
            "pn": 1, "pz": 40, "po": 1, "np": 1,
            "fltt": 2, "invt": 2, "fid": "f3",
            "fs": "m:90+t:2",
            "fields": "f3,f8,f14",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        items = resp.json().get("data", {}).get("diff", [])
        if not items:
            return ""

        # 取涨幅前10和跌幅前5
        top = items[:10]
        bottom = sorted(items, key=lambda x: x["f3"])[:5]

        lines = ["领涨行业:"]
        for d in top:
            lines.append(f"  {d['f14']} {d['f3']:+.2f}%")
        lines.append("领跌行业:")
        for d in bottom:
            lines.append(f"  {d['f14']} {d['f3']:+.2f}%")

        # 统计涨跌家数
        up = sum(1 for d in items if d["f3"] > 0)
        down = sum(1 for d in items if d["f3"] < 0)
        lines.append(f"行业涨跌: {up}涨 {down}跌")

        return "\n".join(lines)
    except Exception:
        return ""


def get_concept_hot() -> str:
    """获取热门概念板块（过滤掉连板/涨停类纯技术概念）"""
    try:
        url = "http://push2.eastmoney.com/api/qt/clist/get"
        headers = {"User-Agent": "Mozilla/5.0"}
        params = {
            "pn": 1, "pz": 30, "po": 1, "np": 1,
            "fltt": 2, "invt": 2, "fid": "f3",
            "fs": "m:90+t:3",
            "fields": "f3,f14",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        items = resp.json().get("data", {}).get("diff", [])

        # 过滤掉 "昨日连板/涨停/首板" 等纯技术概念
        skip_keywords = {"昨日连板", "昨日涨停", "昨日首板", "含一字"}
        filtered = [d for d in items
                    if not any(kw in d["f14"] for kw in skip_keywords)]

        lines = []
        for d in filtered[:10]:
            lines.append(f"  {d['f14']} {d['f3']:+.2f}%")
        return "\n".join(lines) if lines else ""
    except Exception:
        return ""


def get_market_breadth() -> str:
    """获取市场涨跌家数（分页拉取全A股）"""
    try:
        url = "http://push2.eastmoney.com/api/qt/clist/get"
        headers = {"User-Agent": "Mozilla/5.0"}
        up = down = flat = 0
        for page in range(1, 6):  # 最多拉5页，每页1000
            params = {
                "pn": page, "pz": 1000, "np": 1, "fltt": 2,
                "invt": 2, "fid": "f12",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f3",
            }
            resp = requests.get(url, params=params, headers=headers, timeout=8)
            items = resp.json().get("data", {}).get("diff", [])
            if not items:
                break
            for d in items:
                pct = d.get("f3", 0)
                if pct > 0:
                    up += 1
                elif pct < 0:
                    down += 1
                else:
                    flat += 1
        total = up + down + flat
        if total == 0:
            return ""
        ratio = up / max(down, 1)
        return (f"全市场涨跌: {up}涨/{down}跌/{flat}平 "
                f"(涨跌比 {ratio:.2f}, 共{total}只)")
    except Exception:
        return ""


def _get_hot_stocks_fallback(top_n: int = 15) -> str:
    """固定股票池兜底（东方财富 API 不可用时）"""
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


def get_losers(top_n: int = 10) -> str:
    """获取实时跌幅榜"""
    try:
        losers = _eastmoney_rank("f3", ascending=True, count=top_n)
        lines = []
        for i, d in enumerate(losers[:top_n], 1):
            lines.append(
                f"{i:2d}. {d['name']}({d['code']}) "
                f"现价:{d['price']:.2f} "
                f"跌幅:{d['change_pct']:.2f}% "
                f"成交额:{d['amount'] / 1e8:.1f}亿"
            )
        return "\n".join(lines) if lines else ""
    except Exception:
        return ""


def get_hot_stock_codes(top_n: int = 5) -> list[str]:
    """获取涨幅榜前 N 只股票的纯数字代码"""
    try:
        gainers = _eastmoney_rank("f3", ascending=False, count=top_n)
        return [d["code"] for d in gainers[:top_n]]
    except Exception:
        # 回退
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
