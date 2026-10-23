import { useEffect, useRef } from "react";
import { reduceMotion } from "../hooks/reduceMotion.ts";

const TAU = Math.PI * 2;

function roundedRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
}

function drawGizmo(ctx: CanvasRenderingContext2D, x: number, y: number, time: number) {
  const axes: [string, number, string][] = [
    ["#ff6b6b", 0, "x"],
    ["#5ee6c3", 2.09, "y"],
    ["#6fe3ff", 4.18, "z"],
  ];
  ctx.lineWidth = 2;
  ctx.font = "9px 'JetBrains Mono'";
  for (const [color, offset, label] of axes) {
    const angle = offset + Math.sin(time * 0.4) * 0.4;
    const endX = x + Math.cos(angle) * 16;
    const endY = y - Math.sin(angle) * 9.6;
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(endX, endY);
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.fillText(label, endX + 2, endY);
  }
}

/** Decorative "coming soon" simulation preview, drawn only while shown. */
export function SimCanvas({ running }: { running: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const time = useRef(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!running || !canvas || !ctx) return;
    let raf = 0;
    let active = true;

    const draw = () => {
      const rect = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * ratio));
      canvas.height = Math.max(1, Math.floor(rect.height * ratio));
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      const width = rect.width;
      const height = rect.height;
      const t = (time.current += 0.016);

      const gradient = ctx.createLinearGradient(0, 0, 0, height);
      gradient.addColorStop(0, "#0a1018");
      gradient.addColorStop(1, "#05080e");
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, width, height);

      const centerX = width / 2;
      const horizon = height * 0.46;
      ctx.strokeStyle = "rgba(111,227,255,0.16)";
      ctx.lineWidth = 1;
      for (let i = -6; i <= 6; i += 1) {
        ctx.beginPath();
        ctx.moveTo(centerX + i * 14, horizon);
        ctx.lineTo(centerX + (i * width * 0.5) / 6, height);
        ctx.stroke();
      }
      for (let j = 1; j <= 7; j += 1) {
        const y = horizon + (height - horizon) * Math.pow(j / 7, 2);
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(width, y);
        ctx.stroke();
      }

      const skew = Math.sin(t * 0.4) * 5;
      const bob = Math.sin(t * 1.2) * 4;
      ctx.save();
      ctx.translate(centerX, horizon + (height - horizon) * 0.34 + bob);
      ctx.fillStyle = "rgba(0,0,0,0.35)";
      ctx.beginPath();
      ctx.ellipse(0, 46, 42, 10, 0, 0, TAU);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.10)";
      ctx.strokeStyle = "rgba(255,255,255,0.22)";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.ellipse(0, 36, 34, 12, 0, 0, TAU);
      ctx.fill();
      ctx.stroke();
      roundedRect(ctx, -32 + skew, -34, 64, 64, 20);
      const headGradient = ctx.createLinearGradient(0, -34, 0, 30);
      headGradient.addColorStop(0, "rgba(255,255,255,0.22)");
      headGradient.addColorStop(1, "rgba(255,255,255,0.05)");
      ctx.fillStyle = headGradient;
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = "#05060c";
      ctx.beginPath();
      ctx.arc(-12 + skew, -2, 6, 0, TAU);
      ctx.arc(14 + skew, -2, 6, 0, TAU);
      ctx.fill();
      ctx.fillStyle = "#6fe3ff";
      ctx.beginPath();
      ctx.arc(-11 + skew, -3, 2, 0, TAU);
      ctx.arc(15 + skew, -3, 2, 0, TAU);
      ctx.fill();
      ctx.strokeStyle = "#6fe3ff";
      ctx.lineWidth = 3;
      ctx.lineCap = "round";
      const tipL = -30 + skew + Math.sin(t * 2) * 4;
      const tipR = 30 + skew + Math.sin(t * 2 + 1) * 4;
      ctx.beginPath();
      ctx.moveTo(-20 + skew, -34);
      ctx.lineTo(tipL, -64);
      ctx.moveTo(20 + skew, -34);
      ctx.lineTo(tipR, -64);
      ctx.stroke();
      ctx.fillStyle = "#6fe3ff";
      ctx.beginPath();
      ctx.arc(tipL, -64, 4, 0, TAU);
      ctx.arc(tipR, -64, 4, 0, TAU);
      ctx.fill();
      ctx.restore();
      drawGizmo(ctx, 34, height - 34, t);

      if (active && !reduceMotion) raf = requestAnimationFrame(draw);
    };

    draw();
    return () => {
      active = false;
      cancelAnimationFrame(raf);
    };
  }, [running]);

  return <canvas id="sim" ref={canvasRef} aria-hidden="true"></canvas>;
}
