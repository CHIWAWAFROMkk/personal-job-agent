import { fetchLocal, readApiResponse } from './api.js';
import { escapeHtml, safeExternalUrl } from './utils.js';

export function isVersionComplete(version) {
  return Boolean(
    version &&
    version.pdf_sha256 &&
    /^[0-9A-Fa-f]{64}$/.test(version.pdf_sha256) &&
    version.manifest_path
  );
}

export function createReviewDialog({
  $,
  getJobs,
  postLocalJson,
  showError,
  showSuccess,
  loadDashboard,
}) {
  let reviewSessionSeq = 0;

  async function openReviewPanel(jobId) {
    const dialog = $("reviewDialog");
    if (!dialog) return;
    const currentJobs = typeof getJobs === "function" ? getJobs() : [];
    const job = currentJobs.find(j => j.job_id === Number(jobId));
    const hasJobPage = Boolean(safeExternalUrl(job?.source_url));
    const reviewLabel = hasJobPage ? "审阅并打开招聘页" : "审阅投递材料";
    const approveLabel = (facts = false) => `${facts ? "已核对事实与版面" : "已核对版面"}，${hasJobPage ? "批准并打开招聘页" : "批准投递材料"}`;
    const titleEl = $("reviewDialogTitle");
    if (titleEl) {
      titleEl.textContent = `${reviewLabel} — ${job?.title || `岗位 #${jobId}`}`;
    }
    const copyEl = $("reviewDialogCopy");
    if (copyEl) copyEl.textContent = hasJobPage
      ? "在同一面板核对 PDF 简历与打招呼语。确认后打开招聘页，并在资源管理器中定位简历。"
      : "核对 PDF 简历与打招呼语后批准材料。该岗位没有招聘链接，请自行找到申请入口；批准不会记录为已投递。";

    const currentSeq = ++reviewSessionSeq;
    let reviewVersion = null;

    const greetingsList = $("reviewGreetingsList");
    const openBtn = $("reviewOpenPageBtn");
    const revealBtn = $("reviewRevealBtn");
    const appliedBtn = $("reviewMarkAppliedBtn");
    const pdfFrame = $("reviewPdfFrame");
    const pdfLink = $("reviewPdfLink");
    const factSection = $("reviewFactSection");
    const factList = $("reviewFactList");
    const factConfirm = $("reviewFactConfirm");
    let factReviewRequired = false;
    let factComparisonsReady = false;

    if (openBtn) {
      openBtn.disabled = true;
      openBtn.textContent = approveLabel();
    }
    if (revealBtn) revealBtn.disabled = true;
    if (appliedBtn) appliedBtn.disabled = true;
    if (pdfFrame) pdfFrame.src = "about:blank";
    if (pdfLink) { pdfLink.hidden = true; pdfLink.removeAttribute?.("href"); }
    if (factSection) factSection.hidden = true;
    if (factConfirm) factConfirm.checked = false;

    const closeBtn = $("closeReviewButton");
    if (closeBtn) closeBtn.onclick = () => dialog.close?.();
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute?.("open", "");

    const loadVersionAndGreetings = async () => {
      if (greetingsList) {
        greetingsList.innerHTML = '<p class="quiet">正在读取招呼语与版本信息…</p>';
      }
      if (openBtn) openBtn.disabled = true;
      if (revealBtn) revealBtn.disabled = true;
      if (factSection) factSection.hidden = true;
      if (factConfirm) factConfirm.checked = false;
      if (pdfLink) { pdfLink.hidden = true; pdfLink.removeAttribute?.("href"); }
      factReviewRequired = false;
      factComparisonsReady = false;

      try {
        const detail = await readApiResponse(await fetchLocal(`/api/jobs/${encodeURIComponent(jobId)}/detail`));
        if (currentSeq !== reviewSessionSeq) return; // 废弃过期请求结果

        const version = detail?.resume_version;
        if (!isVersionComplete(version)) {
          if (pdfFrame) pdfFrame.src = "about:blank";
          if (greetingsList) {
            greetingsList.innerHTML = `
              <div class="banner warning" style="margin: 8px 0; padding: 8px 12px; border-radius: 6px; background: rgba(234, 179, 8, 0.1); border: 1px solid rgba(234, 179, 8, 0.3);">
                <p style="margin: 0 0 6px 0;">⚠️ 尚未获取到当前岗位的有效简历版本或版本信息不完整。请先在工作台点击【准备投递】生成专属材料。</p>
                <button class="action-button secondary" type="button" id="reviewRetryBtn" style="padding: 4px 10px; font-size: 12px;">🔄 重新加载版本信息</button>
              </div>`;
            $("reviewRetryBtn")?.addEventListener("click", () => loadVersionAndGreetings());
          }
          if (openBtn) openBtn.disabled = true;
          if (revealBtn) revealBtn.disabled = true;
          if (appliedBtn) appliedBtn.disabled = false;
          return;
        }

        reviewVersion = version;
        factReviewRequired = version.fact_review_required === true;
        const comparisons = Array.isArray(version.fact_comparisons) ? version.fact_comparisons : [];
        factComparisonsReady = factReviewRequired && comparisons.length > 0
          && /^[0-9A-Fa-f]{64}$/.test(version.content_sha256 || "")
          && !version.fact_review_error;
        if (factSection) factSection.hidden = !factReviewRequired;
        if (factList) {
          factList.innerHTML = factReviewRequired
            ? (factComparisonsReady
              ? comparisons.map((row, index) => `<div class="review-fact-pair"><strong>${index + 1}. ${escapeHtml(row.label || "事实对照")}</strong><p><b>原句：</b>${escapeHtml(row.source || "")}</p><p><b>当前改写：</b>${escapeHtml(row.draft || "")}</p></div>`).join("")
              : `<p class="quiet">${escapeHtml(version.fact_review_error || "事实对照暂不可用，请重新生成草稿后再确认。")}</p>`)
            : "";
        }
        if (factConfirm) {
          factConfirm.disabled = !factComparisonsReady;
          factConfirm.checked = false;
          factConfirm.onchange = () => {
            if (currentSeq === reviewSessionSeq && openBtn) {
              openBtn.disabled = factReviewRequired && (!factComparisonsReady || !factConfirm.checked);
            }
          };
        }
        // 绑定版本哈希到 URL，确保预览所见即所审
        if (pdfFrame) {
          pdfFrame.src = `/resume-draft/${encodeURIComponent(jobId)}/pdf?v=${encodeURIComponent(reviewVersion.pdf_sha256)}`;
        }
        if (pdfLink) {
          pdfLink.href = `/resume-draft/${encodeURIComponent(jobId)}/pdf?v=${encodeURIComponent(reviewVersion.pdf_sha256)}`;
          pdfLink.hidden = false;
        }

        const currentJob = (typeof getJobs === "function" ? getJobs() : []).find(j => String(j.job_id) === String(jobId));
        const isApproved = detail?.workspace?.resume_status === "ready"
          || currentJob?.workspace?.resume_status === "ready"
          || detail?.resume_version?.qa?.pdf_visual_review === "passed";
        const isPackReady = Boolean(detail?.application_pack_ready ?? currentJob?.workspace?.application_pack_ready);
        if (isApproved && !isPackReady && openBtn) {
          openBtn.textContent = "🔄 重试材料包同步与打开";
        } else if (openBtn) {
          openBtn.textContent = approveLabel(factReviewRequired);
        }

        const greetings = detail.greetings || [];
        if (greetingsList) {
          if (greetings.length) {
            greetingsList.innerHTML = greetings.map((g, idx) => {
              const content = typeof g === "object" ? (g.content || g.text || "") : String(g);
              const title = typeof g === "object" ? (g.title || `招呼语 ${idx + 1}`) : `招呼语 ${idx + 1}`;
              return `<div class="review-greeting-card">
                <div class="review-greeting-head">
                  <strong>${escapeHtml(title)}</strong>
                  <button class="action-button secondary" type="button" style="padding:4px 10px;font-size:12px;" data-copy-review-greeting="${idx}">📋 复制</button>
                </div>
                <p id="reviewGreetingText_${idx}" class="review-greeting-text">${escapeHtml(content)}</p>
              </div>`;
            }).join("");
            greetingsList.querySelectorAll("[data-copy-review-greeting]").forEach(btn => {
              btn.addEventListener("click", () => {
                const idx = btn.dataset.copyReviewGreeting;
                const text = $(`reviewGreetingText_${idx}`)?.innerText || "";
                navigator?.clipboard?.writeText?.(text)?.then?.(() => {
                  const old = btn.textContent; btn.textContent = "✅ 已复制";
                  setTimeout(() => { btn.textContent = old; }, 2000);
                })?.catch?.(() => showError?.("复制失败，请手动选中文字复制。"));
              });
            });
          } else {
            greetingsList.innerHTML = '<p class="quiet">暂无预生成招呼语。可关闭后点击「💬 打招呼语」单独生成。</p>';
          }
        }

        if (openBtn) openBtn.disabled = factReviewRequired;
        if (revealBtn) revealBtn.disabled = false;
        if (appliedBtn) appliedBtn.disabled = false;
      } catch (err) {
        if (currentSeq !== reviewSessionSeq) return;
        if (greetingsList) {
          greetingsList.innerHTML = `
            <div class="banner error" style="margin: 8px 0; padding: 8px 12px; border-radius: 6px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3);">
              <p style="margin: 0 0 6px 0;">版本信息与招呼语读取失败：${escapeHtml(err.message || String(err))}</p>
              <button class="action-button secondary" type="button" id="reviewRetryBtn" style="padding: 4px 10px; font-size: 12px;">🔄 重新加载</button>
            </div>`;
          $("reviewRetryBtn")?.addEventListener("click", () => loadVersionAndGreetings());
        }
        if (openBtn) openBtn.disabled = true;
        if (revealBtn) revealBtn.disabled = true;
        if (appliedBtn) appliedBtn.disabled = true;
      }
    };

    await loadVersionAndGreetings();
    // 关键：await 返回后必须再次检查会话是否有效，防止旧请求覆盖新岗位按钮！
    if (currentSeq !== reviewSessionSeq) return;

    if (openBtn) {
      openBtn.onclick = async () => {
        if (currentSeq !== reviewSessionSeq) return;
        if (!isVersionComplete(reviewVersion)) {
          showError?.("当前未加载到有效的简历版本信息，无法确认。请点击重新加载。");
          return;
        }
        if (factReviewRequired && (!factComparisonsReady || !factConfirm?.checked)) {
          showError?.("请先逐项核对原句和云端改写，并单独确认事实。 ");
          return;
        }
        openBtn.disabled = true;
        try {
          const body = {
            expected_sha256: reviewVersion.pdf_sha256,
            expected_version_id: reviewVersion.version_id || "",
            manifest_path: reviewVersion.manifest_path
          };
          if (factReviewRequired) {
            body.confirmed_fact_review = true;
            body.fact_review_sha256 = reviewVersion.pdf_sha256;
            body.fact_review_content_sha256 = reviewVersion.content_sha256;
          }
          const res = await postLocalJson(`/api/jobs/${encodeURIComponent(jobId)}/review-and-open`, body);
          if (currentSeq !== reviewSessionSeq) return;

          const problems = [];
          if (!res.pdf_opened) problems.push(`PDF阅读器无法启动${res.pdf_error ? `(${res.pdf_error})` : ""}`);
          if (hasJobPage && !res.page_opened) problems.push(`招聘网页无法打开${res.page_error ? `(${res.page_error})` : ""}`);
          if (!res.revealed) problems.push(`文件夹定位未完成${res.reveal_error ? `(${res.reveal_error})` : ""}`);

          if (problems.length === 0) {
            showSuccess?.(hasJobPage
              ? "招聘页与 PDF 已打开，并在资源管理器中选中专属简历。登录与最终提交由你完成。"
              : "投递材料已批准，PDF 与文件夹已打开。该岗位没有招聘链接，请自行找到申请入口；尚未记录为已投递。");
          } else if (res.page_opened || res.pdf_opened || res.revealed) {
            showSuccess?.(`部分应用已打开（注意：${problems.join("；")}）。登录与最终提交由你完成。`);
          } else {
            showError?.(`未能自动打开外部应用（${problems.join("；")}），请手动在 output 目录查看简历并打开岗位。`);
          }
          await loadDashboard?.();
          if (currentSeq !== reviewSessionSeq) return;
          if (openBtn) openBtn.textContent = approveLabel(factReviewRequired);
          dialog.close?.();
        } catch (err) {
          if (currentSeq !== reviewSessionSeq) return;
          const isPartial = Boolean(err?.data?.visual_review_approved);
          const rawErr = err?.data?.error || err?.message || String(err);
          const errorMsg = isPartial
            ? `部分完成（审批已落盘）：${rawErr}`
            : `打开失败：${rawErr}`;
          showError?.(errorMsg);
          openBtn.disabled = false;
          if (isPartial) {
            if (openBtn) {
              openBtn.textContent = "🔄 重试材料包同步与打开";
            }
            if (greetingsList) {
              const existingBanner = greetingsList.querySelector(".review-status-banner");
              if (existingBanner) existingBanner.remove();
              const banner = document.createElement("div");
              banner.className = "banner warning review-status-banner";
              banner.style.cssText = "margin: 8px 0; padding: 10px 14px; border-radius: 6px; background: rgba(234, 179, 8, 0.12); border: 1px solid rgba(234, 179, 8, 0.4);";
              banner.innerHTML = `
                <div style="font-weight: 600; margin-bottom: 6px; color: #b45309;">⚠️ 步骤执行状态：部分完成（审批已落盘）</div>
                <ul style="margin: 0 0 8px 0; padding-left: 20px; font-size: 13px; line-height: 1.6;">
                  <li><strong style="color: #15803d;">✅ 第一步：简历版面人工审阅</strong> — 已核验通过并落盘保存。</li>
                  <li><strong style="color: #b91c1c;">❌ 第二步：投递材料包同步</strong> — 未完成（${escapeHtml(rawErr)}）。</li>
                  <li><strong style="color: #6b7280;">⏸️ 第三步：打开外部应用与文件</strong> — 等待材料包生成完成后自动触发。</li>
                </ul>
                <p style="margin: 0; font-size: 12px; color: #78350f;">提示：请根据提示解除文件占用或检查写入权限后，直接点击下方【重试材料包同步与打开】重试，无需重复审阅。</p>`;
              greetingsList.prepend(banner);
            }
            await loadDashboard?.({ preserveAlert: true });
            if (currentSeq !== reviewSessionSeq) return;
          }
          if (currentSeq !== reviewSessionSeq) return;
          if (String(err?.message || err).includes("简历版本已更新") || String(err?.message || err).includes("409") || err?.status === 409) {
            await loadVersionAndGreetings();
          }
        }
      };
    }

    if (revealBtn) {
      // 1. 定位文件夹按钮调用专门的定位接口，绝对不调用 review-and-open，避免顺带批准简历！
      revealBtn.onclick = async () => {
        if (currentSeq !== reviewSessionSeq) return;
        try {
          const res = await postLocalJson(`/api/jobs/${encodeURIComponent(jobId)}/reveal-resume`, {});
          if (currentSeq !== reviewSessionSeq) return;
          if (res.revealed) {
            showSuccess?.("简历文件夹已在资源管理器中选中。");
          } else {
            showError?.(`未能调起资源管理器定位文件${res.reveal_error ? `（${res.reveal_error}）` : ""}，请在 output 目录查看。`);
          }
        } catch (err) {
          if (currentSeq !== reviewSessionSeq) return;
          showError?.(`定位失败：${err?.message || err}`);
        }
      };
    }

    if (appliedBtn) {
      appliedBtn.onclick = async () => {
        if (currentSeq !== reviewSessionSeq) return;
        if (typeof window !== "undefined" && window.confirm && !window.confirm(`确认你已在招聘网站完成提交，将岗位 #${jobId} 标记为「已投递」？`)) return;
        appliedBtn.disabled = true;
        try {
          await postLocalJson(`/api/applications/${encodeURIComponent(jobId)}/status`, {
            status: "applied", detail: "本人手动登记：已在招聘网站提交申请。", confirmed: true,
          });
          if (currentSeq !== reviewSessionSeq) return;
          await loadDashboard?.();
          if (currentSeq !== reviewSessionSeq) return;
          showSuccess?.(`岗位 #${jobId} 已标记为已投递。`);
          dialog.close?.();
        } catch (err) {
          if (currentSeq !== reviewSessionSeq) return;
          showError?.(`标记失败：${err?.message || err}`);
          appliedBtn.disabled = false;
        }
      };
    }
  }

  return { openReviewPanel };
}
