"""Isolated responsive control audit; never uses personal data or external AI."""
import json
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright
from workspace_qa import fixture_server

OUT = Path(__file__).resolve().parents[1] / 'work' / 'ui-controls-qa'
OUT.mkdir(parents=True, exist_ok=True)
server = fixture_server()
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
report = {}
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, height in [(1440, 1000), (960, 640), (390, 844), (844, 390)]:
            page = browser.new_page(viewport={'width': width, 'height': height}, reduced_motion='reduce')
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
            page.wait_for_selector('.job-choice')
            page.locator('.job-choice').first.click()
            page.screenshot(path=str(OUT / f'{width}.png'), full_page=True)
            result = page.evaluate('''() => {
              const issues=[];
              for(const el of document.querySelectorAll('button:not(.job-choice), a.action-button')) {
                if(!el.getClientRects().length) continue;
                if(el.scrollWidth > el.clientWidth + 2) issues.push({text:el.textContent.trim(),type:'clipped'});
              const ys=[];
                const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
                let node;
                while(node=walker.nextNode()) {
                  if(!node.textContent.trim()) continue;
                  const range=document.createRange(); range.selectNodeContents(node);
                  for(const r of range.getClientRects()) if(r.width&&r.height) ys.push({top:r.top,bottom:r.bottom});
                }
                if(ys.length && Math.max(...ys.map(r=>r.top)) >= Math.min(...ys.map(r=>r.bottom))) issues.push({text:el.textContent.trim(),type:'multiline'});
              }
              return {overflow:document.documentElement.scrollWidth>innerWidth,issues};
            }''')
            result['errors'] = errors
            report[str(width)] = result
            page.close()
        browser.close()
    (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    assert all(not r['overflow'] and not r['issues'] and not r['errors'] for r in report.values())
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
