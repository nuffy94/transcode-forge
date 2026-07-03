/* Workers page — add-worker wizard, token management (single home),
 * fleet maintenance, and live per-worker progress.
 */

import { showToast } from './toast.js';

/* ---- Add-worker wizard ---- */

function toggleAddWorker() {
    const panel = document.getElementById('add-worker-panel');
    const toggle = document.getElementById('add-worker-toggle');
    const chevron = document.getElementById('add-worker-chevron');
    panel.classList.toggle('hidden');
    const open = !panel.classList.contains('hidden');
    toggle.setAttribute('aria-expanded', String(open));
    chevron.style.transform = open ? 'rotate(180deg)' : '';
}

function populateJoinCommands(origin, token, label) {
    const safe = label.replace(/[^a-z0-9-]/gi, '-').toLowerCase();
    document.getElementById('cmd-docker-run').textContent = `docker run -d \\
  --name transcode-worker-${safe} \\
  --restart unless-stopped \\
  -e TF_SERVER_URL=${origin} \\
  -e TF_WORKER_TOKEN=${token} \\
  -e TF_WORKER_NAME=${label} \\
  -e TF_PREFERRED_BACKEND=auto \\
  -v /mnt/media/movies:/media/movies \\
  ghcr.io/nuffy94/transcode-forge:latest \\
  python -m transcode_forge.worker`;

    document.getElementById('cmd-uv-run').textContent = `export TF_SERVER_URL=${origin}
export TF_WORKER_TOKEN=${token}
export TF_WORKER_NAME=${label}
export TF_PREFERRED_BACKEND=auto

uv run python -m transcode_forge.worker`;
}

async function issueWorkerToken() {
    const label = document.getElementById('add-worker-label').value.trim();
    if (!label) {
        showToast('Worker name required', 'error');
        return;
    }
    try {
        const r = await fetch('/api/worker-tokens', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label }),
        });
        if (!r.ok) {
            showToast('Failed to issue token', 'error');
            return;
        }
        const data = await r.json();
        document.getElementById('add-worker-token-value').textContent = data.token;
        document.getElementById('add-worker-token-reveal').classList.remove('hidden');
        populateJoinCommands(window.location.origin, data.token, data.label);
        for (const id of ['add-worker-commands', 'add-worker-storage', 'add-worker-live-status']) {
            document.getElementById(id).classList.remove('hidden');
        }
        document.getElementById('scheduler-url-display').textContent = window.location.origin;
        document.getElementById('add-worker-label').value = '';
        showToast('Token issued', 'success');
        htmx.trigger('#add-worker-tokens-list', 'load');
        htmx.trigger('#tokens-list', 'load');
    } catch (e) {
        showToast('Issue failed: ' + e.message, 'error');
    }
}

function copyText(text) {
    navigator.clipboard
        .writeText(text)
        .then(() => showToast('Copied to clipboard', 'success'))
        .catch(() => showToast('Copy failed — select manually', 'error'));
}

function switchCommandType(type) {
    document.querySelectorAll('[data-cmd-type]').forEach((btn) => {
        btn.classList.toggle('forge-btn--ember', btn.dataset.cmdType === type);
    });
    document.getElementById('cmd-docker').classList.toggle('hidden', type !== 'docker');
    document.getElementById('cmd-uv').classList.toggle('hidden', type !== 'uv');
}

/* ---- Tokens (single home: this page) ---- */

async function revokeToken(fingerprint) {
    if (!confirm('Revoke this token? The worker using it is cut off at its next request.')) return;
    try {
        const resp = await fetch('/api/worker-tokens', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token: fingerprint }),
        });
        if (!resp.ok) {
            showToast('Failed to revoke token', 'error');
            return;
        }
        showToast('Token revoked', 'warning');
        htmx.trigger('#tokens-list', 'load');
        htmx.trigger('#add-worker-tokens-list', 'load');
    } catch (e) {
        showToast('Revoke failed: ' + e.message, 'error');
    }
}
window.revokeToken = revokeToken; // the shared tokens partial calls it inline

/* ---- Fleet maintenance ---- */

let _deadIds = [];

// Workers silent this long are safe to remove; matches the server's
// WORKER_STALE_THRESHOLD_SECONDS.
const STALE_THRESHOLD_MS = 30 * 60 * 1000;

function isStale(worker) {
    if (!worker.last_heartbeat) return true;
    return Date.now() - new Date(worker.last_heartbeat).getTime() >= STALE_THRESHOLD_MS;
}

async function refreshDeadCount() {
    try {
        const resp = await fetch('/api/workers');
        if (!resp.ok) return;
        const { data } = await resp.json();
        _deadIds = data.filter(isStale).map((w) => w.id);
        const btn = document.getElementById('clear-dead-btn');
        const label = document.getElementById('clear-dead-label');
        if (!btn || !label) return;
        btn.classList.toggle('hidden', _deadIds.length === 0);
        if (_deadIds.length) {
            label.textContent = `Clear ${_deadIds.length} stale worker${_deadIds.length !== 1 ? 's' : ''}`;
        }
    } catch {
        /* next poll retries */
    }
}

async function clearAllDead() {
    if (!_deadIds.length) return;
    const n = _deadIds.length;
    if (!confirm(`Remove ${n} stale worker registration${n !== 1 ? 's' : ''}? Only dead workers are affected — anything online stays.`)) return;
    const btn = document.getElementById('clear-dead-btn');
    btn.disabled = true;
    const results = await Promise.all(
        _deadIds.map((id) => fetch(`/api/workers/${id}`, { method: 'DELETE' }))
    );
    const ok = results.filter((r) => r.ok).length;
    btn.disabled = false;
    if (ok === n) showToast(`Removed ${ok} stale worker${ok !== 1 ? 's' : ''}`, 'success');
    else showToast(`Removed ${ok} of ${n} — ${n - ok} could not be deleted (still online?)`, 'warning');
    htmx.ajax('GET', '/partials/workers', { target: '#workers-container', swap: 'innerHTML' });
    refreshDeadCount();
}

async function removeDeadWorker(id, name) {
    if (!confirm(`Remove worker "${name}"? Only dead workers can be removed.`)) return;
    try {
        const resp = await fetch(`/api/workers/${id}`, { method: 'DELETE' });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            showToast(`Cannot remove: ${data.detail || resp.statusText}`, 'error');
            return;
        }
        showToast(`Removed ${name}`, 'success');
        htmx.ajax('GET', '/partials/workers', { target: '#workers-container', swap: 'innerHTML' });
    } catch (err) {
        showToast(`Failed to remove worker: ${err.message}`, 'error');
    }
}

function loadFleetStats() {
    fetch('/api/workers')
        .then((r) => r.json())
        .then(({ data }) => {
            const live = data.filter((w) => w.status === 'online' || w.status === 'busy');
            const slots = live.reduce((sum, w) => sum + (w.max_concurrent || 1), 0);
            document.getElementById('stat-active-nodes').textContent = String(live.length);
            document.getElementById('stat-throughput').textContent = String(slots);
        })
        .catch(() => {});
}

/* ---- Wiring ---- */

document.getElementById('add-worker-toggle')?.addEventListener('click', toggleAddWorker);
document.getElementById('issue-token-btn')?.addEventListener('click', issueWorkerToken);
document.getElementById('copy-token-btn')?.addEventListener('click', () => {
    copyText(document.getElementById('add-worker-token-value').textContent);
});
document.getElementById('clear-dead-btn')?.addEventListener('click', clearAllDead);
document.querySelectorAll('[data-cmd-type]').forEach((btn) => {
    btn.addEventListener('click', () => switchCommandType(btn.dataset.cmdType));
});

document.addEventListener('click', (e) => {
    const copy = e.target.closest('[data-copy]');
    if (copy) {
        copyText(document.getElementById(copy.dataset.copy).textContent);
        return;
    }
    const remove = e.target.closest('[data-action="remove-worker"]');
    if (remove) removeDeadWorker(remove.dataset.id, remove.dataset.name);
});

/* Live per-worker progress: cards are matched by data-worker-id (unlike
 * the job-row contract in ops.js) and also carry an encode speed. */
function initWorkerProgress() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    function connect() {
        const ws = new WebSocket(`${proto}//${location.host}/ws/updates`);
        ws.onmessage = (evt) => {
            const data = JSON.parse(evt.data);
            const card = document.querySelector(`[data-worker-id="${data.worker_id}"]`);
            if (!card) return;
            const pct = Math.round(data.progress * 100);
            const pctEl = card.querySelector('[data-progress-pct]');
            const barEl = card.querySelector('[data-progress-bar]');
            const speedEl = card.querySelector('[data-speed]');
            if (pctEl) pctEl.textContent = pct + '%';
            if (barEl) barEl.style.width = pct + '%';
            if (speedEl && data.speed) speedEl.textContent = data.speed.toFixed(1) + '×';
        };
        ws.onclose = () => setTimeout(connect, 5000);
    }
    connect();
}

initWorkerProgress();
loadFleetStats();
refreshDeadCount();
setInterval(refreshDeadCount, 10000);
setInterval(loadFleetStats, 10000);
