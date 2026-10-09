const assert = require('node:assert/strict');
const metrics = require('./static/metrics.js');
const parts = metrics.tokenParts({input_tokens:100,cached_input_tokens:70,output_tokens:20});
assert.equal(parts.reduce((sum, part) => sum + part.value, 0), 120);
assert.equal(parts[1].value, 30);
assert.equal(metrics.tokenParts({input_tokens:100,cached_input_tokens:null,output_tokens:20}), null);
assert.equal(metrics.tokenParts({input_tokens:10,cached_input_tokens:20,output_tokens:1}), null);
assert.equal(metrics.tokenParts({input_tokens:0,cached_input_tokens:0,output_tokens:0}).length, 3);
const days = metrics.dailyUsage([{turns:[
  {started_at:'2026-10-08T10:00:00-04:00',usage:{total_tokens:120}},
  {started_at:'2026-10-08T11:00:00-04:00',usage:{total_tokens:80}},
  {started_at:'2026-10-09T11:00:00-04:00',usage:{total_tokens:null}},
  {started_at:null,usage:{total_tokens:900}}
]}]);
assert.equal(days.length, 1);
assert.equal(days[0].tokens, 200);
assert.equal(days[0].turns, 2);
console.log('Chart accounting checks passed.');
