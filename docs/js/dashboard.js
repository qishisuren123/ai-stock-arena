/**
 * AI 群英会 - 仪表盘逻辑
 * 加载 data/latest.json 和 data/history.json，渲染排行榜、走势图、模型卡片
 * 每 5 分钟自动刷新
 */

// 10 个模型的专属颜色（与 CSS 变量对应）
const MODEL_COLORS = {
    "Claude-Haiku":   "#f97316",
    "GPT-5.4":        "#10b981",
    "Gemini-3.1-Pro": "#3b82f6",
    "Minimax2.5":     "#8b5cf6",
    "GLM5":           "#ef4444",
    "DeepSeek-V3.2":  "#06b6d4",
    "Kimi-K2.5":      "#f59e0b",
    "Qwen3.5-397B":   "#ec4899",
    "Intern-S1":      "#14b8a6",
    "Intern-S1-Pro":  "#a78bfa",
};

// 排名奖牌
const MEDALS = ["🥇", "🥈", "🥉"];

let chartInstance = null;

// === 数据加载 ===
async function fetchJSON(url) {
    const resp = await fetch(url + "?t=" + Date.now());
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
}

async function loadData() {
    try {
        const [latest, history] = await Promise.all([
            fetchJSON("data/latest.json"),
            fetchJSON("data/history.json").catch(() => []),
        ]);
        renderAll(latest, history);
    } catch (e) {
        console.error("加载数据失败:", e);
        document.getElementById("updateTime").textContent = "数据加载失败，请稍后刷新";
    }
}

// === 渲染总入口 ===
function renderAll(latest, history) {
    document.getElementById("updateTime").textContent =
        `最后更新: ${latest.timestamp}`;

    renderLeaderboard(latest.models);
    renderChart(history);
    renderModelGrid(latest.models);
}

// === 排行榜 ===
function renderLeaderboard(models) {
    const tbody = document.getElementById("leaderboardBody");
    tbody.innerHTML = "";

    models.forEach((m, i) => {
        const tr = document.createElement("tr");
        if (i === 0) tr.classList.add("rank-1");

        // 排名
        const rankText = i < 3 ? `<span class="rank-medal">${MEDALS[i]}</span>` : (i + 1);

        // 收益率样式
        let pnlClass = "pnl-flat";
        let pnlPrefix = "";
        if (m.return_pct > 0) { pnlClass = "pnl-up"; pnlPrefix = "+"; }
        else if (m.return_pct < 0) { pnlClass = "pnl-down"; }

        // 数据源标签
        const srcClass = m.source === "商业API" ? "source-api" : "source-internal";

        // 持仓摘要
        let posText = "空仓";
        if (m.positions && m.positions.length > 0) {
            posText = m.positions.map(p => p.name).join(", ");
        }

        tr.innerHTML = `
            <td>${rankText}</td>
            <td style="color:${MODEL_COLORS[m.name] || '#e6edf3'}">${m.name}</td>
            <td><span class="source-tag ${srcClass}">${m.source}</span></td>
            <td>&yen;${m.total_value.toLocaleString("zh-CN", {minimumFractionDigits: 2})}</td>
            <td class="${pnlClass}">${pnlPrefix}${m.return_pct.toFixed(2)}%</td>
            <td>${m.trade_count}</td>
            <td>${m.trade_count > 0 ? m.win_rate.toFixed(0) + "%" : "-"}</td>
            <td>${posText}</td>
        `;
        tbody.appendChild(tr);
    });
}

// === 收益走势图 ===
function renderChart(history) {
    if (!history || history.length === 0) {
        return;
    }

    const ctx = document.getElementById("returnChart").getContext("2d");

    // 提取时间标签
    const labels = history.map(h => {
        const d = h.timestamp;
        // 只显示时分
        return d.substring(5, 16);
    });

    // 收集所有模型名
    const modelNames = Object.keys(MODEL_COLORS);

    const datasets = modelNames.map(name => {
        const data = history.map(h => {
            const found = h.models.find(m => m.name === name);
            return found ? found.return_pct : null;
        });
        return {
            label: name,
            data: data,
            borderColor: MODEL_COLORS[name],
            backgroundColor: MODEL_COLORS[name] + "20",
            borderWidth: 2,
            pointRadius: 0,
            pointHoverRadius: 4,
            tension: 0.3,
            spanGaps: true,
        };
    });

    if (chartInstance) {
        chartInstance.destroy();
    }

    chartInstance = new Chart(ctx, {
        type: "line",
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: "index",
                intersect: false,
            },
            plugins: {
                legend: {
                    position: "bottom",
                    labels: {
                        color: "#8b949e",
                        usePointStyle: true,
                        pointStyle: "circle",
                        padding: 15,
                        font: { size: 11 },
                    },
                },
                tooltip: {
                    backgroundColor: "#161b22",
                    borderColor: "#30363d",
                    borderWidth: 1,
                    titleColor: "#e6edf3",
                    bodyColor: "#e6edf3",
                    callbacks: {
                        label: function(ctx) {
                            const val = ctx.parsed.y;
                            const prefix = val > 0 ? "+" : "";
                            return `${ctx.dataset.label}: ${prefix}${val.toFixed(2)}%`;
                        }
                    }
                },
            },
            scales: {
                x: {
                    ticks: { color: "#8b949e", maxRotation: 45, font: { size: 10 } },
                    grid: { color: "#21262d" },
                },
                y: {
                    ticks: {
                        color: "#8b949e",
                        callback: v => (v > 0 ? "+" : "") + v.toFixed(1) + "%",
                    },
                    grid: { color: "#21262d" },
                },
            },
        },
    });
}

// === 模型卡片 ===
function renderModelGrid(models) {
    const grid = document.getElementById("modelGrid");
    grid.innerHTML = "";

    models.forEach(m => {
        const color = MODEL_COLORS[m.name] || "#58a6ff";
        const pnlClass = m.return_pct > 0 ? "pnl-up" : (m.return_pct < 0 ? "pnl-down" : "pnl-flat");
        const pnlPrefix = m.return_pct > 0 ? "+" : "";

        // 持仓列表
        let posHTML = '<span style="color:var(--text-dim)">空仓</span>';
        if (m.positions && m.positions.length > 0) {
            posHTML = m.positions.map(p => {
                const pnlColor = p.unrealized_pnl > 0 ? "var(--up)" : (p.unrealized_pnl < 0 ? "var(--down)" : "var(--flat)");
                return `<span class="pos-item">${p.name} <span style="color:${pnlColor}">${p.unrealized_pnl >= 0 ? "+" : ""}${p.unrealized_pnl.toFixed(0)}</span></span>`;
            }).join(" ");
        }

        const card = document.createElement("div");
        card.className = "model-card";
        card.style.borderLeftColor = color;
        card.innerHTML = `
            <div class="mc-header">
                <span class="mc-name" style="color:${color}">${m.name}</span>
                <span class="mc-return ${pnlClass}">${pnlPrefix}${m.return_pct.toFixed(2)}%</span>
            </div>
            <div class="mc-stats">
                <span>总资产: &yen;${m.total_value.toLocaleString("zh-CN", {minimumFractionDigits: 2})}</span>
                <span>现金: &yen;${m.cash.toLocaleString("zh-CN", {minimumFractionDigits: 2})}</span>
                <span>交易: ${m.trade_count}次 | 胜率: ${m.trade_count > 0 ? m.win_rate.toFixed(0) + "%" : "-"}</span>
                <span>已实现盈亏: &yen;${m.realized_pnl >= 0 ? "+" : ""}${m.realized_pnl.toFixed(2)}</span>
            </div>
            <div class="mc-positions">${posHTML}</div>
        `;
        grid.appendChild(card);
    });
}

// === 启动 + 自动刷新 ===
loadData();
setInterval(loadData, 5 * 60 * 1000);
