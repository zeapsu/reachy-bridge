import {
  ACESFilmicToneMapping,
  CanvasTexture,
  DirectionalLight,
  Group,
  HemisphereLight,
  type Material,
  Matrix4,
  Mesh,
  MeshBasicMaterial,
  MeshStandardMaterial,
  PerspectiveCamera,
  PlaneGeometry,
  Scene,
  SRGBColorSpace,
  Texture,
  WebGLRenderer,
} from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import modelUrl from "../assets/reachy_mini.glb?url";
import type { RobotState } from "../api.ts";
import { reduceMotion } from "../hooks/reduceMotion.ts";
import { finite } from "../logic/pose.ts";
import { RIG, partMatrices } from "../logic/rig.ts";

/** Easing rate (1/s) toward the latest sample: the stream is 10 Hz, frames are 60. */
const SMOOTH = 12;
/** Closer than this to the target counts as there (radians, metres). Above
 * encoder noise, so a robot holding still lets the view go idle. */
const SETTLED_RAD = 2e-3;
const SETTLED_M = 1e-4;

interface Shown {
  head_pose: Record<string, number>;
  head_joints: number[];
  body_yaw: number;
  antennas_position: number[];
}

const NEUTRAL: Shown = {
  head_pose: { x: 0, y: 0, z: 0, roll: 0, pitch: 0, yaw: 0 },
  head_joints: [0, ...RIG.horns.map((h) => h.neutral)],
  body_yaw: 0,
  antennas_position: [RIG.antennas.right.neutral, RIG.antennas.left.neutral],
};

function disposeMaterial(material: Material | Material[]): void {
  for (const m of Array.isArray(material) ? material : [material]) {
    for (const value of Object.values(m)) if (value instanceof Texture) value.dispose();
    m.dispose();
  }
}

/** `value` as `length` finite numbers, or null. */
function numbers(value: unknown, length: number): number[] | null {
  if (!Array.isArray(value) || value.length !== length) return null;
  const out = value.map(finite);
  return out.every((v) => v != null) ? (out as number[]) : null;
}

/**
 * The 3D view: Reachy Mini's own model (tools/model), posed from the daemon's
 * state stream. Renders only while running, and only when something moved.
 */
export class RobotView {
  private renderer: WebGLRenderer;
  private scene = new Scene();
  private camera = new PerspectiveCamera(24, 1, 0.01, 10);
  private parts = new Map<string, Mesh>();
  private shown: Shown = structuredClone(NEUTRAL);
  private target: Shown = structuredClone(NEUTRAL);
  private frame = 0;
  private last = 0;
  private running = false;
  private disposed = false;
  private resize: ResizeObserver;

  constructor(private canvas: HTMLCanvasElement) {
    this.renderer = new WebGLRenderer({ canvas, antialias: true, alpha: true });
    this.renderer.outputColorSpace = SRGBColorSpace;
    this.renderer.toneMapping = ACESFilmicToneMapping;
    this.scene.add(new HemisphereLight(0xdfe8f2, 0x2a2622, 2.2));
    const key = new DirectionalLight(0xffffff, 2.4);
    key.position.set(0.6, 0.9, 0.5);
    const rim = new DirectionalLight(0xffd7a8, 1.4);
    rim.position.set(-0.7, 0.4, -0.6);
    this.scene.add(key, rim, this.contactShadow());

    // The model is z-up and faces +x; three.js is y-up.
    const robot = new Group();
    robot.rotation.x = -Math.PI / 2;
    this.scene.add(robot);
    new GLTFLoader().load(
      modelUrl,
      (gltf) => {
        if (this.disposed) return;
        gltf.scene.traverse((o) => {
          if (!(o instanceof Mesh)) return;
          disposeMaterial(o.material);
          o.material = new MeshStandardMaterial({ vertexColors: true, roughness: 0.5, metalness: 0.05 });
          o.matrixAutoUpdate = false;
          this.parts.set(o.name, o);
        });
        for (const mesh of this.parts.values()) robot.add(mesh);
        this.apply(this.shown);
        this.draw();
      },
      undefined,
      (error) => console.error("3D view: model failed to load", error),
    );

    // Frames the robot from the base to the antenna tips (about 0.5 m).
    this.camera.position.set(1.27, 0.36, 0.43);
    this.camera.lookAt(0, 0.23, 0);
    this.resize = new ResizeObserver(() => this.fit());
    this.resize.observe(canvas);
  }

  /** Latest daemon state; missing fields keep their last value. The stream
   * sends head_pose as x/y/z/roll/pitch/yaw; the matrix form is ignored. */
  setState(state: RobotState): void {
    const t = this.target;
    const pose = state.head_pose;
    if (pose && !Array.isArray(pose)) {
      for (const key of Object.keys(t.head_pose)) {
        const value = finite(pose[key as keyof typeof pose]);
        if (value != null) t.head_pose[key] = value;
      }
    }
    const joints = numbers(state.head_joints, 7);
    if (joints) t.head_joints = joints;
    const yaw = finite(state.body_yaw);
    if (yaw != null) t.body_yaw = yaw;
    const antennas = numbers(state.antennas_position, 2);
    if (antennas) t.antennas_position = antennas;
    this.wake();
  }

  /** Back to the neutral pose (robot off). */
  reset(): void {
    this.target = structuredClone(NEUTRAL);
    this.wake();
  }

  /** Render only while the 3D view is on screen. */
  setRunning(running: boolean): void {
    this.running = running;
    if (running) this.wake();
    else if (this.frame) cancelAnimationFrame(this.frame), (this.frame = 0);
  }

  dispose(): void {
    this.disposed = true;
    this.setRunning(false);
    this.resize.disconnect();
    this.scene.traverse((o) => {
      if (!(o instanceof Mesh)) return;
      o.geometry.dispose();
      disposeMaterial(o.material);
    });
    this.renderer.dispose();
  }

  /** Start the frame loop if it is idle. */
  private wake(): void {
    if (!this.running || this.frame || this.disposed) return;
    this.last = performance.now();
    this.schedule();
  }

  private schedule(): void {
    this.frame = requestAnimationFrame((now) => this.tick(now));
  }

  private tick(now: number): void {
    this.frame = 0;
    const dt = Math.min(0.1, Math.max(0, (now - this.last) / 1000));
    const k = reduceMotion ? 1 : 1 - Math.exp(-dt * SMOOTH);
    this.last = now;
    const s = this.shown;
    const t = this.target;
    let moving = false;
    const ease = (a: number, b: number, settled = SETTLED_RAD) => {
      const next = a + (b - a) * k;
      if (Math.abs(b - next) <= settled) return b;
      moving = true;
      return next;
    };
    for (const key of Object.keys(s.head_pose)) {
      const metres = key === "x" || key === "y" || key === "z";
      s.head_pose[key] = ease(s.head_pose[key], t.head_pose[key] ?? 0, metres ? SETTLED_M : SETTLED_RAD);
    }
    s.head_joints = s.head_joints.map((a, i) => ease(a, t.head_joints[i]));
    s.body_yaw = ease(s.body_yaw, t.body_yaw);
    s.antennas_position = s.antennas_position.map((a, i) => ease(a, t.antennas_position[i]));
    this.apply(s);
    this.draw();
    if (moving && this.running) this.schedule();
  }

  private apply(state: Shown): void {
    const matrices = partMatrices(state);
    for (const [name, mesh] of this.parts) mesh.matrix.copy(matrices[name] ?? new Matrix4());
  }

  private fit(): void {
    const { clientWidth: w, clientHeight: h } = this.canvas;
    if (!w || !h) return;
    this.renderer.setPixelRatio(Math.min(2, devicePixelRatio || 1));
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    // Narrow stages zoom out so the robot still fits across.
    this.camera.zoom = Math.min(1, this.camera.aspect / 0.8);
    this.camera.updateProjectionMatrix();
    this.draw();
  }

  private draw(): void {
    this.renderer.render(this.scene, this.camera);
  }

  private contactShadow(): Mesh {
    const c = document.createElement("canvas");
    c.width = c.height = 128;
    const g = c.getContext("2d");
    if (g) {
      const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
      grad.addColorStop(0, "rgba(0,0,0,0.45)");
      grad.addColorStop(1, "rgba(0,0,0,0)");
      g.fillStyle = grad;
      g.fillRect(0, 0, 128, 128);
    }
    const shadow = new Mesh(
      new PlaneGeometry(0.26, 0.26),
      new MeshBasicMaterial({ map: new CanvasTexture(c), transparent: true, depthWrite: false }),
    );
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.y = 0.001;
    return shadow;
  }
}
