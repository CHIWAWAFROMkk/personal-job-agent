"""Real DOM field safety checks using synthetic data."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parents[1]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel='msedge')
        page = browser.new_page()
        page.set_default_timeout(10000)
        page.set_default_navigation_timeout(10000)
        page.route('https://jobs.example/**', lambda r: r.fulfill(content_type='text/html; charset=utf-8', body='''
          <form id="form"><label>姓名<input id="name"></label><label>手机<input id="phone"></label>
          <label>邮箱<input id="email" value="keep@example.com"></label>
          <label>学校<input id="school1"></label><label>学校<input id="school2"></label>
          <label>期望薪资<input id="salary"></label><label>短信验证码<input id="otp" autocomplete="one-time-code" maxlength="6"></label>
          <label>图片验证码<input id="captcha"></label><button type="submit">提交申请</button></form>
          <script>window.submits=0;window.otpEvents=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();submits++};
          document.querySelector('#otp').oninput=()=>otpEvents++;
          document.querySelector('#name').onchange=()=>document.querySelector('form').requestSubmit();
          </script>'''))
        page.goto('https://jobs.example/apply')
        page.add_script_tag(path=str(root / 'src/job_agent/extension/fill.js'))
        result = page.evaluate("PjaFill.fill({name:'合成测试',phone:'123456',email:'replace@example.com',school:'测试大学',salary:'9999'})")
        assert set(result['filled']) == {'name', 'phone'}, result
        assert page.locator('#email').input_value() == 'keep@example.com'
        assert page.locator('#salary').input_value() == ''
        assert page.locator('#school1').input_value() == ''
        assert page.locator('#school2').input_value() == ''
        assert page.locator('#otp').input_value() == ''
        assert page.locator('#captcha').input_value() == ''
        assert page.evaluate('submits') == 0
        assert page.evaluate('otpEvents') == 0
        assert page.evaluate("['arm','start','tick','otpEligible'].every(key => PjaFill[key] === undefined)")
        browser.close()
    print(json.dumps({'filled': result['filled'], 'ambiguous_skipped': True, 'no_submit': True, 'verification_fields_untouched': True, 'otp_api_removed': True}))


if __name__ == '__main__':
    main()
