// Two matching observations avoid refreshing while a multi-file update is being written.
export function watchForUpdates({
  version,
  hasUnsavedChanges = () => false,
  onAvailable = () => {},
  reload = () => location.reload(),
  fetchVersion = async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch('/api/frontend-version', {
        cache: 'no-store', credentials: 'same-origin', signal: controller.signal,
      });
      if (!response.ok) throw new Error('Update check unavailable');
      return (await response.json()).version;
    } finally { clearTimeout(timeout); }
  },
  interval = 15000,
} = {}) {
  let stopped = false, checking = false, reloading = false, candidate, notified;
  async function check() {
    if (stopped || checking || reloading || !/^[a-f0-9]{64}$/.test(version || '')) return;
    checking = true;
    try {
      const latest = await fetchVersion();
      if (stopped || typeof latest !== 'string' || !/^[a-f0-9]{64}$/.test(latest)) return;
      if (latest === version) { candidate = null; return; }
      if (candidate !== latest) { candidate = latest; return; }
      if (hasUnsavedChanges()) {
        if (notified !== latest) { notified = latest; onAvailable(); }
      } else {
        reloading = true;
        reload();
      }
    } catch {
      // A restart, disconnected LAN, or partial deployment is not a reason to reload.
      candidate = null;
    } finally { checking = false; }
  }
  const timer = setInterval(check, interval);
  const visible = () => { if (!document.hidden) check(); };
  document.addEventListener('visibilitychange', visible);
  window.addEventListener('online', check);
  check();
  return {
    check,
    stop() {
      stopped = true; clearInterval(timer);
      document.removeEventListener('visibilitychange', visible);
      window.removeEventListener('online', check);
    },
  };
}
