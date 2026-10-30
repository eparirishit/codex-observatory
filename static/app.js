'use strict';

const params = new URLSearchParams(location.hash.slice(1));
const accessToken = params.get('token') || '';
history.replaceState(null, '', location.pathname);
let snapshot = null;
let analytics = null;
const preferences = {speed:'recorded',overrides:Object.create(null),labels:Object.create(null)};
try {
  const saved = JSON.parse(localStorage.getItem('observatory.preferences.v1') || '{}');
  if (['recorded','standard','fast','ultrafast','flex'].includes(saved.speed)) preferences.speed = saved.speed;
  preferences.overrides = ObservatoryMetrics.validatePrices(saved.overrides || {});
  for (const [key,value] of Object.entries(saved.labels || {})) {
    if (/^[a-f0-9]{16}$/.test(key) && typeof value === 'string' && value.length <= 80) preferences.labels[key] = value;
  }
} catch { /* Storage may be disabled or contain an older invalid preference. */ }
const usd = new Intl.NumberFormat(undefined,{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:4});
const money = value => value == null ? '—' : usd.format(value);
const percent = value => value == null ? '—' : `${(value*100).toFixed(1)}%`;
const costText = value => value?.usd != null ? money(value.usd) : value?.priced ? `${money(value.known_usd)} partial` : '—';
const projectName = id => id === 'unassigned' || !id ? 'Unassigned' : preferences.labels[id] || `Project ${id.slice(0,8)}`;
function persistPreferences() {
  try { localStorage.setItem('observatory.preferences.v1',JSON.stringify(preferences)); return 'Saved in this browser.'; }
  catch { return 'Browser storage is unavailable; changes apply to this tab only.'; }
}
function sessionProjects(session) {
  return [...new Set((session.responses || []).map(r=>r.project_id || session.project_id || 'unassigned').concat(session.project_id || []).filter(Boolean))];
}
let selectedId = null;
let callLimit = 200;
let loading = false;
const outerCategories = new Set(['wrapper', 'agent_tool']);
const integer = new Intl.NumberFormat();
const short = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 2 });
const byId = id => document.getElementById(id);
const format = value => value == null ? '—' : integer.format(value);
const compact = value => value == null ? '—' : short.format(value);
const date = value => value ? new Date(value).toLocaleString() : '—';
const kindLabel = kind => ({ main: 'Main chats', subagent: 'Subagents', approval_subagent: 'Approval subagents' })[kind] || kind;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function table(target, headers, rows) {
  const container = byId(target);
  container.replaceChildren();
  if (!rows.length) {
    container.append(element('p', 'No recorded data for this view.', 'empty'));
    return;
  }
  const grid = element('table');
  const head = element('thead');
  const headRow = element('tr');
  headers.forEach(header => { const cell = element('th', header); cell.scope = 'col'; headRow.append(cell); });
  head.append(headRow);
  grid.append(head);
  const body = element('tbody');
  rows.forEach(values => {
    const row = element('tr');
    values.forEach(value => {
      const cell = element('td');
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value ?? '—';
      row.append(cell);
    });
    body.append(row);
  });
  grid.append(body);
  container.append(grid);
}

function card(title, value, note) {
  const node = element('article', null, 'card');
  node.append(element('div', title, 'card-label'), element('div', value, 'card-number'), element('div', note, 'card-note'));
  return node;
}

function breakdown(target, entries, unit = 'tokens') {
  const container = byId(target);
  container.replaceChildren();
  const max = Math.max(1, ...entries.map(entry => entry[1] || 0));
  entries.forEach(([name, total]) => {
    const row = element('div', null, 'breakdown');
    const meter = element('meter', null, 'meter');
    meter.min = 0;
    meter.max = max;
    meter.value = total || 0;
    meter.setAttribute('aria-label', `${name}: ${format(total)} ${unit}`);
    row.append(element('span', name), meter, element('span', compact(total)));
    container.append(row);
  });
  if (!entries.length) container.append(element('p', 'Token usage unavailable.', 'empty'));
}

function svgElement(tag, attributes = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function renderVisuals() {
  const usage = snapshot.summary.usage;
  const parts = ObservatoryMetrics.tokenParts(usage);
  const mix = byId('token-mix');
  mix.replaceChildren();
  if (parts) {
    const total = parts.reduce((sum, part) => sum + part.value, 0);
    const svg = svgElement('svg', {viewBox:'0 0 220 220',role:'img','aria-label':parts.map(part => `${part.label}: ${format(part.value)} tokens`).join('. ')});
    svg.append(svgElement('circle', {cx:110,cy:110,r:82,fill:'none',stroke:'var(--line)','stroke-width':25}));
    let offset = 0;
    for (const part of parts) {
      const length = total ? part.value / total * 100 : 0;
      svg.append(svgElement('circle', {cx:110,cy:110,r:82,fill:'none',stroke:part.color,'stroke-width':25,pathLength:100,
        'stroke-dasharray':`${length} ${100-length}`,'stroke-dashoffset':-offset,transform:'rotate(-90 110 110)'}));
      offset += length;
    }
    const label = svgElement('text',{x:110,y:107,'text-anchor':'middle',class:'ring-value'});
    label.textContent = snapshot.summary.cache_ratio == null ? '—' : `${(snapshot.summary.cache_ratio*100).toFixed(1)}%`;
    const caption = svgElement('text',{x:110,y:130,'text-anchor':'middle',class:'ring-caption'});
    caption.textContent = 'of input reused';
    svg.append(label,caption);
    const legend = element('div',null,'chart-legend');
    parts.forEach((part,index) => { const row = element('div',null,`legend-row part-${index}`); row.append(element('span',part.label),element('strong',compact(part.value)),element('small',`${total ? (100*part.value/total).toFixed(1) : '0'}% of total`)); legend.append(row); });
    mix.append(svg,legend);
  } else mix.append(element('p','Some token counters are unavailable; no composition is inferred.','empty'));
  const days = ObservatoryMetrics.rollups(analytics.records,byId('period-group').value).map(day=>({...day,date:day.key,turns:day.responses}));
  const chart = byId('daily-chart');
  chart.replaceChildren();
  const shown = days.filter(day=>day.tokens != null);
  if (shown.length) {
    const max = Math.max(1,...shown.map(day=>day.tokens));
    const svg = svgElement('svg',{viewBox:'0 0 640 220',role:'img','aria-label':`Recorded tokens by response time. ${shown.length} dates shown. Exact totals in the table.`});
    [0,0.5,1].forEach(fraction=>{ const top = 178-fraction*150; svg.append(svgElement('line',{x1:57,y1:top,x2:630,y2:top,stroke:'var(--line)'})); const text=svgElement('text',{x:48,y:top+4,'text-anchor':'end',class:'axis-label'}); text.textContent=compact(max*fraction); svg.append(text); });
    const step=570/shown.length;
    shown.forEach((day,index)=>{ const height=day.tokens/max*150; const bar=svgElement('rect',{x:60+index*step+step*.18,y:178-height,width:step*.64,height:Math.max(1,height),rx:4,fill:'var(--accent)'});const title=svgElement('title');title.textContent=`${day.date}: ${format(day.tokens)} tokens, ${day.turns} responses`;bar.append(title);svg.append(bar);
      if (index%Math.max(1,Math.ceil(shown.length/7))===0 || index===shown.length-1) { const text=svgElement('text',{x:60+index*step+step*.5,y:204,'text-anchor':'middle',class:'axis-label'});text.textContent=day.date.slice(5);svg.append(text); }
    });
    chart.append(svg);
  } else chart.append(element('p','No dated token records yet.','empty'));
  table('daily-table',['Period','Recorded tokens','Responses','Cache hit','API-equivalent cost⁸','Priced responses'],days.map(day=>[day.date,format(day.tokens),format(day.turns),percent(day.cache_ratio),costText(day),`${day.priced}/${day.responses}`]));
  const executed=snapshot.summary.tools.filter(tool=>!outerCategories.has(tool.category));
  const mcp=executed.filter(tool=>tool.category==='mcp');
  byId('tool-cards').replaceChildren(card('Recorded executions',compact(executed.reduce((sum,tool)=>sum+tool.calls,0)),'Inner executions only'),
    card('MCP executions',compact(mcp.reduce((sum,tool)=>sum+tool.calls,0)),'Calls to connected services'),
    card('Explicit failures',compact(executed.reduce((sum,tool)=>sum+tool.failed,0)),'Recorded failed status or nonzero exit'));
  breakdown('mcp-chart',mcp.slice(0,8).map(tool=>[tool.name,tool.calls]),'executions');
  byId('headline').textContent= snapshot.summary.cache_ratio == null ? 'Open a session to understand the work behind its recorded usage.' : `${(snapshot.summary.cache_ratio*100).toFixed(1)}% of recorded input was reused context across observed responses.`;
}

function showPage(name) {
  document.querySelectorAll('[data-page]').forEach(button=>{
    const selected=button.dataset.page===name;
    button.setAttribute('aria-selected',String(selected));
    button.tabIndex=selected?0:-1;
    byId(`page-${button.dataset.page}`).hidden=!selected;
  });
}

function renderLimits() {
  const container = byId('limits');
  container.replaceChildren();
  const latest = new Map();
  snapshot.summary.rate_limits.forEach(item => latest.set(item.limit_id, item));
  latest.forEach(item => {
    ['primary', 'secondary'].forEach(key => {
      const window = item[key];
      if (!window) return;
      const node = element('div', null, 'limit-card');
      const minutes = window.window_minutes;
      const windowName = minutes === 300 ? '5-hour window' : minutes === 10080 ? 'Weekly window' : `${format(minutes)}-minute window`;
      const remaining = window.used_percent == null ? null : 100 - window.used_percent;
      node.append(element('div', `${item.limit_id} · ${windowName}`, 'limit-title'));
      node.append(element('div', remaining == null ? 'Unavailable' : `${remaining.toFixed(1)}% remaining`, 'limit-number'));
      if (remaining != null) {
        const meter = element('meter', null, 'meter');
        meter.min = 0; meter.max = 100; meter.value = remaining;
        meter.setAttribute('aria-label', `${windowName} remaining ${remaining.toFixed(1)} percent`);
        node.append(meter);
      }
      node.append(element('p', `Recorded ${date(item.at)}`, 'subtle'));
      node.append(element('p', `Reset ${window.resets_at == null ? '—' : date(window.resets_at * 1000)}`, 'subtle'));
      if (item.at && Date.now() - new Date(item.at).getTime() > 5 * 60 * 1000) node.append(element('p', 'Stale snapshot — refreshes only when Codex logs a new event.', 'warn'));
      if (window.resets_at != null && Date.now() / 1000 > window.resets_at) node.append(element('p', 'Recorded reset has passed; current remaining quota is unknown.', 'warn'));
      container.append(node);
    });
  });
  if (!latest.size) container.append(element('p', 'No subscription rate-limit snapshots were recorded.', 'empty'));
}

function renderSessions() {
  const kind = byId('kind').value;
  const model = byId('model').value;
  const project = byId('project-filter').value;
  const sessions = snapshot.sessions.filter(session => (kind === 'all' || session.kind === kind) && (model === 'all' || session.models.includes(model)) && (project === 'all' || sessionProjects(session).includes(project)));
  if (byId('sort').value === 'tokens') sessions.sort((first, second) => (second.usage.total_tokens || 0) - (first.usage.total_tokens || 0));
  if (byId('sort').value === 'cost') sessions.sort((a,b)=>(analytics.sessions.find(x=>x.id===b.id)?.known_usd || 0)-(analytics.sessions.find(x=>x.id===a.id)?.known_usd || 0));
  table('sessions', ['Session', 'Project', 'API-equivalent cost⁸', 'Activity', 'Models', 'Turns', 'Input', 'Cached input', 'Output', 'Reasoning¹', 'Total', 'Last active', 'Source'], sessions.map(session => {
    const button = element('button', session.id.slice(0, 8), 'session-button');
    button.setAttribute('aria-label', `Inspect session ${session.id.slice(0, 8)}`);
    button.addEventListener('click', () => { selectedId = session.id; callLimit = 200; byId('call-layer').value = 'all'; renderDetail(); byId('detail').scrollIntoView({ behavior: 'smooth', block: 'start' }); });
    const projects=sessionProjects(session);
    return [button,projects.length===1?projectName(projects[0]):projects.length?'Multiple projects':'Unassigned',costText(analytics.sessions.find(x=>x.id===session.id)),kindLabel(session.kind), session.models.join(', ') || '—', session.turns.length,
      format(session.usage.input_tokens), format(session.usage.cached_input_tokens), format(session.usage.output_tokens),
      format(session.usage.reasoning_output_tokens), format(session.usage.total_tokens), date(session.updated_at), session.usage_source];
  }));
}

function renderTools() {
  const headers = ['Tool', 'Layer', 'Calls', 'Failed', 'Repeats after failure²', 'Timed calls', 'Recorded duration³', 'Output bytes'];
  const rows = tools => tools.map(tool => [tool.name, tool.category, format(tool.calls), format(tool.failed), format(tool.repeat_candidates),
    format(tool.timed_calls), tool.timed_calls ? `${(tool.duration_ms / 1000).toFixed(1)}s` : '—', format(tool.output_bytes)]);
  table('tools', headers, rows(snapshot.summary.tools.filter(tool => !outerCategories.has(tool.category))));
  table('outer-tools', headers, rows(snapshot.summary.tools.filter(tool => outerCategories.has(tool.category))));
}

function renderTimeline() {
  const session = snapshot.sessions.find(item => item.id === selectedId);
  if (!session) return;
  const turnNumbers = new Map(session.turns.map(turn => [turn.id, turn.number]));
  const layer = byId('call-layer').value;
  const selectedTurn = byId('call-turn').value;
  const calls = session.calls.filter(call => {
    if (selectedTurn !== 'all' && call.turn_id !== selectedTurn) return false;
    if (layer === 'executed') return !outerCategories.has(call.category);
    if (layer === 'outer') return outerCategories.has(call.category);
    if (layer === 'mcp') return call.category === 'mcp';
    if (layer === 'failed') return call.status === 'failed';
    if (layer === 'repeat') return call.repeat_candidate;
    return true;
  });
  table('timeline', ['Recorded at', 'Turn', 'Tool', 'Layer', 'Status', 'Duration³', 'Output bytes', 'Context change⁹', 'Repeat²'], calls.slice(0, callLimit).map(call => [
    date(call.at), turnNumbers.get(call.turn_id), call.name, call.category, call.status,
    call.duration_ms == null ? '—' : `${(call.duration_ms / 1000).toFixed(2)}s`, format(call.output_bytes),contextForCall(session.id,call.id), call.repeat_candidate ? 'Candidate' : '—'
  ]));
  byId('more-calls').hidden = calls.length <= callLimit;
  byId('more-calls').textContent = `Show next 200 (${format(calls.length)} total)`;
}

function renderDetail() {
  const session = snapshot.sessions.find(item => item.id === selectedId);
  byId('detail').hidden = !session;
  if (!session) return;
  byId('detail-title').textContent = `Session ${session.id.slice(0, 8)}`;
  byId('detail-meta').textContent = `${kindLabel(session.kind)} · ${session.models.join(', ')} · ${date(session.started_at)} · Parent: ${session.parent_id ? session.parent_id.slice(0, 8) : '—'}`;
  byId('detail-warnings').replaceChildren(...session.warnings.map(warning => element('p', warning, 'warning-note')));
  byId('session-summary').replaceChildren(card('Recorded tokens',compact(session.usage.total_tokens),'Across all observed turns'),card('Turns',format(session.turns.length),'One stretch of assistant work each'),card('Largest response input',compact(session.context.length ? Math.max(...session.context.map(point=>point.input_tokens||0)) : null),'Largest recorded response input⁵'),card('Compactions',format(session.compactions.length),'Recorded context checkpoints'));
  if (session.historical_baseline.total_tokens != null) byId('detail-warnings').append(element('p', `Excluded historical baseline: ${format(session.historical_baseline.total_tokens)} tokens.`, 'warning-note'));
  const turnNumbers = new Map(session.turns.map(turn => [turn.id, turn.number]));
  const chart = byId('context-chart');
  chart.replaceChildren();
  chart.setAttribute('role', 'img');
  const points = session.context;
  const max = Math.max(1, ...points.map(point => point.input_tokens || 0));
  chart.setAttribute('aria-label', `${points.length} recorded input sizes, maximum ${format(max)} tokens. Exact values in the table below.`);
  const stride = Math.max(1, Math.ceil(points.length / 240));
  const svgNamespace = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNamespace, 'svg');
  svg.setAttribute('viewBox', '0 0 1000 150');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', '150');
  const line = document.createElementNS(svgNamespace, 'polyline');
  const coordinates = [];
  for (let index = 0; index < points.length; index += stride) {
    const point = points[index];
    coordinates.push(`${points.length > 1 ? index / (points.length - 1) * 1000 : 500},${145 - (point.input_tokens || 0) / max * 135}`);
  }
  line.setAttribute('points', coordinates.join(' '));
  line.setAttribute('fill', 'none');
  line.setAttribute('stroke', 'var(--accent)');
  line.setAttribute('stroke-width', '2');
  line.setAttribute('vector-effect', 'non-scaling-stroke');
  svg.append(line);
  chart.append(svg);
  if (!points.length) chart.append(element('p', 'Input sizes unavailable.', 'empty'));
  table('context-table', ['Recorded at', 'Turn', 'Response input', 'Window capacity', 'Source'], points.map(point => [date(point.at), turnNumbers.get(point.turn_id), format(point.input_tokens), format(point.context_window), point.source]));
  renderCacheTrend('session-cache',analytics.records.filter(r=>r.session_id===session.id).map(r=>({key:date(r.at),cache_ratio:validCacheRatio(r.usage)})));
  table('turns', ['Turn', 'Started', 'Model', 'Effort', 'Responses⁴', 'Input', 'Cached input', 'Output', 'Reasoning¹', 'Total', 'Calls', 'API-equivalent cost⁸', 'Tokens / execution¹⁰', 'Tokens / minute¹⁰', 'Status'], session.turns.map(turn => [
    turn.number, date(turn.started_at), turn.model, turn.effort, turn.responses, format(turn.usage.input_tokens), format(turn.usage.cached_input_tokens),
    format(turn.usage.output_tokens), format(turn.usage.reasoning_output_tokens), format(turn.usage.total_tokens), turn.tool_calls, costText(analytics.turns.find(t=>t.session_id===session.id && t.id===turn.id)), format(analytics.turns.find(t=>t.session_id===session.id && t.id===turn.id)?.tokens_per_call), format(analytics.turns.find(t=>t.session_id===session.id && t.id===turn.id)?.tokens_per_minute), turn.status
  ]));
  const oldTurn = byId('call-turn').value;
  byId('call-turn').replaceChildren(element('option', 'All turns'));
  byId('call-turn').firstChild.value = 'all';
  session.turns.forEach(turn => { const option = element('option', `Turn ${turn.number}`); option.value = turn.id; byId('call-turn').append(option); });
  if (session.turns.some(turn => turn.id === oldTurn)) byId('call-turn').value = oldTurn;
  renderTimeline();
  table('compactions', ['Recorded at', 'Turn', 'Window number'], session.compactions.map(point => [date(point.at), turnNumbers.get(point.turn_id), format(point.window_number)]));
  table('session-limits', ['Recorded at', 'Bucket', 'Primary used', 'Primary reset', 'Secondary used', 'Secondary reset'], session.rate_limits.map(item => [
    date(item.at), item.limit_id, item.primary?.used_percent == null ? '—' : `${item.primary.used_percent}%`, item.primary?.resets_at == null ? '—' : date(item.primary.resets_at * 1000),
    item.secondary?.used_percent == null ? '—' : `${item.secondary.used_percent}%`, item.secondary?.resets_at == null ? '—' : date(item.secondary.resets_at * 1000)
  ]));
}

function validCacheRatio(usage) {
  return usage && Number.isFinite(usage.input_tokens) && usage.input_tokens>0 && Number.isFinite(usage.cached_input_tokens) && usage.cached_input_tokens>=0 && usage.cached_input_tokens<=usage.input_tokens ? usage.cached_input_tokens/usage.input_tokens:null;
}
function contextForCall(sessionId, callId) {
  if (!callId) return '—';
  const own=analytics.contextChanges.filter(c=>c.session_id===sessionId && c.call_id===callId);
  if (own.length) return own.map(c=>c.delta==null?c.reason:`${c.delta>0?'+':''}${format(c.delta)}`).join(', ');
  return analytics.contextChanges.some(c=>c.session_id===sessionId && c.call_id==null && c.call_ids?.includes(callId)) ? 'Shared interval':'—';
}
function renderCacheTrend(target, points) {
  const container=byId(target);container.replaceChildren();
  const valid=points.filter(p=>p.cache_ratio!=null);
  if (!valid.length) {container.append(element('p','No complete input/cache observations.','empty'));return;}
  const svg=svgElement('svg',{viewBox:'0 0 640 210',role:'img','aria-label':`${valid.length} cache-hit observations, from ${percent(valid[0].cache_ratio)} to ${percent(valid.at(-1).cache_ratio)}. Missing observations are gaps.`});
  for (const fraction of [0,.5,1]) {
    const y=170-fraction*140;svg.append(svgElement('line',{x1:48,y1:y,x2:628,y2:y,stroke:'var(--line)'}));
    const label=svgElement('text',{x:40,y:y+4,'text-anchor':'end',class:'axis-label'});label.textContent=`${fraction*100}%`;svg.append(label);
  }
  let segment=[];
  const flush=()=>{if(segment.length)svg.append(svgElement('polyline',{points:segment.join(' '),fill:'none',stroke:'var(--accent)','stroke-width':2}));segment=[];};
  points.forEach((point,index)=>{
    if(point.cache_ratio==null){flush();return;}
    const x=52+(points.length>1?index/(points.length-1):.5)*570,y=170-point.cache_ratio*140;
    segment.push(`${x},${y}`);
    if(points.length===1)svg.append(svgElement('circle',{cx:x,cy:y,r:3,fill:'var(--accent)'}));
  });flush();
  for (const [x,text,anchor] of [[52,valid[0].key,'start'],[622,valid.at(-1).key,'end']]) {
    const label=svgElement('text',{x,y:199,'text-anchor':anchor,class:'axis-label'});label.textContent=text;svg.append(label);
  }
  container.append(svg);
}
function renderTurnComparison() {
  const field=({cost:'known_usd',tokens:'tokens','per-call':'tokens_per_call','per-minute':'tokens_per_minute'})[byId('turn-sort').value];
  const turns=[...analytics.turns].sort((a,b)=>(b[field]??-1)-(a[field]??-1));
  table('turn-comparison',['Session / turn','Tokens','Inner executions','Tokens / execution','Tokens / minute','API-equivalent cost⁸','Priced responses'],turns.map(turn=>{
    const button=element('button',`${turn.session_id.slice(0,8)} / ${turn.number}`,'session-button');
    button.addEventListener('click',()=>{selectedId=turn.session_id;showPage('sessions');renderDetail();byId('detail').scrollIntoView({behavior:'smooth',block:'start'});});
    return [button,format(turn.tokens),format(turn.calls),format(turn.tokens_per_call),format(turn.tokens_per_minute),costText(turn),`${turn.priced}/${turn.responses}`];
  }));
}
function renderAnalytics() {
  const summary=analytics.summary;
  byId('cost-overview').replaceChildren(
    card(summary.usd==null?'Priced subtotal⁸':'API-equivalent cost⁸',summary.priced?money(summary.known_usd):'—','Not what you pay on a subscription'),
    card('Pricing coverage',`${summary.priced}/${summary.responses}`,'Responses with complete rates and counters'),
    card('Pricing basis',preferences.speed==='recorded'?'Recorded tier':`${preferences.speed} scenario`,'Current catalog rates or local overrides'),
    card('Projects',format(analytics.projects.filter(p=>p.id!=='unassigned').length),'Hashed working directories'));
  byId('price-coverage').textContent=summary.priced===summary.responses && summary.responses ? 'All observed responses have token-price estimates. Separate service/tool fees and regional premiums are excluded (note 8).' : `${summary.responses-summary.priced} responses are unpriced. Partial subtotals omit them; open rates and coverage for the reasons.`;
  const groups=new Map();
  for(const record of analytics.records) {
    const key=record.model+'|'+(record.cost.tier || 'unknown');
    if(!groups.has(key))groups.set(key,[]);groups.get(key).push(record);
  }
  table('price-table',['Model','API tier','Responses','Priced','Reason / rate source'],[...groups.values()].map(records=>[
    records[0].model,records[0].cost.tier || 'unknown',format(records.length),format(records.filter(r=>r.cost.usd!=null).length),
    [...new Set(records.map(r=>r.cost.reason || (r.cost.source==='override'?'Local override':'Published API rate')))].join('; ')
  ]));
  table('reasoning-table',['Model','Effort','Reasoning tokens','Covered output','Reasoning share','Covered responses','API-equivalent cost⁸'],analytics.reasoning.map(row=>[
    row.model,row.effort,format(row.reasoning),format(row.output),percent(row.share),`${row.covered}/${row.responses}`,costText(row)
  ]));
  const periods=ObservatoryMetrics.rollups(analytics.records,byId('period-group').value);
  renderCacheTrend('cache-trend',periods.map(p=>({...p,key:p.key})));
  table('cache-health',['Period','Input','Cached input','Cache hit','Change','Signal'],periods.map((row,index)=>{
    const previous=periods[index-1];
    const change=row.cache_ratio!=null && previous?.cache_ratio!=null ? row.cache_ratio-previous.cache_ratio:null;
    const collapsed=change!=null && previous.input>=10000 && row.input>=10000 && previous.cache_ratio>=.8 && row.cache_ratio<=.5 && change<=-.3;
    return [row.key,format(row.input),format(row.cached),percent(row.cache_ratio),change==null?'—':`${change>0?'+':''}${(change*100).toFixed(1)} pp`,collapsed?'Cache-hit drop':'—'];
  }));
  const windows=analytics.windows.filter(w=>byId('window-group').value==='all' || String(w.minutes)===byId('window-group').value);
  table('window-table',['Bucket','Window start','Reset','Observed tokens','Tokens / hour','API-equivalent cost⁸','Account used','Account pp / hour','Observation'],windows.map(row=>[
    `${row.limit_id} / ${row.bucket}`,date(row.start),date(row.end),format(row.tokens),format(row.tokens_per_hour),costText(row),row.used_percent==null?'—':`${row.used_percent}%`,format(row.account_points_per_hour),`${date(row.at)}${row.stale?' · historical/stale':''}`
  ]));
  const tools=[...analytics.tools].sort((a,b)=>Math.abs(b.context_delta)-Math.abs(a.context_delta));
  table('tool-context',['Tool','Executions','Net post-call input change⁹','Single-call intervals','Recorded output bytes'],tools.map(row=>[
    row.name,format(row.calls),row.intervals?`${row.context_delta>0?'+':''}${format(row.context_delta)}`:'—',format(row.intervals),row.byte_calls?format(row.output_bytes):'—'
  ]));
  table('context-intervals',['Session','Turn','Next response at','Calls','Signed input change⁹','Interpretation'],analytics.contextChanges.filter(row=>row.call_id==null || row.delta==null).map(row=>[row.session_id.slice(0,8),row.turn_id.slice(0,8),date(row.at),row.calls,row.delta==null?'—':`${row.delta>0?'+':''}${format(row.delta)}`,row.reason]));
  const oldProject=byId('project-filter').value;
  const projects=[...new Set(snapshot.sessions.flatMap(sessionProjects))].sort();
  byId('project-filter').replaceChildren(element('option','All projects'));byId('project-filter').firstChild.value='all';
  for(const id of projects){const option=element('option',projectName(id));option.value=id;byId('project-filter').append(option);}
  if(projects.includes(oldProject))byId('project-filter').value=oldProject;
  if(!document.activeElement?.classList.contains('project-label')) table('projects',['Project','Local label','Sessions','Observed tokens','API-equivalent cost⁸','Priced responses'],[...analytics.projects].sort((a,b)=>b.known_usd-a.known_usd).map(row=>{
    const input=element('input',null,'project-label');input.type='text';input.maxLength=80;input.value=preferences.labels[row.id] || '';input.placeholder=row.id==='unassigned'?'No cwd recorded':'Name this project';input.disabled=row.id==='unassigned';input.setAttribute('aria-label',`Local label for ${row.id}`);
    input.addEventListener('change',()=>{const label=input.value.trim();if(label)preferences.labels[row.id]=label;else delete preferences.labels[row.id];byId('project-status').textContent=persistPreferences();renderAnalytics();renderSessions();});
    return [projectName(row.id),input,format(row.sessions),format(row.tokens),costText(row),`${row.priced}/${row.responses}`];
  }));
  renderTurnComparison();
}

function render() {
  analytics = ObservatoryMetrics.analyze(snapshot,{speed:preferences.speed,overrides:preferences.overrides});
  renderAnalytics();
  const summary = snapshot.summary;
  const usage = summary.usage;
  const uncached = usage.input_tokens != null && usage.cached_input_tokens != null ? usage.input_tokens - usage.cached_input_tokens : null;
  byId('overview').replaceChildren(
    card('Observed input tokens', compact(usage.input_tokens), `${format(uncached)} uncached · derived difference`),
    card('Cached input tokens', compact(usage.cached_input_tokens), `${summary.cache_ratio == null ? '—' : (summary.cache_ratio * 100).toFixed(1) + '%'} of input · included in input`),
    card('Output tokens', compact(usage.output_tokens), `${format(usage.reasoning_output_tokens)} reasoning tokens · included in output`),
    card('Recorded total tokens', compact(usage.total_tokens), `${summary.measured_sessions}/${snapshot.coverage.sessions} sessions have observed usage`)
  );
  renderLimits();
  renderVisuals();
  breakdown('kinds', Object.entries(summary.by_kind).map(([kind, data]) => [kindLabel(kind), data.usage.total_tokens]));
  breakdown('models', Object.entries(summary.by_model).sort((first, second) => (second[1].usage.total_tokens || 0) - (first[1].usage.total_tokens || 0)).map(([model, data]) => [model, data.usage.total_tokens]));
  byId('recommendations').replaceChildren(...summary.recommendations.map(item => {
    const node = element('article', null, 'recommendation');
    node.append(element('strong', item.title), element('p', item.evidence, 'evidence'), element('p', item.action), element('div', item.confidence, 'subtle'));
    return node;
  }));
  if (!summary.recommendations.length) byId('recommendations').append(element('p', 'No strong signals in the observed data.', 'subtle'));
  const oldModel = byId('model').value;
  byId('model').replaceChildren(element('option', 'All models'));
  byId('model').firstChild.value = 'all';
  const models = [...new Set(snapshot.sessions.flatMap(session => session.models))].sort();
  models.forEach(model => { const option = element('option', model); option.value = model; byId('model').append(option); });
  if (models.includes(oldModel)) byId('model').value = oldModel;
  renderSessions();
  renderTools();
  renderDetail();
  const coverage = snapshot.coverage;
  byId('coverage').textContent = `${coverage.sessions} sessions across ${coverage.log_files} readable log files · ${coverage.compressed_skipped} compressed archives skipped · ${coverage.unreadable_files} unreadable files · ${coverage.invalid_lines} skipped lines · ${coverage.unknown_events} unsupported events or items · ${summary.compactions} committed compactions · ${summary.duplicate_responses} cross-session response duplicates excluded.`;
  const config = snapshot.config;
  byId('config').textContent = config.status === 'measured' ? `Configured default: ${config.model} · reasoning: ${config.reasoning_effort} · service: ${config.service_tier}. MCP servers: ${config.mcp_servers.map(server => `${server.name} (${server.enabled ? 'enabled' : 'disabled'})`).join(', ') || 'none'} · ${config.plugin_count ?? '—'} plugin entries. Config defaults can differ from the model recorded in a turn.` : 'Configuration could not be read; log metrics remain available.';
  byId('status').textContent = `Updated ${date(snapshot.generated_at)} · read-only access · memory storage · no AI calls`;
}

async function refresh() {
  if (loading) return;
  if (!accessToken) { byId('auth-help').hidden = false; byId('status').textContent = 'A local access link is required.'; return; }
  loading = true;
  byId('refresh').disabled = true;
  try {
    const response = await fetch('/api/snapshot', { headers: { 'X-Observatory-Token': accessToken }, cache: 'no-store', credentials: 'omit' });
    if (!response.ok) throw new Error(response.status === 401 ? 'This access link has expired. Open the link printed by the current server.' : 'Local metrics could not be read. Check that the observatory is running.');
    snapshot = await response.json();
    render();
  } catch (error) {
    byId('status').textContent = error.message;
  } finally {
    loading = false;
    byId('refresh').disabled = false;
  }
}

byId('pricing-speed').value = preferences.speed;
byId('price-overrides').value = JSON.stringify(preferences.overrides,null,2);
byId('pricing-speed').addEventListener('change',()=>{ preferences.speed=byId('pricing-speed').value; byId('price-status').textContent=persistPreferences(); if(snapshot) render(); });
byId('apply-prices').addEventListener('click',()=>{
  try {
    const overrides=ObservatoryMetrics.validatePrices(JSON.parse(byId('price-overrides').value || '{}'));
    preferences.overrides=overrides; byId('price-status').textContent=persistPreferences(); if(snapshot) render();
  } catch(error) { byId('price-status').textContent=`Rates were not changed: ${error.message}`; }
});
byId('reset-prices').addEventListener('click',()=>{ preferences.overrides=Object.create(null); byId('price-overrides').value='{}'; byId('price-status').textContent=persistPreferences(); if(snapshot) render(); });
byId('period-group').addEventListener('change',()=>{if(snapshot) {renderVisuals();renderAnalytics();}});
byId('window-group').addEventListener('change',()=>snapshot && renderAnalytics());
byId('turn-sort').addEventListener('change',()=>snapshot && renderTurnComparison());
byId('refresh').addEventListener('click', refresh);
const tabButtons=[...document.querySelectorAll('[data-page]')];
tabButtons.forEach((button,index)=>{
  button.addEventListener('click',()=>showPage(button.dataset.page));
  button.addEventListener('keydown',event=>{
    const movement={ArrowRight:(index+1)%tabButtons.length,ArrowLeft:(index+tabButtons.length-1)%tabButtons.length,Home:0,End:tabButtons.length-1};
    if (!(event.key in movement)) return;
    event.preventDefault();
    const target=tabButtons[movement[event.key]];
    showPage(target.dataset.page);
    target.focus();
  });
});
['kind', 'model', 'sort', 'project-filter'].forEach(id => byId(id).addEventListener('change', () => snapshot && renderSessions()));
['call-layer', 'call-turn'].forEach(id => byId(id).addEventListener('change', () => { callLimit = 200; renderTimeline(); }));
byId('more-calls').addEventListener('click', () => { callLimit += 200; renderTimeline(); });
byId('close-detail').addEventListener('click', () => { selectedId = null; byId('detail').hidden = true; });
byId('export').addEventListener('click', () => {
  if (!snapshot) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' }));
  const anchor = element('a'); anchor.href = url; anchor.download = 'codex-observatory-redacted.json'; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
setInterval(() => { if (byId('live').checked && !document.hidden) refresh(); }, 15000);
refresh();
