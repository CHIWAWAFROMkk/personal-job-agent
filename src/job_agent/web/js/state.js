const _data = {};
const _listeners = new Map();

export function get(key) { return _data[key]; }
export function set(key, value) {
  _data[key] = value;
  for (const fn of _listeners.get(key) || []) fn(value);
}
export function on(key, fn) {
  if (!_listeners.has(key)) _listeners.set(key, []);
  _listeners.get(key).push(fn);
}

// Overlapping refreshes share one request loop. A refresh requested after a
// mutation still gets a fresh trailing read instead of accepting old data.
export function coalesceRefresh(refresh) {
  let pending = null;
  let queued = false;
  return function () {
    queued = true;
    if (!pending) {
      pending = Promise.resolve().then(async () => {
        while (queued) {
          queued = false;
          await refresh();
        }
      }).finally(() => { pending = null; });
    }
    return pending;
  };
}
