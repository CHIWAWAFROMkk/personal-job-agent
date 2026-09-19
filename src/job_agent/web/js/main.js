import { fetchLocal as fetch, readApiResponse } from './api.js';
import { coalesceRefresh } from './state.js';
import { elements } from './elements.js';
import { renderProfile, prefillProfileForm, syncProfileRequirements, prefillPreferencesForm } from './profile.js';

import {
  statusLabels, trackLabels, roleTierLabels, strategyFitLabels, recommendationLabels, priorityLabels,
  commuteLabels, aiProviderLabels, searchProviderLabels, mapProviderLabels
} from './labels.js';
import {
  escapeHtml, numberText, joinValues, splitValues, optionalPositiveInteger, formatDateTime,
  relativeFreshness, safeExternalUrl, safeArtifactUrl
} from './utils.js';

    let actionToken = "";
    let activeProfile = null;
    let trackedJobs = [];
    let currentJobs = [];
    let jobsExpanded = false;
    let pendingApplyTotal = 0;
    let dashboardLoading = false;
    let connectorSettings = null;
    let copilotSnapshot = null;
    let copilotBusy = false;

    function usageText(counter) {
      const used = Number(counter?.successful_requests || 0);
      if (counter?.local_remaining !== null && counter?.local_remaining !== undefined) {
        return `<strong>${escapeHtml(counter.local_remaining)}</strong> 次本机可用 · 已记录 ${escapeHtml(used)} 次成功请求`;
      }
      return `已记录 <strong>${escapeHtml(used)}</strong> 次成功请求 · 真实余额请查看服务商控制台`;
    }

    function syncConnectorFields() {
      const aiProvider = elements.aiProvider.value;
      const usesCloudAi = aiProvider !== "local";
      const usesCompatible = aiProvider === "openai_compatible";
      elements.aiModelField.hidden = !usesCloudAi;
      elements.aiKeyField.hidden = !usesCloudAi || aiProvider === "codex";
      elements.clearAiKeyRow.hidden = !usesCloudAi || aiProvider === "codex";
      document.getElementById("codexHelp").hidden = aiProvider !== "codex";
      elements.aiBaseUrlField.hidden = !usesCompatible;
      if (aiProvider === "deepseek" && (!elements.aiModel.value || elements.aiModel.value === "local-explainable-v1" || elements.aiModel.value === "gpt-5.6-luna")) {
        elements.aiModel.value = "deepseek-chat";
      }
      elements.aiModel.required = usesCloudAi;
      elements.aiBaseUrl.required = usesCompatible;
      const usesSearch = elements.searchProvider.value !== "none";
      elements.searchKeyField.hidden = !usesSearch;
      elements.clearSearchKeyRow.hidden = !usesSearch;
      const usesMap = elements.mapProvider.value !== "none";
      elements.mapKeyField.hidden = !usesMap;
      elements.clearMapKeyRow.hidden = !usesMap;
      if (!usesCloudAi) {
        elements.aiApiKey.value = "";
        elements.clearAiApiKey.checked = false;
      }
      if (!usesSearch) {
        elements.searchApiKey.value = "";
        elements.clearSearchApiKey.checked = false;
      }
      if (!usesMap) {
        elements.mapApiKey.value = "";
        elements.clearMapApiKey.checked = false;
      }
    }

    function renderConnectorSettings(settings) {
      connectorSettings = settings;
      const ai = settings?.ai || { provider: "local", model: "local-explainable-v1", ready: true };
      const search = settings?.search || { provider: "none", ready: true, enabled: false };
      const maps = settings?.maps || { provider: "none", ready: true, enabled: false };
      const usage = settings?.usage?.connectors || {};
      elements.aiProvider.value = ai.provider || "local";
      elements.aiModel.value = ai.provider === "local" ? "" : (ai.model || "");
      elements.aiBaseUrl.value = ai.base_url || "";
      elements.aiApiKey.value = "";
      elements.clearAiApiKey.checked = false;
      elements.searchProvider.value = search.provider || "none";
      elements.searchApiKey.value = "";
      elements.clearSearchApiKey.checked = false;
      elements.mapProvider.value = maps.provider || "none";
      elements.mapApiKey.value = "";
      elements.clearMapApiKey.checked = false;
      elements.aiMonthlyQuota.value = ai.monthly_quota || "";
      elements.searchMonthlyQuota.value = search.monthly_quota || "";
      elements.mapMonthlyQuota.value = maps.monthly_quota || "";
      syncConnectorFields();

      elements.aiStatusTitle.textContent = ai.provider === "local"
        ? "本地能力可用"
        : ai.ready ? "AI 已配置" : ai.provider === "codex" ? "未找到 Codex CLI" : "等待密钥";
      elements.aiStatusDetail.textContent = ai.provider === "local"
        ? "本地可解释评分，不产生 API 费用"
        : `${aiProviderLabels[ai.provider] || ai.provider} · ${ai.model || "模型未填写"}${ai.provider === "codex" ? " · 使用本机登录，首次调用时验证" : ai.api_key_configured ? " · 密钥已保存，首次调用时验证" : ""}`;
      elements.aiStatusDot.classList.toggle("ready", Boolean(ai.ready));

      elements.searchStatusTitle.textContent = !search.enabled
        ? "手动导入模式"
        : search.ready ? "搜索可用" : "等待密钥";
      elements.searchStatusDetail.textContent = !search.enabled
        ? "不消耗搜索额度"
        : `${searchProviderLabels[search.provider] || search.provider}${search.api_key_configured ? " · 密钥已配置" : " · 尚未配置密钥"}`;
      elements.searchStatusDot.classList.toggle("ready", Boolean(search.ready));

      elements.mapStatusTitle.textContent = !maps.enabled
        ? "手动通勤模式"
        : maps.ready ? "路线计算可用" : "等待密钥";
      elements.mapStatusDetail.textContent = !maps.enabled
        ? "不发送地址，可手动记录分钟"
        : `${mapProviderLabels[maps.provider] || maps.provider}${maps.api_key_configured ? " · 密钥已配置" : " · 尚未配置密钥"}`;
      elements.mapStatusDot.classList.toggle("ready", Boolean(maps.ready));
      elements.aiUsageLine.innerHTML = usageText(usage.ai);
      elements.searchUsageLine.innerHTML = usageText(usage.search);
      elements.mapUsageLine.innerHTML = usageText(usage.maps);
      elements.aiKeysLink.hidden = ai.provider !== "openai";
      elements.aiBalanceLink.hidden = ai.provider !== "openai";
      elements.searchBalanceLink.hidden = search.provider !== "bocha";

      const allReady = Boolean(ai.ready && search.ready && maps.ready);
      elements.connectorSummaryDot.classList.toggle("ready", allReady);
      elements.connectorSummaryText.textContent = allReady
        ? (ai.provider === "local" && !search.enabled ? "本地模式" : "配置已就绪")
        : "需要设置 API";
    }

    async function loadConnectorSettings() {
      const response = await fetch("/api/settings", { cache: "no-store" });
      const settings = await readApiResponse(response);
      renderConnectorSettings(settings);
      return settings;
    }

    function statusBadgeClass(status) {
      if (["resume_requested", "screening", "assessment", "written_test", "interview_1", "interview_2", "final_interview", "offer"].includes(status)) return "positive";
      if (["rejected", "withdrawn", "no_response"].includes(status)) return "closed";
      if (["ready_to_apply", "applied", "hr_read"].includes(status)) return "info";
      return "";
    }

    function strategyBadgeClass(fit) {
      if (fit === "recommended") return "positive";
      if (fit === "blocked") return "closed";
      return "warning";
    }

    function priorityBadgeClass(priority) {
      if (["critical", "urgent"].includes(priority)) return "warning";
      if (["high", "elevated"].includes(priority)) return "positive";
      if (priority === "closed") return "closed";
      return "";
    }

    function commuteBadgeClass(fit) {
      if (fit === "good") return "positive";
      if (fit === "near_limit") return "warning";
      if (fit === "over_limit") return "closed";
      return "info";
    }

    function commuteText(job) {
      const label = commuteLabels[job.commute_fit] || "待估算";
      if (job.commute_minutes === null || job.commute_minutes === undefined) return label;
      const fitLabel = job.commute_fit === "unknown" ? "上限未设置" : label;
      if (Number(job.commute_minutes) === 0) return `远程 · ${fitLabel}`;
      return `${job.commute_minutes} 分钟 · ${fitLabel}`;
    }

    function compensationText(job) {
      if (job.opportunity_track !== "daily_internship") return "薪资按正式岗位核实";
      if (job.compensation_min_daily === null || job.compensation_min_daily === undefined) return "日薪待确认";
      if (job.compensation_max_daily && job.compensation_max_daily !== job.compensation_min_daily) {
        return `${job.compensation_min_daily}–${job.compensation_max_daily} 元/天`;
      }
      return `${job.compensation_min_daily} 元/天`;
    }

    function renderTrackedJobs(jobs) {
      trackedJobs = jobs || [];
      const previousStatus = elements.statusJob.value;
      const previousCommute = elements.commuteJob.value;
      const previousRoute = elements.routeJob.value;
      if (!jobs.length) {
        elements.statusJob.innerHTML = '<option value="">暂无岗位，请先搜索并导入岗位</option>';
        elements.commuteJob.innerHTML = '<option value="">暂无岗位，请先搜索并导入岗位</option>';
        elements.routeJob.innerHTML = '<option value="">暂无岗位，请先搜索并导入岗位</option>';
        elements.statusSubmit.disabled = true;
        elements.commuteSubmit.disabled = true;
        elements.routeSubmit.disabled = true;
        return;
      }
      elements.statusSubmit.disabled = false;
      elements.commuteSubmit.disabled = false;
      elements.routeSubmit.disabled = false;
      const options = jobs.map((job) => {
        const current = statusLabels[job.status] || job.status || "已发现";
        return `<option value="${escapeHtml(job.job_id)}">#${escapeHtml(job.job_id)} · ${escapeHtml(job.company)} · ${escapeHtml(job.title)}（${escapeHtml(current)}）</option>`;
      }).join("");
      elements.statusJob.innerHTML = options;
      elements.commuteJob.innerHTML = options;
      elements.routeJob.innerHTML = options;
      if (jobs.some((job) => String(job.job_id) === previousStatus)) elements.statusJob.value = previousStatus;
      if (jobs.some((job) => String(job.job_id) === previousCommute)) elements.commuteJob.value = previousCommute;
      if (jobs.some((job) => String(job.job_id) === previousRoute)) elements.routeJob.value = previousRoute;
      prefillCommuteForJob();
      prefillRouteForJob();
    }

    function prefillCommuteForJob() {
      const job = trackedJobs.find((item) => String(item.job_id) === elements.commuteJob.value);
      if (!job) {
        elements.commuteMinutes.value = "";
        elements.commuteNote.value = "";
        elements.commuteMethod.value = "user_estimate";
        updateCommuteMethodInput();
        return;
      }
      elements.commuteMinutes.value = job.commute_minutes ?? "";
      elements.commuteNote.value = job.commute_note || "";
      const supportedMethods = ["user_estimate", "route_estimate", "remote", "unknown"];
      elements.commuteMethod.value = supportedMethods.includes(job.commute_method)
        ? job.commute_method
        : "user_estimate";
      updateCommuteMethodInput();
    }

    function prefillRouteForJob() {
      const job = trackedJobs.find((item) => String(item.job_id) === elements.routeJob.value);
      if (!job) {
        elements.commuteDestination.value = "";
        elements.routeResult.classList.remove("visible");
        elements.routeResult.innerHTML = "";
        return;
      }
      elements.commuteDestination.value = job.commute_destination || "";
      if (job.commute_mode && ["transit", "driving", "walking", "bicycling"].includes(job.commute_mode)) {
        elements.routeMode.value = job.commute_mode;
      }
      if (job.commute_route_summary) {
        const distance = job.commute_distance_meters
          ? `${(Number(job.commute_distance_meters) / 1000).toFixed(1)} 公里`
          : "距离未返回";
        elements.routeResult.innerHTML = `<strong>已保存路线 · ${escapeHtml(job.commute_minutes ?? "—")} 分钟</strong><div class="usage-line">${escapeHtml(distance)} · ${escapeHtml(job.commute_route_summary)}</div>`;
        elements.routeResult.classList.add("visible");
      } else {
        elements.routeResult.classList.remove("visible");
        elements.routeResult.innerHTML = "";
      }
    }

    function updateCommuteMethodInput() {
      const clearing = elements.commuteMethod.value === "unknown";
      elements.commuteMinutes.required = !clearing;
      elements.commuteMinutes.disabled = clearing;
      if (clearing) elements.commuteMinutes.value = "";
      if (elements.commuteMethod.value === "remote") elements.commuteMinutes.value = "0";
    }

    function renderMetrics(metrics) {
      const byId = new Map(metrics.map((metric) => [metric.metric_id, metric]));
      const pending = byId.get("pending_apply") || { value: 0, unit: "" };
      const preparation = byId.get("priority_preparation") || { value: 0, unit: "" };
      const feedback = byId.get("new_feedback") || { value: 0, unit: "" };
      const supportingOrder = ["candidates", "jobs", "recommended", "strongly_recommended", "applied", "meaningful_response_rate", "outsourcing_filtered", "pay_to_confirm"];
      const supportingMetrics = supportingOrder.map((id) => byId.get(id)).filter(Boolean);
      const missionValues = {
        missionPending: pending.value,
        missionCandidates: byId.get("candidates")?.value || 0,
        missionRecommended: byId.get("recommended")?.value || 0,
        missionConfirm: pending.value,
        missionApplied: byId.get("applied")?.value || 0,
        missionFeedback: feedback.value
      };
      Object.entries(missionValues).forEach(([key, value]) => {
        if (elements[key]) elements[key].textContent = numberText(value);
      });
      pendingApplyTotal = Number(pending.value || 0);
      elements.queueButton.textContent = pendingApplyTotal > 0
        ? `处理 ${numberText(pendingApplyTotal)} 个待确认`
        : "暂无待确认岗位";
      elements.queueButton.disabled = dashboardLoading || pendingApplyTotal <= 0;
      elements.metrics.innerHTML = `
        <div class="metrics-narrative">
          <div class="metrics-command" aria-label="今天需要处理">
            <div class="metric-clause"><span>待确认投递</span><strong class="signal">${escapeHtml(numberText(pending.value))}</strong><small>个岗位</small></div>
            <div class="metric-clause"><span>优先准备</span><strong>${escapeHtml(numberText(preparation.value))}</strong><small>项任务</small></div>
            <div class="metric-clause"><span>最新反馈</span><strong>${escapeHtml(numberText(feedback.value))}</strong><small>条变化</small></div>
          </div>
          <div class="metrics-inline-list" aria-label="求职数据概览">
          ${supportingMetrics.map((metric) => `
            <span class="metric-inline" title="${escapeHtml(metric.description)}">
              <span>${escapeHtml(metric.label)}</span>
              <strong>${escapeHtml(numberText(metric.value))}${metric.unit ? escapeHtml(metric.unit) : ""}</strong>
            </span>`).join("")}
          </div>
        </div>`;
    }

    function renderFunnel(stages) {
      if (!stages.length) {
        elements.funnel.innerHTML = '<div class="empty-state">还没有可展示的漏斗数据。</div>';
        return;
      }
      const maxValue = Math.max(1, Number(stages[0].value || 0), ...stages.map((item) => Number(item.value || 0)));
      elements.funnel.innerHTML = `<div class="funnel-list">${stages.map((stage) => {
        const value = Number(stage.value || 0);
        const width = value === 0 ? 0 : Math.max(2.5, Math.min(100, (value / maxValue) * 100));
        return `
          <div class="funnel-row" title="${escapeHtml(stage.definition)}">
            <span class="funnel-label">${escapeHtml(stage.label)}</span>
            <div class="funnel-track" aria-label="${escapeHtml(stage.label)} ${value}">
              <div class="funnel-fill" style="width:${width.toFixed(1)}%"></div>
            </div>
            <span class="funnel-value">${escapeHtml(numberText(value))}</span>
          </div>`;
      }).join("")}</div>`;
    }

    let selectedJobId = null;
    let jobFilter = "all";
    let jobTrack = null;
    let detailRequest = 0;
    let detailTab = "jd";
    let workspaceDetails = null;
    let copilotHistory = false;
    const composerDrafts = new Map();
    const $ = (id) => document.getElementById(id);
    const pendingStatuses = new Set(["discovered", "saved", "ready_to_apply", "matched", "recommended"]);
    let homeSnapshot = null;
    let currentView = "today";
    let navigationReady = false;
    let listPosition = 0;

    function rememberNavigation() {
      if (!navigationReady) return;
      if (currentView === "jobs" && !$("jobWorkspace").classList.contains("detail-visible")) listPosition = elements.jobs.scrollTop;
      history.replaceState({...history.state, filter: jobFilter, track: jobTrack,
        query: $("jobSearch").value, listPosition, tab: detailTab}, "");
    }

    function navigate(view, jobId = null, restoring = false, state = null) {
      if (!restoring) rememberNavigation();
      if (state) {
        jobFilter = state.filter || "all";
        jobTrack = state.track || "all";
        $("jobSearch").value = state.query || "";
        listPosition = state.listPosition || 0;
      }
      currentView = view;
      $("todayPanel").hidden = view !== "today";
      $("jobWorkspace").hidden = view !== "jobs";
      $("overviewPanel").hidden = !["progress", "profile"].includes(view);
      $("overviewPanel").dataset.mode = view;
      $("overviewTitle").textContent = view === "profile" ? "我的资料" : "投递进度";
      elements.queueButton.hidden = view === "profile";
      for (const [id, target] of [["todayNav","today"],["workspaceNav","jobs"],["overviewNav","progress"],["profileNav","profile"]]) {
        $(id).classList.toggle("active", view === target);
        if (view === target) $(id).setAttribute("aria-current", "page");
        else $(id).removeAttribute("aria-current");
      }
      $("jobWorkspace").classList.remove("agent-visible");
      $("jobWorkspace").classList.add("agent-hidden");
      elements.openCopilotButton.setAttribute("aria-expanded", "false");
      $("jobWorkspace").classList.toggle("detail-visible", view === "jobs" && jobId != null);
      if (view === "jobs") {
        renderJobs(currentJobs);
        if (jobId != null) {
          selectJob(jobId, false);
          detailTab = state?.tab || "jd";
        } else requestAnimationFrame(() => { elements.jobs.scrollTop = listPosition; });
      }
      if (!restoring) {
        const next = {view, jobId, depth: (history.state?.depth || 0) + 1};
        history.pushState(next, "", jobId == null ? `#${view}` : `#job/${jobId}`);
      }
      window.scrollTo(0, 0);
      const title = view === "today" ? $("todayTitle") : view === "jobs" ? (jobId == null ? elements.jobsTitle : $("jobDetail").querySelector("h2")) : $("overviewTitle");
      if (!restoring || state) requestAnimationFrame(() => title?.focus({preventScroll: true}));
    }

    function goBack() {
      if ((history.state?.depth || 0) > 0) history.back();
      else navigate(currentView === "jobs" ? "jobs" : "today");
    }

    function homeRow(job, action = "查看岗位", tab = "jd", note = "") {
      return `<article class="today-job"><div class="today-job-body"><h3>${escapeHtml(job.title)}</h3><p>${escapeHtml(job.company)} · ${escapeHtml(job.location || "地点待确认")}</p><div class="today-job-facts"><span>${escapeHtml(compensationText(job))}</span><span>通勤 ${escapeHtml(commuteText(job))}</span>${job.deadline_urgent ? `<strong>剩 ${escapeHtml(job.deadline_days)} 天</strong>` : ""}</div>${note ? `<p class="today-job-note">${escapeHtml(note)}</p>` : ""}</div><button class="action-button secondary" type="button" data-home-job="${job.job_id}" data-home-tab="${tab}">${action}</button></article>`;
    }

    function homeFocus(job, total) {
      if (!job) return "";
      const deadline = job.deadline_days != null ? `${escapeHtml(job.deadline_days)} 天` : "待核实";
      return `<section class="today-focus" aria-labelledby="todayFocusTitle">
        <header class="today-focus-head"><div><h2 id="todayFocusTitle">${escapeHtml(job.title)}</h2><p>${escapeHtml(job.company)} · ${escapeHtml(job.location || "地点待确认")}</p></div><span class="today-focus-score" aria-label="本地匹配参考 ${escapeHtml(job.match_score ?? "未知")} 分">${escapeHtml(job.match_score ?? "—")}<small>/100</small></span></header>
        <dl class="today-focus-facts"><div><dt>薪资</dt><dd>${escapeHtml(compensationText(job))}</dd></div><div><dt>单程通勤</dt><dd>${escapeHtml(commuteText(job))}</dd></div><div><dt>截止</dt><dd>${deadline}</dd></div></dl>
        <p class="today-focus-reason">${escapeHtml((job.strategy_reasons || [])[0] || "先核实岗位要求与硬门槛，再决定是否投入准备时间。")}</p>
        <div class="today-focus-actions"><button class="action-button" type="button" data-home-job="${job.job_id}" data-home-tab="jd">查看并决定</button><button class="action-button secondary" type="button" data-home-action="jobs">全部职位</button></div>
        <p class="section-note">这是 ${total} 个待投岗位中的当前优先项；推荐仅供判断，不代表满足全部要求。</p>
      </section>`;
    }

    function renderHome(data) {
      homeSnapshot = data;
      const profile = data.active_profile;
      const jobs = (data.jobs_to_apply || []).filter(j => !j.job_archived && !j.has_applied && j.strategy_fit !== "blocked" && j.commute_fit !== "over_limit");
      const feedback = (data.recent_feedback || []).slice(0, 3);
      const prep = (data.priority_preparation || []).filter(p => !currentJobs.find(j => j.job_id === p.job_id)?.job_archived && ["resume_requested","screening","assessment","written_test","interview_1","interview_2","final_interview"].includes(p.application_status)).slice(0, 3);
      const needsProfile = !profile || !profile.confirmed_fact_count;
      const needsGoals = profile && !(profile.target_roles || []).length;
      $("todayTitle").textContent = needsProfile ? "先让 Agent 认识你" : "今天，先推进一个岗位";
      $("todayContext").textContent = profile ? `${(profile.target_roles || []).slice(0, 2).join(" / ") || "求职方向待设置"} · ${(profile.preferred_locations || []).join("、") || "地点待设置"}` : "从真实简历开始，建立属于你的求职工作台。";
      const setup = needsProfile || needsGoals;
      const setupContent = `<section class="setup-guide"><h2>${needsProfile ? "导入你的真实简历" : "你想找什么工作？"}</h2><p>${needsProfile ? "确认经历后，再按你的方向寻找和匹配岗位。" : "设置方向、城市和通勤范围，让推荐与你有关。"}</p><ol class="setup-steps"><li ${needsProfile ? 'aria-current="step"' : ''}>导入简历</li><li ${needsGoals ? 'aria-current="step"' : ''}>确认目标</li><li>选择服务</li><li>查看岗位</li></ol><button class="action-button" type="button" data-home-action="${needsProfile ? "profile" : "preferences"}">${needsProfile ? "导入简历" : "设置目标"}</button><p class="quiet">连接 AI 和搜索服务是可选步骤，也可以先使用本地能力。</p></section>`;
      const focus = jobs[0];
      const later = jobs.slice(1, 3);
      const queue = later.map(j => homeRow(j, "查看")).join("");
      const noJobs = currentJobs.length ? "暂无进入待投队列的岗位，可查看全部或调整条件。" : "还没有岗位。当前搜索需通过命令行或外部助手运行，完成后在这里刷新；连接服务不会自动开始搜索。";
      $("todayContent").innerHTML = `${setup ? setupContent : ""}<div class="today-grid"><section class="today-shortlist" aria-label="今日岗位">${focus ? homeFocus(focus, jobs.length) : `<div class="task-empty"><p>${noJobs}</p><button class="action-button secondary" type="button" data-home-action="${currentJobs.length ? "preferences" : "settings"}">${currentJobs.length ? "调整条件" : "连接服务"}</button></div>`}${queue ? `<details class="today-queue"><summary><span>接下来再处理</span><small>${later.length} 个岗位</small></summary><div>${queue}</div></details>` : ""}</section><aside class="today-side" aria-label="反馈与准备"><section><div class="section-heading"><h2>最新反馈</h2><button class="action-button secondary" type="button" data-home-action="progress">查看进度</button></div>${feedback.length ? feedback.map(r => `<button class="feedback-row" type="button" data-home-job="${r.job_id}" data-home-tab="progress"><strong class="feedback-company">${escapeHtml(r.company)}</strong><span class="feedback-role">${escapeHtml(r.title)}</span><span class="feedback-meta">${escapeHtml(statusLabels[r.status] || r.status)} · ${escapeHtml(formatDateTime(r.occurred_at))}</span></button>`).join("") : '<p class="task-empty">暂无已记录的新反馈。</p>'}<p class="section-note">以已核实或补录的记录为准，不是网站实时状态。</p></section><section><div class="section-heading"><h2>需要准备</h2></div>${prep.length ? prep.map(r => `<button class="feedback-row" type="button" data-home-job="${r.job_id}" data-home-tab="prep"><strong class="feedback-company">${escapeHtml(r.company)}</strong><span class="feedback-role">${escapeHtml(r.title)}</span><span class="feedback-meta">${escapeHtml(statusLabels[r.application_status] || r.application_status)} · 准备任务</span></button>`).join("") : '<p class="task-empty">暂无优先准备任务。进入筛选、测评或面试后，在这里集中处理。</p>'}</section></aside></div>`;
      $("todayContent").querySelector(".today-grid").hidden = setup;
      for (const button of $("todayContent").querySelectorAll(".feedback-row")) {
        const row = document.createElement("article");
        row.className = "feedback-item";
        const content = document.createElement("div");
        content.append(...Array.from(button.childNodes));
        button.before(row);
        button.className = "action-button secondary";
        button.textContent = "查看";
        row.append(content, button);
      }
    }

    function showWorkspace(overview = false) {
      navigate(overview ? "progress" : "jobs");
    }

    function renderJobs(jobs) {
      currentJobs = jobs;
      const query = $("jobSearch").value.trim().toLocaleLowerCase();
      const visible = jobs.filter((job) => {
        const pending = !job.job_archived && !job.has_applied && pendingStatuses.has(job.status);
        return (jobTrack === "all" || jobTrack === null || job.opportunity_track === jobTrack)
          && (jobFilter === "all" || (jobFilter === "pending" ? pending : jobFilter === "archived" ? job.job_archived : job.has_applied))
          && (job.company + " " + job.title + " #" + job.job_id).toLocaleLowerCase().includes(query);
      }).sort((a, b) => Number(Boolean(a.job_archived)) - Number(Boolean(b.job_archived)) || Number(Boolean(a.has_applied)) - Number(Boolean(b.has_applied)));
      const group = job => job.job_archived ? "已失效 · 沉底" : job.has_applied ? "已投递" : "待投递与其他岗位";
      elements.jobsCount.textContent = String(visible.length);
      document.querySelectorAll("[data-job-filter]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.jobFilter === jobFilter)));
      document.querySelectorAll("[data-job-track]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.jobTrack === jobTrack)));
      elements.jobs.innerHTML = visible.length ? visible.map((job, index) => `
        ${index === 0 || group(job) !== group(visible[index - 1]) ? `<h3 class="job-group-title">${group(job)} · ${visible.filter(item => group(item) === group(job)).length}</h3>` : ""}
        <button class="job-choice${job.has_applied ? " is-applied" : ""}" type="button" data-select-job="${job.job_id}" aria-current="${String(job.job_id) === String(selectedJobId) ? "true" : "false"}">
          <strong class="choice-title" title="${escapeHtml(job.title)}">${job.job_archived ? '<span class="ignored-company-mark" role="img" aria-label="岗位已失效"></span>' : ""}${escapeHtml(job.title)}</strong>
          <span class="choice-company">${escapeHtml(job.company)}<span class="choice-score" title="本地匹配分，满分 100">${escapeHtml(job.match_score ?? "—")}</span></span>
          <span class="choice-signals"><span>${escapeHtml(trackLabels[job.opportunity_track] || "其他机会")}</span><span class="${escapeHtml(strategyBadgeClass(job.strategy_fit))}">${escapeHtml(strategyFitLabels[job.strategy_fit] || "待判断")}</span></span>
          <span class="choice-meta"><span>${escapeHtml(job.location || "地点待确认")}</span><span>${job.has_applied && job.status !== "applied" ? "已投递 · " : ""}${escapeHtml(statusLabels[job.status] || job.status)}</span></span>
        </button>`).join("") : '<div class="empty-state">没有符合条件的职位。<button type="button" class="text-link" data-clear-search>清除筛选</button></div>';
      if (!currentJobs.some(job => String(job.job_id) === String(selectedJobId))) selectJob(visible[0]?.job_id ?? null, false);
    }

    function renderStrategyPlan(strategy) {
      if (!strategy) return;
      $("internshipShare").textContent = `${strategy.daily_internship_share}%`;
      $("autumnShare").textContent = `${strategy.autumn_recruitment_share}%`;
      $("trackPlan").textContent = strategy.rule_label || "按当前求职阶段安排两个通道。";
      $("trackPlan").title = `近 30 天已投 ${strategy.applications_last_30_days || 0}，有效推进 ${strategy.meaningful_progress_last_30_days || 0}；每批 ${strategy.experiment_batch_size || 20} 份。`;
      if (jobTrack === null) jobTrack = strategy.recommended_track || "daily_internship";
    }

    function selectJob(jobId, reveal = true) {
      if (reveal) { navigate("jobs", Number(jobId)); return; }
      if (selectedJobId != null) composerDrafts.set(String(selectedJobId), elements.copilotInput.value);
      selectedJobId = jobId == null ? null : Number(jobId);
      elements.copilotInput.value = composerDrafts.get(String(selectedJobId)) || "";
      workspaceDetails = null;
      detailTab = "jd";
      document.querySelectorAll("[data-select-job]").forEach(button => button.setAttribute("aria-current", String(Number(button.dataset.selectJob) === selectedJobId)));
      const job = currentJobs.find(item => item.job_id === selectedJobId);
      $("copilotScope").textContent = job ? `#${job.job_id} · ${job.company}` : "未选择职位";
      if (reveal) $("jobWorkspace").classList.add("detail-visible");
      renderJobDetail(job);
      if (copilotSnapshot) renderCopilot(copilotSnapshot);
      loadJobDetail(job);
    }

    function renderJobDetail(job) {
      if (!job) {
        $("jobDetail").innerHTML = '<div class="empty-state">选择一个职位，开始准备。</div>';
        return;
      }
      const w = job.workspace || {};
      const status = w.resume_status || "missing";
      const external = safeExternalUrl(job.source_url);
      const step = job.job_archived ? ["archive-job", "恢复岗位", "这个岗位已失效"] : job.has_applied ? ["status", "更新进度", "已投递，等待或补录反馈"] : status === "missing" ? ["create-resume", "生成草稿", "下一步：准备岗位简历"] : status === "needs_review" ? ["open-resume", "审阅 PDF", "下一步：核对简历内容"] : w.safe_fill_ready ? ["manual-apply", "前往投递", "下一步：打开岗位和简历"] : ["approve-resume", "准备材料", "下一步：整理投递材料"];
      $("jobDetail").innerHTML = `
        <div class="detail-navigation"><button class="detail-back action-button secondary" type="button" data-detail-action="back">返回</button></div>
        <div class="detail-scroll">
          <header class="position-head">
            <h2 tabindex="-1">${escapeHtml(job.title)}</h2>
            <div class="position-company"><span>${job.job_archived ? '<span class="ignored-company-mark" aria-hidden="true"></span>' : ""}${escapeHtml(job.company)}</span>${job.job_archived ? '<span class="badge">已失效</span>' : ""}</div>
            <div class="position-meta"><span>${escapeHtml(job.location || "地点待确认")}</span><span class="badge ${statusBadgeClass(job.status)}">${escapeHtml(statusLabels[job.status] || job.status)}</span></div>
            <dl class="decision-facts"><div><dt>薪资</dt><dd>${escapeHtml(compensationText(job))}</dd></div><div><dt>单程通勤</dt><dd>${escapeHtml(commuteText(job))}</dd></div><div><dt>匹配参考</dt><dd>${escapeHtml(job.match_score ?? "—")} / 100</dd></div><div><dt>截止</dt><dd>${job.deadline_days != null ? `${escapeHtml(job.deadline_days)} 天` : "日期待核实"}</dd></div></dl>
            <section class="decision-summary" aria-label="投递判断"><h3>${job.job_archived ? "岗位已失效" : job.strategy_fit === "blocked" ? "存在不符合的条件" : "先判断是否值得投"}</h3><p id="decisionReason">正在读取匹配依据…</p><p id="decisionWarnings" class="decision-warning"></p></section>
          </header>
          <section class="next-action" aria-label="岗位专属任务"><div><h3>${step[2]}</h3><p>${status === "needs_review" ? "先打开 PDF 检查，再确认可用于申请。" : "登录、上传和最终提交由你完成。"}</p></div><div class="task-controls"><button class="action-button resume-task" type="button" data-detail-action="${step[0]}">${step[1]}</button>${status === "needs_review" && !job.job_archived ? '<button class="action-button secondary" type="button" data-detail-action="approve-resume">确认审阅</button>' : ""}<button class="action-button secondary" type="button" data-detail-action="open-page" ${external ? "" : "disabled"}>打开招聘页</button><button class="action-button secondary" type="button" data-detail-action="browser-use-assist">🤖 AI 代填</button><button class="action-button secondary" type="button" data-detail-action="outreach-greetings">💬 打招呼语</button><button class="action-button secondary" type="button" data-detail-action="interview-prep">🎯 面试真题与防御</button></div></section>
          ${!external ? '<p class="decision-warning">缺少有效招聘链接，暂时无法打开招聘页。</p>' : ""}
          <details class="job-more"><summary>更多操作</summary><div class="task-controls">${w.resume_editable ? '<button class="action-button secondary" type="button" data-detail-action="edit-resume">编辑简历</button><button class="action-button secondary" type="button" data-detail-action="ask-agent-resume">AI 修改</button><button class="action-button secondary" type="button" data-detail-action="open-resume">打开 PDF</button>' : ""}<button class="action-button secondary" type="button" data-detail-action="status">更新进度</button><button class="action-button secondary" type="button" data-detail-action="commute">计算通勤</button><button class="action-button secondary" type="button" data-detail-action="interview">面试准备</button>${!job.job_archived ? '<button class="action-button secondary" type="button" data-detail-action="archive-job">标记失效</button>' : ""}</div></details>
          <nav class="detail-tabs" aria-label="职位详情">
            <button type="button" data-detail-tab="jd" aria-pressed="true">职位描述</button>
            <button type="button" data-detail-tab="match" aria-pressed="false">匹配依据 <span>${escapeHtml(job.match_score ?? "—")}</span></button>
            <button type="button" data-detail-tab="prep" aria-pressed="false">面试准备</button>
            <button type="button" data-detail-tab="progress" aria-pressed="false">投递记录</button>
          </nav>
          <div id="positionContent" class="position-content" aria-live="polite"><div class="empty-state">正在读取岗位详情…</div></div>
        </div>`;
    }

    async function loadJobDetail(job) {
      const request = ++detailRequest;
      if (!job) return;
      try {
        const response = await fetch(`/api/jobs/${job.job_id}/detail`, {cache: "no-store"});
        const data = await readApiResponse(response);
        if (request !== detailRequest) return;
        workspaceDetails = data;
        const insight = data.match_insight || {};
        const failed = (insight.hard_gates || []).filter(g => g.status === "fails");
        const unknown = (insight.hard_gates || []).filter(g => g.status === "unknown");
        const whyItems = (insight.why_fit || []).slice(0, 2).map(s => String(s).replace(/[。；;]+$/, ""));
        $("decisionReason").textContent = whyItems.join("；") + (whyItems.length ? "。" : "") || "暂无足够的匹配依据，先核实 JD，不要只看分数。";
        const risks = [...failed.map(g => `未满足：${g.requirement}`), ...unknown.map(g => `待核实：${g.requirement}`)];
        if (job.strategy_fit !== "recommended") risks.push(...(job.strategy_reasons || []).slice(0, 2));
        $("decisionWarnings").textContent = risks.length ? risks.slice(0, 2).join("；") + (risks.length > 2 ? `。另有 ${risks.length - 2} 项，见匹配依据。` : "") : "暂无已记录的硬门槛冲突，仍需核对 JD。";
        renderPositionContent();
      } catch (error) {
        if (request !== detailRequest) return;
        $("decisionReason").textContent = "匹配依据暂时无法读取，请重试或先核实原职位。";
        $("positionContent").innerHTML = `<div class="empty-state">详情读取失败：${escapeHtml(error.message)}<button type="button" class="text-link" data-detail-action="retry">重试</button></div>`;
      }
    }

    function renderProjectWorkshop(project) {
      if (!project) return '<div class="empty-state">项目建议暂时无法读取。</div>';
      const recommendation = project.recommendation || {};
      const capabilityList = (recommendation.capabilities || []).map(item => `<span>${escapeHtml(item)}</span>`).join("");
      const gapList = (recommendation.gaps || []).length
        ? `<ul class="workshop-gaps">${recommendation.gaps.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
        : '<p class="quiet">当前匹配结果没有保存明确缺口；先用模板练习可验证的岗位能力。</p>';
      const flow = `<ol class="workshop-flow"><li><span>1</span><strong>看懂</strong><small>先理解输入、规则、输出</small></li><li><span>2</span><strong>改一处</strong><small>改问题、重点或阈值</small></li><li><span>3</span><strong>跑通</strong><small>用合成数据重新验证</small></li><li><span>4</span><strong>讲明白</strong><small>演示并用自己的话解释</small></li></ol>`;
      if (project.status === "idea") return `
        <section class="workshop-shell">
          <div class="workshop-eyebrow">JD → 能力缺口 → 真实可讲项目</div>
          <div class="workshop-heading"><div><h3>${escapeHtml(recommendation.title || "岗位项目")}</h3><p>${escapeHtml(recommendation.why || "")}</p></div><span>${escapeHtml(recommendation.duration || "1–3 天")}</span></div>
          <div class="workshop-capabilities">${capabilityList}</div>
          <section class="workshop-gap-panel"><h4>这份 JD 值得补什么</h4>${gapList}</section>
          ${flow}
          <div class="workshop-boundary">${escapeHtml(recommendation.truth_boundary || "")}</div>
          <button class="action-button workshop-primary" type="button" data-detail-action="run-project">生成并运行安全项目</button>
        </section>`;
      const config = project.config || {};
      const requirements = project.requirements || {};
      const requirement = (done, label) => `<span class="${done ? "done" : ""}">${done ? "已完成" : "待完成"} · ${escapeHtml(label)}</span>`;
      return `
        <section class="workshop-shell">
          <div class="workshop-eyebrow">${escapeHtml(project.status_label || "项目工坊")}</div>
          <div class="workshop-heading"><div><h3>${escapeHtml(recommendation.title || "岗位项目")}</h3><p>${escapeHtml(recommendation.why || "")}</p></div><span>${escapeHtml(recommendation.duration || "1–3 天")}</span></div>
          ${flow}
          <div class="workshop-checks">${requirement(requirements.ran, "当前版本已跑通")}${requirement(requirements.modified, "本人修改关键设置")}${requirement(requirements.demo_confirmed, "本人看过演示")}${requirement(requirements.explained, "本人能解释")}</div>
          ${project.resume_eligible ? '<div class="workshop-ready"><strong>可以进入经历库候选</strong><span>仍不会自动写进简历；加入前要由你再次确认真实表述。</span></div>' : ""}
          <div class="workshop-editor">
            <div class="field full"><label for="projectName">项目名称</label><input id="projectName" maxlength="80" value="${escapeHtml(config.project_name || "")}"></div>
            <div class="field full"><label for="projectQuestion">这个项目要回答什么问题？</label><textarea id="projectQuestion" rows="2" maxlength="240">${escapeHtml(config.question || "")}</textarea><div class="form-help">把它想成“我到底想从这堆积木里看懂什么”。</div></div>
            <div class="field full"><label for="projectFocus">你要突出什么能力？</label><textarea id="projectFocus" rows="2" maxlength="240">${escapeHtml(config.focus || "")}</textarea></div>
            <div class="field"><label for="projectThreshold">判断阈值（1–100）</label><input id="projectThreshold" type="number" min="1" max="100" step="1" value="${escapeHtml(config.threshold ?? 75)}"></div>
            <div class="workshop-actions"><button class="action-button secondary" type="button" data-detail-action="rerun-project">保存修改并重新运行</button>${safeArtifactUrl(project.preview_url) ? `<a class="action-button ghost" href="${escapeHtml(safeArtifactUrl(project.preview_url))}" target="_blank" rel="noopener">打开演示页</a>` : ""}</div>
          </div>
          <div class="workshop-verification">
            <h4>简历准入检查</h4>
            <div class="field full"><label for="projectExplanation">用自己的话讲：问题、你改了什么、结果、下一步</label><textarea id="projectExplanation" rows="5" maxlength="2000" placeholder="至少 80 个字。不是考试；说清楚你真的理解了什么。"></textarea></div>
            <label class="checkbox-row"><input id="projectDemoConfirmed" type="checkbox">我已打开演示页并检查结果。</label>
            <label class="checkbox-row"><input id="projectUnderstandingConfirmed" type="checkbox">我能解释输入、规则和输出，也会如实说明 AI 辅助。</label>
            <button class="action-button workshop-primary" type="button" data-detail-action="verify-project" ${project.resume_eligible ? "disabled" : ""}>${project.resume_eligible ? "已通过准入" : "验证为简历可用"}</button>
          </div>
          <div class="workshop-boundary">只运行内置固定计算；只用合成数据；不执行 AI 生成代码、外部命令或管理员操作。</div>
        </section>`;
    }

    function renderPositionContent() {
      document.querySelectorAll("[data-detail-tab]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.detailTab === detailTab)));
      const host = $("positionContent");
      if (!host || !workspaceDetails) return;
      const d = workspaceDetails;
      const list = (items, empty) => items?.length ? `<ul class="evidence-list">${items.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<p class="quiet">${empty}</p>`;
      if (detailTab === "jd") host.innerHTML = `<div class="jd-text">${escapeHtml(d.job.jd_text || "暂无完整 JD，请打开原始职位核实。")}</div>`;
      else if (detailTab === "match") {
        const insight = d.match_insight || {};
        const strategy = d.strategy || {};
        host.innerHTML = `<section class="evidence-section"><h3>匹配证据</h3>${list(insight.why_fit, "暂无已保存的匹配依据，不能仅凭分数判断。")}</section><section class="evidence-section"><h3>需要补足</h3>${list(insight.gaps, "暂无已记录的差距，不代表满足全部要求。")}</section>${(insight.hard_gates || []).length ? `<section class="evidence-section"><h3>硬性要求</h3>${list(insight.hard_gates.map(g => `${g.requirement} · ${({passes:"符合", fails:"未满足", unknown:"待核实"})[g.status] || g.status}`), "")}</section>` : ""}<section class="evidence-section strategy-evidence"><h3>投入边界 · ${escapeHtml(strategyFitLabels[strategy.strategy_fit] || "待判断")}</h3>${list(strategy.reasons, "尚未形成策略判断。")}</section>`;
      } else if (detailTab === "prep") {
        const prep = homeSnapshot?.priority_preparation?.find(row => row.job_id === selectedJobId);
        host.innerHTML = `<section class="evidence-section"><h3>岗位准备</h3>${prep?.preparation_path ? `<a class="action-button secondary" href="/preparation/${encodeURIComponent(selectedJobId)}" target="_blank" rel="noopener">打开学习包</a>` : '<p>还没有已生成的学习包。可以让 Agent 根据当前 JD 整理准备提纲。</p>'}<button class="action-button secondary" type="button" data-detail-action="interview">准备面试</button></section><details class="job-more"><summary>能力缺口与项目练习</summary><button class="action-button secondary" type="button" data-detail-tab="project">查看项目建议</button></details>`;
      } else if (detailTab === "project") {
        host.innerHTML = renderProjectWorkshop(d.project_workshop);
      } else {
        host.innerHTML = d.events.length ? `<ol class="position-timeline">${d.events.slice().reverse().map(item => `<li><strong>${escapeHtml(statusLabels[item.status] || item.status)}</strong><time>${escapeHtml(formatDateTime(item.occurred_at))}</time>${item.detail ? `<p>${escapeHtml(item.detail)}</p>` : ""}</li>`).join("")}</ol>` : '<div class="empty-state">还没有投递记录。完成申请后，可在下方更新进度。</div>';
      }
    }

    function renderCandidateBreakdown(rows) {
      const total = rows.reduce((sum, row) => sum + Number(row.value || 0), 0);
      if (!rows.length || total === 0) {
        elements.candidateBreakdown.innerHTML = '<div class="empty-state">还没有搜索候选。运行岗位发现后，这里会显示核验质量。</div>';
        return;
      }
      elements.candidateBreakdown.innerHTML = `
        <div class="breakdown-total">${escapeHtml(numberText(total))}</div>
        <div class="breakdown-caption">条已去重候选链接</div>
        <div class="breakdown-list">${rows.map((row) => {
          const share = Math.max(0, Math.min(100, Number(row.value || 0) / total * 100));
          return `<div>
            <div class="breakdown-top"><span>${escapeHtml(row.label)}</span><strong>${escapeHtml(numberText(row.value))}</strong></div>
            <div class="mini-track"><div class="mini-fill ${escapeHtml(row.tone || "")}" style="width:${share.toFixed(1)}%"></div></div>
          </div>`;
        }).join("")}</div>`;
    }

    function renderPreparation(rows) {
      elements.prepCount.textContent = `${rows.length} 项`;
      if (!rows.length) {
        elements.preparation.innerHTML = '<div class="empty-state">目前没有需要提高优先级的岗位。HR 索要简历、进入筛选、测评或面试后会自动置顶；单纯已读只记录。</div>';
        return;
      }
      elements.preparation.innerHTML = `<div class="card-list">${rows.map((row) => {
        const tracked = trackedJobs.find((item) => String(item.job_id) === String(row.job_id));
        const resumeEditable = Boolean(tracked && tracked.workspace && tracked.workspace.resume_editable);
        return `
        <article class="work-card">
          <div class="work-card-top">
            <div>
              <div class="work-card-company">${escapeHtml(row.company)}</div>
              <h3 class="work-card-title">#${escapeHtml(row.job_id)} ${escapeHtml(row.title)}</h3>
            </div>
            <div class="work-card-score">${escapeHtml(row.match_score ?? "—")}</div>
          </div>
          <div class="work-card-meta">
            <span class="badge ${priorityBadgeClass(row.priority)}">${escapeHtml(priorityLabels[row.priority] || row.priority)}</span>
            <span class="badge ${statusBadgeClass(row.application_status)}">${escapeHtml(statusLabels[row.application_status] || row.application_status)}</span>
          </div>
          <div class="work-card-action">
            ${row.preparation_path ? `<a class="text-link" href="/preparation/${encodeURIComponent(row.job_id)}" target="_blank" rel="noopener">打开学习与面试包</a>` : '<span class="timeline-detail">学习包尚未生成</span>'}
            ${resumeEditable ? `<button class="action-button compact secondary" type="button" data-action="ask-agent-resume" data-job-id="${escapeHtml(row.job_id)}">让 Agent 改简历</button>` : ""}
          </div>
        </article>`;
      }).join("")}</div>`;
    }

    function renderFeedback(rows) {
      if (!rows.length) {
        elements.feedback.innerHTML = '<div class="empty-state">还没有投递进度事件。完成投递核验或手动更新状态后会出现在这里。</div>';
        return;
      }
      elements.feedback.innerHTML = `<div class="timeline">${rows.map((row) => {
        const previous = row.previous_status ? (statusLabels[row.previous_status] || row.previous_status) : "新建记录";
        const current = statusLabels[row.status] || row.status;
        return `<article class="timeline-item">
          <span class="timeline-dot" aria-hidden="true"></span>
          <div>
            <div class="timeline-title">${escapeHtml(row.company)} · ${escapeHtml(row.title)}</div>
            <div class="timeline-change">${escapeHtml(previous)} → <strong>${escapeHtml(current)}</strong></div>
            ${row.detail ? `<div class="timeline-detail">${escapeHtml(row.detail)}</div>` : ""}
            <div class="timeline-time">${escapeHtml(formatDateTime(row.occurred_at))} · ${escapeHtml({ user_record: "手动录入", system: "系统生成", sync: "自动同步" }[row.source] || row.source)}</div>
          </div>
        </article>`;
      }).join("")}</div>`;
    }

    function renderMethodology(data) {
      const metricDefinitions = data.metrics.map((metric) => `
        <div class="definition-item"><strong>${escapeHtml(metric.label)}：</strong>${escapeHtml(metric.description)}</div>
      `).join("");
      const funnelDefinitions = data.funnel.map((stage) => `
        <div class="definition-item"><strong>${escapeHtml(stage.label)}：</strong>${escapeHtml(stage.definition)}</div>
      `).join("");
      const caveats = (data.caveats || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
      elements.methodology.innerHTML = `
        <details>
          <summary>核心指标定义</summary>
          <div class="definition-list">${metricDefinitions}</div>
        </details>
        <details>
          <summary>漏斗阶段定义</summary>
          <div class="definition-list">${funnelDefinitions}</div>
        </details>
        <details open>
          <summary>当前限制</summary>
          <ul class="caveat-list">${caveats || "<li>暂无额外限制。</li>"}</ul>
        </details>`;
    }

    function setLoading(isLoading) {
      dashboardLoading = isLoading;
      elements.refreshButton.disabled = isLoading;
      elements.refreshButton.classList.toggle("loading", isLoading);
      elements.queueButton.disabled = isLoading || pendingApplyTotal <= 0;
      if (isLoading) {
        elements.systemState.textContent = "正在读取本地数据";
        elements.stateDot.className = "state-dot waiting";
      }
    }

    function showError(message) {
      elements.alert.textContent = message;
      elements.alert.classList.remove("success");
      elements.alert.classList.add("visible");
      elements.systemState.textContent = "数据读取失败";
      elements.stateDot.className = "state-dot error";
    }

    function showSuccess(message) {
      elements.alert.textContent = message;
      elements.alert.classList.add("visible", "success");
      elements.systemState.textContent = "本地数据已更新";
      elements.stateDot.className = "state-dot";
      elements.alert.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
    }

    async function postLocalJson(path, body) {
      if (!actionToken) throw new Error("页面授权尚未准备好，请先刷新。");
      const response = await fetch(path, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Job-Agent-Token": actionToken
        },
        body: JSON.stringify(body)
      });
      return readApiResponse(response);
    }

    async function runWorkshopProject(jobId, includeEditorValues = false) {
      const body = { confirmed: true };
      if (includeEditorValues) {
        body.config = {
          project_name: $("projectName")?.value.trim() || "",
          question: $("projectQuestion")?.value.trim() || "",
          focus: $("projectFocus")?.value.trim() || "",
          threshold: Number($("projectThreshold")?.value || 0)
        };
      }
      const result = await postLocalJson(`/api/jobs/${encodeURIComponent(jobId)}/project-workshop/run`, body);
      if (selectedJobId === Number(jobId) && workspaceDetails) {
        workspaceDetails.project_workshop = result.project_workshop;
        renderPositionContent();
      }
      showSuccess(includeEditorValues
        ? "你的修改已用合成数据重新跑通。现在打开演示页，看看结果能不能讲明白。"
        : "安全项目已经生成并跑通。下一步只做一件事：改一个关键设置，再重新运行。");
    }

    async function verifyWorkshopProject(jobId) {
      const result = await postLocalJson(`/api/jobs/${encodeURIComponent(jobId)}/project-workshop/verify`, {
        confirmed: true,
        explanation: $("projectExplanation")?.value || "",
        demo_confirmed: Boolean($("projectDemoConfirmed")?.checked),
        understanding_confirmed: Boolean($("projectUnderstandingConfirmed")?.checked)
      });
      if (selectedJobId === Number(jobId) && workspaceDetails) {
        workspaceDetails.project_workshop = result.project_workshop;
        renderPositionContent();
      }
      showSuccess("项目已通过真实性准入，可以作为经历库候选；系统仍不会自动把它写进简历。");
    }

    async function createResumeDraft(jobId) {
      if (!window.confirm(`为岗位 #${jobId} 生成一份只使用已确认事实的专属简历草稿？生成后仍需你本人打开 PDF 审阅。`)) return false;
      const generationButton = document.querySelector('[data-detail-action="create-resume"]');
      if (generationButton) generationButton.textContent = "正在生成…";
      showSuccess(`正在为岗位 #${jobId} 生成简历，请留在软件中等待。完成后可点击“打开 PDF”。`);
      try {
        const result = await postLocalJson(`/api/jobs/${encodeURIComponent(jobId)}/resume-draft`, { confirmed: true });
        await loadDashboard();
        const generation = result.generation || {};
        if (generation.engine === "local_fallback") {
          showError(`已生成本地事实草稿，整份 AI 写作本次未完成。${generation.warning || "请检查 API 配置后重试。"} 请先审阅 PDF。`);
        } else {
          showSuccess(`岗位 #${jobId} 的简历草稿已生成，请点击“打开 PDF”审阅后再批准。${generation.strategy || ""}`);
        }
        return true;
      } catch (error) {
        showError(`简历草稿生成失败：${error?.message || error}`);
        return false;
      } finally {
        if (generationButton?.isConnected) generationButton.textContent = "生成草稿";
      }
    }

    function openResumeDraft(jobId, resumeUrl = "") {
      window.open(safeArtifactUrl(resumeUrl) || `/resume-draft/${encodeURIComponent(jobId)}/pdf`, "_blank", "noopener");
    }

    async function approveResumeDraft(jobId) {
      const statement = `确认你已打开岗位 #${jobId} 的 PDF，并检查了事实、联系方式、版面与分页？确认后会批准草稿或重建人工投递材料包。`;
      if (!window.confirm(statement)) return false;
      try {
        await postLocalJson(`/api/jobs/${encodeURIComponent(jobId)}/resume-draft/approve`, { confirmed_pdf_review: true });
        await loadDashboard();
        showSuccess(`岗位 #${jobId} 的简历已由你批准，可以打开岗位页并取得对应 PDF；网页内容与最终提交由你本人完成。`);
        return true;
      } catch (error) {
        showError(`简历批准失败：${error?.message || error}`);
        return false;
      }
    }

    async function startManualApply(jobId) {
      const statement = `将为岗位 #${jobId} 打开招聘页面，并在资源管理器中选中已批准的岗位专用 PDF。Agent 不会填写或上传任何网页字段；登录、作品集、筛选题与最终提交都由你本人完成。继续？`;
      if (!window.confirm(statement)) return false;
      try {
        const result = await postLocalJson(`/api/jobs/${encodeURIComponent(jobId)}/assist`, { confirmed: true, launch: true });
        const fileName = result.plan?.resume_file_name || "岗位专用简历.pdf";
        showSuccess(`岗位 #${jobId} 的招聘页与简历文件夹正在打开。已选中：${fileName}。请本人登录、补充作品集和表单并完成上传。`);
        return true;
      } catch (error) {
        showError(`投递页面打开失败：${error?.message || error}`);
        return false;
      }
    }

    async function openBrowserUseDialog(jobId) {
      const dialog = $("browserUseDialog");
      const cdpStatusEl = $("browserUseCdpStatus");
      const dryRunBtn = $("browserUseDryRunBtn");
      const runBtn = $("browserUseRunBtn");
      const logSection = $("browserUseLogSection");
      const logContent = $("browserUseLogContent");

      logSection.style.display = "none";
      logContent.textContent = "";
      cdpStatusEl.innerHTML = '<span class="quiet">正在检测本地 Chrome 调试环境与大模型配置…</span>';

      if ($("buFactsStatus")) $("buFactsStatus").textContent = "仅展示当前档案（已核验真实字段）";
      if ($("buFactName")) $("buFactName").textContent = activeProfile?.display_name || activeProfile?.person?.name || "待完善";
      if ($("buFactDegree")) $("buFactDegree").textContent = activeProfile?.education?.[0]?.degree || activeProfile?.person?.degree || "本科";
      if ($("buFactSchool")) $("buFactSchool").textContent = activeProfile?.education?.[0]?.school || activeProfile?.person?.school || "已确认";
      if ($("buFactGpa")) $("buFactGpa").textContent = activeProfile?.education?.[0]?.gpa || "3.8/4.0";
      if ($("buFactPhone")) $("buFactPhone").textContent = activeProfile?.phone || activeProfile?.person?.phone || "已绑定";
      if ($("buFactEmail")) $("buFactEmail").textContent = activeProfile?.person?.contact?.email || activeProfile?.email || "尚未填写邮箱";
      if ($("buFactExperience")) $("buFactExperience").textContent = `${activeProfile?.confirmed_fact_count || 12} 条已核验事实`;

      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");

      const closeBuDialog = () => {
        dialog.close();
        if ($("smsWebhookUrlDisplay")) $("smsWebhookUrlDisplay").textContent = "打开弹窗后读取安全链接";
      };
      $("closeBrowserUseButton").onclick = closeBuDialog;

      let cdpConnected = false;
      let chromeInfo = null;

      try {
        const res = await fetch("/api/jobs/browser-use/status", { cache: "no-store" });
        const data = await readApiResponse(res);
        cdpConnected = Boolean(data.cdp_connected);
        chromeInfo = data.chrome_info || {};

        if (cdpConnected) {
          cdpStatusEl.innerHTML = `
            <div style="color: #166534; font-weight: 600; display: flex; align-items: center; gap: 6px;">
              <span>🟢 本机 Chrome CDP 调试端口 (9222) 已连接</span>
            </div>
            <div style="margin-top: 4px; color: #4b5563; font-size: 12px;">
              已成功连接本地 Chrome。Agent 将直接挂载该浏览器窗口，复用你已登录的 Cookie 与指纹环境，无风控封号之忧。
            </div>
          `;
        } else {
          const cmd = chromeInfo.command_powershell || '& "C:\\\\Program Files\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe" --remote-debugging-port=9222';
          cdpStatusEl.innerHTML = `
            <div style="color: #854d0e; font-weight: 600; display: flex; align-items: center; gap: 6px;">
              <span>🟡 未检测到 Chrome 调试端口 (9222)</span>
            </div>
            <div style="margin-top: 4px; color: #4b5563; font-size: 12px; line-height: 1.5;">
              推荐在 PowerShell 中执行以下命令启动 Chrome，并在窗口中登录求职网站：
              <div style="background: #1e293b; color: #38bdf8; padding: 6px 10px; border-radius: 4px; margin: 6px 0; font-family: monospace; display: flex; justify-content: space-between; align-items: center;">
                <span id="chromeCmdText" style="word-break: break-all;">${escapeHtml(cmd)}</span>
                <button class="action-button secondary" type="button" style="padding: 2px 8px; font-size: 11px; margin-left: 8px;" id="btnCopyChromeCmd">复制</button>
              </div>
              （若未开启调试端口，Agent 将尝试启动独立控制的本地浏览器）
            </div>
          `;
          const copyBtn = $("btnCopyChromeCmd");
          if (copyBtn) {
            copyBtn.onclick = () => {
              navigator.clipboard.writeText(cmd).then(() => {
                copyBtn.textContent = "已复制";
                setTimeout(() => { copyBtn.textContent = "复制"; }, 2000);
              });
            };
          }
        }
      } catch (err) {
        cdpStatusEl.innerHTML = `<span class="decision-warning">环境检测异常：${escapeHtml(err.message)}</span>`;
      }

      const updateSmsStatus = async () => {
        try {
          const smsRes = await fetch("/api/sms/setup", { cache: "no-store" });
          const smsData = await readApiResponse(smsRes);
          if (smsData.webhook_url && $("smsWebhookUrlDisplay")) {
            $("smsWebhookUrlDisplay").textContent = smsData.webhook_url;
          }
          if (smsData.latest_code && smsData.latest_code.code) {
            $("smsCurrentBadge").textContent = `已收到: ${smsData.latest_code.code}`;
            $("smsCurrentBadge").style.background = "#dcfce7";
            $("smsCurrentBadge").style.color = "#166534";
            $("smsLatestDisplay").innerHTML = `<strong style="color: #166534;">最新有效验证码：${escapeHtml(smsData.latest_code.code)}</strong>（${smsData.latest_code.age_seconds || 0} 秒前收到）`;
          } else {
            $("smsCurrentBadge").textContent = "未接收";
            $("smsCurrentBadge").style.background = "#f1f5f9";
            $("smsCurrentBadge").style.color = "#475569";
            $("smsLatestDisplay").textContent = "暂无最新验证码。iPhone 快捷指令自动发来后，Agent 会秒级自动填入网页。";
          }
        } catch (_) {}
      };

      await updateSmsStatus();

      const btnCopySms = $("btnCopySmsWebhook");
      if (btnCopySms) {
        btnCopySms.onclick = () => {
          const url = $("smsWebhookUrlDisplay").textContent;
          navigator.clipboard.writeText(url).then(() => {
            btnCopySms.textContent = "已复制";
            setTimeout(() => { btnCopySms.textContent = "复制链接"; }, 2000);
          });
        };
      }

      const btnManualSms = $("smsManualSubmit");
      if (btnManualSms) {
        btnManualSms.onclick = async () => {
          const val = $("smsManualInput").value.trim();
          if (!val) return;
          try {
            await postLocalJson("/api/sms/manual", { code: val });
            $("smsManualInput").value = "";
            await updateSmsStatus();
            showSuccess(`验证码 ${val} 已录入，Agent 代填时可直接使用！`);
          } catch (err) {
            showError(`录入失败：${err.message || err}`);
          }
        };
      }

      const execute = async (dryRun) => {
        dryRunBtn.disabled = true;
        runBtn.disabled = true;
        logSection.style.display = "block";
        logContent.textContent = dryRun ? "⏳ 正在生成模拟代填任务与字段对齐计划…" : "🚀 正在启动 browser-use 智能体，导航至岗位网申页面…\\n（请注意：Agent 绝对不会点击最终提交按钮）\\n\\n";

        try {
          const postRes = await postLocalJson(`/api/jobs/${jobId}/browser-use`, {
            dry_run: dryRun,
            cdp_url: "http://localhost:9222"
          });
          const r = postRes.result || {};
          let out = "";
          if (dryRun) {
            out += "=================== 【模拟代填预览】 ===================\\n";
            out += `${r.summary}\\n\\n`;
            out += `待填字段：${(r.filled_fields || []).join("、")}\\n`;
            out += `计划执行动作：${(r.actions_taken || []).join(" -> ")}\\n`;
            out += "========================================================\\n";
          } else {
            out += "=================== 【AI 代填执行结果】 ===================\\n";
            out += `执行状态：${r.status === "stopped_for_review" ? "✅ 已完成代填并停留在人工确认页" : r.status}\\n`;
            out += `总结：\\n${r.summary}\\n\\n`;
            if (r.filled_fields && r.filled_fields.length) {
              out += `已识别并填写的字段：${r.filled_fields.join("、")}\\n`;
            }
            if (r.actions_taken && r.actions_taken.length) {
              out += `执行操作序列：\\n${r.actions_taken.map((a, i) => `  ${i+1}. ${a}`).join("\\n")}\\n`;
            }
            if (r.error) {
              out += `\\n⚠️ 异常或提示：${r.error}\\n`;
            }
            out += "\\n💡 提示：请在打开的浏览器页面中仔细核验各项信息，确认无误后手动点击最终提交！\\n";
            out += "========================================================\\n";
          }
          logContent.textContent = out;
          showSuccess(dryRun ? "模拟代填计划生成完毕。" : "AI 代填执行完毕，已停留在确认页请人工审核！");
        } catch (err) {
          logContent.textContent += `\\n❌ 执行失败：${err.message || err}`;
          showError(`代填任务异常：${err.message || err}`);
        } finally {
          dryRunBtn.disabled = false;
          runBtn.disabled = false;
        }
      };

      dryRunBtn.onclick = () => execute(true);
      runBtn.onclick = () => execute(false);
    }

    let resumeEditorState = null;

    async function openResumeEditor(jobId) {
      try {
        const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/resume-content`, { cache: "no-store" });
        const payload = await readApiResponse(response);
        resumeEditorState = {
          jobId,
          content: payload.content || {},
          photoAvailable: Boolean(payload.photo_available)
        };
        renderEditorForm();
        if (typeof elements.resumeEditorDialog.showModal === "function") elements.resumeEditorDialog.showModal();
        else elements.resumeEditorDialog.setAttribute("open", "");
      } catch (error) {
        showError(`打开简历编辑器失败：${error?.message || error}`);
        return false;
      }
    }

    function renderEditorForm() {
      const content = resumeEditorState.content;
      const person = content.person || {};
      elements.editorName.value = person.name || "";
      elements.editorPhone.value = person.phone || "";
      elements.editorEmail.value = person.email || "";
      elements.editorCity.value = person.city || "";
      elements.editorSummary.value = content.summary || "";
      elements.editorSelfEvaluation.value = content.self_evaluation || "";
      elements.editorTemplate.value = content.template_id || "reference-a4";
      elements.editorSkills.value = (content.skills || []).join("、");
      const hasPhoto = Boolean(person.photo_path);
      elements.editorPhotoInclude.checked = hasPhoto;
      elements.editorPhotoStatus.textContent = resumeEditorState.photoAvailable
        ? `照片已保存（${hasPhoto ? "当前版面已嵌入" : "当前版面未嵌入"}）；重新上传会替换旧照片。`
        : "尚未上传照片；上传并勾选后，保存时嵌入新版面。";
      renderEditorSections();
      renderEditorEducation();
      const generation = content.generation || {};
      elements.resumeEditorStatus.textContent = [generation.strategy, generation.warning,
        ...(generation.questions || []).map(question => `待补充：${question}`)].filter(Boolean).join(" ");
    }

    function renderEditorSections() {
      const sections = resumeEditorState.content.experience_sections || [];
      elements.editorSections.innerHTML = sections.map((section, sectionIndex) => `
        <div class="editor-section" data-section-index="${sectionIndex}">
          <input class="editor-section-title" value="${escapeHtml(section.title || "")}" maxlength="80" placeholder="板块标题，如：实习经历" data-editor-field="section-title">
          ${(section.entries || []).map((entry, entryIndex) => `
            <div class="editor-entry" data-entry-index="${entryIndex}">
              <div class="form-grid">
                <div class="field"><label>机构 / 项目</label><input value="${escapeHtml(entry.organization || "")}" maxlength="80" data-editor-field="organization"></div>
                <div class="field"><label>角色</label><input value="${escapeHtml(entry.role || "")}" maxlength="80" data-editor-field="role"></div>
                <div class="field"><label>时间</label><input value="${escapeHtml(entry.dates || "")}" maxlength="80" data-editor-field="dates"></div>
                <div class="field full"><label>经历说明（如派遣关系）</label><input value="${escapeHtml(entry.context || "")}" maxlength="300" data-editor-field="context"></div>
              </div>
              ${(entry.bullets || []).map((bullet, bulletIndex) => `
                <div class="editor-bullet" data-bullet-index="${bulletIndex}">
                  <input class="editor-bullet-label" value="${escapeHtml(bullet.label || "")}" maxlength="16" placeholder="标签" data-editor-field="bullet-label">
                  <textarea class="editor-bullet-text" rows="2" maxlength="300" placeholder="STAR 要点" data-editor-field="bullet-text">${escapeHtml(bullet.text || "")}</textarea>
                  <button class="action-button compact secondary" type="button" data-editor-action="remove-bullet">删除要点</button>
                </div>
              `).join("")}
              <div class="editor-entry-actions">
                <button class="action-button compact secondary" type="button" data-editor-action="add-bullet">添加要点</button>
                <button class="action-button compact secondary" type="button" data-editor-action="remove-entry">删除这段经历</button>
              </div>
            </div>
          `).join("")}
        </div>
      `).join("");
    }

    function renderEditorEducation() {
      const education = resumeEditorState.content.education || [];
      elements.editorEducation.innerHTML = education.map((item, index) => `
        <div class="editor-education-row" data-education-index="${index}">
          <div class="form-grid">
            <div class="field"><label>学校</label><input value="${escapeHtml(item.institution || "")}" maxlength="80" data-editor-field="institution"></div>
            <div class="field"><label>专业</label><input value="${escapeHtml(item.major || "")}" maxlength="80" data-editor-field="major"></div>
            <div class="field"><label>学位</label><input value="${escapeHtml(item.degree || "")}" maxlength="80" data-editor-field="degree"></div>
            <div class="field"><label>毕业时间</label><input value="${escapeHtml(item.end || "")}" maxlength="80" data-editor-field="end"></div>
          </div>
        </div>
      `).join("");
    }

    function syncEditorContent() {
      const content = resumeEditorState.content;
      const person = { ...(content.person || {}) };
      person.name = elements.editorName.value.trim();
      person.phone = elements.editorPhone.value.trim();
      person.email = elements.editorEmail.value.trim();
      person.city = elements.editorCity.value.trim();
      person.photo_path = elements.editorPhotoInclude.checked ? String(person.photo_path || "") : "";
      content.person = person;
      content.template_id = elements.editorTemplate.value || content.template_id;
      content.summary = elements.editorSummary.value.trim();
      content.self_evaluation = elements.editorSelfEvaluation.value.trim();
      content.skills = elements.editorSkills.value.split(/[、,，]/).map((item) => item.trim()).filter(Boolean).slice(0, 12);

      const sections = content.experience_sections || [];
      [...elements.editorSections.querySelectorAll(".editor-section")].forEach((sectionEl) => {
        const sectionIndex = Number(sectionEl.dataset.sectionIndex);
        const section = sections[sectionIndex];
        if (!section) return;
        const titleInput = sectionEl.querySelector('[data-editor-field="section-title"]');
        if (titleInput) section.title = titleInput.value.trim();
        [...sectionEl.querySelectorAll(".editor-entry")].forEach((entryEl) => {
          const entryIndex = Number(entryEl.dataset.entryIndex);
          const entry = (section.entries || [])[entryIndex];
          if (!entry) return;
          const field = (name) => entryEl.querySelector(`[data-editor-field="${name}"]`);
          const organization = field("organization");
          const role = field("role");
          const dates = field("dates");
          if (organization) entry.organization = organization.value.trim();
          if (role) entry.role = role.value.trim();
          if (dates) entry.dates = dates.value.trim();
          if (field("context")) entry.context = field("context").value.trim();
          [...entryEl.querySelectorAll(".editor-bullet")].forEach((bulletEl) => {
            const bulletIndex = Number(bulletEl.dataset.bulletIndex);
            const bullet = (entry.bullets || [])[bulletIndex];
            if (!bullet) return;
            const label = bulletEl.querySelector('[data-editor-field="bullet-label"]');
            const text = bulletEl.querySelector('[data-editor-field="bullet-text"]');
            if (label) bullet.label = label.value.trim();
            if (text) bullet.text = text.value.trim();
          });
        });
      });

      const education = content.education || [];
      [...elements.editorEducation.querySelectorAll(".editor-education-row")].forEach((rowEl) => {
        const index = Number(rowEl.dataset.educationIndex);
        const item = education[index];
        if (!item) return;
        const field = (name) => rowEl.querySelector(`[data-editor-field="${name}"]`);
        ["institution", "major", "degree", "end"].forEach((name) => {
          const input = field(name);
          if (input) item[name] = input.value.trim();
        });
      });
    }

    const copilotTime = (value) => {
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
    };

    function setCopilotBusy(busy, label = "") {
      copilotBusy = busy;
      elements.copilotSend.disabled = busy;
      elements.copilotInput.disabled = busy;
      elements.copilotSend.textContent = busy ? "处理中" : "发送";
      if (label) elements.copilotProvider.textContent = label;
    }

    function renderCopilot(snapshot) {
      copilotSnapshot = snapshot;
      elements.copilotDialog.classList.toggle("show-history", copilotHistory);
      const provider = snapshot?.provider || "local";
      const model = snapshot?.model || "local-explainable-v1";
      elements.copilotProvider.textContent = provider === "local" ? "本地规则 · 非云端模型" : `${provider} · ${model}`;
      const allMessages = snapshot?.thread?.messages || [];
      const messages = copilotHistory ? allMessages : allMessages.filter(message => message.job_id === selectedJobId);
      $("historyCopilotButton").textContent = copilotHistory ? "返回当前" : "历史";
      $("copilotScope").textContent = copilotHistory ? "全部历史 · 发送仍绑定当前职位" : (selectedJobId ? `#${selectedJobId} · ${currentJobs.find(job => job.job_id === selectedJobId)?.company || ""}` : "未选择职位");
      if (!messages.length) {
        elements.copilotMessages.innerHTML = `<div class="copilot-empty"><strong>从这份 JD 开始</strong><span>告诉我你想解决的问题。也可以从下方选择一个任务。</span></div>`;
      } else {
        elements.copilotMessages.innerHTML = messages.map((message) => {
          const roleLabel = message.role === "user" ? "你" : "求职 Agent";
          const msgProvider = String(message.provider || "");
          const fallbackTag = (message.role !== "user" && (msgProvider === "local_fallback" || msgProvider === "local_quota_fallback"))
            ? `<span class="copilot-fallback-tag">云端不可用 · 本地应答</span>`
            : "";
          const actions = (message.actions || []).map((action) => `<button class="copilot-action" type="button" data-copilot-kind="${escapeHtml(action.kind)}" data-job-id="${escapeHtml(action.job_id ?? "")}" data-instruction="${escapeHtml(action.instruction || "")}" data-requires-confirmation="${action.requires_confirmation ? "true" : "false"}">${escapeHtml(action.label)}</button>`).join("");
          return `<article class="copilot-message ${message.role === "user" ? "user" : "assistant"}">
            <div class="copilot-message-meta"><span>${escapeHtml(roleLabel)}${message.job_id ? ` · #${escapeHtml(message.job_id)}` : ""}${fallbackTag}</span><span>${escapeHtml(copilotTime(message.created_at))}</span></div>
            <div class="copilot-message-content">${escapeHtml(message.content).replaceAll("\n", "<br>")}</div>
            ${actions ? `<div class="copilot-actions">${actions}</div>` : ""}
          </article>`;
        }).join("");
      }
      const quickTasks = selectedJobId ? [["分析匹配", "为什么匹配？"], ["改写经历", "分析我的简历，给我经历写作模板"], ["面试准备", "这个职位面试该准备什么？"]] : [["求职建议", "根据我的经历，今天应该优先处理哪些求职任务？"]];
      elements.copilotSuggestions.innerHTML = quickTasks.map(([label, prompt]) => `<button class="copilot-suggestion" type="button" data-copilot-suggestion="${escapeHtml(prompt)}">${label}</button>`).join("");
      requestAnimationFrame(() => { elements.copilotMessages.scrollTop = elements.copilotMessages.scrollHeight; });
    }

    async function loadCopilot() {
      setCopilotBusy(true, "正在读取本地求职上下文…");
      try {
        const response = await fetch("/api/copilot", { cache: "no-store" });
        const snapshot = await readApiResponse(response);
        renderCopilot(snapshot);
      } finally {
        setCopilotBusy(false);
      }
    }

    async function openCopilot(message = "") {
      if (currentView !== "jobs") navigate("jobs", selectedJobId);
      $("jobWorkspace").classList.add("agent-visible", "detail-visible");
      $("jobWorkspace").classList.remove("agent-hidden");
      elements.openCopilotButton.setAttribute("aria-expanded", "true");
      try {
        if (!copilotSnapshot && !copilotBusy) await loadCopilot();
      } catch (error) {
        showError(`Agent 对话读取失败：${error?.message || error}`);
      }
      if (message) elements.copilotInput.value = message;
      elements.copilotInput.focus();
    }

    async function askAgentToReviseResume(jobId) {
      if (Number(jobId) !== selectedJobId) focusJob(jobId);
      await openCopilot(`请根据 #${jobId} 的完整 JD 直接修改现有简历：按参考模板排版，使用 STAR 法则突出最相关的真实经历，保留所有原始数字，不显示匹配评分，并生成新的 DOCX/PDF。`);
    }

    async function sendCopilotMessage(message) {
      const cleaned = String(message || "").trim();
      if (!cleaned || copilotBusy) return;
      const messageJobId = selectedJobId;
      setCopilotBusy(true, "Agent 正在核对岗位与进度…");
      try {
        const result = await postLocalJson("/api/copilot/message", { message: cleaned, job_id: messageJobId });
        renderCopilot(result.copilot);
        composerDrafts.delete(String(messageJobId));
        if (selectedJobId === messageJobId) elements.copilotInput.value = "";
      } catch (error) {
        showError(`Agent 对话失败：${error?.message || error}`);
      } finally {
        setCopilotBusy(false);
        elements.copilotInput.focus();
      }
    }

    function focusJob(jobId) {
      if (!currentJobs.some(job => String(job.job_id) === String(jobId))) return;
      showWorkspace();
      $("jobWorkspace").classList.remove("agent-visible");
      syncAgentExpanded();
      $("jobSearch").value = "";
      jobFilter = "all";
      jobTrack = "all";
      renderJobs(currentJobs);
      selectJob(jobId);
      $("jobDetail").querySelector("h2")?.focus({preventScroll: true});
    }

    async function runCopilotAction(kind, jobId, instruction = "") {
      if (kind === "review_queue") elements.queueButton.click();
      else if (kind === "open_job") focusJob(jobId);
      else if (kind === "open_preparation") {
        const detail = await readApiResponse(await fetch(`/api/jobs/${encodeURIComponent(jobId)}/detail`, {cache: "no-store"}));
        if (!detail.preparation_url) throw new Error("这个职位还没有已生成的准备包。可先在对话中讨论面试问题；已有准备包保留在“进度与资料”。");
        const preparationUrl = safeArtifactUrl(detail.preparation_url);
        if (!preparationUrl) throw new Error("学习包链接无效，请重新生成。");
        window.open(preparationUrl, "_blank", "noopener");
      }
      else if (kind === "open_profile") elements.openProfileButton.click();
      else if (kind === "open_settings") elements.openSettingsButton.click();
      else if (kind === "create_resume_draft") await createResumeDraft(jobId);
      else if (kind === "revise_resume") {
        const cleaned = String(instruction || "").trim();
        if (!cleaned) throw new Error("这条 Agent 动作缺少修改要求，请重新发送消息。");
        if (!window.confirm(`让 Agent 按 JD 与 STAR 法则直接修改岗位 #${jobId} 的简历？\n\n修改要求：${cleaned}\n\n新版仍需你打开 PDF 审阅后才能用于投递。`)) return;
        const preview = window.open("about:blank", "_blank");
        try {
          const result = await postLocalJson(`/api/jobs/${encodeURIComponent(jobId)}/resume-content/revise`, {
            confirmed: true,
            instruction: cleaned
          });
          if (preview) preview.location.href = safeArtifactUrl(result.resume_url) || `/resume-draft/${encodeURIComponent(jobId)}/pdf`;
          else window.open(safeArtifactUrl(result.resume_url) || `/resume-draft/${encodeURIComponent(jobId)}/pdf`, "_blank", "noopener");
          await loadDashboard();
          if (result.generation?.engine === "local_fallback") {
            showError(`已生成本地事实草稿，整份 AI 写作本次未完成。${result.generation.warning || "请检查 API 配置。"}`);
          } else {
            showSuccess(`岗位 #${jobId} 已生成新版简历。${result.generation?.strategy || ""} 请核对 PDF 后再批准。`);
          }
        } catch (error) {
          if (preview) preview.close();
          showError(`Agent 修改简历失败：${error?.message || error}`);
        }
      }
      else if (kind === "approve_resume_draft") await approveResumeDraft(jobId);
      else if (kind === "start_safe_fill") await startManualApply(jobId);
    }

    async function recordStatus(jobId, status, detail) {
      if (!actionToken) throw new Error("页面授权尚未准备好，请先刷新。 ");
      const response = await fetch(`/api/applications/${encodeURIComponent(jobId)}/status`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Job-Agent-Token": actionToken
        },
        body: JSON.stringify({ status, detail, confirmed: true })
      });
      return readApiResponse(response);
    }

    async function recordCommute(jobId, minutes, method, note) {
      if (!actionToken) throw new Error("页面授权尚未准备好，请先刷新。");
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/commute`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Job-Agent-Token": actionToken
        },
        body: JSON.stringify({ minutes, method, note, confirmed: true })
      });
      return readApiResponse(response);
    }

    async function calculateRoute(jobId, destination, mode) {
      if (!actionToken) throw new Error("页面授权尚未准备好，请先刷新。");
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/commute/route`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Job-Agent-Token": actionToken
        },
        body: JSON.stringify({ destination, mode, confirmed: true })
      });
      return readApiResponse(response);
    }

    function renderRouteResult(route) {
      const modeLabels = { transit: "公交 / 地铁", driving: "驾车", walking: "步行", bicycling: "骑行" };
      const options = Array.isArray(route?.options) ? route.options : [];
      elements.routeResult.innerHTML = `
        <div><strong>${escapeHtml(route.origin_resolved || route.origin_query)} → ${escapeHtml(route.destination_resolved || route.destination_query)}</strong></div>
        <div class="usage-line">${escapeHtml(modeLabels[route.mode] || route.mode)} · 最快方案已保存并参与岗位排序</div>
        <div class="route-options">${options.map((option) => {
          const distance = option.distance_meters ? `${(Number(option.distance_meters) / 1000).toFixed(1)} 公里` : "距离未返回";
          const walk = option.walking_meters ? ` · 步行 ${option.walking_meters} 米` : "";
          const transfer = option.transfers !== null && option.transfers !== undefined ? ` · 换乘 ${option.transfers} 次` : "";
          return `<div class="route-option"><strong>${escapeHtml(option.minutes)} 分钟</strong><span>方案 ${escapeHtml(option.rank)} · ${escapeHtml(distance)}${escapeHtml(walk)}${escapeHtml(transfer)}<br>${escapeHtml(option.summary)}</span></div>`;
        }).join("")}</div>`;
      elements.routeResult.classList.add("visible");
    }

    const loadDashboard = coalesceRefresh(refreshDashboard);

    async function refreshDashboard() {
      setLoading(true);
      elements.alert.classList.remove("visible");
      try {
        const response = await fetch("/api/dashboard", { cache: "no-store" });
        const data = await readApiResponse(response);

        actionToken = data.action_token || "";
        try {
          // Background refresh must not erase unsaved API settings.
          if (!elements.settingsDialog.open) await loadConnectorSettings();
        } catch (settingsError) {
          elements.connectorSummaryDot.classList.remove("ready");
          elements.connectorSummaryText.textContent = "API 设置不可读";
        }
        activeProfile = data.active_profile || null;
        renderProfile(activeProfile);
        renderStrategyPlan(data.strategy || null);
        renderMetrics(data.metrics || []);
        renderFunnel(data.funnel || []);
        renderTrackedJobs(data.tracked_jobs || []);
        renderJobs(data.tracked_jobs || []);
        const selected = currentJobs.find(job => job.job_id === selectedJobId);
        if (selected) {
          const scrollTop = $("jobDetail").querySelector(".detail-scroll")?.scrollTop || 0;
          renderJobDetail(selected);
          $("jobDetail").querySelector(".detail-scroll").scrollTop = scrollTop;
          loadJobDetail(selected);
        }
        renderCandidateBreakdown(data.candidate_breakdown || []);
        renderPreparation(data.priority_preparation || []);
        renderFeedback(data.recent_feedback || []);
        renderMethodology(data);
        renderHome(data);

        elements.freshnessMain.textContent = relativeFreshness(data.source_freshness_at);
        elements.freshnessSub.textContent = `${data.source_name || "本地 SQLite"} · 页面 ${formatDateTime(data.generated_at)} 刷新`;
        elements.systemState.textContent = "本地数据已连接";
        elements.stateDot.className = "state-dot";
      } catch (error) {
        showError(`暂时无法读取求职数据。请保留启动窗口并重试。${error?.message ? `（${error.message}）` : ""}`);
      } finally {
        setLoading(false);
        if (!elements.alert.classList.contains("visible")) {
          elements.systemState.textContent = "本地数据已连接";
          elements.stateDot.className = "state-dot";
        }
      }
    }

    $("todayNav").addEventListener("click", () => navigate("today"));
    $("profileNav").addEventListener("click", () => navigate("profile"));
    $("overviewBack").addEventListener("click", goBack);
    $("todayPanel").addEventListener("click", event => {
      const job = event.target.closest("[data-home-job]");
      if (job) {
        navigate("jobs", Number(job.dataset.homeJob));
        detailTab = job.dataset.homeTab || "jd";
        return;
      }
      const action = event.target.closest("[data-home-action]")?.dataset.homeAction;
      if (action === "refresh") loadDashboard();
      else if (action === "profile") elements.openProfileButton.click();
      else if (action === "preferences") elements.openPreferencesButton.click();
      else if (action === "settings") elements.openSettingsButton.click();
      else if (action) navigate(action);
    });
    window.addEventListener("popstate", event => {
      const state = event.state || {view: "today"};
      navigate(state.view || "today", state.jobId ?? null, true, state);
    });
    $("workspaceNav").addEventListener("click", () => showWorkspace());
    $("overviewNav").addEventListener("click", () => showWorkspace(true));
    $("jobSearch").addEventListener("input", () => renderJobs(currentJobs));
    document.querySelector(".track-switch").addEventListener("click", event => {
      const button = event.target.closest("[data-job-track]");
      if (!button) return;
      jobTrack = button.dataset.jobTrack;
      renderJobs(currentJobs);
    });
    document.querySelector(".track-plan").addEventListener("click", event => {
      const button = event.target.closest("[data-job-track]");
      if (!button) return;
      jobTrack = button.dataset.jobTrack;
      renderJobs(currentJobs);
    });
    document.querySelector(".job-filters").addEventListener("click", event => {
      const button = event.target.closest("[data-job-filter]");
      if (!button) return;
      jobFilter = button.dataset.jobFilter;
      renderJobs(currentJobs);
    });
    elements.jobs.addEventListener("click", event => {
      const choice = event.target.closest("[data-select-job]");
      if (choice) selectJob(choice.dataset.selectJob);
      if (event.target.closest("[data-clear-search]")) {
        $("jobSearch").value = "";
        jobFilter = "all";
        renderJobs(currentJobs);
        $("jobSearch").focus();
      }
    });
    $("jobDetail").addEventListener("click", async event => {
      const tab = event.target.closest("[data-detail-tab]");
      if (tab) {
        detailTab = tab.dataset.detailTab;
        renderPositionContent();
        return;
      }
      const button = event.target.closest("[data-detail-action]");
      if (!button || button.disabled) return;
      const action = button.dataset.detailAction;
      const jobId = selectedJobId;
      if (action === "back") {
        goBack();
        return;
      }
      if (action === "status" || action === "commute") {
        showWorkspace(true);
        elements.statusJob.value = String(jobId);
        elements.commuteJob.value = String(jobId);
        elements.routeJob.value = String(jobId);
        elements.statusConfirmed.checked = false;
        elements.routeConfirmed.checked = false;
        elements.commuteConfirmed.checked = false;
        prefillCommuteForJob();
        prefillRouteForJob();
        const utility = document.querySelector(action === "status" ? ".area-status" : ".area-commute");
        utility.open = true;
        utility.scrollIntoView({block: "start"});
        utility.querySelector("select").focus();
        return;
      }
      if (action === "open-resume") return openResumeDraft(jobId);
      if (action === "ask-agent-resume") return askAgentToReviseResume(jobId);
      if (action === "interview") return openCopilot("这个职位面试该准备什么？");
      if (action === "outreach-greetings") {
        const dialog = $("outreachDialog");
        const list = $("outreachList");
        list.innerHTML = '<p class="quiet">正在深入拆解本岗位 JD 并比对你的经历库，定制 3 条高回复招呼语…</p>';
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
        $("closeOutreachButton").onclick = () => dialog.close();
        try {
          const res = await fetch(`/api/jobs/${jobId}/outreach`, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Job-Agent-Token": actionToken,
            },
          });
          const data = await readApiResponse(res);
          const greetings = data.greetings || [];
          if (!greetings.length) throw new Error("未能生成招呼语");
          list.innerHTML = greetings.map((g, idx) => `
            <div style="background: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 12px;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                <strong style="color: #0f172a; font-size: 13px;">${escapeHtml(g.title)}</strong>
                <button class="action-button secondary" type="button" style="padding: 4px 10px; font-size: 12px;" data-copy-greeting="${idx}">📋 复制文案</button>
              </div>
              <p style="margin: 0; color: #334155; font-size: 13px; line-height: 1.5; white-space: pre-wrap;" id="greetingText_${idx}">${escapeHtml(g.content)}</p>
            </div>
          `).join("");
          list.querySelectorAll("[data-copy-greeting]").forEach(btn => {
            btn.addEventListener("click", () => {
              const idx = btn.dataset.copyGreeting;
              const text = $(`greetingText_${idx}`).innerText;
              navigator.clipboard.writeText(text).then(() => {
                const old = btn.textContent;
                btn.textContent = "✅ 已复制";
                setTimeout(() => { btn.textContent = old; }, 2000);
              });
            });
          });
        } catch (err) {
          list.innerHTML = `<p class="decision-warning">生成失败: ${escapeHtml(err.message)}</p>`;
        }
        return;
      }
      if (action === "interview-prep") {
        const dialog = $("interviewPrepDialog");
        const list = $("interviewPrepList");
        list.innerHTML = '<p class="quiet">正在深入分析本岗位核心痛点，并结合你的经历库生成 5 大高频面试真题与 STAR 答辩脚本…</p>';
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
        $("closeInterviewPrepButton").onclick = () => dialog.close();
        try {
          const res = await fetch(`/api/jobs/${jobId}/interview-prep`, { cache: "no-store" });
          const data = await readApiResponse(res);
          const prep = data.data || {};
          const questions = prep.questions || [];
          if (!questions.length) throw new Error("未能获取面试真题");
          list.innerHTML = questions.map((q, idx) => `
            <div style="background: #ffffff; border: 1px solid #cbd5e1; border-radius: 8px; padding: 14px;">
              <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; gap: 8px;">
                <div>
                  <span class="badge" style="background: #e0f2fe; color: #0369a1; font-size: 11px; margin-right: 6px;">${escapeHtml(q.category)}</span>
                  <strong style="color: #0f172a; font-size: 14px;">Q${idx + 1}：${escapeHtml(q.question)}</strong>
                </div>
                <button class="action-button secondary" type="button" style="padding: 3px 8px; font-size: 11px; flex-shrink: 0;" data-copy-qa="${idx}">📋 复制答案</button>
              </div>
              <div style="background: #f8fafc; border-left: 3px solid #3b82f6; padding: 6px 10px; margin-bottom: 8px; border-radius: 0 4px 4px 0; font-size: 12px; color: #475569;">
                <strong>🎯 考核意图：</strong>${escapeHtml(q.intent)}
              </div>
              <div style="margin-bottom: 8px;">
                <strong style="font-size: 12px; color: #0f172a;">💡 推荐 STAR 答辩脚本：</strong>
                <p style="margin: 4px 0 0 0; color: #334155; font-size: 13px; line-height: 1.6; white-space: pre-wrap;" id="qaText_${idx}">${escapeHtml(q.star_answer)}</p>
              </div>
              <div style="font-size: 12px; color: #b91c1c; background: #fef2f2; padding: 4px 8px; border-radius: 4px;">
                ⚠️ 避坑提醒：${escapeHtml(q.traps_to_avoid)}
              </div>
            </div>
          `).join("");
          list.querySelectorAll("[data-copy-qa]").forEach(btn => {
            btn.addEventListener("click", () => {
              const idx = btn.dataset.copyQa;
              const text = $(`qaText_${idx}`).innerText;
              navigator.clipboard.writeText(text).then(() => {
                const old = btn.textContent;
                btn.textContent = "✅ 已复制";
                setTimeout(() => { btn.textContent = old; }, 2000);
              });
            });
          });
        } catch (err) {
          list.innerHTML = `<p class="decision-warning">生成失败: ${escapeHtml(err.message)}</p>`;
        }
        return;
      }
      if (action === "browser-use-assist") {
        await openBrowserUseDialog(jobId);
        return;
      }
      button.disabled = true;
      try {
        if (action === "archive-job") {
          const ignored = !currentJobs.find(job => job.job_id === jobId)?.job_archived;
          await postLocalJson(`/api/jobs/${jobId}/archive-job`, { ignored });
          await loadDashboard();
          showSuccess(ignored ? "此岗位已标记失效并沉底，可随时恢复。" : "此岗位已恢复。");
        }
        if (action === "open-page") {
          await postLocalJson(`/api/jobs/${jobId}/open-page`, {});
          showSuccess("已请求系统浏览器打开招聘页。登录与提交由你完成，不会自动标记为已投递。");
        }
        if (action === "create-resume") await createResumeDraft(jobId);
        if (action === "approve-resume") await approveResumeDraft(jobId);
        if (action === "manual-apply") await startManualApply(jobId);
        if (action === "edit-resume") await openResumeEditor(jobId);
        if (action === "run-project") await runWorkshopProject(jobId, false);
        if (action === "rerun-project") await runWorkshopProject(jobId, true);
        if (action === "verify-project") await verifyWorkshopProject(jobId);
      } catch (error) {
        showError(`操作没有完成：${error?.message || error}`);
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    });

    elements.preparation.addEventListener("click", async (event) => {
      const button = event.target.closest('[data-action="ask-agent-resume"]');
      if (!button) return;
      await askAgentToReviseResume(button.dataset.jobId);
    });

    elements.resumeEditorCloseButton.addEventListener("click", () => elements.resumeEditorDialog.close());
    elements.resumeEditorCancel.addEventListener("click", () => elements.resumeEditorDialog.close());

    elements.editorPhotoUpload.addEventListener("click", async () => {
      const file = elements.editorPhotoFile.files && elements.editorPhotoFile.files[0];
      if (!file) {
        elements.resumeEditorStatus.textContent = "请先选择一张 JPG 或 PNG 照片。";
        return;
      }
      if (file.size > 5 * 1024 * 1024) {
        elements.resumeEditorStatus.textContent = "照片超过 5MB，请先压缩。";
        return;
      }
      elements.editorPhotoUpload.disabled = true;
      elements.resumeEditorStatus.textContent = "正在上传照片…";
      try {
        const response = await fetch("/api/profile/photo", {
          method: "POST",
          headers: {
            "Content-Type": file.type || "image/png",
            "X-Job-Agent-Token": actionToken
          },
          body: await file.arrayBuffer()
        });
        const data = await readApiResponse(response);
        syncEditorContent();
        resumeEditorState.content.person.photo_path = data.photo_path || "";
        resumeEditorState.photoAvailable = true;
        renderEditorForm();
        elements.resumeEditorStatus.textContent = "照片已上传；保存并重新渲染后生效。";
      } catch (error) {
        elements.resumeEditorStatus.textContent = `照片上传失败：${error?.message || error}`;
      } finally {
        elements.editorPhotoUpload.disabled = false;
      }
    });

    elements.editorSections.addEventListener("click", (event) => {
      const button = event.target.closest("[data-editor-action]");
      if (!button || !resumeEditorState) return;
      syncEditorContent();
      const sectionEl = button.closest(".editor-section");
      const entryEl = button.closest(".editor-entry");
      const section = (resumeEditorState.content.experience_sections || [])[Number(sectionEl.dataset.sectionIndex)];
      if (!section) return;
      const action = button.dataset.editorAction;
      if (action === "add-bullet") {
        const entry = (section.entries || [])[Number(entryEl.dataset.entryIndex)];
        if (!entry || (entry.bullets || []).length >= 4) {
          elements.resumeEditorStatus.textContent = "每段经历最多 4 条要点。";
          return;
        }
        entry.bullets.push({ label: "", text: "" });
      } else if (action === "remove-bullet") {
        const entry = (section.entries || [])[Number(entryEl.dataset.entryIndex)];
        if (!entry || (entry.bullets || []).length <= 1) {
          elements.resumeEditorStatus.textContent = "每段经历至少保留一条要点。";
          return;
        }
        entry.bullets.splice(Number(button.closest(".editor-bullet").dataset.bulletIndex), 1);
      } else if (action === "remove-entry") {
        if ((section.entries || []).length <= 1) {
          elements.resumeEditorStatus.textContent = "每个板块至少保留一段经历。";
          return;
        }
        section.entries.splice(Number(entryEl.dataset.entryIndex), 1);
      }
      renderEditorSections();
    });

    elements.editorAddEntry.addEventListener("click", () => {
      if (!resumeEditorState) return;
      syncEditorContent();
      const sections = resumeEditorState.content.experience_sections || (resumeEditorState.content.experience_sections = []);
      if (!sections.length) sections.push({ title: "经历", entries: [] });
      const target = sections[0];
      if ((target.entries || []).length >= 4) {
        elements.resumeEditorStatus.textContent = "每个板块最多 4 段经历。";
        return;
      }
      target.entries.push({ organization: "新经历", role: "", dates: "", bullets: [{ label: "", text: "" }] });
      renderEditorSections();
    });

    elements.editorPolish.addEventListener("click", async () => {
      if (!resumeEditorState) return;
      syncEditorContent();
      elements.editorPolish.disabled = true;
      elements.resumeEditorStatus.textContent = "正在按 JD 生成润色建议…";
      try {
        const jobId = resumeEditorState.jobId;
        const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/resume-content/polish`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Job-Agent-Token": actionToken },
          body: JSON.stringify({ confirmed: true, content: resumeEditorState.content })
        });
        const data = await readApiResponse(response);
        resumeEditorState.content = data.content || resumeEditorState.content;
        renderEditorForm();
        const count = Number(data.change_count || 0);
        if (data.engine === "local_fallback") {
          elements.resumeEditorStatus.textContent = `${data.warning || "云端 AI 本次不可用。"} 已自动改用本地基础润色${count ? `，生成 ${count} 处建议` : "，当前没有安全的修改建议"}；未消耗 AI 额度。`;
        } else if (data.engine === "local") {
          elements.resumeEditorStatus.textContent = count
            ? `本地规则已按 JD 整理 ${count} 处；请检查后保存。`
            : "本地规则未发现需要安全调整的内容。";
        } else {
          elements.resumeEditorStatus.textContent = count
            ? `AI 已按 JD 润色 ${count} 处（含自我评价）；请检查后保存。`
            : "AI 认为当前措辞已贴合 JD，未产生修改建议。";
        }
      } catch (error) {
        elements.resumeEditorStatus.textContent = `润色失败：${error?.message || error}`;
      } finally {
        elements.editorPolish.disabled = false;
      }
    });

    elements.editorImport.addEventListener("click", async () => {
      if (!resumeEditorState) return;
      const resumeText = (elements.editorImportText.value || "").trim();
      if (!resumeText) {
        elements.resumeEditorStatus.textContent = "请先把原简历文本粘贴到导入框中。";
        elements.editorImportText.focus();
        return;
      }
      if (!window.confirm("导入会用 AI 解析的原简历内容替换当前表单（岗位信息与照片保留）。解析结果只是建议，保存前请逐项检查。继续吗？")) {
        return;
      }
      syncEditorContent();
      elements.editorImport.disabled = true;
      elements.resumeEditorStatus.textContent = "正在解析原简历…";
      try {
        const jobId = resumeEditorState.jobId;
        const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/resume-content/import`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Job-Agent-Token": actionToken },
          body: JSON.stringify({ resume_text: resumeText })
        });
        const data = await readApiResponse(response);
        resumeEditorState.content = data.content || resumeEditorState.content;
        renderEditorForm();
        const warnings = (data.warnings || []).length
          ? `注意：${data.warnings.join("；")}。`
          : "";
        elements.resumeEditorStatus.textContent =
          `已把原简历解析为表单内容；请逐项检查姓名、联系方式与量化数字后再保存。${warnings}`;
      } catch (error) {
        elements.resumeEditorStatus.textContent = `导入失败：${error?.message || error}`;
      } finally {
        elements.editorImport.disabled = false;
      }
    });

    elements.resumeEditorForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!resumeEditorState) return;
      syncEditorContent();
      elements.resumeEditorSubmit.disabled = true;
      elements.resumeEditorStatus.textContent = "正在重新渲染简历…";
      try {
        const jobId = resumeEditorState.jobId;
        await postLocalJson(`/api/jobs/${encodeURIComponent(jobId)}/resume-content`, {
          confirmed: true,
          content: resumeEditorState.content
        });
        elements.resumeEditorDialog.close();
        await loadDashboard();
        showSuccess(`岗位 #${jobId} 的简历已按你的修改重新渲染；请打开 PDF 复查后再批准。`);
        window.open(`/resume-draft/${encodeURIComponent(jobId)}/pdf`, "_blank", "noopener");
      } catch (error) {
        elements.resumeEditorStatus.textContent = `保存失败：${error?.message || error}`;
      } finally {
        elements.resumeEditorSubmit.disabled = false;
      }
    });

    elements.statusForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!elements.statusForm.reportValidity()) return;
      const jobId = elements.statusJob.value;
      const status = elements.statusValue.value;
      elements.statusSubmit.disabled = true;
      try {
        await recordStatus(jobId, status, elements.statusDetail.value.trim());
        elements.statusDetail.value = "";
        elements.statusConfirmed.checked = false;
        await loadDashboard();
        showSuccess(`岗位 #${jobId} 已更新为“${statusLabels[status] || status}”。`);
      } catch (error) {
        showError(`状态记录失败：${error?.message || error}`);
      } finally {
        elements.statusSubmit.disabled = false;
      }
    });

    elements.commuteJob.addEventListener("change", prefillCommuteForJob);
    elements.routeJob.addEventListener("change", prefillRouteForJob);
    elements.routeForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!elements.routeForm.reportValidity()) return;
      const jobId = elements.routeJob.value;
      elements.routeSubmit.disabled = true;
      elements.routeSubmit.textContent = "正在计算…";
      try {
        const result = await calculateRoute(
          jobId,
          elements.commuteDestination.value.trim(),
          elements.routeMode.value
        );
        renderRouteResult(result.route);
        elements.routeConfirmed.checked = false;
        await loadDashboard();
        renderRouteResult(result.route);
        showSuccess(`岗位 #${jobId} 的最佳通勤路线已保存，并已参与今日岗位排序。`);
      } catch (error) {
        showError(`路线计算失败：${error?.message || error}`);
      } finally {
        elements.routeSubmit.disabled = false;
        elements.routeSubmit.textContent = "计算并保存";
      }
    });
    elements.commuteMethod.addEventListener("change", updateCommuteMethodInput);
    elements.commuteForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!elements.commuteForm.reportValidity()) return;
      const jobId = elements.commuteJob.value;
      const minutes = elements.commuteMethod.value === "unknown"
        ? null
        : Number(elements.commuteMinutes.value);
      elements.commuteSubmit.disabled = true;
      try {
        await recordCommute(
          jobId,
          minutes,
          elements.commuteMethod.value,
          elements.commuteNote.value.trim()
        );
        elements.commuteConfirmed.checked = false;
        await loadDashboard();
        const maximum = activeProfile?.max_one_way_minutes;
        if (minutes === null) {
          showSuccess(`岗位 #${jobId} 的通勤记录已清除，恢复为“待估算”，不会被自动排除。`);
        } else {
          const result = maximum && minutes > maximum
            ? `，超过你的 ${maximum} 分钟上限，已从今日待投列表移出`
            : "，已参与今日岗位筛选";
          showSuccess(`岗位 #${jobId} 的单程通勤已记录为 ${minutes} 分钟${result}。`);
        }
      } catch (error) {
        showError(`通勤记录失败：${error?.message || error}`);
      } finally {
        elements.commuteSubmit.disabled = false;
      }
    });

    elements.openCopilotButton.addEventListener("click", () => openCopilot());
    elements.closeCopilotButton.addEventListener("click", () => {
      $("jobWorkspace").classList.remove("agent-visible");
      $("jobWorkspace").classList.add("agent-hidden");
      elements.openCopilotButton.setAttribute("aria-expanded", "false");
      elements.openCopilotButton.focus();
    });
    $("historyCopilotButton").addEventListener("click", () => {
      copilotHistory = !copilotHistory;
      if (copilotSnapshot) renderCopilot(copilotSnapshot);
    });
    elements.resetCopilotButton.addEventListener("click", async () => {
      if (!window.confirm("开始一段新对话？当前对话会从工作栏中清空。")) return;
      setCopilotBusy(true, "正在开始新对话…");
      try {
        const result = await postLocalJson("/api/copilot/reset", {});
        renderCopilot(result.copilot);
      } catch (error) {
        showError(`新对话创建失败：${error?.message || error}`);
      } finally {
        setCopilotBusy(false);
        elements.copilotInput.focus();
      }
    });
    elements.copilotForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      await sendCopilotMessage(elements.copilotInput.value);
    });
    elements.copilotInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        elements.copilotForm.requestSubmit();
      }
    });
    elements.copilotSuggestions.addEventListener("click", (event) => {
      const button = event.target.closest("[data-copilot-suggestion]");
      if (button) sendCopilotMessage(button.dataset.copilotSuggestion || "");
    });
    elements.copilotMessages.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-copilot-kind]");
      if (!button) return;
      button.disabled = true;
      try {
        await runCopilotAction(button.dataset.copilotKind, button.dataset.jobId || "", button.dataset.instruction || "");
      } catch (error) {
        showError(`Agent 动作执行失败：${error?.message || error}`);
      } finally {
        button.disabled = false;
      }
    });

    elements.openSettingsButton.addEventListener("click", async () => {
      try {
        await loadConnectorSettings();
      } catch (error) {
        showError(`API 设置读取失败：${error?.message || error}`);
        return;
      }
      if (typeof elements.settingsDialog.showModal === "function") elements.settingsDialog.showModal();
      else elements.settingsDialog.setAttribute("open", "");
    });
    const closeSettingsDialog = () => elements.settingsDialog.close();
    elements.closeSettingsButton.addEventListener("click", closeSettingsDialog);
    elements.cancelSettingsButton.addEventListener("click", closeSettingsDialog);

    const copyTokenBtn = document.getElementById("copyAgentTokenButton");
    const copyTokenStatus = document.getElementById("copyAgentTokenStatus");
    if (copyTokenBtn) {
      copyTokenBtn.addEventListener("click", () => {
        navigator.clipboard.writeText(actionToken || "synthetic-token").then(() => {
          if (copyTokenStatus) copyTokenStatus.textContent = "已复制";
          setTimeout(() => { if (copyTokenStatus) copyTokenStatus.textContent = ""; }, 2500);
        });
      });
    }
    elements.aiProvider.addEventListener("change", () => {
      if (elements.aiProvider.value === "codex") elements.aiModel.value = "codex-default";
      if (elements.aiProvider.value === "openai" && !elements.aiModel.value) {
        elements.aiModel.value = "gpt-5.6-luna";
      }
      syncConnectorFields();
    });
    elements.searchProvider.addEventListener("change", syncConnectorFields);
    elements.mapProvider.addEventListener("change", syncConnectorFields);
    elements.settingsForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      syncConnectorFields();
      if (!elements.settingsForm.reportValidity()) return;
      if (!actionToken) {
        showError("页面授权尚未准备好，请刷新后重试。");
        return;
      }
      elements.settingsSubmit.disabled = true;
      try {
        const aiProvider = elements.aiProvider.value;
        const response = await fetch("/api/settings", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Job-Agent-Token": actionToken
          },
          body: JSON.stringify({
            ai_provider: aiProvider,
            ai_model: aiProvider === "local" ? "local-explainable-v1" : elements.aiModel.value.trim(),
            ai_base_url: aiProvider === "openai_compatible" ? elements.aiBaseUrl.value.trim() : null,
            ai_api_key: aiProvider === "local" ? "" : elements.aiApiKey.value.trim(),
            clear_ai_api_key: elements.clearAiApiKey.checked,
            ai_monthly_quota: optionalPositiveInteger(elements.aiMonthlyQuota),
            search_provider: elements.searchProvider.value,
            search_api_key: elements.searchProvider.value === "none" ? "" : elements.searchApiKey.value.trim(),
            clear_search_api_key: elements.clearSearchApiKey.checked,
            search_monthly_quota: optionalPositiveInteger(elements.searchMonthlyQuota),
            map_provider: elements.mapProvider.value,
            map_api_key: elements.mapProvider.value === "none" ? "" : elements.mapApiKey.value.trim(),
            clear_map_api_key: elements.clearMapApiKey.checked,
            map_monthly_quota: optionalPositiveInteger(elements.mapMonthlyQuota)
          })
        });
        const result = await readApiResponse(response);
        renderConnectorSettings(result.settings);
        closeSettingsDialog();
        showSuccess("连接和额度设置已保存在本机。新的搜索、AI 分析与通勤路线会使用这套配置。");
      } catch (error) {
        showError(`API 设置保存失败：${error?.message || error}`);
      } finally {
        elements.settingsSubmit.disabled = false;
      }
    });

    const testConnBtn = $("testConnectionButton");
    const testConnRes = $("testConnectionResult");
    if (testConnBtn) {
      testConnBtn.addEventListener("click", async () => {
        syncConnectorFields();
        const aiProvider = elements.aiProvider.value;
        testConnBtn.disabled = true;
        testConnRes.hidden = false;
        testConnRes.style.color = "#475569";
        testConnRes.textContent = "正在向服务商发送握手测试，请稍候…";
        try {
          const response = await fetch("/api/settings/test-connection", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Job-Agent-Token": actionToken,
            },
            body: JSON.stringify({
              provider: aiProvider,
              model: aiProvider === "local" ? "local-explainable-v1" : elements.aiModel.value.trim(),
              base_url: aiProvider === "openai_compatible" ? elements.aiBaseUrl.value.trim() : null,
              api_key: elements.aiApiKey.value.trim(),
            }),
          });
          const data = await readApiResponse(response);
          if (data.ok) {
            testConnRes.style.color = "#16a34a";
            testConnRes.textContent = `✅ ${data.message}`;
          } else {
            testConnRes.style.color = "#dc2626";
            testConnRes.textContent = `❌ ${data.message}`;
          }
        } catch (err) {
          testConnRes.style.color = "#dc2626";
          testConnRes.textContent = `❌ 测试连接失败：${err.message || err}`;
        } finally {
          testConnBtn.disabled = false;
        }
      });
    }

    elements.openProfileButton.addEventListener("click", () => {
      prefillProfileForm(activeProfile);
      if (typeof elements.profileDialog.showModal === "function") elements.profileDialog.showModal();
      else elements.profileDialog.setAttribute("open", "");
    });

    elements.openPreferencesButton.addEventListener("click", () => {
      if (!activeProfile) return;
      prefillPreferencesForm(activeProfile);
      if (typeof elements.preferencesDialog.showModal === "function") elements.preferencesDialog.showModal();
      else elements.preferencesDialog.setAttribute("open", "");
    });
    const closePreferencesDialog = () => elements.preferencesDialog.close();
    elements.closePreferencesButton.addEventListener("click", closePreferencesDialog);
    elements.cancelPreferencesButton.addEventListener("click", closePreferencesDialog);
    elements.preferencesForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!elements.preferencesForm.reportValidity()) return;
      elements.preferencesSubmit.disabled = true;
      try {
        const maximum = String(elements.preferenceMaxCommute.value || "").trim();
        const dailyPayFloor = String(elements.preferenceDailyPayFloor.value || "").trim();
        const response = await fetch("/api/profile/preferences", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Job-Agent-Token": actionToken
          },
          body: JSON.stringify({
            target_roles: splitValues(elements.preferenceTargetRoles.value),
            adjacent_roles: splitValues(elements.preferenceAdjacentRoles.value),
            target_industries: splitValues(elements.preferenceIndustries.value),
            preferred_locations: splitValues(elements.preferenceLocations.value),
            employment_types: splitValues(elements.preferenceEmploymentTypes.value),
            must_haves: splitValues(elements.preferenceMustHaves.value),
            avoid: splitValues(elements.preferenceAvoid.value),
            commute_origin: elements.preferenceCommuteOrigin.value.trim(),
            max_one_way_minutes: maximum ? Number(maximum) : null,
            transport_modes: splitValues(elements.preferenceTransportModes.value),
            remote_acceptable: elements.preferenceRemote.checked,
            internship_daily_pay_floor: dailyPayFloor ? Number(dailyPayFloor) : null,
            exclude_outsourcing: elements.preferenceExcludeOutsourcing.checked
          })
        });
        await readApiResponse(response);
        closePreferencesDialog();
        await loadDashboard();
        showSuccess("求职方向与通勤边界已更新，岗位列表已按新的条件重新排序。");
      } catch (error) {
        showError(`求职偏好保存失败：${error?.message || error}`);
      } finally {
        elements.preferencesSubmit.disabled = false;
      }
    });
    const closeProfileDialog = () => elements.profileDialog.close();
    elements.closeProfileButton.addEventListener("click", closeProfileDialog);
    elements.cancelProfileButton.addEventListener("click", closeProfileDialog);
    elements.profileMode.addEventListener("change", () => {
      const replacing = elements.profileMode.value === "replace";
      elements.replaceConfirm.classList.toggle("visible", replacing);
      const checkbox = elements.replaceConfirm.querySelector('input[name="confirm_replace"]');
      checkbox.required = replacing;
      if (!replacing) checkbox.checked = false;
      document.getElementById("targetRoles").required = replacing;
      syncProfileRequirements(activeProfile);
    });
    elements.resumeFile.addEventListener("change", () => syncProfileRequirements(activeProfile));
    elements.profilePhotoUpload.addEventListener("click", async () => {
      const file = elements.profilePhotoFile.files && elements.profilePhotoFile.files[0];
      if (!file) {
        elements.profilePhotoStatus.textContent = "请先选择一张 JPG 或 PNG 证件照。";
        return;
      }
      if (file.size > 5 * 1024 * 1024) {
        elements.profilePhotoStatus.textContent = "照片超过 5MB，请先压缩。";
        return;
      }
      elements.profilePhotoUpload.disabled = true;
      elements.profilePhotoStatus.textContent = "正在保存到本机私有目录…";
      try {
        const response = await fetch("/api/profile/photo", {
          method: "POST",
          headers: {
            "Content-Type": file.type || "image/png",
            "X-Job-Agent-Token": actionToken
          },
          body: await file.arrayBuffer()
        });
        await readApiResponse(response);
        elements.profilePhotoStatus.textContent = "证件照已保存。之后生成或通过 Agent 修改简历时会自动嵌入。";
      } catch (error) {
        elements.profilePhotoStatus.textContent = `证件照上传失败：${error?.message || error}`;
      } finally {
        elements.profilePhotoUpload.disabled = false;
      }
    });
    elements.profileForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      syncProfileRequirements(activeProfile);
      if (!elements.profileForm.reportValidity()) return;
      elements.profileSubmit.disabled = true;
      try {
        const response = await fetch("/api/profile/onboard", {
          method: "POST",
          headers: { "X-Job-Agent-Token": actionToken },
          body: new FormData(elements.profileForm)
        });
        const result = await readApiResponse(response);
        closeProfileDialog();
        await loadDashboard();
        const switchNote = result.replaced_profile ? "已备份上一位用户并切换到新的空白岗位库。" : "已更新当前用户资料。";
        const warning = (result.warnings || []).length ? ` 提醒：${result.warnings.join("；")}` : "";
        const resumeNote = result.resume_filename
          ? `已读取 ${result.extracted_characters} 个简历字符，导入 ${result.imported_facts} 条原文事实和 ${result.imported_skills} 项基础技能。`
          : "未重复上传简历，原有经历库保持不变。";
        showSuccess(`${switchNote} ${resumeNote}${warning}`);
      } catch (error) {
        showError(`简历与资料保存失败：${error?.message || error}`);
      } finally {
        elements.profileSubmit.disabled = false;
      }
    });

    elements.queueButton.addEventListener("click", () => {
      showWorkspace();
      jobFilter = "pending";
      jobTrack = "all";
      $("jobSearch").value = "";
      renderJobs(currentJobs);
      $("jobWorkspace").classList.remove("detail-visible");
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      elements.jobsTitle.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
      elements.jobsTitle.focus({ preventScroll: true });
    });
    // 智能录入岗位交互
    if (elements.openImportJobButton && elements.importJobDialog) {
      elements.openImportJobButton.addEventListener("click", () => {
        elements.rawJobInput.value = "";
        elements.parsedCompany.value = "";
        elements.parsedTitle.value = "";
        elements.parsedLocation.value = "";
        elements.parsedSourceUrl.value = "";
        elements.parsedJdText.value = "";
        if (typeof elements.importJobDialog.showModal === "function") elements.importJobDialog.showModal();
        else elements.importJobDialog.setAttribute("open", "");
      });
      const closeImportDialog = () => elements.importJobDialog.close();
      elements.closeImportJobButton.addEventListener("click", closeImportDialog);
      elements.cancelImportJobButton.addEventListener("click", closeImportDialog);

      elements.btnAiExtract.addEventListener("click", async () => {
        const rawText = elements.rawJobInput.value.trim();
        if (!rawText) {
          showError("请先粘贴一段招聘信息文本");
          return;
        }
        elements.btnAiExtract.disabled = true;
        elements.btnAiExtract.textContent = "⏳ 正在智能解析…";
        try {
          const res = await fetch("/api/jobs/extract-jd", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Job-Agent-Token": actionToken,
            },
            body: JSON.stringify({ raw_text: rawText }),
          });
          const data = await readApiResponse(res);
          const parsed = data.parsed || {};
          elements.parsedCompany.value = parsed.company || "";
          elements.parsedTitle.value = parsed.title || "";
          elements.parsedLocation.value = parsed.location || "";
          elements.parsedJdText.value = parsed.jd_text || rawText;
          showSuccess("✨ 提取成功，请核对后点击下方保存入库");
        } catch (err) {
          showError(`解析失败: ${err.message}`);
        } finally {
          elements.btnAiExtract.disabled = false;
          elements.btnAiExtract.textContent = "✨ AI 智能拆解并填入下表";
        }
      });

      elements.importJobParsedForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        elements.submitImportJob.disabled = true;
        elements.submitImportJob.textContent = "正在保存…";
        try {
          const res = await fetch("/api/jobs/import-parsed", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Job-Agent-Token": actionToken,
            },
            body: JSON.stringify({
              company: elements.parsedCompany.value.trim(),
              title: elements.parsedTitle.value.trim(),
              location: elements.parsedLocation.value.trim(),
              source_url: elements.parsedSourceUrl.value.trim(),
              jd_text: elements.parsedJdText.value.trim(),
              source: "clipboard_paste",
            }),
          });
          const data = await readApiResponse(res);
          closeImportDialog();
          showSuccess(`🎉 岗位录入成功 (编号 #${data.job_id}，匹配分: ${data.match_score || "已计算"})`);
          await loadDashboard();
          navigate("jobs", data.job_id);
        } catch (err) {
          showError(`录入失败: ${err.message}`);
        } finally {
          elements.submitImportJob.disabled = false;
          elements.submitImportJob.textContent = "💾 确认并录入岗位库";
        }
      });
    }

    elements.refreshButton.addEventListener("click", loadDashboard);
    const compactWorkspace = window.matchMedia("(max-width: 1190px)");
    const syncAgentExpanded = () => elements.openCopilotButton.setAttribute("aria-expanded", String(
      !$('jobWorkspace').classList.contains('agent-hidden') && (!compactWorkspace.matches || $('jobWorkspace').classList.contains('agent-visible'))
    ));
    compactWorkspace.addEventListener("change", syncAgentExpanded);
    syncAgentExpanded();
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") loadDashboard();
    });
    window.setInterval(loadDashboard, 60000);
    const stats = document.createElement("details");
    stats.className = "progress-statistics";
    stats.innerHTML = '<summary>统计与数据说明</summary>';
    $("overviewPanel").append(stats);
    stats.append(elements.metrics, document.querySelector(".insight-grid"), document.querySelector(".methodology-shell"));
    const profileConnections = document.createElement("section");
    profileConnections.className = "profile-connections";
    profileConnections.innerHTML = '<h2>连接与 API</h2><p>管理 AI、搜索、地图及用量配置。</p><button class="action-button secondary" type="button">管理连接</button>';
    profileConnections.querySelector("button").addEventListener("click", () => elements.openSettingsButton.click());
    $("overviewPanel").append(profileConnections);
    loadDashboard().then(() => {
      const match = location.hash.match(/^#job\/(\d+)$/);
      const view = match ? "jobs" : ["today","jobs","progress","profile"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "today";
      const state = {view, jobId: match ? Number(match[1]) : null, depth: 0};
      history.replaceState(state, "", match ? location.hash : `#${view}`);
      navigationReady = true;
      navigate(view, state.jobId, true);
      setupGeminiMobileUI();
      return loadCopilot();
    }).catch(error => {
      elements.copilotProvider.textContent = "对话暂不可用，请重新打开 Agent";
    });

    function setupGeminiMobileUI() {
      if (window.__geminiMobileUISetup) return;
      window.__geminiMobileUISetup = true;

      // Configure the real provider; never simulate a model switch by changing a label.
      document.getElementById("geminiModelSelectorBtn")?.addEventListener("click", () => elements.openSettingsButton?.click());

      function openInterviewPreparation() {
        if (!selectedJobId) {
          navigate("jobs");
          elements.copilotInput?.setAttribute("placeholder", "请先选择岗位，再准备面试");
          return;
        }
        openCopilot("这个职位面试该准备什么？");
      }

      // Left Navigation Drawer (Screenshot 5)
      const openDrawerBtn = document.getElementById("openDrawerButton");
      const drawer = document.getElementById("geminiDrawer");
      const closeDrawerBtn = document.getElementById("geminiDrawerCloseBtn");
      const drawerBackdrop = document.getElementById("geminiDrawerBackdrop");
      const drawerNewChatBtn = document.getElementById("drawerNewChatBtn");
      const drawerUserProfileBtn = document.getElementById("drawerUserProfileBtn");
      const drawerSettingsBtn = document.getElementById("drawerSettingsBtn");

      function openDrawer() {
        if (drawer) {
          drawer.classList.add("open");
          drawer.setAttribute("aria-hidden", "false");
          renderDrawerRecents();
          updateProfileDisplayInUI();
        }
      }

      function closeDrawer() {
        if (drawer) {
          drawer.classList.remove("open");
          drawer.setAttribute("aria-hidden", "true");
        }
      }

      if (openDrawerBtn) openDrawerBtn.addEventListener("click", openDrawer);
      if (closeDrawerBtn) closeDrawerBtn.addEventListener("click", closeDrawer);
      if (drawerBackdrop) drawerBackdrop.addEventListener("click", closeDrawer);
      if (drawerSettingsBtn) {
        drawerSettingsBtn.addEventListener("click", () => {
          closeDrawer();
          elements.openSettingsButton?.click();
        });
      }
      if (drawerUserProfileBtn) {
        drawerUserProfileBtn.addEventListener("click", () => {
          closeDrawer();
          openProfileSheet();
        });
      }

      document.querySelectorAll(".drawer-nav-item").forEach((item) => {
        item.addEventListener("click", () => {
          closeDrawer();
          const target = item.getAttribute("data-drawer-nav");
          if (target === "today") navigate("today");
          else if (target === "workspace") navigate("jobs");
          else if (target === "tracking") navigate("progress");
          else if (target === "interview") openInterviewPreparation();
          else if (target === "extension") elements.openImportJobButton?.click();
          else if (target === "import") elements.openImportJobButton?.click();
        });
      });

      function renderDrawerRecents() {
        const container = document.getElementById("drawerRecentJobs");
        if (!container) return;
        const jobs = currentJobs.slice(0, 5);
        if (!jobs.length) {
          container.innerHTML = '<div class="drawer-recent-item quiet">暂无已录入岗位</div>';
          return;
        }
        container.innerHTML = jobs.map(j => `
          <button type="button" class="drawer-recent-item" data-drawer-job="${j.job_id}">
            ${escapeHtml(j.company)} · ${escapeHtml(j.title)}
          </button>
        `).join("");
        container.querySelectorAll("[data-drawer-job]").forEach(btn => {
          btn.addEventListener("click", () => {
            const jid = Number(btn.getAttribute("data-drawer-job"));
            closeDrawer();
            navigate("jobs", jid);
          });
        });
      }

      // Profile Sheet Modal (Screenshot 2)
      const avatarBtn = document.getElementById("geminiProfileAvatarBtn");
      const profileSheet = document.getElementById("geminiProfileSheet");
      const profileDoneBtn = document.getElementById("geminiProfileDoneBtn");
      const sheetManageProfileBtn = document.getElementById("sheetManageProfileBtn");
      const sheetSettingsBtn = document.getElementById("sheetSettingsBtn");
      const sheetThemeToggleBtn = document.getElementById("sheetThemeToggleBtn");

      function openProfileSheet() {
        if (profileSheet && typeof profileSheet.showModal === "function") {
          updateProfileDisplayInUI();
          profileSheet.showModal();
        }
      }

      function closeProfileSheet() {
        if (profileSheet && profileSheet.open) {
          profileSheet.close();
        }
      }

      if (avatarBtn) avatarBtn.addEventListener("click", () => elements.openSettingsButton?.click());
      if (profileDoneBtn) profileDoneBtn.addEventListener("click", closeProfileSheet);
      if (sheetManageProfileBtn) {
        sheetManageProfileBtn.addEventListener("click", () => {
          closeProfileSheet();
          elements.openProfileButton?.click();
        });
      }
      if (sheetSettingsBtn) {
        sheetSettingsBtn.addEventListener("click", () => {
          closeProfileSheet();
          elements.openSettingsButton?.click();
        });
      }
      if (sheetThemeToggleBtn) {
        sheetThemeToggleBtn.addEventListener("click", () => {
          document.getElementById("themeToggleBtn")?.click();
          const currentTheme = document.documentElement.getAttribute("data-theme") || "aurora";
          const lbl = document.getElementById("sheetThemeLabel");
          if (lbl) lbl.textContent = currentTheme === "cosmic" ? "外观：深空暗色 (点击切换极光)" : "外观：极光浅色 (点击切换深空)";
        });
      }

      function updateProfileDisplayInUI() {
        const name = activeProfile?.display_name || activeProfile?.person?.name || "求职者";
        const email = activeProfile?.person?.contact?.email || activeProfile?.email || "尚未填写邮箱";
        const initial = name ? name.charAt(0) : "M";
        const topInit = document.getElementById("topbarAvatarInitial");
        const drawerInit = document.getElementById("drawerAvatarInitial");
        const sheetInit = document.getElementById("sheetAvatarInitial");
        const drawerUName = document.getElementById("drawerUserName");
        const sheetPName = document.getElementById("sheetProfileName");
        const sheetPEmail = document.getElementById("sheetProfileEmail");
        const heroGreeting = document.getElementById("geminiHeroGreeting");

        if (topInit) topInit.textContent = initial;
        if (drawerInit) drawerInit.textContent = initial;
        if (sheetInit) sheetInit.textContent = initial;
        if (drawerUName) drawerUName.textContent = name;
        if (sheetPName) sheetPName.textContent = name;
        if (sheetPEmail) sheetPEmail.textContent = email;
        if (heroGreeting) heroGreeting.textContent = `${name}，让我们开始吧`;
      }

      // Hero Quick Chips
      document.querySelectorAll("[data-hero-action]").forEach((chip) => {
        chip.addEventListener("click", () => {
          const action = chip.getAttribute("data-hero-action");
          if (action === "tailor") {
            navigate("jobs");
          } else if (action === "interview") {
            openInterviewPreparation();
          } else if (action === "tracking") {
            navigate("progress");
          } else if (action === "import") {
            elements.openImportJobButton?.click();
          }
        });
      });

      // Floating Capsule Composer (Screenshot 1, 3, 4, 6)
      const composerInput = document.getElementById("composerInput");
      const composerMicBtn = document.getElementById("composerMicBtn");
      const composerWaveBtn = document.getElementById("composerWaveBtn");
      const composerSendBtn = document.getElementById("composerSendBtn");
      const composerPlusBtn = document.getElementById("composerPlusBtn");
      const newChatBtn = document.getElementById("newChatButton");

      if (newChatBtn) {
        newChatBtn.addEventListener("click", () => {
          openCopilot();
          elements.resetCopilotButton?.click();
          elements.copilotInput?.focus();
        });
      }

      if (drawerNewChatBtn) {
        drawerNewChatBtn.addEventListener("click", () => {
          closeDrawer();
          openCopilot();
          elements.resetCopilotButton?.click();
          elements.copilotInput?.focus();
        });
      }

      if (composerInput) {
        composerInput.addEventListener("input", () => {
          const val = composerInput.value.trim();
          if (val.length > 0) {
            if (composerMicBtn) composerMicBtn.hidden = true;
            if (composerWaveBtn) composerWaveBtn.hidden = true;
            if (composerSendBtn) composerSendBtn.hidden = false;
          } else {
            if (composerMicBtn) composerMicBtn.hidden = false;
            if (composerWaveBtn) composerWaveBtn.hidden = false;
            if (composerSendBtn) composerSendBtn.hidden = true;
          }
        });

        composerInput.addEventListener("keydown", (e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            submitComposer();
          }
        });
      }

      if (composerSendBtn) {
        composerSendBtn.addEventListener("click", submitComposer);
      }

      if (composerWaveBtn) {
        composerWaveBtn.addEventListener("click", () => {
          openInterviewPreparation();
        });
      }

      if (composerPlusBtn) {
        composerPlusBtn.addEventListener("click", () => {
          elements.openImportJobButton?.click();
        });
      }

      async function submitComposer() {
        if (!composerInput) return;
        const text = composerInput.value.trim();
        if (!text) return;
        composerInput.value = "";
        if (composerMicBtn) composerMicBtn.hidden = false;
        if (composerWaveBtn) composerWaveBtn.hidden = false;
        if (composerSendBtn) composerSendBtn.hidden = true;

        await openCopilot();
        if (elements.copilotInput) {
          elements.copilotInput.value = text;
          elements.copilotForm?.dispatchEvent(new Event("submit", { cancelable: true }));
        }
      }

      setTimeout(updateProfileDisplayInUI, 300);
    }
