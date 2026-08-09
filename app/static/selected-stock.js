(() => {
  const form = document.querySelector("#selected-stock-form");
  if (!form) return;

  const output = document.querySelector("#selected-stock-result");
  const progress = document.querySelector("#selected-stock-progress");
  const replayForm = document.querySelector("#selected-stock-replay-form");
  const replayOutput = document.querySelector("#selected-stock-replay-result");
  const historyOutput = document.querySelector("#selected-stock-history");
  const comparisonOutput = document.querySelector("#selected-stock-comparison");
  const compareBefore = document.querySelector("#selected-stock-compare-before");
  const compareAfter = document.querySelector("#selected-stock-compare-after");
  const steps = [
    "校验输入", "股票历史", "行业映射", "行业历史", "沪深300",
    "技术指标", "硬门禁", "评分", "触发", "风险计划", "策略对比", "完成",
  ];
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const value = (data, fallback = "数据不足") => data === null || data === undefined || data === "" ? fallback : esc(data);
  const statusClass = (status) => {
    if (["FRESH", "AVAILABLE", "VERIFIED", "SINGLE_SOURCE", "PASS"].includes(status)) return "good";
    if (["STALE_ONE_SESSION", "BREADTH_UNAVAILABLE", "INDUSTRY_CONTEXT_UNAVAILABLE", "WAIT_FOR_TRIGGER", "WARN", "NOT_EVALUATED", "INSUFFICIENT_DATA", "USER_PRICE_STALE", "LATEST_CLOSE_ONLY"].includes(status)) return "warn";
    return "bad";
  };
  const status = (text) => `<span class="selected-status ${statusClass(text)}">${esc(text)}</span>`;
  const list = (items, kind) => `<ul class="selected-list">${(items || []).map((item) => `<li class="${kind}">${esc(item.reason_code || item)}</li>`).join("") || `<li class="${kind}">无</li>`}</ul>`;
  const money = (data) => data == null ? "数据不足" : Number(data).toLocaleString("zh-CN", {maximumFractionDigits: 4});
  const range = (low, high) => low == null || high == null ? "未生成" : `${money(low)} - ${money(high)}`;

  function showProgress(active) {
    progress.innerHTML = steps.map((step, index) => `<span class="${index < active ? "done" : index === active ? "active" : ""}">${index + 1}. ${step}</span>`).join("");
  }

  function payload() {
    const raw = Object.fromEntries(new FormData(form));
    const numeric = ["account_id", "current_price", "account_size", "current_position_quantity", "current_position_pct", "average_cost", "available_cash", "risk_budget", "max_position_pct"];
    const result = {stock_code: raw.stock_code, strategy_mode: raw.strategy_mode};
    Object.entries(raw).forEach(([key, item]) => {
      if (!item || ["stock_code", "strategy_mode", "catalyst_summary"].includes(key)) return;
      result[key] = numeric.includes(key) ? Number(item) : item;
    });
    if (raw.current_price_observed_at) result.current_price_observed_at = new Date(raw.current_price_observed_at).toISOString();
    if (raw.catalyst_summary) result.catalyst_context = {catalyst_type: "UNKNOWN", summary: raw.catalyst_summary};
    return result;
  }

  function render(data) {
    const blocked = ["NO_TRADE", "INSUFFICIENT_DATA", "EXIT"].includes(data.plan_status);
    const waiting = data.plan_status === "WAIT_FOR_TRIGGER";
    const gates = data.hard_gates || [];
    const scores = data.scores || {};
    const price = data.price_plan || {};
    const position = data.position_plan || {};
    const lineage = data.source_lineage || [];
    const quality = data.quality_bindings || [];
    const observed = data.price_observation || {};
    const account = data.account_context || {};
    const industry = data.industry_context || {};
    const role = industry.role_evidence || {};
    const discipline = data.survival_discipline || [];
    output.innerHTML = `
      <div class="selected-decision ${blocked ? "blocked" : waiting ? "waiting" : "holding"}">
        <small>${esc(data.strategy_id)} @ ${esc(data.strategy_version)} · ${esc(data.strategy_mode)}</small>
        <h3>${esc(data.plan_status)}</h3>
        <p>CSV_V2 当前仅提供建议与影子对比，正式执行权限仍属于 Product V1。</p>
      </div>
      <div class="selected-summary">
        <article><small>分析日期</small><b>${value(data.analysis_date)}</b></article>
        <article><small>股票数据</small>${status(data.data_status)}</article>
        <article><small>市场上下文</small>${status(data.market_context_status)}</article>
        <article><small>行业上下文</small>${status(data.industry_context_status)}<b>${value(data.industry_name, "行业未证明")}</b></article>
        <article><small>周期状态</small><b>${value(data.cycle_state)}</b></article>
        <article><small>股票角色</small><b>${value(data.stock_role)}</b></article>
        <article><small>交易模式</small><b>${value(data.trade_mode)}</b></article>
        <article><small>评分 / 等级</small><b>${money(scores.total)} / ${value(scores.grade)}</b></article>
      </div>
      <h3>价格与仓位计划</h3>
      <div class="selected-section-grid">
        <article><small>支撑区间</small><b>${range(price.support_zone_low, price.support_zone_high)}</b></article>
        <article><small>买入区间</small><b>${range(price.entry_zone_low, price.entry_zone_high)}</b></article>
        <article><small>止损 / 失效价</small><b>${money(price.stop_loss)} / ${money(price.invalidation_price)}</b></article>
        <article><small>第一 / 第二目标</small><b>${money(price.first_take_profit)} / ${money(price.second_take_profit)}</b></article>
        <article><small>初始 / 最大仓位</small><b>${money(position.initial_position_pct)}% / ${money(position.max_position_pct)}%</b></article>
        <article><small>初始 / 最大数量</small><b>${value(position.quantity)} / ${value(position.max_quantity)}</b></article>
        <article><small>风险金额</small><b>${money(position.risk_amount)}</b></article>
        <article><small>风险收益比</small><b>${money(price.risk_reward_ratio)}</b></article>
      </div>
      <div class="selected-section-grid">
        <article><h4>通过条件</h4>${list(data.passed_conditions, "pass")}</article>
        <article><h4>失败条件</h4>${list(data.failed_conditions, "fail")}</article>
        <article><h4>待确认条件</h4>${list(data.pending_conditions, "pending")}</article>
        <article><h4>执行阻断</h4>${list(data.execution_blockers, "fail")}</article>
      </div>
      <h3>数据可信度与账户来源</h3>
      <div class="selected-section-grid">
        <article><small>价格来源 / 状态</small><b>${value(observed.source)} / ${status(observed.trust_status)}</b><p>${value(observed.observed_at)}</p></article>
        <article><small>价格用途</small><b>入场 ${observed.executable_for_entry ? "可用" : "不可用"} / 仓位 ${observed.executable_for_position ? "可用" : "不可用"}</b><p>${value(observed.reason_code)}</p></article>
        <article><small>账户来源 / 状态</small><b>${value(account.source)} / ${status(account.trust_status)}</b><p>账户 ${value(account.account_id, "研究手工输入")}</p></article>
        <article><small>当日损益</small><b>${money(account.daily_loss_amount)} / ${money(account.daily_loss_pct)}%</b><p>${value(account.observed_at)}</p></article>
        <article><small>单股总风险</small><b>${money(position.maximum_loss_after_trade)}</b></article>
        <article><small>加仓后仓位</small><b>${money(position.post_trade_position_pct)}%</b></article>
        <article><small>T+1 跳空风险</small><b>${money(position.t1_overnight_gap_risk_pct)}% / ${money(position.t1_risk_amount)}</b></article>
        <article><small>账户冲突字段</small><b>${value((account.conflict_fields || []).join("、"), "无")}</b></article>
      </div>
      <h3>行业与股票角色证据</h3>
      <div class="selected-section-grid">
        <article><small>行业映射</small><b>${value(industry.mapping?.industry_name, "未证明")}</b><p>${value(industry.mapping?.provider)}</p></article>
        <article><small>行业历史</small><b>${value(industry.history_row_count)} 行</b><p>${status(industry.status)}</p></article>
        <article><small>成分覆盖</small><b>${value(industry.valid_member_count)} / ${value(industry.constituent_count)}</b><p>${money(Number(industry.coverage_ratio || 0) * 100)}%</p></article>
        <article><small>股票角色</small><b>${value(data.stock_role)}</b><p>${value(role.reason_code)}</p></article>
        <article><small>20 / 60 日排名</small><b>${value(role.return_rank_20)} / ${value(role.return_rank_60)}</b></article>
        <article><small>成交额排名 / 分位</small><b>${value(role.amount_rank)} / ${money(role.amount_percentile)}</b></article>
        <article><small>相对行业 20 / 60 日</small><b>${money(role.excess_return_20)} / ${money(role.excess_return_60)}</b></article>
        <article><small>连续领先天数</small><b>${value(role.consecutive_leading_days)}</b></article>
      </div>
      <h3>保命纪律检查</h3>
      <div class="table-wrap"><table class="selected-table"><thead><tr><th>规则</th><th>状态</th><th>原因</th><th>行动</th><th>证据</th></tr></thead><tbody>${discipline.map((item) => `<tr><td>${esc(item.rule_code)}</td><td>${status(item.status)}</td><td>${esc(item.reason_code)}</td><td>${esc(item.action)}</td><td>${esc(JSON.stringify(item.evidence || {}))}</td></tr>`).join("")}</tbody></table></div>
      <h3>硬门禁</h3>
      <div class="table-wrap"><table class="selected-table"><thead><tr><th>门禁</th><th>状态</th><th>原因</th><th>证据</th></tr></thead><tbody>${gates.map((gate) => `<tr><td>${esc(gate.code)}</td><td>${status(gate.status)}</td><td>${esc(gate.reason_code)}</td><td>${esc((gate.evidence || []).join("；"))}</td></tr>`).join("")}</tbody></table></div>
      <h3>数据质量与来源</h3>
      <div class="table-wrap"><table class="selected-table"><thead><tr><th>能力</th><th>主体</th><th>质量</th><th>数据时间</th><th>质量记录</th></tr></thead><tbody>${quality.map((item) => `<tr><td>${esc(item.capability)}</td><td>${esc(item.subject_type)}:${esc(item.subject_id)}</td><td>${status(item.quality_status)}</td><td>${esc(item.observed_at)}</td><td>#${esc(item.quality_record_id)}</td></tr>`).join("")}</tbody></table></div>
      <div class="table-wrap"><table class="selected-table"><thead><tr><th>Provider</th><th>能力</th><th>来源</th><th>行数</th><th>复权 / 单位</th></tr></thead><tbody>${lineage.map((item) => `<tr><td>${esc(item.provider_id)}</td><td>${esc(item.capability)}</td><td>${esc(item.source)}</td><td>${esc(item.row_count)}</td><td>${value(item.adjustment, "-")} / ${value(item.price_unit, "-")} / ${value(item.volume_unit, "-")}</td></tr>`).join("")}</tbody></table></div>
      <h3>Product V1 影子对比</h3>
      <div class="selected-section-grid"><article><small>冲突状态</small><b>${value(data.product_v1_comparison?.conflict_status)}</b></article><article><small>正式执行所有者</small><b>${value(data.product_v1_comparison?.formal_execution_owner)}</b></article><article><small>Product V1</small><b>${value(data.product_v1_comparison?.product_v1_status)}</b></article><article><small>CSV_V2</small><b>${value(data.product_v1_comparison?.csv_v2_status)}</b></article></div>
      <details class="developer-details selected-raw"><summary>技术指标与不可变快照</summary><pre>${esc(JSON.stringify(data, null, 2))}</pre></details>`;
  }

  function runLabel(item) {
    return `${item.analysis_date} · ${item.plan_status} · ${item.stock_role} · #${item.run_id}`;
  }

  async function loadHistory(symbol) {
    if (!symbol) return;
    const response = await fetch(`/api/v1/selected-stock-analysis/runs?symbol=${encodeURIComponent(symbol)}&limit=50`);
    const rows = await response.json();
    if (!response.ok) throw new Error(rows.error?.message || "历史分析加载失败");
    historyOutput.innerHTML = `<div class="table-wrap"><table class="selected-table"><thead><tr><th>日期</th><th>周期</th><th>角色</th><th>计划</th><th>数据</th><th>行业</th></tr></thead><tbody>${rows.map((item) => `<tr><td>${esc(item.analysis_date)}</td><td>${esc(item.cycle_state)}</td><td>${esc(item.stock_role)}</td><td>${status(item.plan_status)}</td><td>${status(item.data_status)}</td><td>${status(item.industry_status)}</td></tr>`).join("")}</tbody></table></div>`;
    const options = rows.map((item) => `<option value="${esc(item.run_id)}">${esc(runLabel(item))}</option>`).join("");
    compareBefore.innerHTML = options;
    compareAfter.innerHTML = options;
    if (rows.length > 1) compareAfter.selectedIndex = 1;
  }

  document.querySelector("#selected-stock-compare")?.addEventListener("click", async () => {
    if (!compareBefore.value || !compareAfter.value) return;
    try {
      const response = await fetch(`/api/v1/selected-stock-analysis/runs/${encodeURIComponent(compareBefore.value)}/compare/${encodeURIComponent(compareAfter.value)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || "分析比较失败");
      comparisonOutput.innerHTML = `<h4>结构化变化</h4><div class="table-wrap"><table class="selected-table"><thead><tr><th>项目</th><th>之前</th><th>之后</th></tr></thead><tbody>${Object.entries(data.changes || {}).map(([key, change]) => `<tr><td>${esc(key)}</td><td>${esc(JSON.stringify(change.before))}</td><td>${esc(JSON.stringify(change.after))}</td></tr>`).join("")}</tbody></table></div>`;
    } catch (error) {
      comparisonOutput.innerHTML = `<div class="missing-data">${esc(error.message)}</div>`;
    }
  });

  replayForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const raw = Object.fromEntries(new FormData(replayForm));
    const symbol = form.elements.stock_code.value;
    replayOutput.innerHTML = '<div class="missing-data">正在使用已持久化数据回放…</div>';
    try {
      const response = await fetch("/api/v1/selected-stock-analysis/replay", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({symbol, ...raw, strategy_version: "2.0.0"}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || "历史回放失败");
      replayOutput.innerHTML = `<div class="selected-summary"><article><small>样本数</small><b>${esc(data.analysis_sample_count)}</b></article><article><small>有效触发</small><b>${esc(data.valid_trigger_count)}</b></article><article><small>样本结论</small>${status(data.sample_status)}</article><article><small>20日胜率</small><b>${money(data.win_rate)}</b></article><article><small>平均收益</small><b>${money(data.average_return)}</b></article><article><small>最大回撤</small><b>${money(data.maximum_drawdown)}</b></article></div><details class="developer-details"><summary>回放详细结果</summary><pre>${esc(JSON.stringify(data, null, 2))}</pre></details>`;
    } catch (error) {
      replayOutput.innerHTML = `<div class="missing-data">${esc(error.message)}</div>`;
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    showProgress(0);
    let active = 0;
    const timer = setInterval(() => { active = Math.min(active + 1, steps.length - 2); showProgress(active); }, 700);
    output.innerHTML = '<div class="missing-data">正在获取并验证指定股票、行业和基准数据。</div>';
    try {
      const response = await fetch("/api/v1/selected-stock-analysis", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload())});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || data.message || data.detail || "分析失败");
      clearInterval(timer); showProgress(steps.length); render(data); await loadHistory(data.stock_code);
    } catch (error) {
      clearInterval(timer);
      progress.innerHTML = '<span class="active">分析失败</span>';
      output.innerHTML = `<div class="missing-data">${esc(error.message)}</div>`;
    } finally {
      button.disabled = false;
    }
  });
})();
