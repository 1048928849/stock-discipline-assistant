(() => {
  const legacyRenderOneClick = renderOneClick;
  const text = value => value == null || value === "" ? "数据不足" : esc(value);
  const list = value => Array.isArray(value) && value.length
    ? value.map(item => `<li>${text(item)}</li>`).join("")
    : "<li>数据不足</li>";
  const field = (label, value, emphasis = false) => `
    <div class="product-field ${emphasis ? "emphasis" : ""}">
      <small>${esc(label)}</small><strong>${text(value)}</strong>
    </div>`;
  const qualityTone = status => ({
    VERIFIED: "verified", SINGLE_SOURCE: "single", CONFLICTED: "blocked",
    STALE: "stale", MISSING: "missing",
  })[status] || "missing";

  function renderRequiredData(data) {
    const items = data.required_data?.items || [];
    if (!items.length) return '<div class="missing-data">尚未形成完整数据计划。</div>';
    return `<div class="product-quality-grid">${items.map(item => `
      <article class="quality-item ${qualityTone(item.quality_status)}">
        <div><b>${text(item.capability)}</b><span class="quality-code">${text(item.quality_status)}</span></div>
        <small>${text(item.action)} · ${item.cache_used ? "缓存" : "本次数据"}${item.fallback_used ? " · 回退" : ""}</small>
        <span>${text(item.reason)}</span>
      </article>`).join("")}</div>`;
  }

  function renderEvidence(data) {
    const required = (data.decision_package?.evidence || []).filter(item => item.required);
    return `<div class="product-evidence-list">${required.map(item => `
      <article>
        <div><b>${text(item.category)}</b><span class="quality-code ${qualityTone(item.quality_status)}">${text(item.quality_status)}</span></div>
        <span>${text(item.capability)}</span>
        <small>${text(item.source_name)} · ${text(item.observed_at)}</small>
      </article>`).join("")}</div>`;
  }

  function renderProductOneClick(data, result, actions) {
    const plan = data.plan || {};
    const trade = data.trade_decision || {};
    const entry = trade.buy_point_assessment || {};
    const position = trade.position_constraints || {};
    const risk = trade.risk_plan || {};
    const exit = trade.exit_plan || {};
    const technical = data.technical_context || {};
    const market = data.market_regime || {};
    const industry = data.industry_context || {};
    const concept = data.concept_chain_context || {};
    const decision_package = data.decision_package || {};
    const allowed = trade.buy_allowed === true;
    const zone = Array.isArray(trade.buy_zone) ? trade.buy_zone.join(" ～ ") : "数据不足";

    result.innerHTML = `
      <section class="product-decision ${allowed ? "allowed" : "blocked"}">
        <div><small>正式交易结论</small><h2>${allowed ? "允许按计划等待触发" : "当前不允许买入"}</h2></div>
        <span class="decision-code">${text(trade.executable_status || "WAIT")}</span>
        <p>${(trade.blocked_reasons || []).map(text).join("；") || text(trade.trigger_condition)}</p>
      </section>
      <section class="product-band">
        <div class="section-heading"><div><small>MARKET</small><h3>市场与行业环境</h3></div><span>${text(market.state)}</span></div>
        <div class="product-field-grid">
          ${field("市场周期", market.state, true)}${field("市场仓位上限", market.market_position_cap)}
          ${field("上涨占比", market.advance_ratio)}${field("主动进攻", market.offensive_allowed === true ? "允许" : "不允许")}
          ${field("当前主线", (industry.mainlines || []).join("、"))}${field("次主线", (industry.secondary || []).join("、"))}
          ${field("退潮行业", (industry.fading_industries || []).join("、"))}${field("数据完整度", market.data_completeness)}
        </div>
      </section>
      <section class="product-band">
        <div class="section-heading"><div><small>STOCK</small><h3>股票结构与产业位置</h3></div><span>${text(plan.symbol)}</span></div>
        <div class="product-field-grid">
          ${field("日线趋势", technical.daily_trend, true)}${field("60分钟趋势", technical.intraday_trend, true)}
          ${field("日线/60分钟一致", technical.daily_intraday_aligned === true ? "是" : "否")}${field("当前换手率", technical.current_turnover)}
          ${field("20日换手倍数", technical.relative_turnover_20d)}${field("核心概念", concept.core_concept)}
          ${field("产业链", concept.chain)}${field("产业链位置", concept.chain_node)}
          ${field("主线核心公司", concept.is_mainline_core_company === true ? "是" : "否")}${field("收入相关度", concept.revenue_relevance)}
        </div>
      </section>
      <section class="product-band trade-plan-band">
        <div class="section-heading"><div><small>FORMAL PLAN</small><h3>正式交易计划</h3></div><span>${text(entry.buy_point_type)}</span></div>
        <div class="plan-metrics">
          ${field("买入区间", zone, true)}${field("初始试错数量", position.trial_quantity, true)}
          ${field("确认后目标数量", position.target_quantity, true)}${field("最大允许数量", position.maximum_quantity)}
          ${field("每股风险", risk.per_share_risk)}${field("最大计划亏损", risk.maximum_plan_loss)}
          ${field("硬止损", risk.hard_stop, true)}${field("生效约束", position.effective_constraint)}
        </div>
        <div class="condition-columns">
          <article><h4>买入触发</h4><p>${text(trade.trigger_condition)}</p><h4>逻辑失效</h4><p>${text(risk.logical_invalidation)}</p></article>
          <article><h4>补仓条件</h4><p>${text(risk.add_condition)}</p><h4>禁止补仓</h4><p>${text(risk.no_add_condition)}</p></article>
          <article><h4>第一止盈</h4><p>${text(exit.first_take_profit)}</p><h4>第二止盈</h4><p>${text(exit.second_take_profit)}</p></article>
          <article><h4>移动止损</h4><p>${text(exit.trailing_stop)}</p><h4>减仓条件</h4><p>${text(exit.reduce_condition)}</p></article>
          <article><h4>清仓条件</h4><p>${text(exit.exit_condition)}</p><h4>不交易条件</h4><p>${text(exit.no_trade_condition)}</p></article>
          <article><h4>下一交易日观察</h4><ul>${list(trade.next_session_observations)}</ul></article>
        </div>
      </section>
      <section class="product-band">
        <div class="section-heading"><div><small>DATA QUALITY</small><h3>数据质量与 Evidence</h3></div><span>${text(decision_package.quality_status)}</span></div>
        ${renderRequiredData(data)}${renderEvidence(data)}
      </section>
      <section class="product-band">
        <div class="section-heading"><div><small>RESEARCH</small><h3>研究解释</h3></div><span>不改变规则结论</span></div>
        ${aiExplanation(data.ai)}
      </section>
      <details class="developer-details"><summary>原始结构化结果</summary><pre>${esc(JSON.stringify(data, null, 2))}</pre></details>`;

    actions.hidden = false;
    const button = actions.querySelector(".confirm-one-click-plan");
    const reason = actions.querySelector(".save-reason");
    button.disabled = !decision_package.freeze_allowed;
    button.title = data.save_disabled_reason || "";
    reason.textContent = decision_package.freeze_allowed
      ? "关键 Evidence 将在确认时重新验证。"
      : data.save_disabled_reason || "当前计划不可冻结。";
    oneClickRuns[actions.id] = data.run_id;
    const output = document.querySelector("#trade-plan-json");
    if (output) output.textContent = JSON.stringify(data, null, 2);
    if (plan.chart?.length && page === "trade-plans") {
      document.querySelector("#plan-chart-shell").hidden = false;
      requestAnimationFrame(() => drawPlanKline(plan.chart, plan.pattern || {}));
    }
  }

  renderOneClick = (data, result, actions) => {
    if (!data.trade_decision || !data.decision_package?.product_snapshot_hash) {
      legacyRenderOneClick(data, result, actions);
      return;
    }
    renderProductOneClick(data, result, actions);
  };
})();
