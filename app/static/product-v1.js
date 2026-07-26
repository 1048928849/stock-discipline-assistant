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
        <small><b>${item.required ? "REQUIRED" : "OPTIONAL"}</b> · ${text(item.action)} · ${item.cache_used ? "缓存" : "本次数据"}${item.fallback_used ? " · 回退" : ""}</small>
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

  function renderStrategyBindings(data) {
    const bindings = data.decision_package?.strategy_bindings || [];
    if (!bindings.length) return "";
    return `
      <section class="product-band">
        <div class="section-heading"><div><small>STRATEGY BINDING</small><h3>策略执行绑定</h3></div><span>${bindings.length}</span></div>
        <div class="strategy-binding-list">${bindings.map(binding => `
          <article>
            <div><b>${text(binding.strategy_id)}@${text(binding.strategy_version)}</b><span class="quality-code">BOUND</span></div>
            <dl>
              <dt>implementation_hash</dt><dd>${text(binding.implementation_hash)}</dd>
              <dt>parameter_hash</dt><dd>${text(binding.parameter_hash)}</dd>
              <dt>signal_hash</dt><dd>${text(binding.signal_hash)}</dd>
              <dt>binding_hash</dt><dd>${text(binding.binding_hash)}</dd>
            </dl>
          </article>`).join("")}</div>
      </section>`;
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
    concept.chain = concept.chain_name;
    concept.chain_node = concept.node_name;
    concept.revenue_relevance = [concept.relevance, concept.stage, concept.revenue_relevance]
      .filter(Boolean).join(" / ");
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
      ${renderStrategyBindings(data)}
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
    let watchAction = actions.querySelector(".watchlist-add-action");
    if (!watchAction) {
      actions.insertAdjacentHTML("beforeend", `
        <span class="watchlist-add-action">
          <input class="watchlist-thesis" maxlength="4000" placeholder="观察理由" aria-label="观察理由">
          <button type="button" class="add-analysis-to-watchlist">加入观察名单</button>
        </span>`);
      watchAction = actions.querySelector(".watchlist-add-action");
    }
    watchAction.dataset.runId = String(data.run_id);
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

  const healthTone = value => ["MISSING", "STALE", "CONFLICTED", "DATA_BLOCKED", "PROVIDER_UNAVAILABLE"].includes(value) ? "blocked" : "verified";
  const formatTime = value => value ? new Date(value.endsWith?.("Z") ? value : `${value}Z`).toLocaleString("zh-CN") : "—";

  function watchlistCard(item) {
    return `<button type="button" class="watchlist-card" data-watchlist-id="${item.id}">
      <span><b>${text(item.symbol)} ${text(item.name || "")}</b><small>${text(item.strategy_id)}@${text(item.strategy_version)}</small></span>
      <span><b>${text(item.status)}</b><small class="quality-code ${healthTone(item.monitoring_health)}">${text(item.monitoring_health)}</small></span>
      <span><small>现价 / 买入区</small><b>${text(item.current_price)} / ${text(item.entry_low)}–${text(item.entry_high)}</b></span>
      <span><small>最后扫描</small><b>${formatTime(item.last_scanned_at)}</b></span>
      ${item.unread_event_count ? `<i>${item.unread_event_count}</i>` : ""}
    </button>`;
  }

  async function loadWatchlist() {
    const [items, events] = await Promise.all([json("/api/watchlist/items"), json("/api/watchlist/events?limit=100")]);
    const itemNode = document.querySelector("#watchlist-items");
    if (!itemNode) return;
    itemNode.innerHTML = items.length ? items.map(watchlistCard).join("") : '<div class="missing-data">尚无观察项。请先完成 Product 分析并加入观察名单。</div>';
    document.querySelector("#watchlist-summary").innerHTML = `
      <article><small>观察项</small><b>${items.length}</b></article>
      <article><small>接近 / 触发</small><b>${items.filter(x => ["NEAR_ENTRY", "ENTRY_TRIGGERED"].includes(x.status)).length}</b></article>
      <article><small>数据阻断</small><b>${items.filter(x => healthTone(x.monitoring_health) === "blocked").length}</b></article>
      <article><small>未读事件</small><b>${events.filter(x => !x.acknowledged_at).length}</b></article>`;
    document.querySelector("#watchlist-events").innerHTML = events.length ? events.map(event => `
      <article class="watchlist-event ${event.severity.toLowerCase()}">
        <div><b>${text(event.title)}</b><span>${text(event.severity)}</span></div>
        <small>${formatTime(event.observed_at)} · ${text(event.event_type)}</small>
        <p>${(event.reason_codes || []).map(text).join("；")}</p>
        ${event.acknowledged_at ? "" : `<button type="button" data-ack-event="${event.id}">标记已读</button>`}
      </article>`).join("") : '<div class="missing-data">暂无监控事件。</div>';
  }

  async function showWatchlistDetail(id) {
    const [item, revisions, transitions, events] = await Promise.all([
      json(`/api/watchlist/items/${id}`),
      json(`/api/watchlist/items/${id}/revisions`),
      json(`/api/watchlist/items/${id}/transitions`),
      json(`/api/watchlist/events?item_id=${id}&limit=100`),
    ]);
    const pkg = item.latest_decision_package || {};
    const bindings = pkg.strategy_bindings || [];
    const optionalMissing = pkg.research_decision?.missing_optional_evidence || [];
    document.querySelector("#watchlist-detail").innerHTML = `
      <div class="watchlist-detail-head"><div><small>${text(item.symbol)}</small><h3>${text(item.thesis)}</h3></div><span class="quality-code ${healthTone(item.monitoring_health)}">${text(item.monitoring_health)}</span></div>
      <div class="product-field-grid">
        ${field("状态", item.status, true)}${field("数据质量", item.data_quality)}${field("数据时间", formatTime(item.current_price_observed_at))}${field("当前价格", item.current_price)}
        ${field("买入区间", `${text(item.entry_low)} – ${text(item.entry_high)}`, true)}${field("距离买入区", item.distance_to_entry_pct == null ? "—" : `${item.distance_to_entry_pct}%`)}${field("硬止损", item.hard_stop, true)}${field("分析资金", item.analysis_capital)}
        ${field("市场状态", item.market_state)}${field("行业状态", item.industry_state)}${field("最后分析", formatTime(item.last_analyzed_at))}${field("最后扫描", formatTime(item.last_scanned_at))}
      </div>
      <div class="condition-columns"><article><h4>等待条件</h4><ul>${list(item.waiting_conditions)}</ul></article><article><h4>失效条件</h4><ul>${list(item.invalidation_conditions)}</ul></article><article><h4>可选数据缺失</h4><ul>${list(optionalMissing)}</ul></article></div>
      <h4>StrategyBinding</h4>${bindings.length ? `<div class="strategy-binding-list">${bindings.map(binding => `<article><b>${text(binding.strategy_id)}@${text(binding.strategy_version)}</b><small>${text(binding.binding_hash)}</small></article>`).join("")}</div>` : '<div class="missing-data">暂无策略绑定。</div>'}
      <div class="action-row"><button type="button" data-reanalyze-item="${id}">重新分析</button><button type="button" data-archive-item="${id}">归档</button></div>
      <h4>Revision 历史</h4><div class="watchlist-timeline">${revisions.map(x => `<p><b>v${x.revision_number}</b> ${text(x.change_reason)} <small>${formatTime(x.created_at)}</small></p>`).join("") || "—"}</div>
      <h4>状态迁移</h4><div class="watchlist-timeline">${transitions.map(x => `<p><b>${text(x.from_status)} → ${text(x.to_status)}</b> ${(x.reason_codes || []).map(text).join("；")} <small>${formatTime(x.observed_at)}</small></p>`).join("") || "—"}</div>
      <h4>监控事件</h4><div class="watchlist-timeline">${events.map(x => `<p><b>${text(x.title)}</b> <small>${formatTime(x.observed_at)}</small></p>`).join("") || "—"}</div>`;
  }

  document.addEventListener("click", async event => {
    const add = event.target.closest?.(".add-analysis-to-watchlist");
    if (add) {
      const action = add.closest(".watchlist-add-action");
      const thesis = action.querySelector(".watchlist-thesis").value.trim();
      if (!thesis) return toast("请填写观察理由");
      add.disabled = true;
      try {
        await json("/api/watchlist/items", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({source_type: "PRODUCT_ANALYSIS", analysis_run_id: Number(action.dataset.runId), thesis})});
        toast("已加入观察名单");
      } catch (error) { toast(error.message); } finally { add.disabled = false; }
    }
    const card = event.target.closest?.("[data-watchlist-id]");
    if (card) await showWatchlistDetail(card.dataset.watchlistId);
    const ack = event.target.closest?.("[data-ack-event]");
    if (ack) { await json(`/api/watchlist/events/${ack.dataset.ackEvent}/acknowledge`, {method: "POST"}); await loadWatchlist(); }
    const reanalyze = event.target.closest?.("[data-reanalyze-item]");
    if (reanalyze) { reanalyze.disabled = true; await json(`/api/watchlist/items/${reanalyze.dataset.reanalyzeItem}/reanalyze`, {method: "POST"}); await loadWatchlist(); await showWatchlistDetail(reanalyze.dataset.reanalyzeItem); }
    const archive = event.target.closest?.("[data-archive-item]");
    if (archive) { await json(`/api/watchlist/items/${archive.dataset.archiveItem}/archive`, {method: "POST"}); await loadWatchlist(); document.querySelector("#watchlist-detail").innerHTML = '<div class="missing-data">观察项已归档。</div>'; }
  });
  document.querySelector("#watchlist-scan")?.addEventListener("click", async event => {
    event.currentTarget.disabled = true;
    try { const result = await json("/api/watchlist/scan", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"}); toast(`扫描完成：${result.scanned} 项，${result.transitioned} 项变化`); await loadWatchlist(); } catch (error) { toast(error.message); } finally { event.currentTarget.disabled = false; }
  });
  if (page === "watchlist") loadWatchlist().catch(error => toast(error.message));
})();
