// Typed client for the controller API, re-exported from one module per area
// (src/api/*.ts, #164) so a feature adds its paths and types in its own file.

import { appsPaths } from "./api/apps.ts";
import { authPaths } from "./api/auth.ts";
import { controlPaths } from "./api/control.ts";
import { expressionsPaths } from "./api/expressions.ts";
import { hfPaths } from "./api/hf.ts";
import { mediaPaths } from "./api/media.ts";
import { powerPaths } from "./api/power.ts";
import { simPaths } from "./api/sim.ts";
import { systemPaths } from "./api/system.ts";
import { voicePaths } from "./api/voice.ts";

export * from "./api/apps.ts";
export * from "./api/client.ts";
export * from "./api/control.ts";
export * from "./api/expressions.ts";
export * from "./api/hf.ts";
export * from "./api/media.ts";
export * from "./api/power.ts";
export * from "./api/sim.ts";
export * from "./api/system.ts";
export * from "./api/voice.ts";

export const paths = {
  ...powerPaths,
  ...simPaths,
  ...appsPaths,
  ...expressionsPaths,
  ...mediaPaths,
  ...systemPaths,
  ...authPaths,
  ...controlPaths,
  ...hfPaths,
  ...voicePaths,
} as const;
