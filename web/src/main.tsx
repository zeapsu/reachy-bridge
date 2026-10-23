import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App.tsx";
import "./style.css";

// Glass sheen follows the pointer (CSS reads --mx/--my).
document.addEventListener("pointermove", (event) => {
  const glass = (event.target as Element | null)?.closest?.(".glass") as HTMLElement | null;
  if (!glass) return;
  const rect = glass.getBoundingClientRect();
  glass.style.setProperty("--mx", `${((event.clientX - rect.left) / rect.width) * 100}%`);
  glass.style.setProperty("--my", `${((event.clientY - rect.top) / rect.height) * 100}%`);
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
