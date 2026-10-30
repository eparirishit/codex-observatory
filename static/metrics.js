'use strict';

const ObservatoryMetrics = {
  tokenParts(usage) {
    const input = usage.input_tokens;
    const cached = usage.cached_input_tokens;
    const output = usage.output_tokens;
    if ([input, cached, output].some(value => !Number.isFinite(value) || value < 0) || cached > input) return null;
    return [
      { label: 'Reused input', value: cached, color: '#5fd39a' },
      { label: 'Fresh input', value: input - cached, color: '#519dfa' },
      { label: 'Generated output', value: output, color: '#d4a35b' }
    ];
  },
  dailyUsage(sessions) {
    const days = new Map();
    for (const session of sessions) {
      for (const turn of session.turns) {
        if (turn.usage.total_tokens == null || !turn.started_at) continue;
        const started = new Date(turn.started_at);
        if (!Number.isFinite(started.getTime())) continue;
        const key = `${started.getFullYear()}-${String(started.getMonth() + 1).padStart(2, '0')}-${String(started.getDate()).padStart(2, '0')}`;
        const day = days.get(key) || { date: key, tokens: 0, turns: 0 };
        day.tokens += turn.usage.total_tokens;
        day.turns += 1;
        days.set(key, day);
      }
    }
    return [...days.values()].sort((first, second) => first.date.localeCompare(second.date));
  }
};
if (typeof module !== 'undefined') module.exports = ObservatoryMetrics;

// Published API-equivalent token rates, USD per million; verified 2026-10-30.
// https://developers.openai.com/api/docs/pricing and the linked model pages.
ObservatoryMetrics.pricing = {
  asOf: '2026-10-30', source: 'https://developers.openai.com/api/docs/pricing',
  models: {
    'gpt-6-astra': {input:10,cached:1,output:50,long_threshold:272000,fast:2,ultrafast:6,flex:.5},
    'gpt-6-sol': {input:2,cached:.2,output:10,long_threshold:272000,fast:2,flex:.5},
    'gpt-6.1-terra': {input:1,cached:.05,output:5,long_threshold:272000,fast:2,flex:.5},
    'gpt-6-luna': {input:.1,cached:.01,output:.5,long_threshold:272000,fast:2,flex:.5},
    'gpt-6.1-sol': {input:2,cached:.1,output:10,long_threshold:272000,fast:2,ultrafast:6,flex:.5},
    'gpt-5.6-terra': {input:2,cached:.2,output:12,long_threshold:272000},
    'gpt-5.6-sol': {input:4,cached:.4,output:20,long_threshold:272000,fast:2,flex:.5},
    'gpt-5.3-codex': {input:1.75,cached:.175,output:14,fast:2}
  }
};
ObservatoryMetrics.validNumber = value => Number.isFinite(value) && value >= 0;
ObservatoryMetrics.validatePrices = function(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length > 500) throw new Error('Prices must be an object with at most 500 model|tier entries.');
  const result = Object.create(null);
  for (const [key, rate] of Object.entries(value)) {
    if (!/^[A-Za-z0-9_.:/-]{1,160}\|(standard|fast|ultrafast|flex)$/.test(key) || !rate || typeof rate !== 'object' || Array.isArray(rate)) throw new Error('Use model|standard, model|fast, model|ultrafast, or model|flex keys.');
    if (!['input','cached','output'].every(k => this.validNumber(rate[k])) || (rate.cache_write != null && !this.validNumber(rate.cache_write))) throw new Error('Each rate needs finite, nonnegative input, cached, and output prices.');
    result[key] = {input:rate.input,cached:rate.cached,output:rate.output};
    if (rate.cache_write != null) result[key].cache_write = rate.cache_write;
  }
  return result;
};
ObservatoryMetrics.priceResponse = function(record, options = {}) {
  const rawTier = options.speed && options.speed !== 'recorded' ? options.speed : record.service_tier;
  const tier = ({default:'standard',standard:'standard',priority:'fast',fast:'fast',ultrafast:'ultrafast',flex:'flex',batch:'flex'})[rawTier];
  const unknown = reason => ({usd:null,reason,tier:tier || 'unknown'});
  if (!tier) return unknown('service tier not recorded');
  const key = record.model + '|' + tier;
  const override = Object.hasOwn(options.overrides || {},key) ? options.overrides[key] : null;
  const published = Object.hasOwn(this.pricing.models,record.model) ? this.pricing.models[record.model] : null;
  if (!override && !published) return unknown('model has no published rate in this catalog');
  const multiplier = tier === 'standard' ? 1 : published?.[tier];
  if (!override && !this.validNumber(multiplier)) return unknown('tier has no published rate');
  const u = record.usage || {};
  const input=u.input_tokens, cached=u.cached_input_tokens, output=u.output_tokens, writes=u.cache_write_input_tokens;
  if (![input,cached,output,writes].every(this.validNumber) || cached+writes>input) return unknown('missing or inconsistent input/output/cache counters');
  if (writes>0 && !this.validNumber(override?.cache_write)) return unknown('cache-write retention/rate is not recorded; provide an explicit write-rate override');
  const rate = override || published;
  if (!['input','cached','output'].every(k=>this.validNumber(rate[k]))) return unknown('invalid price override');
  const long = published?.long_threshold != null && input>published.long_threshold;
  const inputMultiplier=long ? 2 : 1, outputMultiplier=long ? 1.5 : 1;
  const scale=override ? 1 : multiplier;
  const usd=((input-cached-writes)*rate.input*inputMultiplier+cached*rate.cached*inputMultiplier+writes*(rate.cache_write || 0)*inputMultiplier+output*rate.output*outputMultiplier)*scale/1e6;
  return Number.isFinite(usd) ? {usd,reason:null,tier,source:override?'override':'published',long_context:long,scenario:!!options.speed && options.speed!=='recorded'} : unknown('cost overflow');
};
ObservatoryMetrics.periodKey = function(value, group='day') {
  const d=new Date(value);
  if (value == null || !Number.isFinite(d.getTime())) return null;
  if (group==='week') d.setDate(d.getDate()-((d.getDay()+6)%7));
  const month=`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;
  return group==='month' ? month : `${month}-${String(d.getDate()).padStart(2,'0')}`;
};
ObservatoryMetrics.recordSummary = function(records) {
  const valid=this.validNumber;
  const complete=key=>records.length>0 && records.every(r=>valid(r.usage?.[key]));
  const sum=key=>records.reduce((n,r)=>n+(valid(r.usage?.[key])?r.usage[key]:0),0);
  const cacheComplete=complete('input_tokens') && complete('cached_input_tokens') && records.every(r=>r.usage.cached_input_tokens<=r.usage.input_tokens);
  const priced=records.filter(r=>valid(r.cost?.usd));
  const knownUsd=priced.reduce((n,r)=>n+r.cost.usd,0);
  return {responses:records.length,tokens:complete('total_tokens')?sum('total_tokens'):null,
    input:complete('input_tokens')?sum('input_tokens'):null,cached:cacheComplete?sum('cached_input_tokens'):null,
    cache_ratio:cacheComplete && sum('input_tokens')>0 ? sum('cached_input_tokens')/sum('input_tokens'):null,
    usd:records.length>0 && priced.length===records.length ? knownUsd:null,known_usd:knownUsd,priced:priced.length};
};
ObservatoryMetrics.rollups = function(records, group='day') {
  const buckets=new Map();
  for (const record of records) {
    const key=this.periodKey(record.at,group);
    if (!key) continue;
    if (!buckets.has(key)) buckets.set(key,[]);
    buckets.get(key).push(record);
  }
  return [...buckets].sort(([a],[b])=>a.localeCompare(b)).map(([key,values])=>({key,...this.recordSummary(values)}));
};
ObservatoryMetrics.analyze = function(snapshot, options={}) {
  const records=[], seen=new Set(), sessionRecords=new Map(), sessionRows=[];
  for (const session of snapshot.sessions || []) {
    const own=[];
    for (const [index,raw] of (session.responses || []).entries()) {
      const key=raw.id || `${session.id}:fallback:${index}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const record={...raw,session_id:session.id,project_id:raw.project_id || session.project_id || null,
        at_ms:raw.at == null ? NaN:Date.parse(raw.at),
        cost:raw.id ? this.priceResponse(raw,options):{usd:null,reason:'cumulative fallback cannot be priced per response'}};
      records.push(record); own.push(record);
    }
    sessionRecords.set(session.id,own);
    sessionRows.push({id:session.id,...this.recordSummary(own)});
  }
  const reasoningGroups=new Map(), projectGroups=new Map(), turns=[], contextChanges=[], toolGroups=new Map();
  for (const record of records) {
    const key=JSON.stringify([record.model,record.effort]);
    if (!reasoningGroups.has(key)) reasoningGroups.set(key,[]);
    reasoningGroups.get(key).push(record);
    const project=record.project_id || 'unassigned';
    if (!projectGroups.has(project)) projectGroups.set(project,[]);
    projectGroups.get(project).push(record);
  }
  const reasoning=[...reasoningGroups.values()].map(values=>{
    const covered=values.filter(r=>this.validNumber(r.usage.reasoning_output_tokens) && this.validNumber(r.usage.output_tokens) && r.usage.reasoning_output_tokens<=r.usage.output_tokens);
    const output=covered.reduce((n,r)=>n+r.usage.output_tokens,0), reasoning=covered.reduce((n,r)=>n+r.usage.reasoning_output_tokens,0);
    return {model:values[0].model,effort:values[0].effort,responses:values.length,covered:covered.length,output,reasoning,share:output>0?reasoning/output:null,...this.recordSummary(values)};
  });
  const outer=new Set(['wrapper','agent_tool']);
  for (const session of snapshot.sessions || []) {
    const own=sessionRecords.get(session.id) || [];
    const byTurn=new Map();
    for (const record of own) {
      if (!byTurn.has(record.turn_id)) byTurn.set(record.turn_id,[]);
      byTurn.get(record.turn_id).push(record);
    }
    const callsByTurn=new Map();
    for (const call of session.calls || []) {
      if (!outer.has(call.category)) {
        if (!callsByTurn.has(call.turn_id)) callsByTurn.set(call.turn_id,[]);
        callsByTurn.get(call.turn_id).push(call);
      }
    }
    for (const turn of session.turns || []) {
      const summary=this.recordSummary(byTurn.get(turn.id) || []);
      const calls=callsByTurn.get(turn.id) || [];
      turns.push({session_id:session.id,id:turn.id,number:turn.number,...summary,calls:calls.length,
        tokens_per_call:summary.tokens!=null && calls.length>0 ? summary.tokens/calls.length:null,
        tokens_per_minute:summary.tokens!=null && turn.duration_seconds>0 ? summary.tokens/(turn.duration_seconds/60):null});
    }
    for (const call of session.calls || []) {
      if (outer.has(call.category)) continue;
      const key=call.category+'/'+call.name;
      if (!toolGroups.has(key)) toolGroups.set(key,{name:call.name,category:call.category,calls:0,context_delta:0,intervals:0,output_bytes:0,byte_calls:0});
      const tool=toolGroups.get(key); tool.calls++;
      if (this.validNumber(call.output_bytes)) {tool.output_bytes+=call.output_bytes;tool.byte_calls++;}
    }
    const chronological=own.filter(r=>r.id && r.at).sort((a,b)=>a.at.localeCompare(b.at));
    const completions=(session.calls || []).filter(c=>c.ended_at).sort((a,b)=>a.ended_at.localeCompare(b.ended_at));
    const compactions=[...(session.compactions || [])].filter(c=>c.at).sort((a,b)=>a.at.localeCompare(b.at));
    let completionIndex=0,compactionIndex=0;
    for (let i=1;i<chronological.length;i++) {
      const before=chronological[i-1], after=chronological[i];
      if (before.turn_id!==after.turn_id) continue;
      const completed=[];
      while(completionIndex<completions.length && completions[completionIndex].ended_at<=after.at) {
        const call=completions[completionIndex++];
        if(call.ended_at>before.at && call.turn_id===after.turn_id) completed.push(call);
      }
      let compacted=false;
      while(compactionIndex<compactions.length && compactions[compactionIndex].at<=after.at) {
        if(compactions[compactionIndex++].at>before.at) compacted=true;
      }
      const inner=completed.filter(c=>!outer.has(c.category));
      const calls=inner.length ? inner:completed;
      if (!calls.length) continue;
      let reason=null;
      if (before.model!==after.model || before.effort!==after.effort) reason='model/effort changed';
      else if (compacted) reason='compaction';
      else if (![before.usage.input_tokens,after.usage.input_tokens].every(this.validNumber)) reason='input counters missing';
      const delta=reason ? null:after.usage.input_tokens-before.usage.input_tokens;
      const single=calls.length===1;
      const change={session_id:session.id,turn_id:after.turn_id,at:after.at,call_id:single?calls[0].id:null,
        tool:single?calls[0].name:'Multiple calls',call_ids:calls.map(c=>c.id).filter(Boolean),calls:calls.length,delta,reason:reason || (single?'post-call proxy':'shared interval; no individual attribution')};
      contextChanges.push(change);
      if (single && !outer.has(calls[0].category) && delta!=null) {
        const tool=toolGroups.get(calls[0].category+'/'+calls[0].name);
        tool.context_delta+=delta; tool.intervals++;
      }
    }
  }
  const windows=new Map();
  for (const session of snapshot.sessions || []) for (const observation of session.rate_limits || []) {
    for (const bucket of ['primary','secondary']) {
      const window=observation[bucket];
      if (!window || !this.validNumber(window.window_minutes) || window.window_minutes<=0 || !this.validNumber(window.resets_at) || !Number.isFinite(Date.parse(observation.at))) continue;
      if (!Number.isFinite(new Date(window.resets_at*1000).getTime()) || !Number.isFinite(new Date(window.resets_at*1000-window.window_minutes*60000).getTime())) continue;
      const key=JSON.stringify([observation.limit_id,bucket,window.window_minutes,window.resets_at]);
      if (!windows.has(key)) windows.set(key,{limit_id:observation.limit_id,bucket,minutes:window.window_minutes,end:window.resets_at*1000,start:window.resets_at*1000-window.window_minutes*60000,observations:new Map()});
      windows.get(key).observations.set(observation.at,window.used_percent);
    }
  }
  const now=Date.parse(snapshot.generated_at) || Date.now();
  const windowRows=[...windows.values()].map(window=>{
    const samples=[...window.observations].sort(([a],[b])=>a.localeCompare(b));
    const valid=samples.filter(([at,p])=>this.validNumber(p) && p<=100 && Date.parse(at)>=window.start && Date.parse(at)<=window.end);
    const latest=samples.at(-1), first=valid[0], last=valid.at(-1);
    const monotonic=valid.every(([,p],i)=>i===0 || p>=valid[i-1][1]);
    const hours=first && last ? (Date.parse(last[0])-Date.parse(first[0]))/3600000:0;
    const accountRate=hours>=1/12 && monotonic ? (last[1]-first[1])/hours:null;
    const own=records.filter(r=>r.at_ms>=window.start && r.at_ms<window.end);
    const summary=this.recordSummary(own), elapsed=(Math.min(now,window.end)-window.start)/3600000;
    return {...summary,limit_id:window.limit_id,bucket:window.bucket,minutes:window.minutes,start:new Date(window.start).toISOString(),end:new Date(window.end).toISOString(),
      at:latest[0],used_percent:latest[1],stale:now>window.end || now-Date.parse(latest[0])>15*60000,
      tokens_per_hour:summary.tokens!=null && elapsed>0?summary.tokens/elapsed:null,account_points_per_hour:accountRate};
  }).sort((a,b)=>b.end.localeCompare(a.end));
  const projects=[...projectGroups].map(([id,values])=>({id,sessions:new Set(values.map(r=>r.session_id)).size,...this.recordSummary(values)}));
  return {records,summary:this.recordSummary(records),sessions:sessionRows,turns,reasoning,projects,
    contextChanges,tools:[...toolGroups.values()],windows:windowRows,days:this.rollups(records,'day')};
};
