import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// Base styles first, so each component's own stylesheet can override them.
import "./styles/base.css";
import { App } from "./App.tsx";
import { startGlass } from "./glass.ts";

startGlass();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
