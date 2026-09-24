export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

// Reading a page or uploading a file must eventually return control to the user.
// AI/export operations get longer than local read requests and are never retried
// automatically: a timed-out write may already have completed on the server.
export async function fetchLocal(path, options = {}) {
  const { timeoutMs = options.method && options.method !== 'GET' ? 180000 : 20000, ...init } = options;
  const controller = new AbortController();
  const abort = () => controller.abort(options.signal?.reason);
  if (options.signal?.aborted) abort();
  else options.signal?.addEventListener('abort', abort, { once: true });
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await globalThis.fetch(path, { ...init, signal: controller.signal });
    // fetch resolves at headers. Drain a clone while the deadline is still
    // active, so a stalled JSON body aborts too. Return the original Response:
    // callers retain status/headers/url and an unread body for json()/text().
    await response.clone().arrayBuffer();
    return response;
  } catch (error) {
    if (controller.signal.aborted && !options.signal?.aborted) {
      throw new ApiError(0, 'timeout', '等待响应超时。操作可能仍在后台完成，请刷新核对后再重试。');
    }
    if (options.signal?.aborted) throw error;
    throw new ApiError(0, 'network_error', '无法连接本机服务，请确认程序仍在运行，然后重试。');
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener('abort', abort);
  }
}

export async function readApiResponse(response) {
  const data = await response.json().catch(() => null);
  if (!response.ok || data?.error) {
    const err = new ApiError(response.status, data?.code || 'request_failed', data?.error || `请求失败（HTTP ${response.status}），请刷新后重试。`);
    err.data = data;
    throw err;
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new ApiError(response.status, 'invalid_response', '服务返回了无法读取的数据，请刷新后重试。');
  }
  return data;
}

let _token = null;
export function setToken(token) { _token = token; }

export async function request(path, options = {}) {
  const res = await fetchLocal(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Job-Agent-Token': _token,
      ...options.headers,
    },
  });
  return readApiResponse(res);
}

export const get = (path) => request(path);
export const post = (path, data) => request(path, { method: 'POST', body: JSON.stringify(data) });
export const postRaw = (path, body, headers) => request(path, { method: 'POST', body, headers });
