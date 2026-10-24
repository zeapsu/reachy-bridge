// The steps the app view shows while an app starts (#159).

export const LOADING_STEPS = ["waking", "starting", "opening"] as const;
export type LoadingStep = (typeof LOADING_STEPS)[number];
export const LOADING_PROGRESS: Record<LoadingStep, number> = { waking: 22, starting: 55, opening: 88 };
