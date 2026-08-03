export class APIClient {
  constructor(etags, inflight) {
    this.etags = etags;
    this.inflight = inflight;
  }

  request(key, path, options = {}) {
    if (this.inflight.has(key)) return this.inflight.get(key);
    const promise = this.#request(key, path, options).finally(() => this.inflight.delete(key));
    this.inflight.set(key, promise);
    return promise;
  }

  async #request(key, path, options) {
    const headers = new Headers(options.headers || {});
    const etag = this.etags.get(key);
    if (etag && !options.method) headers.set("If-None-Match", etag);
    if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(path, {...options, headers});
    if (response.status === 304) return {notModified: true, data: null};
    const nextEtag = response.headers.get("ETag");
    if (nextEtag) this.etags.set(key, nextEtag);
    const contentType = response.headers.get("Content-Type") || "";
    const data = contentType.includes("json") ? await response.json() : await response.text();
    if (!response.ok) {
      const message = data?.error?.message || `${response.status} ${response.statusText}`;
      const error = new Error(message);
      error.status = response.status;
      error.payload = data;
      throw error;
    }
    return {notModified: false, data};
  }

  json(key, path, body, idempotencyKey) {
    return this.request(key, path, {
      method: "POST",
      headers: {"Idempotency-Key": idempotencyKey},
      body: JSON.stringify(body),
    });
  }
}
