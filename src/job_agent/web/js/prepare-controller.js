import { escapeHtml } from './utils.js';

/**
 * 管理岗位投递材料准备（匹配分析、定向简历、打招呼语与材料包）的执行状态与界面交互。
 * 针对网络延时期间页面被后台刷新重绘的场景，在 catch/finally 中始终动态寻址当前 DOM 节点，
 * 保证错误提示与重试按钮准确落入当前视口，避免写入已被卸载的孤儿节点。
 */
export function createPrepareController({
  getActiveProfile,
  activePreparingJobIds,
  postLocalJson,
  loadDashboard,
  showError,
  showSuccess,
  openProfile,
  onPrepared,
  getSelectedJobId,
  getDocument = () => globalThis.document,
}) {
  const ownsVisibleDetail = (jobId) => typeof getSelectedJobId !== "function"
    || Number(getSelectedJobId()) === Number(jobId);

  async function startPrepare(jobId) {
    const profile = typeof getActiveProfile === "function" ? getActiveProfile() : getActiveProfile;
    if (!profile?.confirmed_fact_count) {
      showError?.("请先在个人资料中录入并确认真实经历，再准备投递材料。照片可选。");
      openProfile?.();
      return;
    }

    if (activePreparingJobIds?.has(Number(jobId))) return;
    const doc = getDocument();
    activePreparingJobIds?.add(Number(jobId));

    const initialProgressEl = ownsVisibleDetail(jobId) ? doc?.getElementById("prepareProgress") : null;
    const initialPrimaryBtn = ownsVisibleDetail(jobId) ? doc?.querySelector?.('[data-detail-action="prepare"]') : null;
    if (initialProgressEl) {
      initialProgressEl.hidden = false;
      initialProgressEl.innerHTML = `
        <div class="prepare-indicator">
          <span class="prepare-spinner" aria-hidden="true"></span>
          <span>正在准备投递材料（匹配分析、专属简历、打招呼语与材料包）… 请留在页面稍候</span>
        </div>`;
    }
    if (initialPrimaryBtn) {
      initialPrimaryBtn.disabled = true;
      initialPrimaryBtn.textContent = "准备中…";
    }

    showSuccess?.(`正在为岗位 #${jobId} 准备投递材料，请稍候。`);

    try {
      const result = await postLocalJson(`/api/jobs/${encodeURIComponent(jobId)}/prepare-apply`, {});
      activePreparingJobIds?.delete(Number(jobId));

      const liveDoc = typeof getDocument === "function" ? getDocument() : globalThis.document;
      const currentProgressEl = ownsVisibleDetail(jobId) ? liveDoc?.getElementById("prepareProgress") : null;
      if (currentProgressEl) currentProgressEl.hidden = true;

      try {
        await loadDashboard?.();
      } catch {
        showError?.(`岗位 #${jobId} 的材料已准备好，但列表刷新失败。请刷新页面查看结果，不要重复准备。`);
        return;
      }
      const note = result?.resume_reused ? "" : "（已生成新版简历）";
      const commuteMsg = result?.commute_fit === "unknown" ? " ⚠️ 通勤路线尚未核实，未自动视为合格。" : "";
      showSuccess?.(`岗位 #${jobId} 材料已就绪${note}。${commuteMsg}点击「审阅并打开招聘页」核对。`);

      if (ownsVisibleDetail(jobId) && typeof onPrepared === "function") {
        try {
          await onPrepared(jobId);
        } catch {
          // Dialog failure will not block dashboard state
        }
      }
    } catch (error) {
      activePreparingJobIds?.delete(Number(jobId));
      const msg = error?.message || String(error);
      const stageName = error?.data?.failed_stage_name || "";
      const stagePrefix = stageName ? `（失败环节：${escapeHtml(stageName)}）` : "";

      // 关键修复：刷新重绘后，动态寻址当前活跃在 DOM 中的 live 节点，绝不写入已脱落的 initialProgressEl
      const liveDoc = typeof getDocument === "function" ? getDocument() : globalThis.document;
      const currentProgressEl = ownsVisibleDetail(jobId) ? liveDoc?.getElementById("prepareProgress") : null;
      if (currentProgressEl && (currentProgressEl.isConnected === undefined || currentProgressEl.isConnected)) {
        currentProgressEl.hidden = false;
        currentProgressEl.innerHTML = `
          <p class="prepare-error">准备材料未完成${stagePrefix}：${escapeHtml(msg)}</p>
          <button class="action-button secondary prepare-retry" type="button">重试</button>`;
        const retryBtn = currentProgressEl.querySelector?.(".prepare-retry");
        if (retryBtn) {
          retryBtn.addEventListener("click", () => startPrepare(jobId));
        }
      }
      showError?.(`岗位 #${jobId} 准备材料未完成${stagePrefix}：${msg}`);
    } finally {
      activePreparingJobIds?.delete(Number(jobId));
      const liveDoc = typeof getDocument === "function" ? getDocument() : globalThis.document;
      const currentPrimaryBtn = ownsVisibleDetail(jobId) ? liveDoc?.querySelector?.('[data-detail-action="prepare"]') : null;
      if (currentPrimaryBtn && (currentPrimaryBtn.isConnected === undefined || currentPrimaryBtn.isConnected)) {
        currentPrimaryBtn.disabled = false;
        currentPrimaryBtn.textContent = "准备投递";
      }
    }
  }

  return { startPrepare };
}
