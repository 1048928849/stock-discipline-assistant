(() => {
  const form = document.querySelector("#selected-stock-form");
  if (!form) return;

  const output = document.querySelector("#selected-stock-result");
  const progress = document.querySelector("#selected-stock-progress");
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
    if (["STALE_ONE_SESSION", "BREADTH_UNAVAILABLE", "INDUSTRY_CONTEXT_UNAVAILABLE", "WAIT_FOR_TRIGGER"].includes(status)) return "warn";
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
    const numeric = ["current_price", "account_size", "current_position_quantity", "current_position_pct", "average_cost", "available_cash", "risk_budget", "max_position_pct"];
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
      <h3>硬门禁</h3>
      <div class="table-wrap"><table class="selected-table"><thead><tr><th>门禁</th><th>状态</th><th>原因</th><th>证据</th></tr></thead><tbody>${gates.map((gate) => `<tr><td>${esc(gate.code)}</td><td>${status(gate.passed ? "PASS" : "BLOCKED")}</td><td>${esc(gate.reason_code)}</td><td>${esc((gate.evidence || []).join("；"))}</td></tr>`).join("")}</tbody></table></div>
      <h3>数据质量与来源</h3>
      <div class="table-wrap"><table class="selected-table"><thead><tr><th>能力</th><th>主体</th><th>质量</th><th>数据时间</th><th>质量记录</th></tr></thead><tbody>${quality.map((item) => `<tr><td>${esc(item.capability)}</td><td>${esc(item.subject_type)}:${esc(item.subject_id)}</td><td>${status(item.quality_status)}</td><td>${esc(item.observed_at)}</td><td>#${esc(item.quality_record_id)}</td></tr>`).join("")}</tbody></table></div>
      <div class="table-wrap"><table class="selected-table"><thead><tr><th>Provider</th><th>能力</th><th>来源</th><th>行数</th><th>复权 / 单位</th></tr></thead><tbody>${lineage.map((item) => `<tr><td>${esc(item.provider_id)}</td><td>${esc(item.capability)}</td><td>${esc(item.source)}</td><td>${esc(item.row_count)}</td><td>${value(item.adjustment, "-")} / ${value(item.price_unit, "-")} / ${value(item.volume_unit, "-")}</td></tr>`).join("")}</tbody></table></div>
      <h3>Product V1 影子对比</h3>
      <div class="selected-section-grid"><article><small>冲突状态</small><b>${value(data.product_v1_comparison?.conflict_status)}</b></article><article><small>正式执行所有者</small><b>${value(data.product_v1_comparison?.formal_execution_owner)}</b></article><article><small>Product V1</small><b>${value(data.product_v1_comparison?.product_v1_status)}</b></article><article><small>CSV_V2</small><b>${value(data.product_v1_comparison?.csv_v2_status)}</b></article></div>
      <details class="developer-details selected-raw"><summary>技术指标与不可变快照</summary><pre>${esc(JSON.stringify(data, null, 2))}</pre></details>`;
  }

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
      clearInterval(timer); showProgress(steps.length); render(data);
    } catch (error) {
      clearInterval(timer);
      progress.innerHTML = '<span class="active">分析失败</span>';
      output.innerHTML = `<div class="missing-data">${esc(error.message)}</div>`;
    } finally {
      button.disabled = false;
    }
  });
})();
