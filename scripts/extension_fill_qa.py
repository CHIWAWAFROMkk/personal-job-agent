"""Real DOM field safety checks with a mocked extension transport, synthetic data."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parents[1]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel='msedge')
        page = browser.new_page()
        page.route('https://jobs.example/**', lambda r: r.fulfill(content_type='text/html; charset=utf-8', body='''
          <form id="form"><label>姓名<input id="name"></label><label>手机<input id="phone"></label>
          <label>邮箱<input id="email" value="keep@example.com"></label>
          <label>学校<input id="school1"></label><label>学校<input id="school2"></label>
          <label>期望薪资<input id="salary"></label><label>短信验证码<input id="otp" autocomplete="one-time-code" maxlength="6"></label>
          <label>图片验证码<input id="captcha"></label><button type="submit">提交申请</button></form>
          <script>window.submits=0;window.otpEvents=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();submits++};
          document.querySelector('#otp').oninput=()=>otpEvents++;
          document.querySelector('#name').onchange=()=>document.querySelector('form').requestSubmit();
          window.chrome={runtime:{sendMessage:async()=>({code:'004321'})}};</script>'''))
        page.goto('https://jobs.example/apply')
        page.add_script_tag(path=str(root / 'src/job_agent/extension/fill.js'))
        result = page.evaluate("PjaFill.fill({name:'合成测试',phone:'123456',email:'replace@example.com',school:'测试大学',salary:'9999'})")
        assert set(result['filled']) == {'name', 'phone'}, result
        assert page.locator('#email').input_value() == 'keep@example.com'
        assert page.locator('#salary').input_value() == ''
        assert page.locator('#school1').input_value() == ''
        page.locator('#otp').focus()
        page.evaluate('PjaFill.arm(); PjaFill.start()')
        page.wait_for_function("document.querySelector('#otp').value==='004321'")
        assert page.evaluate('submits') == 0
        assert page.evaluate('otpEvents') == 0
        page.locator('#otp').fill('')
        page.locator('#otp').focus()
        page.evaluate("PjaFill.arm(); PjaFill.start(); history.pushState({},'', '/other')")
        page.wait_for_timeout(1200)
        assert page.locator('#otp').input_value() == ''
        browser.close()
    print(json.dumps({'filled': result['filled'], 'ambiguous_skipped': True, 'no_submit': True, 'otp_no_events': True, 'navigation_stops': True}))


if __name__ == '__main__':
    main()
