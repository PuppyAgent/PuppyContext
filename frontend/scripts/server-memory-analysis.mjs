export function analyzeHeap(samples, { maxGrowthBytes = 32 * 1024 * 1024 } = {}) {
  if (samples.length < 6) throw new Error('At least six post-warmup samples are required.');
  if (samples.some(sample => !Number.isFinite(sample.heapUsed) || !Number.isFinite(sample.timestamp))) {
    throw new Error('Invalid heap sample.');
  }
  const median = values => {
    const sorted = [...values].sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  };
  const size = Math.floor(samples.length / 3);
  const baselineBytes = median(samples.slice(0, size).map(sample => sample.heapUsed));
  const tailBytes = median(samples.slice(-size).map(sample => sample.heapUsed));
  const growthBytes = tailBytes - baselineBytes;
  const times = samples.map(sample => (sample.timestamp - samples[0].timestamp) / 1000);
  const meanX = times.reduce((a, b) => a + b, 0) / times.length;
  const meanY = samples.reduce((sum, sample) => sum + sample.heapUsed, 0) / samples.length;
  const variance = times.reduce((sum, time) => sum + (time - meanX) ** 2, 0);
  const bytesPerSecond = variance === 0 ? 0 : samples.reduce((sum, sample, i) =>
    sum + (times[i] - meanX) * (sample.heapUsed - meanY), 0) / variance;
  return {
    baselineBytes, tailBytes, growthBytes, bytesPerSecond, maxGrowthBytes,
    passed: growthBytes <= maxGrowthBytes,
    // A passing finite test is evidence for this window, not proof that no
    // slower leak exists. Reports retain the trend and all raw samples.
  };
}
