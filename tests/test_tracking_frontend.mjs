import test from 'node:test';
import assert from 'node:assert/strict';
import { countdown } from '../src/job_agent/web/js/tracking.js';
test('countdown uses elapsed time, distinguishes expired and invalid values',()=>{
  const now=Date.parse('2026-09-20T00:00:00+08:00');
  assert.equal(countdown('2026-09-20T00:30:00+08:00',now),'剩余 30 分钟');
  assert.equal(countdown('2026-09-22T00:00:00+08:00',now),'剩余 48 小时 0 分钟');
  assert.equal(countdown('2026-09-19T16:00:00Z',now),'已到期 · 请核对结果');
  assert.equal(countdown('invalid',now),'时间待核对');
});
