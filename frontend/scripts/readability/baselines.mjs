export function suppressionCounts(baseline) {
  const counts = new Map();
  for (const [file, rules] of Object.entries(baseline)) {
    for (const [rule, entry] of Object.entries(rules)) {
      if (!Number.isSafeInteger(entry.count) || entry.count < 1) {
        throw new Error(`Invalid suppression count: ${file}: ${rule}`);
      }
      counts.set(`${file}: ${rule}`, entry.count);
    }
  }
  return counts;
}

export function violationKey(violation) {
  return JSON.stringify([
    violation.type, violation.rule.name, violation.from, violation.to,
    violation.cycle?.map(step => step.name),
  ]);
}

export function dependencyCounts(baseline) {
  const counts = new Map();
  for (const violation of baseline) {
    const key = violationKey(violation);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return counts;
}

export function assertShrinks(previous, current, label) {
  const increases = [...current].filter(([key, count]) => count > (previous.get(key) ?? 0));
  if (increases.length) {
    throw new Error(`${label} baseline enlarged:\n${increases.map(([key]) => key).join('\n')}`);
  }
}
