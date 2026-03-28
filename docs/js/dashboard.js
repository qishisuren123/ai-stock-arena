/**
 * AI 群英会 - 仪表盘逻辑
 * 加载 data/latest.json 和 data/history.json，渲染排行榜、走势图、模型卡片
 * 每 5 分钟自动刷新
 */

// 9 个模型的专属颜色（与 CSS 变量对应）
const MODEL_COLORS = {
    "Claude-4.6":     "#f97316",
    "Gemini-3.1-Pro": "#3b82f6",
    "Minimax2.5":     "#8b5cf6",
    "GLM5":           "#ef4444",
    "DeepSeek-V3.2":  "#06b6d4",
    "Kimi-K2.5":      "#f59e0b",
    "Qwen3.5-397B":   "#ec4899",
    "Intern-S1":      "#14b8a6",
    "Intern-S1-Pro":  "#a78bfa",
};

// 风格标签 → CSS class 映射
const STYLE_TAG_CLASS = {
    "激进派": "style-tag-aggressive",
    "保守派": "style-tag-conservative",
    "追涨型": "style-tag-chaser",
    "抄底型": "style-tag-bargain",
    "长线选手": "style-tag-longterm",
    "短线选手": "style-tag-shortterm",
    "观望派": "style-tag-observer",
    "分散持仓": "style-tag-spread",
    "集中持仓": "style-tag-focus",
    "新手上路": "style-tag-newbie",
    "稳健型": "style-tag-default",
};

// 排名奖牌
const MEDALS = ["\u{1F947}", "\u{1F948}", "\u{1F949}"];

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
    renderBattleReport(latest);
    renderBoardFund(latest);
    renderEvolution(latest);
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

        // 持仓摘要
        let posText = "空仓";
        if (m.positions && m.positions.length > 0) {
            posText = m.positions.map(p => p.name).join(", ");
        }

        // 高级指标
        const met = m.metrics || {};
        const ddText = met.max_drawdown ? `-${met.max_drawdown.toFixed(1)}%` : "-";
        const sharpeText = met.sharpe_ratio ? met.sharpe_ratio.toFixed(2) : "-";
        const streakText = met.win_streak ? `${met.win_streak}连胜` : (met.lose_streak ? `${met.lose_streak}连败` : "-");
        const streakColor = met.win_streak > 0 ? "var(--up)" : (met.lose_streak > 0 ? "var(--down)" : "var(--flat)");

        tr.innerHTML = `
            <td>${rankText}</td>
            <td style="color:${MODEL_COLORS[m.name] || '#e6edf3'}">${m.name}</td>
            <td>&yen;${m.total_value.toLocaleString("zh-CN", {minimumFractionDigits: 2})}</td>
            <td class="${pnlClass}">${pnlPrefix}${m.return_pct.toFixed(2)}%</td>
            <td class="metric-dd">${ddText}</td>
            <td class="metric-sharpe">${sharpeText}</td>
            <td class="metric-streak" style="color:${streakColor}">${streakText}</td>
            <td>${m.trade_count}</td>
            <td>${m.trade_count > 0 ? m.win_rate.toFixed(0) + "%" : "-"}</td>
            <td>${posText}</td>
        `;
        tbody.appendChild(tr);
    });
}

// === 战报渲染 ===
function renderBattleReport(latest) {
    const section = document.getElementById("battleReportSection");
    const currentDiv = document.getElementById("battleReportCurrent");
    const timeDiv = document.getElementById("battleReportTime");
    const histDiv = document.getElementById("battleReportHistory");

    if (!latest.battle_report) {
        section.style.display = "none";
        return;
    }

    section.style.display = "";
    currentDiv.textContent = latest.battle_report;
    timeDiv.textContent = latest.battle_report_time || "";

    // 历史战报
    histDiv.innerHTML = "";
    if (latest.battle_reports && latest.battle_reports.length > 0) {
        // 倒序显示，最新的在上面（排除当前那条）
        const reports = [...latest.battle_reports].reverse();
        reports.forEach(r => {
            if (r.report === latest.battle_report) return; // 跳过当前战报
            const item = document.createElement("div");
            item.className = "hist-item";
            item.textContent = r.report;
            const timeEl = document.createElement("div");
            timeEl.className = "hist-time";
            timeEl.textContent = r.timestamp;
            item.appendChild(timeEl);
            histDiv.appendChild(item);
        });
    }
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

    // 董事会基金曲线（金色虚线）
    const boardData = history.map(h => h.board_return_pct != null ? h.board_return_pct : null);
    const hasBoard = boardData.some(v => v !== null);
    if (hasBoard) {
        datasets.push({
            label: "董事会基金",
            data: boardData,
            borderColor: "#d4a017",
            backgroundColor: "#d4a01720",
            borderWidth: 3,
            borderDash: [6, 3],
            pointRadius: 0,
            pointHoverRadius: 5,
            tension: 0.3,
            spanGaps: true,
        });
    }

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

        // 风格标签
        let tagsHTML = "";
        if (m.style_tags && m.style_tags.length > 0) {
            tagsHTML = '<div class="mc-style-tags">' +
                m.style_tags.map(tag => {
                    const cls = STYLE_TAG_CLASS[tag] || "style-tag-default";
                    return `<span class="style-tag ${cls}">${tag}</span>`;
                }).join("") +
                '</div>';
        }

        // 高级指标摘要
        const met = m.metrics || {};
        let metricsHTML = "";
        if (met.max_drawdown || met.sharpe_ratio || met.avg_hold_days) {
            const parts = [];
            if (met.max_drawdown) parts.push(`回撤 -${met.max_drawdown}%`);
            if (met.sharpe_ratio) parts.push(`Sharpe ${met.sharpe_ratio}`);
            if (met.avg_hold_days) parts.push(`持仓 ${met.avg_hold_days}天`);
            metricsHTML = `<span>${parts.join(" | ")}</span>`;
        }

        // 持仓列表
        let posHTML = '<span style="color:var(--text-dim)">空仓</span>';
        if (m.positions && m.positions.length > 0) {
            posHTML = m.positions.map(p => {
                const pnlColor = p.unrealized_pnl > 0 ? "var(--up)" : (p.unrealized_pnl < 0 ? "var(--down)" : "var(--flat)");
                return `<span class="pos-item">${p.name} <span style="color:${pnlColor}">${p.unrealized_pnl >= 0 ? "+" : ""}${p.unrealized_pnl.toFixed(0)}</span></span>`;
            }).join(" ");
        }

        // 思考过程
        let thinkingHTML = "";
        if (m.thinking && m.thinking.analysis) {
            let actionsHTML = "";
            if (m.thinking.actions && m.thinking.actions.length > 0) {
                actionsHTML = '<div class="thinking-actions">' +
                    m.thinking.actions.map(a => {
                        const cls = a.action === "buy" ? "thinking-action-buy" : "thinking-action-sell";
                        const label = a.action === "buy" ? "买" : "卖";
                        return `<span class="thinking-action-tag ${cls}">${label} ${a.name || a.code}</span>`;
                    }).join("") +
                    '</div>';
            }
            thinkingHTML = `
                <details class="mc-thinking">
                    <summary>AI 思考过程</summary>
                    <div class="thinking-content">
                        <div class="thinking-analysis">${escapeHTML(m.thinking.analysis)}</div>
                        ${actionsHTML}
                    </div>
                </details>`;
        }

        const card = document.createElement("div");
        card.className = "model-card";
        card.style.borderLeftColor = color;
        card.innerHTML = `
            <div class="mc-header">
                <span class="mc-name" style="color:${color}">${m.name}</span>
                <span class="mc-return ${pnlClass}">${pnlPrefix}${m.return_pct.toFixed(2)}%</span>
            </div>
            ${tagsHTML}
            <div class="mc-stats">
                <span>总资产: &yen;${m.total_value.toLocaleString("zh-CN", {minimumFractionDigits: 2})}</span>
                <span>现金: &yen;${m.cash.toLocaleString("zh-CN", {minimumFractionDigits: 2})}</span>
                <span>交易: ${m.trade_count}次 | 胜率: ${m.trade_count > 0 ? m.win_rate.toFixed(0) + "%" : "-"}</span>
                <span>已实现盈亏: &yen;${m.realized_pnl >= 0 ? "+" : ""}${m.realized_pnl.toFixed(2)}</span>
                ${metricsHTML}
            </div>
            <div class="mc-positions">${posHTML}</div>
            ${thinkingHTML}
        `;
        grid.appendChild(card);
    });
}

// === 董事会基金 ===
function renderBoardFund(latest) {
    const section = document.getElementById("boardFundSection");
    const summaryDiv = document.getElementById("boardFundSummary");
    const decisionsDiv = document.getElementById("boardDecisions");

    if (!latest.board_fund) {
        section.style.display = "none";
        return;
    }

    section.style.display = "";
    const bf = latest.board_fund;

    // 收益率样式
    const pnlClass = bf.return_pct > 0 ? "pnl-up" : (bf.return_pct < 0 ? "pnl-down" : "pnl-flat");
    const pnlPrefix = bf.return_pct > 0 ? "+" : "";

    // 统计摘要
    summaryDiv.innerHTML = `
        <div class="board-stat">
            <div class="stat-label">总资产</div>
            <div class="stat-value">&yen;${bf.total_value.toLocaleString("zh-CN", {minimumFractionDigits: 2})}</div>
        </div>
        <div class="board-stat">
            <div class="stat-label">收益率</div>
            <div class="stat-value ${pnlClass}">${pnlPrefix}${bf.return_pct.toFixed(2)}%</div>
        </div>
        <div class="board-stat">
            <div class="stat-label">可用现金</div>
            <div class="stat-value">&yen;${bf.cash.toLocaleString("zh-CN", {minimumFractionDigits: 2})}</div>
        </div>
    `;

    // 持仓
    if (bf.positions && bf.positions.length > 0) {
        const posHTML = bf.positions.map(p => {
            const pnlColor = p.unrealized_pnl > 0 ? "var(--up)" : (p.unrealized_pnl < 0 ? "var(--down)" : "var(--flat)");
            return `<span class="pos-item">${p.name} <span style="color:${pnlColor}">${p.unrealized_pnl >= 0 ? "+" : ""}${p.unrealized_pnl.toFixed(0)}</span></span>`;
        }).join(" ");
        summaryDiv.innerHTML += `<div class="board-positions" style="width:100%">持仓: ${posHTML}</div>`;
    }

    // 最近决议
    const decisions = bf.recent_decisions || [];
    if (decisions.length > 0) {
        let html = '<div class="board-decisions-title">最近决议</div><div class="decision-list">';
        decisions.forEach(d => {
            const actionClass = d.action === "buy" ? "decision-action-buy" : "decision-action-sell";
            const statusClass = d.approved ? "decision-approved" : "decision-rejected";
            const statusText = d.approved ? "通过" : "否决";
            const score = d.vote_score != null ? (d.vote_score * 100).toFixed(0) + "%" : "-";
            html += `
                <div class="decision-item ${statusClass}">
                    <span class="decision-action ${actionClass}">${d.action.toUpperCase()}</span>
                    <span>${d.name || d.code}</span>
                    <span style="color:var(--text-dim);font-size:0.75rem">by ${d.proposer}</span>
                    <span class="decision-score">${score} ${statusText}</span>
                </div>`;
        });
        html += '</div>';
        decisionsDiv.innerHTML = html;
    } else {
        decisionsDiv.innerHTML = '<div style="color:var(--text-dim);font-size:0.82rem">暂无决议记录</div>';
    }
}

// === 进化面板 ===
function renderEvolution(latest) {
    const section = document.getElementById("evolutionSection");
    const genomesDiv = document.getElementById("evoGenomes");
    const capsulesDiv = document.getElementById("evoCapsules");

    if (!latest.evolution) {
        section.style.display = "none";
        return;
    }

    section.style.display = "";
    const evo = latest.evolution;

    // 基因组排行
    let html = `<div style="font-size:0.8rem;color:var(--text-dim);margin-bottom:0.5rem">第 ${evo.generation} 代</div>`;
    html += '<div class="evo-genome-list">';

    (evo.genomes || []).forEach(g => {
        const color = MODEL_COLORS[g.model] || "#58a6ff";
        // 影响力进度条 (0.3 ~ 3.0 映射为 0% ~ 100%)
        const pct = Math.min(100, Math.max(0, (g.influence - 0.3) / 2.7 * 100));
        const propAcc = (g.proposal_accuracy * 100).toFixed(0);
        const voteAcc = (g.vote_accuracy * 100).toFixed(0);

        html += `
            <div class="evo-genome-row">
                <span class="evo-genome-name" style="color:${color}">${g.model}</span>
                <div class="evo-influence-bar">
                    <div class="evo-influence-fill" style="width:${pct}%"></div>
                </div>
                <span class="evo-genome-stats">
                    影响力 ${g.influence.toFixed(2)} | 提案 ${propAcc}% | 投票 ${voteAcc}%
                </span>
                <span class="evo-generation-badge">G${g.generation}</span>
            </div>`;
    });
    html += '</div>';
    genomesDiv.innerHTML = html;

    // 成功策略 Capsule
    const capsules = evo.recent_capsules || [];
    if (capsules.length > 0) {
        let capHTML = `<div class="evo-capsules-title">成功策略 (${evo.total_capsules} 个)</div>`;
        capHTML += '<div class="evo-capsule-list">';
        capsules.forEach(c => {
            const pnl = c.outcome ? c.outcome.pnl : 0;
            capHTML += `
                <div class="evo-capsule-card">
                    <div class="cap-header">${c.proposer || "?"}</div>
                    <div class="cap-detail">
                        ${c.proposal ? (c.proposal.action || "").toUpperCase() + " " + (c.proposal.code || "") : ""}
                        <span class="cap-pnl">+${pnl.toFixed(2)}</span>
                    </div>
                    <div class="cap-detail">${c.timestamp || ""}</div>
                </div>`;
        });
        capHTML += '</div>';
        capsulesDiv.innerHTML = capHTML;
    } else {
        capsulesDiv.innerHTML = '';
    }
}

// === 工具函数 ===
function escapeHTML(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// === 启动 + 自动刷新 ===
loadData();
setInterval(loadData, 5 * 60 * 1000);
