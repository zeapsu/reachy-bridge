// Expressions and the recorded-move datasets.

/** GET /api/expressions/current */
export interface ExpressionCurrent {
  active: boolean;
  phase?: "playing" | "returning_home" | string;
  dataset?: string | null;
  move?: string | null;
  uuid?: string | null;
}

export const expressionsPaths = {
  // The dataset id contains a slash and is sent unencoded, as main.js does.
  moveDatasetList: (dataset: string) => `/api/move/recorded-move-datasets/list/${dataset}`,
  expressionPlay: (dataset: string, move: string) =>
    `/api/expressions/play/${dataset}/${encodeURIComponent(move)}`,
  expressionStop: "/api/expressions/stop",
  expressionCurrent: "/api/expressions/current",
} as const;
