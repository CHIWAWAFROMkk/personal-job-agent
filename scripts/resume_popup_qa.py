"""Verify generation never opens a browser window, on success or failure."""
import threading
from playwright.sync_api import sync_playwright
from workspace_qa import fixture_server


server = fixture_server()
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.wait_for_selector('[data-detail-action="create-resume"]')
        result = page.evaluate('''async () => {
          let opens = 0;
          window.open = () => { opens++; return null; };
          window.confirm = () => true;
          loadDashboard = async () => {};
          postLocalJson = async () => ({workspace: {resume_pdf_url: '/resume-draft/2/pdf'}, generation: {}});
          const success = await createResumeDraft(2);
          postLocalJson = async () => { throw new Error('测试连接失败'); };
          const failure = await createResumeDraft(2);
          return {opens, success, failure, text: document.querySelector('[data-detail-action="create-resume"]').textContent};
        }''')
        assert result == {"opens": 0, "success": True, "failure": False, "text": "生成草稿"}, result
        browser.close()
    print("PASS: success/failure open zero windows; generation button restored")
finally:
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)
