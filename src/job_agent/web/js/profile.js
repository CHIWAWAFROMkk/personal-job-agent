import { elements } from './elements.js';
import { escapeHtml, joinValues } from './utils.js';

export function renderProfile(profile) {
  if (!profile) {
    elements.profileSummary.querySelector(".profile-main").innerHTML = `
      <div class="profile-name">还没有建立个人求职资料</div>
      <div class="profile-meta">上传简历并填写目标岗位后，Agent 才能按照这个人的情况搜索和匹配。</div>`;
    elements.openProfileButton.textContent = "建立个人资料";
    elements.preferenceRoles.textContent = "先建立个人资料";
    elements.preferenceScope.textContent = "填写目标岗位和工作城市后，Agent 才能定向搜索。";
    elements.preferenceCommute.textContent = "通勤边界尚未设置";
    elements.openPreferencesButton.disabled = true;
    return;
  }
  elements.openPreferencesButton.disabled = false;
  const tags = [
    ...(profile.target_roles || []),
    ...(profile.preferred_locations || []),
    ...(profile.employment_types || [])
  ].slice(0, 8);
  const identity = profile.display_name || "当前用户";
  const stage = profile.stage || "求职阶段未填写";
  const availability = profile.availability || "到岗条件未填写";
  const commute = profile.max_one_way_minutes
    ? `通勤：${profile.commute_origin || "起点未填写"} · 单程 ≤ ${profile.max_one_way_minutes} 分钟`
    : "通勤上限未填写";
  elements.profileSummary.querySelector(".profile-main").innerHTML = `
    <div class="profile-name">${escapeHtml(identity)} · 简历与经历</div>
    <div class="profile-meta">${escapeHtml(profile.resume_count || 0)} 份简历 · ${escapeHtml(profile.confirmed_fact_count || 0)} 条已确认事实</div>`;
  elements.openProfileButton.textContent = "管理简历";
  const roleText = [...(profile.target_roles || []), ...(profile.adjacent_roles || [])].join(" / ") || "目标岗位未填写";
  const scopeParts = [
    (profile.target_industries || []).join("、"),
    (profile.preferred_locations || []).join("、"),
    (profile.employment_types || []).join("、"),
    profile.internship_daily_pay_floor !== null && profile.internship_daily_pay_floor !== undefined
      ? `实习日薪 ≥ ${profile.internship_daily_pay_floor} 元`
      : "实习薪资待确认",
    profile.exclude_outsourcing ? "不接受外包" : "外包人工判断"
  ].filter(Boolean);
  elements.preferenceRoles.textContent = roleText;
  elements.preferenceScope.textContent = scopeParts.join(" · ") || "行业与地点边界尚未完整填写";
  const modeText = (profile.transport_modes || []).join("、") || "交通方式不限";
  elements.preferenceCommute.textContent = profile.max_one_way_minutes
    ? `${profile.commute_origin || "出发地址未填"} → 单程 ≤ ${profile.max_one_way_minutes} 分钟`
    : `${profile.commute_origin || "出发地址未填"} · 单程上限未设置`;
  elements.preferenceCommute.title = `${modeText}${profile.remote_acceptable ? " · 接受远程" : ""}`;
}

export function prefillProfileForm(activeProfile) {
  elements.profileForm.reset();
  elements.profileMode.value = "update";
  elements.replaceConfirm.classList.remove("visible");
  const replaceCheckbox = elements.replaceConfirm.querySelector('input[name="confirm_replace"]');
  replaceCheckbox.required = false;
  syncProfileRequirements(activeProfile);
  if (!activeProfile) return;
  const values = {
    displayName: activeProfile.display_name,
    stage: activeProfile.stage,
    targetRoles: joinValues(activeProfile.target_roles),
    adjacentRoles: joinValues(activeProfile.adjacent_roles),
    targetIndustries: joinValues(activeProfile.target_industries),
    preferredLocations: joinValues(activeProfile.preferred_locations),
    employmentTypes: joinValues(activeProfile.employment_types) || "实习",
    mustHaves: joinValues(activeProfile.must_haves),
    avoid: joinValues(activeProfile.avoid),
    earliestStart: activeProfile.earliest_start,
    daysPerWeek: activeProfile.days_per_week,
    durationMonths: activeProfile.duration_months,
    commuteOrigin: activeProfile.commute_origin,
    maxCommuteMinutes: activeProfile.max_one_way_minutes,
    transportModes: joinValues(activeProfile.transport_modes)
  };
  Object.entries(values).forEach(([id, value]) => {
    const field = document.getElementById(id);
    if (field && value !== null && value !== undefined) field.value = value;
  });
  document.getElementById("remoteAcceptable").checked = Boolean(activeProfile.remote_acceptable);
}

export function syncProfileRequirements(activeProfile) {
  const replacing = elements.profileMode.value === "replace";
  const hasResume = Boolean(elements.resumeFile.files?.length);
  elements.resumeFile.required = replacing || !activeProfile;
  elements.confirmTruth.required = hasResume || replacing || !activeProfile;
  if (!elements.confirmTruth.required) elements.confirmTruth.checked = false;
}

export function prefillPreferencesForm(activeProfile) {
  if (!activeProfile) return;
  elements.preferenceTargetRoles.value = joinValues(activeProfile.target_roles);
  elements.preferenceAdjacentRoles.value = joinValues(activeProfile.adjacent_roles);
  elements.preferenceIndustries.value = joinValues(activeProfile.target_industries);
  elements.preferenceLocations.value = joinValues(activeProfile.preferred_locations);
  elements.preferenceEmploymentTypes.value = joinValues(activeProfile.employment_types);
  elements.preferenceMustHaves.value = joinValues(activeProfile.must_haves);
  elements.preferenceAvoid.value = joinValues(activeProfile.avoid);
  elements.preferenceCommuteOrigin.value = activeProfile.commute_origin || "";
  elements.preferenceMaxCommute.value = activeProfile.max_one_way_minutes || "";
  elements.preferenceTransportModes.value = joinValues(activeProfile.transport_modes);
  elements.preferenceRemote.checked = Boolean(activeProfile.remote_acceptable);
  elements.preferenceDailyPayFloor.value = activeProfile.internship_daily_pay_floor ?? "";
  elements.preferenceExcludeOutsourcing.checked = Boolean(activeProfile.exclude_outsourcing);
}

