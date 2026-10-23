// The dashboard drives this page through the URL hash (#waking, #starting,
// #opening, #failed:<message>), then points the tab at the app's own UI.
import "../style.css";
import "./loading.css";
import { LOADING_PROGRESS, LOADING_STEPS, parseLoadingHash } from "../logic/loading.ts";

const app = new URLSearchParams(window.location.search).get("app") || "app";
const byId = (id: string) => document.getElementById(id)!;

document.title = `Starting ${app}…`;
byId("title").textContent = app;
byId("starting-label").textContent = `Starting ${app}`;

function render(): void {
  const state = parseLoadingHash(window.location.hash);
  const card = byId("card");
  const steps = document.querySelectorAll<HTMLLIElement>(".loading-steps li");
  if (state.kind === "failed") {
    card.classList.add("failed");
    document.title = `${app} didn't start`;
    byId("foot").textContent = `${state.message} You can close this tab.`;
    steps.forEach((li) => li.classList.remove("active"));
    return;
  }
  steps.forEach((li) => {
    const at = LOADING_STEPS.indexOf(li.dataset.step as (typeof LOADING_STEPS)[number]);
    li.classList.toggle("done", at < state.index);
    li.classList.toggle("active", at === state.index);
  });
  byId("bar").style.width = `${LOADING_PROGRESS[LOADING_STEPS[state.index]]}%`;
}

window.addEventListener("hashchange", render);
render();
