"""Visual and focus checks using synthetic resume content, no cloud requests."""
import threading

from playwright.sync_api import expect, sync_playwright

from scripts.workspace_qa import OUTPUT, fixture_server
from tests.test_resume_render import _content


def main():
    server = fixture_server()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for name, width, height in [("desktop", 1440, 1000), ("mobile", 390, 844)]:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="networkidle", timeout=15000)
                page.evaluate("c => { resumeEditorState = {jobId:7,content:c,photoAvailable:false}; renderEditorForm(); elements.resumeEditorDialog.showModal(); }", _content("mono-photo"))
                expect(page.locator("#editorName")).to_be_visible()
                checkbox = page.locator("#editorPhotoInclude").bounding_box()
                assert checkbox["width"] == checkbox["height"] == 18, checkbox
                page.screenshot(path=str(OUTPUT / f"{name}-resume-editor.png"))
                page.locator("#editorSelfEvaluation").fill("结合数据复盘工作。")
                expect(page.locator("#resumeEditorCloseButton")).to_be_visible()
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                page.screenshot(path=str(OUTPUT / f"{name}-resume-evaluation.png"))
                page.locator("#resumeEditorCloseButton").click()
                expect(page.locator("#resumeEditorDialog")).not_to_be_visible()
                page.close()
            browser.close()
        print("Resume editor: desktop/mobile, fields, scrolling, close and overflow passed.")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


if __name__ == "__main__":
    main()
