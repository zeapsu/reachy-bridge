// Typed client for the controller API, re-exported from one module per area
// (src/api/*.ts, #164) so a feature adds its paths and types in its own file.

import { appsPaths } from "./api/apps.ts";
import { authPaths } from "./api/auth.ts";
import { expressionsPaths } from "./api/expressions.ts";
import { hfPaths } from "./api/hf.ts";
import { mediaPaths } from "./api/media.ts";
import { powerPaths } from "./api/power.ts";
import { simPaths } from "./api/sim.ts";
import { systemPaths } from "./api/system.ts";

export * from "./api/apps.ts";
export * from "./api/client.ts";
export * from "./api/expressions.ts";
export * from "./api/hf.ts";
export * from "./api/media.ts";
export * from "./api/power.ts";
export * from "./api/sim.ts";
export * from "./api/system.ts";

export const paths = {
  ...powerPaths,
  ...simPaths,
  ...appsPaths,
  ...expressionsPaths,
  ...mediaPaths,
  ...systemPaths,
  ...authPaths,
  ...hfPaths,
} as const;
