import test from 'node:test';
import assert from 'node:assert/strict';
import {timerText} from '../src/job_agent/web/js/mock-interview.js';
test('practice timer is bounded for invalid values and keeps elapsed minutes',()=>{
  assert.equal(timerText(0),'00:00');
  assert.equal(timerText(61),'01:01');
  assert.equal(timerText(-1),'00:00');
  assert.equal(timerText('invalid'),'00:00');
  assert.equal(timerText(3600),'60:00');
});
