"""Render desktop adaptation with isolated fixtures, never user records."""
import sys
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from workspace_qa import fixture_server


def main():
    before = '--before' in sys.argv
    output = Path('build/desktop-layout-qa')
    output.mkdir(parents=True, exist_ok=True)
    server = fixture_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel='msedge')
            for width, height, theme in [(1440, 900, 'aurora'), (1266, 813, 'cosmic'), (1920, 1080, 'cosmic'), (390, 844, 'aurora')]:
                page = browser.new_page(viewport={'width': width, 'height': height}, reduced_motion='reduce')
                page.set_default_timeout(10000)
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                page.evaluate('(t)=>document.documentElement.dataset.theme=t', theme)
                prefix = f'{"before" if before else "after"}-{width}-{theme}'
                print(page.locator('.today-head, .today-side').evaluate_all('(els)=>els.map(e=>({tag:e.className,hidden:e.hidden,display:getComputedStyle(e).display,rect:e.getBoundingClientRect().toJSON()}))'), flush=True)
                page.screenshot(path=str(output / f'{prefix}-home.png'))
                if before:
                    print(prefix, page.locator('.topbar-inner').bounding_box(), flush=True)
                    page.close()
                    continue
                expect(page.locator('#composerInput')).to_have_attribute('placeholder', '问问 Agent')
                expect(page.locator('.agent-beacon')).to_have_count(2)
                assert page.locator('.agent-beacon').first.evaluate('e=>getComputedStyle(e).animationName') == 'none'
                page.emulate_media(reduced_motion='no-preference')
                assert page.locator('.beacon-cool').evaluate('e=>getComputedStyle(e).animationName') == 'beacon-cool'
                assert page.locator('.beacon-warm').evaluate('e=>getComputedStyle(e).animationName') == 'beacon-warm'
                page.emulate_media(reduced_motion='reduce')
                page.locator('#geminiProfileAvatarBtn').click()
                expect(page.locator('#settingsDialog')).to_be_visible()
                page.keyboard.press('Escape')
                expect(page.locator('#settingsDialog')).not_to_be_visible()
                page.locator('#geminiModelSelectorBtn').click()
                expect(page.locator('#settingsDialog')).to_be_visible()
                page.keyboard.press('Escape')
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), prefix
                if width > 768:
                    for nav_id, target in [('overviewNav', '#overviewTitle'), ('profileNav', '#openProfileButton')]:
                        page.locator('#' + nav_id).click()
                        expect(page.locator(target)).to_be_visible()
                        assert page.locator('.page').evaluate('e=>getComputedStyle(e).zIndex') == '1'
                        page.screenshot(path=str(output / f'{prefix}-{nav_id}.png'))
                    expect(page.locator('#composerWaveBtn')).to_have_count(0)
                    expect(page.locator('#workspaceNav')).to_be_visible()
                    page.locator('#workspaceNav').click()
                    expect(page.locator('#jobWorkspace')).to_be_visible()
                    page.locator('.job-choice').first.click()
                    page.wait_for_load_state('networkidle')
                    expect(page.locator('#jobDetail')).to_be_visible()
                    nav = page.locator('.job-navigator').bounding_box()
                    detail = page.locator('#jobDetail').bounding_box()
                    assert detail['x'] >= nav['x'] + nav['width'] - 2
                    assert detail['width'] >= 430
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                    page.screenshot(path=str(output / f'{prefix}-jobs.png'))
                    page.locator('#openCopilotButton').click()
                    expect(page.locator('#copilotDialog')).to_be_visible()
                    page.wait_for_load_state('networkidle')
                    page.screenshot(path=str(output / f'{prefix}-agent.png'))
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                assert not errors, errors
                print(prefix, 'PASS', flush=True)
                page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
