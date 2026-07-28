export class PollController {
  constructor(callback, intervalMs = 1000) {
    this.callback = callback;
    this.intervalMs = intervalMs;
    this.timer = null;
    this.running = false;
    this.onVisibility = () => {
      if (document.hidden) this.stopTimer();
      else this.refreshNow();
    };
  }

  start() {
    document.addEventListener("visibilitychange", this.onVisibility);
    if (!document.hidden) this.refreshNow();
  }

  stopTimer() {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  async refreshNow() {
    this.stopTimer();
    if (document.hidden || this.running) return;
    this.running = true;
    try { await this.callback(); }
    finally {
      this.running = false;
      if (!document.hidden) this.timer = setTimeout(() => this.refreshNow(), this.intervalMs);
    }
  }

  destroy() {
    this.stopTimer();
    document.removeEventListener("visibilitychange", this.onVisibility);
  }
}
