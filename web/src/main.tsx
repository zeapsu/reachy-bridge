import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
// Base styles first, so each component's own stylesheet can override them.
import "./styles/base.css";
import { App } from "./App.tsx";
import { Setup } from "./components/Setup.tsx";
import { startGlass } from "./glass.ts";
import { isSetupStatus } from "./logic/setup.ts";

startGlass();

const root = createRoot(document.getElementById("root")!);
const render = (page: ReactNode) => root.render(<StrictMode>{page}</StrictMode>);

// The same bundle is the setup page while `reachy-bridge setup` runs (#135):
// only that port answers /api/setup/status. On the dashboard it's a 404 (or
// a 401 before login), so the dashboard renders as before.
fetch("/api/setup/status", { headers: { Accept: "application/json" } })
  .then((response) => (response.ok ? response.json() : null))
  .catch(() => null)
  .then((body: unknown) => render(isSetupStatus(body) ? <Setup initial={body} /> : <App />));
