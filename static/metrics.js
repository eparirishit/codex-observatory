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
