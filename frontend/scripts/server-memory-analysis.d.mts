export function analyzeHeap(samples: { heapUsed: number; timestamp: number }[], options?: { maxGrowthBytes?: number }): {
  baselineBytes: number;
  tailBytes: number;
  growthBytes: number;
  bytesPerSecond: number;
  maxGrowthBytes: number;
  passed: boolean;
};
