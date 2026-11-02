import { apiJson, errorText, paths, type CameraAvailability } from "../api.ts";
import { unavailableMessage } from "../logic/camera.ts";

const FIRST_FRAME_TIMEOUT_MS = 9000;
const FIRST_FRAME_CHECK_MS = 100;
const RETRY_DELAY_MS = 750;

export interface CameraConditions {
  /** The viewport shows the camera layer. */
  selected: boolean;
  daemonOnline: boolean;
  visible: boolean;
}

/**
 * The live-camera MJPEG <img>, ported from main.js. A generation counter
 * invalidates late load/error/timer callbacks from a superseded stream, the
 * first frame must arrive within 9 s, and failures retry after 750 ms while
 * the camera should be connected. DOM writes go straight to the elements.
 */
export class CameraController {
  private c: CameraConditions = { selected: false, daemonOnline: false, visible: true };
  private generation = 0;
  private availabilityRequest = 0;
  private availabilityInflight = false;
  private firstFrameTimer: ReturnType<typeof setTimeout> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private onLoad: (() => void) | null = null;
  private onError: (() => void) | null = null;

  constructor(
    private readonly img: HTMLImageElement,
    private readonly placeholder: HTMLElement,
    private readonly setMeta: (text: string) => void,
  ) {}

  private shouldConnect(): boolean {
    return this.c.selected && this.c.daemonOnline && this.c.visible;
  }

  /** Apply new conditions, running the same transitions main.js ran. */
  update(next: CameraConditions): void {
    const prev = this.c;
    this.c = next;
    if (prev.selected && !next.selected) {
      this.disconnect();
      return;
    }
    if (!next.selected) return;
    if (!prev.selected) {
      // Just switched to the camera layer.
      if (!this.shouldConnect()) {
        const offline = !next.daemonOnline;
        this.disconnect(offline ? "Camera off" : "Camera hidden");
        this.setMeta(offline ? "live camera · offline" : "live camera · hidden");
      } else {
        void this.refreshAvailability();
      }
      return;
    }
    if (!next.daemonOnline) {
      if (prev.daemonOnline) {
        this.disconnect("Camera off");
        this.setMeta("live camera · offline");
      }
      return;
    }
    if (!next.visible) {
      if (prev.visible) {
        this.disconnect("Camera hidden");
        this.setMeta("live camera · hidden");
      }
      return;
    }
    if (!prev.daemonOnline || !prev.visible) void this.refreshAvailability();
  }

  /** 1 Hz nudge (main.js did this from the apps poll): reconnect an idle camera. */
  nudge(): void {
    if (
      this.shouldConnect() &&
      !this.img.getAttribute("src") &&
      this.retryTimer === null &&
      !this.availabilityInflight
    ) {
      void this.refreshAvailability();
    }
  }

  dispose(): void {
    this.disconnect();
  }

  private setPlaceholder(text: string, error = false): void {
    this.placeholder.textContent = text;
    this.placeholder.hidden = false;
    this.placeholder.classList.toggle("error", error);
  }

  private clearFirstFrameTimer(): void {
    if (this.firstFrameTimer !== null) {
      clearTimeout(this.firstFrameTimer);
      this.firstFrameTimer = null;
    }
  }

  private clearRetryTimer(): void {
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }

  private clearHandlers(): void {
    if (this.onLoad) this.img.removeEventListener("load", this.onLoad);
    if (this.onError) this.img.removeEventListener("error", this.onError);
    this.onLoad = null;
    this.onError = null;
  }

  private disconnect(message = "", error = false): void {
    this.clearFirstFrameTimer();
    this.clearRetryTimer();
    this.clearHandlers();
    this.generation += 1;
    this.availabilityRequest += 1;
    this.availabilityInflight = false;
    this.img.removeAttribute("src");
    this.img.hidden = true;
    if (message) this.setPlaceholder(message, error);
  }

  private scheduleRetry(): void {
    this.clearRetryTimer();
    if (!this.shouldConnect()) return;
    const generation = this.generation;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (generation !== this.generation || !this.shouldConnect()) return;
      void this.refreshAvailability();
    }, RETRY_DELAY_MS);
  }

  private markLive(generation: number): boolean {
    if (generation !== this.generation || !this.shouldConnect()) return false;
    if (!this.img.getAttribute("src") || !(this.img.naturalWidth > 0 && this.img.naturalHeight > 0)) return false;
    this.clearFirstFrameTimer();
    this.img.hidden = false;
    this.placeholder.hidden = true;
    this.placeholder.classList.remove("error");
    this.setMeta("live camera · live");
    return true;
  }

  private fail(generation: number, message: string): void {
    if (generation !== this.generation) return;
    if (!this.shouldConnect()) {
      this.disconnect();
      return;
    }
    this.disconnect(message, true);
    this.setMeta("live camera · error");
    this.scheduleRetry();
  }

  private watchFirstFrame(generation: number): void {
    this.clearFirstFrameTimer();
    const deadline = Date.now() + FIRST_FRAME_TIMEOUT_MS;
    const check = () => {
      this.firstFrameTimer = null;
      if (generation !== this.generation) return;
      if (!this.shouldConnect()) {
        this.disconnect();
        return;
      }
      if (this.markLive(generation)) return;
      if (Date.now() >= deadline) {
        this.fail(generation, "Live camera timed out");
        return;
      }
      this.firstFrameTimer = setTimeout(check, FIRST_FRAME_CHECK_MS);
    };
    this.firstFrameTimer = setTimeout(check, FIRST_FRAME_CHECK_MS);
  }

  private connect(): void {
    if (!this.shouldConnect() || this.img.getAttribute("src")) return;
    this.clearFirstFrameTimer();
    this.clearRetryTimer();
    this.clearHandlers();
    const generation = this.generation + 1;
    this.generation = generation;
    this.setPlaceholder("Connecting to live camera");
    this.img.hidden = true;
    this.onLoad = () => void this.markLive(generation);
    this.onError = () => this.fail(generation, "Live camera unavailable");
    this.img.addEventListener("load", this.onLoad);
    this.img.addEventListener("error", this.onError);
    this.img.src = paths.cameraStream(Date.now());
    this.watchFirstFrame(generation);
  }

  private applyAvailability(availability: CameraAvailability | null): void {
    if (!this.shouldConnect()) return;
    if (availability && availability.available === true) {
      this.setMeta("live camera · loading");
      this.connect();
      return;
    }
    this.disconnect(unavailableMessage(availability));
    this.setMeta("live camera · unavailable");
    this.scheduleRetry();
  }

  private async refreshAvailability(): Promise<void> {
    if (!this.shouldConnect() || this.availabilityInflight) return;
    const requestId = ++this.availabilityRequest;
    this.availabilityInflight = true;
    this.setPlaceholder("Checking live camera");
    try {
      const availability = await apiJson<CameraAvailability>(paths.cameraAvailability);
      if (requestId !== this.availabilityRequest || !this.shouldConnect()) return;
      this.applyAvailability(availability);
    } catch (err) {
      if (requestId !== this.availabilityRequest || !this.shouldConnect()) return;
      this.disconnect(errorText(err, "Camera status unavailable"), true);
      this.setMeta("live camera · error");
      this.scheduleRetry();
    } finally {
      if (requestId === this.availabilityRequest) this.availabilityInflight = false;
    }
  }
}
