from __future__ import annotations

import json
import os
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "work" / "dashboard-qa"
URL = os.environ.get("DASHBOARD_QA_URL", "http://127.0.0.1:8787/")


def wait_until(page, expression: str, timeout_ms: int = 10_000, label: str = "") -> None:
    # wait_for_function uses in-page eval(), which the dashboard CSP forbids
    # (script-src lacks 'unsafe-eval'); page.evaluate goes through CDP and is
    # not affected. Poll from Python instead.
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if page.evaluate(expression):
            return
        page.wait_for_timeout(200)
    raise RuntimeError(f"QA 等待超时: {label or expression}")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"url": URL, "viewports": {}}
    with sync_playwright() as playwright:
        browser = None
        launch_errors: list[str] = []
        for channel in (None, "msedge", "chrome"):
            try:
                browser = playwright.chromium.launch(channel=channel, headless=True)
                report["browser_channel"] = channel or "playwright-chromium"
                break
            except PlaywrightError as exc:
                launch_errors.append(f"{channel or 'playwright-chromium'}: {exc}")
        if browser is None:
            raise RuntimeError("No supported browser found:\n" + "\n".join(launch_errors))
        for name, width, height, reduced_motion in (
            ("desktop", 1440, 1100, False),
            ("mobile", 375, 812, False),
            ("landscape", 844, 390, True),
        ):
            page = browser.new_page(viewport={"width": width, "height": height})
            if reduced_motion:
                page.emulate_media(reduced_motion="reduce")
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            response = page.goto(URL, wait_until="networkidle", timeout=15_000)
            page.locator("#systemState").wait_for(state="attached")
            wait_until(
                page,
                "document.querySelector('#systemState') && document.querySelector('#systemState').textContent.includes('已连接')",
                label="systemState 已连接",
            )
            overflow = page.evaluate(
                "document.documentElement.scrollWidth > document.documentElement.clientWidth"
            )
            screenshot = OUTPUT / f"dashboard-{name}.png"
            page.screenshot(path=str(screenshot), full_page=True)
            viewport_screenshot = OUTPUT / f"dashboard-{name}-viewport.png"
            page.screenshot(path=str(viewport_screenshot), full_page=False)
            metrics_screenshot = OUTPUT / f"dashboard-{name}-metrics.png"
            page.locator("#metrics").screenshot(path=str(metrics_screenshot))
            jobs_screenshot = OUTPUT / f"dashboard-{name}-jobs.png"
            page.locator(".area-jobs").screenshot(path=str(jobs_screenshot))
            commute_screenshot = OUTPUT / f"dashboard-{name}-commute.png"
            page.locator(".area-commute").screenshot(path=str(commute_screenshot))
            page.locator("#openCopilotButton").click()
            page.locator("#copilotDialog").wait_for(state="visible")
            wait_until(
                page,
                "document.querySelector('#copilotSend') && !document.querySelector('#copilotSend').disabled",
                label="copilotSend 可用",
            )
            copilot_dialog_radius = page.locator("#copilotDialog").evaluate(
                "element => getComputedStyle(element).borderRadius"
            )
            copilot_input_radius = page.locator("#copilotInput").evaluate(
                "element => getComputedStyle(element).borderRadius"
            )
            copilot_safety_visible = page.locator("#copilotDialog").get_by_text(
                "最终提交始终由本人完成"
            ).is_visible()
            copilot_screenshot = OUTPUT / f"dashboard-{name}-copilot.png"
            page.screenshot(path=str(copilot_screenshot), full_page=False)
            page.locator("#closeCopilotButton").click()
            page.locator("#openSettingsButton").click()
            page.locator("#settingsDialog").wait_for(state="visible")
            settings_dialog_radius = page.locator("#settingsDialog").evaluate(
                "element => getComputedStyle(element).borderRadius"
            )
            settings_field_radius = page.locator(
                "#settingsDialog .field input, #settingsDialog .field select, #settingsDialog .field textarea"
            ).first.evaluate("element => getComputedStyle(element).borderRadius")
            settings_screenshot = OUTPUT / f"dashboard-{name}-settings.png"
            page.screenshot(path=str(settings_screenshot), full_page=False)
            page.locator("#closeSettingsButton").click()
            page.locator("#openPreferencesButton").click()
            page.locator("#preferencesDialog").wait_for(state="visible")
            preferences_dialog_radius = page.locator("#preferencesDialog").evaluate(
                "element => getComputedStyle(element).borderRadius"
            )
            preferences_field_radius = page.locator(
                "#preferencesDialog .field input, #preferencesDialog .field select, #preferencesDialog .field textarea"
            ).first.evaluate("element => getComputedStyle(element).borderRadius")
            preferences_screenshot = OUTPUT / f"dashboard-{name}-preferences.png"
            page.screenshot(path=str(preferences_screenshot), full_page=False)
            page.locator("#closePreferencesButton").click()
            queue_label = page.locator("#queueButton").inner_text().strip()
            queue_enabled = page.locator("#queueButton").is_enabled()
            job_primary_actions = page.locator(".job-open").count()
            job_secondary_actions = page.locator('[data-action="mark-applied"]').count()
            resume_actions = page.locator(".resume-task").count()
            safe_fill_actions = page.locator(".safe-fill-task").count()
            queue_focus = True
            queue_not_obscured = True
            if queue_enabled:
                page.locator("#queueButton").click()
                page.wait_for_timeout(450 if not reduced_motion else 50)
                queue_focus = page.evaluate("document.activeElement?.id === 'jobsTitle'")
                queue_not_obscured = page.evaluate(
                    """
                    (() => {
                      const title = document.querySelector('#jobsTitle').getBoundingClientRect();
                      const topbar = document.querySelector('.topbar').getBoundingClientRect();
                      return title.top >= topbar.bottom;
                    })()
                    """
                )
            route_animation = page.locator(".route-live").evaluate(
                "element => getComputedStyle(element).animationName"
            )
            report["viewports"][name] = {
                "status": response.status if response else None,
                "overflow": overflow,
                "console_errors": errors,
                "reduced_motion": reduced_motion,
                "route_animation": route_animation,
                "queue_label": queue_label,
                "queue_enabled": queue_enabled,
                "queue_focus": queue_focus,
                "queue_not_obscured": queue_not_obscured,
                "job_primary_actions": job_primary_actions,
                "job_secondary_actions": job_secondary_actions,
                "resume_actions": resume_actions,
                "safe_fill_actions": safe_fill_actions,
                "copilot_safety_visible": copilot_safety_visible,
                "copilot_dialog_radius": copilot_dialog_radius,
                "copilot_input_radius": copilot_input_radius,
                "settings_dialog_radius": settings_dialog_radius,
                "settings_field_radius": settings_field_radius,
                "preferences_dialog_radius": preferences_dialog_radius,
                "preferences_field_radius": preferences_field_radius,
                "screenshot": str(screenshot),
                "viewport_screenshot": str(viewport_screenshot),
                "metrics_screenshot": str(metrics_screenshot),
                "jobs_screenshot": str(jobs_screenshot),
                "commute_screenshot": str(commute_screenshot),
                "copilot_screenshot": str(copilot_screenshot),
                "settings_screenshot": str(settings_screenshot),
                "preferences_screenshot": str(preferences_screenshot),
            }
            page.close()
        browser.close()
    report_path = OUTPUT / "qa-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report_path)
    if any(
        item["overflow"]
        or item["console_errors"]
        or item["status"] != 200
        or not item["queue_focus"]
        or not item["queue_not_obscured"]
        or item["job_primary_actions"] != item["job_secondary_actions"]
        or item["resume_actions"] != item["job_secondary_actions"]
        or item["safe_fill_actions"] != item["job_secondary_actions"]
        or not item["copilot_safety_visible"]
        or item["copilot_dialog_radius"] != "0px"
        or item["copilot_input_radius"] != "0px"
        or item["settings_dialog_radius"] != "0px"
        or item["settings_field_radius"] != "0px"
        or item["preferences_dialog_radius"] != "0px"
        or item["preferences_field_radius"] != "0px"
        or (item["reduced_motion"] and item["route_animation"] != "none")
        for item in report["viewports"].values()
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
