import { useCallback, useRef } from "react";

/**
 * The live parts of the glass material: refraction and the moving light.
 *
 * Refraction: each `[data-lens]` element gets its own SVG displacement map, a
 * rounded rectangle whose rim pulls the backdrop inward like a thick lens,
 * with the three colour channels displaced by slightly different amounts for
 * dispersion. Only Chromium applies an SVG filter as a backdrop filter, and
 * it costs GPU time, so it runs on desktop Chromium only; everywhere else the
 * frosted blur in base.css stays.
 *
 * Light: the pointer is the light source. Each glass element's rim and glare
 * turn toward it (--lx, --ly, --ang in base.css).
 */

const NS = "http://www.w3.org/2000/svg";

const desktop = typeof matchMedia === "function" && matchMedia("(hover: hover) and (pointer: fine)").matches;
export const lensCapable =
  desktop &&
  typeof CSS !== "undefined" &&
  CSS.supports("backdrop-filter", "url(#x)") &&
  "chrome" in window;

let defs: SVGDefsElement | null = null;
let nextId = 0;

function lensDefs(): SVGDefsElement {
  if (defs) return defs;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("width", "0");
  svg.setAttribute("height", "0");
  svg.setAttribute("aria-hidden", "true");
  svg.style.position = "absolute";
  defs = document.createElementNS(NS, "defs");
  svg.appendChild(defs);
  document.body.appendChild(svg);
  return defs;
}

/** R and G hold the inward normal of a rounded rect, strongest at the rim. */
function lensMap(w: number, h: number, r: number, bezel: number): string {
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return "";
  const img = ctx.createImageData(w, h);
  const d = img.data;
  const hw = w / 2;
  const hh = h / 2;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const px = x + 0.5 - hw;
      const py = y + 0.5 - hh;
      const qx = Math.abs(px) - (hw - r);
      const qy = Math.abs(py) - (hh - r);
      let dist: number;
      let nx = 0;
      let ny = 0;
      if (qx > 0 && qy > 0) {
        const l = Math.hypot(qx, qy);
        dist = l - r;
        nx = (qx / l) * Math.sign(px);
        ny = (qy / l) * Math.sign(py);
      } else if (qx > qy) {
        dist = qx - r;
        nx = Math.sign(px);
      } else {
        dist = qy - r;
        ny = Math.sign(py);
      }
      const inset = -dist;
      let m = 0;
      if (inset >= 0 && inset < bezel) m = Math.sin(((1 - inset / bezel) * Math.PI) / 2) ** 2;
      const i = (y * w + x) * 4;
      d[i] = 128 - nx * m * 127;
      d[i + 1] = 128 - ny * m * 127;
      d[i + 2] = 128;
      d[i + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  return canvas.toDataURL();
}

function buildLens(el: HTMLElement, filter: SVGFilterElement): void {
  const rect = el.getBoundingClientRect();
  const w = Math.max(8, Math.round(rect.width));
  const h = Math.max(8, Math.round(rect.height));
  const size = `${w}x${h}`;
  if (filter.dataset.size === size) return;
  filter.dataset.size = size;
  const style = getComputedStyle(el);
  const r = Math.min(parseFloat(style.borderTopLeftRadius) || 0, w / 2, h / 2);
  const bezel = Math.max(10, Math.min(r * 0.9 + 6, 34, Math.min(w, h) / 2));
  const strength = Math.min(70, bezel * 1.9);
  const frost = (parseFloat(style.getPropertyValue("--frost")) || 18) / 6;
  for (const [k, v] of Object.entries({ x: 0, y: 0, width: w, height: h })) filter.setAttribute(k, String(v));
  filter.setAttribute("filterUnits", "userSpaceOnUse");
  filter.setAttribute("color-interpolation-filters", "sRGB");
  filter.innerHTML = `
    <feGaussianBlur in="SourceGraphic" stdDeviation="${frost}" result="b"/>
    <feImage href="${lensMap(w, h, r, bezel)}" x="0" y="0" width="${w}" height="${h}" preserveAspectRatio="none" result="m"/>
    <feDisplacementMap in="b" in2="m" scale="${strength}" xChannelSelector="R" yChannelSelector="G" result="dr"/>
    <feDisplacementMap in="b" in2="m" scale="${strength * 0.92}" xChannelSelector="R" yChannelSelector="G" result="dg"/>
    <feDisplacementMap in="b" in2="m" scale="${strength * 0.84}" xChannelSelector="R" yChannelSelector="G" result="db"/>
    <feColorMatrix in="dr" type="matrix" values="1 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 1 0" result="r"/>
    <feColorMatrix in="dg" type="matrix" values="0 0 0 0 0  0 1 0 0 0  0 0 0 0 0  0 0 0 1 0" result="g"/>
    <feColorMatrix in="db" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 1 0 0  0 0 0 1 0" result="bl"/>
    <feBlend in="r" in2="g" mode="screen" result="rg"/>
    <feBlend in="rg" in2="bl" mode="screen"/>`;
}

/** Set up refraction on one element; returns its cleanup. */
function attachLens(el: HTMLElement): () => void {
  const filter = document.createElementNS(NS, "filter");
  filter.id = `lens${++nextId}`;
  lensDefs().appendChild(filter);
  el.style.setProperty("--lens", `url(#${filter.id})`);
  // Building a map is a per-pixel loop: wait for a resize to settle.
  let timer = 0;
  const observer = new ResizeObserver(() => {
    clearTimeout(timer);
    timer = window.setTimeout(() => {
      if (el.getClientRects().length) buildLens(el, filter);
    }, filter.dataset.size ? 150 : 0);
  });
  observer.observe(el);
  return () => {
    clearTimeout(timer);
    observer.disconnect();
    filter.remove();
    el.style.removeProperty("--lens");
  };
}

/**
 * A callback ref that gives a `[data-lens]` glass element its refraction
 * (desktop Chromium only), set up when the element mounts.
 */
export function useLens<T extends HTMLElement>(): (el: T | null) => void {
  const cleanup = useRef<(() => void) | null>(null);
  return useCallback((el: T | null) => {
    cleanup.current?.();
    cleanup.current = el && lensCapable ? attachLens(el) : null;
  }, []);
}

/** Start the pointer light and mark the page for refraction. Call once. */
export function startGlass(): void {
  if (lensCapable) document.documentElement.classList.add("refract");
  if (!desktop) return;
  let light = { x: innerWidth * 0.3, y: -200 };
  let queued = false;
  const relight = () => {
    queued = false;
    for (const el of document.querySelectorAll<HTMLElement>(".glass")) {
      const r = el.getBoundingClientRect();
      if (!r.width || r.bottom < 0 || r.top > innerHeight) continue;
      el.style.setProperty("--lx", `${(((light.x - r.left) / r.width) * 100).toFixed(1)}%`);
      el.style.setProperty("--ly", `${(((light.y - r.top) / r.height) * 100).toFixed(1)}%`);
      const angle = (Math.atan2(light.y - (r.top + r.height / 2), light.x - (r.left + r.width / 2)) * 180) / Math.PI;
      el.style.setProperty("--ang", `${(angle + 60).toFixed(0)}deg`);
    }
  };
  const queue = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(relight);
  };
  addEventListener(
    "pointermove",
    (event) => {
      light = { x: event.clientX, y: event.clientY - 120 };
      queue();
    },
    { passive: true },
  );
  addEventListener("scroll", queue, { passive: true });
  addEventListener("resize", queue);
  queue();
}
