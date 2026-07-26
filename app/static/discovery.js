(() => {
  const page = document.body.dataset.page;
  if (page !== "opportunities") return;
  const text = value => value == null || value === "" ? "-" : String(value);
  const escapeHtml = value => text(value).replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
  const request = async (url, options) => {
    const response = await fetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.message || payload.detail || `HTTP ${response.status}`);
    return payload;
  };
  const notify = message => {
    const toast = document.querySelector("#toast");
    if (toast) { toast.textContent = message; toast.classList.add("show"); setTimeout(() => toast.classList.remove("show"), 2600); }
  };
  const qualityClass = quality => ["MISSING", "STALE", "CONFLICTED"].includes(quality) ? "blocked" : "verified";
  const metric = (label, value) => `<span><small>${escapeHtml(label)}</small><b>${escapeHtml(value)}</b></span>`;

  async function loadRun(run) {
    const [industries, candidates] = await Promise.all([
      request(`/api/discovery/runs/${run.id}/industries`),
      request(`/api/discovery/runs/${run.id}/candidates`),
    ]);
    document.querySelector("#discovery-run-summary").innerHTML = [
      ["运行时间", run.completed_at || run.started_at], ["状态", run.status],
      ["市场状态", run.market_state], ["数据质量", run.quality_status],
      ["行业数量", run.industries_evaluated], ["候选数量", run.candidates_generated],
      ["算法", `${run.algorithm_id}@${run.algorithm_version}`], ["快照", run.input_snapshot_hash],
    ].map(([label, value]) => `<article class="${qualityClass(run.quality_status)}"><small>${escapeHtml(label)}</small><b>${escapeHtml(value)}</b></article>`).join("");
    document.querySelector("#discovery-industries").innerHTML = industries.length ? industries.map(item => `<article class="discovery-industry ${qualityClass(item.quality_status)}"><b>#${escapeHtml(item.rank)} ${escapeHtml(item.industry_name)}</b><span>${escapeHtml(item.classification)} / ${escapeHtml(item.score)}</span><small>资金流 1/5/10日：${escapeHtml(item.metrics.net_inflow_1d)} / ${escapeHtml(item.metrics.net_inflow_5d)} / ${escapeHtml(item.metrics.net_inflow_10d)}</small><small>炸板率：${escapeHtml(item.metrics.broken_limit_rate)} / 质量：${escapeHtml(item.quality_status)}</small></article>`).join("") : '<div class="missing-data">无可评估行业。</div>';
    document.querySelector("#discovery-candidates").innerHTML = candidates.length ? candidates.map(item => `<article class="discovery-candidate ${qualityClass(item.quality_status)}"><header><div><small>#${escapeHtml(item.rank)} ${escapeHtml(item.industry_name)}</small><h4>${escapeHtml(item.symbol)} ${escapeHtml(item.name)}</h4></div><b>${escapeHtml(item.candidate_type)} / ${escapeHtml(item.status)}</b></header><div class="candidate-metrics">${metric("当前价格", item.current_price)}${metric("5日涨幅", item.technical_metrics.return_5d_pct)}${metric("20日涨幅", item.technical_metrics.return_20d_pct)}${metric("距MA20", item.technical_metrics.distance_ma20_pct)}${metric("20日回撤", item.technical_metrics.max_drawdown_20d_pct)}${metric("成交额", item.technical_metrics.average_amount_20d)}${metric("换手率", item.technical_metrics.turnover_rate)}${metric("数据质量", item.quality_status)}</div><div class="candidate-reasons">${(item.reason_codes || []).map(reason => `<span>${escapeHtml(reason)}</span>`).join("")}</div><div class="candidate-risks">${(item.risk_flags || []).map(flag => `<span>${escapeHtml(flag)}</span>`).join("")}</div><div class="candidate-actions"><button type="button" data-review="${item.id}">标记已研究</button><button type="button" data-reject="${item.id}">拒绝</button>${["NEW","REVIEWED"].includes(item.status) ? `<button type="button" data-promote="${item.id}" class="primary-action">研究并加入观察</button>` : ""}</div></article>`).join("") : `<div class="missing-data">${escapeHtml((run.blocked_reasons || []).join("; ") || "本次未产生候选。")}</div>`;
  }

  async function loadLatest() {
    const runs = await request("/api/discovery/runs?limit=1");
    if (runs.length) await loadRun(runs[0]);
  }
  document.querySelector("#discovery-run")?.addEventListener("click", async event => {
    event.currentTarget.disabled = true;
    try { const run = await request("/api/discovery/runs", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"}); await loadRun(run); }
    catch (error) { notify(error.message); } finally { event.currentTarget.disabled = false; }
  });
  document.addEventListener("click", async event => {
    const review = event.target.closest?.("[data-review]");
    const reject = event.target.closest?.("[data-reject]");
    const promote = event.target.closest?.("[data-promote]");
    try {
      if (review) { await request(`/api/discovery/candidates/${review.dataset.review}/review`, {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"}); await loadLatest(); }
      if (reject) { await request(`/api/discovery/candidates/${reject.dataset.reject}/reject`, {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"}); await loadLatest(); }
      if (promote) { const dialog = document.querySelector("#candidate-promote-dialog"); dialog.querySelector('[name="candidate_id"]').value = promote.dataset.promote; dialog.showModal(); }
    } catch (error) { notify(error.message); }
  });
  document.querySelector("#candidate-promote-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const submitter = event.submitter;
    const dialog = event.currentTarget.closest("dialog");
    if (submitter?.value === "cancel") { dialog.close(); return; }
    const form = new FormData(event.currentTarget);
    const waiting = String(form.get("waiting_conditions") || "").split("\n").map(value => value.trim()).filter(Boolean);
    try {
      await request(`/api/discovery/candidates/${form.get("candidate_id")}/promote`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({thesis:form.get("thesis"), analysis_capital:Number(form.get("analysis_capital")), waiting_conditions:waiting})});
      dialog.close(); event.currentTarget.reset(); notify("已进入观察名单并启动正式分析"); await loadLatest();
    } catch (error) { notify(error.message); }
  });
  loadLatest().catch(error => notify(error.message));
})();
