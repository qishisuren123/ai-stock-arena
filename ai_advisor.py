"""AI 分析模块 - 调用 Claude API 生成投资建议"""

import json
import os
import re
import time
import httpx

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

SYSTEM_PROMPT = """你是一位专业的 A 股市场分析师，拥有丰富的技术分析和基本面分析经验。

你的任务：
1. 根据提供的市场数据，分析当前市场状态
2. 给出具体的操作建议（买入/卖出/观望）
3. 推荐 2-3 只值得关注的股票，并说明理由

输出格式要求：
## 市场总览
简要分析当前大盘走势和市场情绪

## 推荐关注
对每只推荐股票：
- 股票代码和名称
- 操作建议：买入 / 卖出 / 观望
- 推荐理由（技术面+基本面）
- 建议仓位比例

## 风险提示
当前市场主要风险因素

注意：
- 所有建议仅供参考，不构成投资建议
- 关注成交量变化和板块轮动
- 结合宏观经济环境分析"""


def _load_config() -> dict:
    """加载 API 配置"""
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _call_api(system: str, user_msg: str, max_retries: int = 5) -> str:
    """直接通过 httpx 调用 Claude API，带重试"""
    cfg = _load_config()
    proxy_url = cfg.get("proxy", {}).get("https")
    client = httpx.Client(proxy=proxy_url, timeout=120.0)

    url = f"{cfg['base_url']}/v1/messages"
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": cfg["model"],
        "max_tokens": 2048,
        "system": system,
        "messages": [{"role": "user", "content": user_msg}],
    }

    for attempt in range(max_retries):
        try:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                return data["content"][0]["text"]
            # 非 200 时解析错误信息
            err_msg = resp.text
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {}).get("message", resp.text)
            except Exception:
                pass
            raise RuntimeError(f"HTTP {resp.status_code}: {err_msg}")
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                print(f"  第 {attempt+1} 次重试失败，{wait}s 后重试...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"API 调用失败（重试 {max_retries} 次）: {e}")
    return ""


def get_advice(market_data: str) -> str:
    """调用 Claude API 获取投资建议"""
    return _call_api(
        SYSTEM_PROMPT,
        f"以下是今日 A 股市场数据，请分析并给出建议：\n\n{market_data}",
    )


def analyze_stock(stock_data: str) -> str:
    """针对单只股票的深度分析"""
    return _call_api(
        SYSTEM_PROMPT,
        f"请对以下个股进行深度分析：\n\n{stock_data}",
    )


# ========== 结构化输出接口（供自动交易使用）==========

STRUCTURED_SYSTEM_PROMPT = """你是一位专业的 A 股量化交易助手，负责管理一个 1 万元的模拟小账户。

你必须严格以 JSON 格式返回交易指令，不要输出任何其他内容（不要 markdown 代码块标记）。

JSON 格式：
{
  "analysis": "简要市场分析（1-2句话）",
  "actions": [
    {
      "code": "600519",
      "name": "贵州茅台",
      "action": "buy",
      "ratio": 0.25
    }
  ]
}

字段说明：
- action: "buy"（买入）、"sell"（卖出）、"hold"（不操作则不用列出）
- ratio: 目标仓位占总资产的比例（0.0 ~ 0.25），单股上限 25%
- 如果不需要任何操作，actions 设为空列表 []

=== 核心交易纪律（必须严格遵守）===

1. 反频繁交易：每天最多产生 1 个交易动作。大多数时候应该返回空 actions []。
   - 如果没有明确的交易信号，就不要操作。"观望"是最好的策略。
   - 不要因为"感觉"而交易，必须有明确的技术面或基本面理由。

2. 最低持仓期：买入后至少持有 3 个交易日（约一周），除非触发止损。
   - 如果持仓中的股票买入不足 3 天，不要建议卖出。
   - A 股 T+1 制度：今天买的股票明天才能卖。

3. 佣金意识：
   - 每笔交易佣金最低 5 元（不是 1 元），买卖各收一次 = 至少 10 元往返成本
   - 卖出额外收 0.05% 印花税
   - 1 万元账户，每次交易成本约 0.1%。频繁交易会被佣金吃掉利润。
   - 只有预期收益 > 2% 时才值得交易

4. 仓位管理：
   - 同时持仓不超过 3 只股票
   - 单股仓位上限 25%（而非 30%）
   - 优先选择股价 50 元以下的股票，方便凑整手（100 股）
   - 新建仓位从小仓位（10-15%）开始，确认趋势后再加仓

5. 止损止盈：
   - 硬止损：亏损超过 5% 必须卖出
   - 止盈参考：盈利超过 8% 可以考虑减仓或止盈
   - 当日大盘跌幅超过 2% 时不要新开仓

6. 选股偏好（必须严格遵守，违反即亏损）：
   - 只允许买入：沪深300成分股、中证500成分股、行业/宽基ETF
   - 绝对禁止：市值<100亿的小盘股、ST/*ST股票、次新股（上市<60天）、日均成交额<2亿的冷门股
   - 禁止追高：当日涨幅>5%的股票不买、连涨3天以上的不买
   - 优先选择：银行/电力/消费等稳健蓝筹，回避概念炒作股
   - 1万元小账户，每只股票最多买100-300股，股价超过50元的不适合

7. 卖出时 ratio 设为 0"""


def get_structured_advice(market_data: str, portfolio_info: str) -> dict:
    """获取结构化交易指令，返回解析后的 dict，失败返回空 actions"""
    user_msg = (
        f"当前市场数据：\n{market_data}\n\n"
        f"当前持仓状态：\n{portfolio_info}\n\n"
        f"请根据以上信息给出交易指令（纯 JSON）。"
    )
    try:
        raw = _call_api(STRUCTURED_SYSTEM_PROMPT, user_msg)
        # 容错：去掉可能的 markdown 代码块标记
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        result = json.loads(text)
        # 校验基本结构
        if "actions" not in result:
            result["actions"] = []
        if "analysis" not in result:
            result["analysis"] = ""
        return result
    except (json.JSONDecodeError, RuntimeError) as e:
        return {"analysis": f"AI 返回解析失败: {e}", "actions": []}


# ========== 多模型 API 调用接口 ==========

def _call_anthropic(base_url: str, api_key: str, model: str,
                    system: str, user_msg: str, proxy_url: str = None) -> str:
    """Anthropic Messages 格式调用（Claude / GPT-5.4 兼容）"""
    client = httpx.Client(proxy=proxy_url, timeout=120.0)
    url = f"{base_url}/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 2048,
        "system": system,
        "messages": [{"role": "user", "content": user_msg}],
    }
    resp = client.post(url, headers=headers, json=payload)
    if resp.status_code == 200:
        data = resp.json()
        return data["content"][0]["text"]
    # 解析错误信息
    err_msg = resp.text
    try:
        err_data = resp.json()
        err_msg = err_data.get("error", {}).get("message", resp.text)
    except Exception:
        pass
    raise RuntimeError(f"Anthropic HTTP {resp.status_code}: {err_msg}")


def _call_gemini(base_url: str, api_key: str, model: str,
                 system: str, user_msg: str, proxy_url: str = None) -> str:
    """Google Gemini 原生 API 调用"""
    client = httpx.Client(proxy=proxy_url, timeout=120.0)
    url = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"maxOutputTokens": 2048},
    }
    resp = client.post(url, json=payload)
    if resp.status_code == 200:
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    err_msg = resp.text
    try:
        err_data = resp.json()
        err_msg = err_data.get("error", {}).get("message", resp.text)
    except Exception:
        pass
    raise RuntimeError(f"Gemini HTTP {resp.status_code}: {err_msg}")


def _call_openai(base_url: str, api_key: str, model: str,
                 system: str, user_msg: str) -> str:
    """OpenAI Chat Completions 格式调用（pjlab 内部模型）"""
    # trust_env=False 禁止 httpx 读取环境变量中的代理，避免干扰内部模型访问
    client = httpx.Client(timeout=120.0, trust_env=False)
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}" if api_key else "",
        "content-type": "application/json",
    }
    # api_key 为空时不发 Authorization
    if not api_key:
        headers.pop("Authorization", None)
    payload = {
        "model": model,
        "max_tokens": 2048,
        # 禁用思考模式，让模型直接输出到 content（GLM5/Qwen3.5 等思考模型需要）
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
    }
    resp = client.post(url, headers=headers, json=payload)
    if resp.status_code == 200:
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content")
        # 部分思考模型（GLM5/Qwen3.5）content 为 None，实际回答在 reasoning 字段
        if not content:
            content = msg.get("reasoning") or msg.get("reasoning_content") or ""
        return content
    err_msg = resp.text
    try:
        err_data = resp.json()
        err_msg = err_data.get("error", {}).get("message", resp.text)
    except Exception:
        pass
    raise RuntimeError(f"OpenAI HTTP {resp.status_code}: {err_msg}")


def call_model_api(model_cfg: dict, system: str, user_msg: str,
                   max_retries: int = 3) -> str:
    """统一入口：按 api_format 分发，带重试"""
    cfg = _load_config()
    # 优先使用模型自带的 proxy_url，否则用全局代理
    if model_cfg.get("proxy_url"):
        proxy_url = model_cfg["proxy_url"]
    elif model_cfg.get("use_proxy"):
        proxy_url = cfg.get("proxy", {}).get("https")
    else:
        proxy_url = None

    for attempt in range(max_retries):
        try:
            if model_cfg["api_format"] == "anthropic":
                return _call_anthropic(
                    model_cfg["base_url"], model_cfg["api_key"],
                    model_cfg["model"], system, user_msg, proxy_url,
                )
            elif model_cfg["api_format"] == "gemini":
                return _call_gemini(
                    model_cfg["base_url"], model_cfg["api_key"],
                    model_cfg["model"], system, user_msg, proxy_url,
                )
            else:  # openai
                return _call_openai(
                    model_cfg["base_url"], model_cfg.get("api_key", ""),
                    model_cfg["model"], system, user_msg,
                )
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                time.sleep(wait)
                continue
            raise RuntimeError(f"[{model_cfg['name']}] API 调用失败（重试 {max_retries} 次）: {e}")
    return ""


def get_structured_advice_multi(model_cfg: dict, market_data: str,
                                portfolio_info: str,
                                trade_history: str = "") -> dict:
    """多模型版结构化交易指令，返回解析后的 dict"""
    user_msg = (
        f"当前市场数据：\n{market_data}\n\n"
        f"当前持仓状态：\n{portfolio_info}\n\n"
    )
    if trade_history:
        user_msg += f"最近交易记录（注意持仓期）：\n{trade_history}\n\n"
    user_msg += "请根据以上信息给出交易指令（纯 JSON）。记住：大多数时候应该返回空 actions []。"
    try:
        raw = call_model_api(model_cfg, STRUCTURED_SYSTEM_PROMPT, user_msg)
        if not raw:
            return {"analysis": "模型返回为空", "actions": []}
        # 容错：去掉模型可能输出的 <think>...</think> 思考过程
        text = re.sub(r"<think>[\s\S]*?</think>\s*", "", raw).strip()
        # 容错：去掉可能的 markdown 代码块标记
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        result = json.loads(text)
        if "actions" not in result:
            result["actions"] = []
        if "analysis" not in result:
            result["analysis"] = ""
        return result
    except (json.JSONDecodeError, RuntimeError) as e:
        return {"analysis": f"AI 返回解析失败: {e}", "actions": []}
