const RECOVERY_MESSAGE = "页面操作遇到异常。请刷新后核对结果；若已生效，不要重复提交。仍无法继续时，请重新启动程序。";

export function installGlobalFeedback(target, showError) {
  target.addEventListener("unhandledrejection", event => {
    if (event.reason?.name !== "AbortError") showError(RECOVERY_MESSAGE);
  });
  target.addEventListener("error", event => {
    // Resource load failures do not have an Error object and are handled by
    // their own UI. Surface uncaught script exceptions instead.
    if (event.target === target && event.error) showError(RECOVERY_MESSAGE);
  });
}
