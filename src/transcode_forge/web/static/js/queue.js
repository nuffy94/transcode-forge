/* Queue page — status-tile filtering, scan launcher, pause/cancel-all,
 * bulk cancel/retry, header sorting, and live WS progress.
 *
 * The status tiles are shortcuts into the #status-filter select (the
 * single source of truth, and the accessible control); clicking the
 * active tile again returns to the default active-jobs view.
 */

import { showToast } from './toast.js';
import { initPauseButton, initLiveProgress } from './ops.js';

const DEFAULT_STATUS = 'pending,queued,transcoding';
const QUEUE_DESC_FIRST = ['source_size'];

/* Called from the sort_th column headers (rendered fresh on each 5s poll).
 * The hidden sort/dir inputs are pulled in by the container's hx-include,
 * so a plain refresh reloads with the chosen sort and survives polling. */
window.sortQueue = function sortQueue(col) {
    const s = document.getElementById('queue-sort');
    const d = document.getElementById('queue-dir');
    if (s.value === col) {
        d.value = d.value === 'asc' ? 'desc' : 'asc';
    } else {
        s.value = col;
        d.value = QUEUE_DESC_FIRST.includes(col) ? 'desc' : 'asc';
    }
    htmx.trigger('#job-table-container', 'refresh');
};

/* ---- Status tiles ---- */

function syncTiles() {
    const current = document.getElementById('status-filter').value;
    document.querySelectorAll('.forge-tile[data-status-value]').forEach((tile) => {
        const active = tile.dataset.statusValue === current;
        tile.classList.toggle('is-active', active);
        tile.setAttribute('aria-pressed', String(active));
    });
}

function initTiles() {
    const sel = document.getElementById('status-filter');
    document.querySelectorAll('.forge-tile[data-status-value]').forEach((tile) => {
        tile.addEventListener('click', () => {
            const v = tile.dataset.statusValue;
            sel.value = sel.value === v ? DEFAULT_STATUS : v;
            sel.dispatchEvent(new Event('change', { bubbles: true }));
            syncTiles();
        });
    });
    sel.addEventListener('change', syncTiles);
    syncTiles();
}

function loadTileCounts() {
    fetch('/api/stats')
        .then((r) => r.json())
        .then(({ data }) => {
            const s = data.jobs_by_status || {};
            for (const key of ['pending', 'queued', 'transcoding', 'complete', 'failed']) {
                const el = document.getElementById(`badge-${key}`);
                if (el) el.textContent = String(s[key] || 0);
            }
        })
        .catch(() => {});
}

/* ---- Scan launcher ---- */

async function triggerScan() {
    const btn = document.getElementById('scan-btn');
    const status = document.getElementById('scan-status');
    const lib = document.getElementById('scan-library').value;
    const limit = parseInt(document.getElementById('scan-limit').value, 10) || 0;

    const body = { limit: limit };
    if (lib) body.library = lib;

    btn.disabled = true;
    status.textContent = 'Starting…';
    try {
        const resp = await fetch('/api/scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (resp.ok) {
            showToast(`Scan started: ${data.scan_ids.join(', ')}`, 'success');
            status.textContent = `Scanning: ${data.scan_ids.join(', ')}`;
        } else {
            showToast(`Scan error: ${data.detail || 'Unknown'}`, 'error');
            status.textContent = '';
        }
    } catch (e) {
        showToast(`Scan error: ${e.message}`, 'error');
        status.textContent = '';
    }
    btn.disabled = false;
    htmx.trigger('#scan-history', 'refresh');
    htmx.trigger('#job-table-container', 'refresh');
}

async function cancelAll() {
    if (!confirm('Cancel all pending and queued jobs? Workers will finish their current job.')) return;
    try {
        const resp = await fetch('/api/jobs/cancel-all', { method: 'POST' });
        if (resp.ok) {
            const data = await resp.json().catch(() => ({}));
            showToast(`${data.cancelled ?? 'All'} pending job(s) cancelled`, 'warning');
        } else {
            showToast(`Cancel failed (HTTP ${resp.status})`, 'error');
        }
    } catch (e) {
        showToast(`Cancel failed: ${e.message}`, 'error');
    }
    htmx.trigger('#job-table-container', 'refresh');
    loadTileCounts();
}

/* ---- Bulk selection ---- */

function selectedCheckboxes() {
    return [...document.querySelectorAll('.job-checkbox:checked')];
}

function updateBulkBar() {
    const bar = document.getElementById('bulk-bar');
    const count = selectedCheckboxes().length;
    document.getElementById('bulk-count').textContent = String(count);
    bar.classList.toggle('hidden', count === 0);
    bar.classList.toggle('flex', count > 0);
}

async function bulkCancel() {
    const ids = selectedCheckboxes()
        .filter((c) => ['pending', 'queued'].includes(c.dataset.status))
        .map((c) => c.value);
    if (!ids.length) {
        showToast('No cancellable jobs selected', 'warning');
        return;
    }
    if (!confirm(`Cancel ${ids.length} job(s)?`)) return;
    const results = await Promise.all(
        ids.map((id) => fetch(`/api/jobs/${id}/cancel`, { method: 'POST' }))
    );
    const ok = results.filter((r) => r.ok).length;
    const failed = results.length - ok;
    if (failed) showToast(`${ok} cancelled, ${failed} failed`, 'error');
    else showToast(`${ok} job(s) cancelled`, 'warning');
    htmx.trigger('#job-table-container', 'refresh');
    loadTileCounts();
}

async function bulkRetry() {
    const ids = selectedCheckboxes()
        .filter((c) => c.dataset.status === 'failed')
        .map((c) => c.value);
    if (!ids.length) {
        showToast('No failed jobs selected', 'warning');
        return;
    }
    const results = await Promise.all(
        ids.map((id) => fetch(`/api/jobs/${id}/retry`, { method: 'POST' }))
    );
    const ok = results.filter((r) => r.ok).length;
    const failed = results.length - ok;
    if (failed) showToast(`${ok} retried, ${failed} failed`, 'error');
    else showToast(`${ok} job(s) retried`, 'success');
    htmx.trigger('#job-table-container', 'refresh');
    loadTileCounts();
}

/* ---- Wiring ---- */

document.getElementById('scan-btn')?.addEventListener('click', triggerScan);
document.getElementById('cancel-all-btn')?.addEventListener('click', cancelAll);
document.getElementById('bulk-cancel')?.addEventListener('click', bulkCancel);
document.getElementById('bulk-retry')?.addEventListener('click', bulkRetry);

document.addEventListener('change', (e) => {
    if (e.target.dataset?.role === 'jobs-select-all') {
        document.querySelectorAll('.job-checkbox').forEach((c) => (c.checked = e.target.checked));
        updateBulkBar();
    } else if (e.target.classList?.contains('job-checkbox')) {
        updateBulkBar();
    }
});

/* Every 5s poll re-renders the table and clears checkboxes — keep the
 * bulk bar honest after each swap. */
document.body.addEventListener('htmx:afterSwap', (e) => {
    if (e.detail.target && e.detail.target.id === 'job-table-container') updateBulkBar();
});

/* Library dropdowns (scan + filter) */
fetch('/api/libraries')
    .then((r) => r.json())
    .then(({ data }) => {
        for (const sel of [
            document.getElementById('scan-library'),
            document.getElementById('library-filter'),
        ]) {
            if (!sel) continue;
            for (const lib of data) {
                const opt = document.createElement('option');
                opt.value = lib.name;
                opt.textContent = lib.name;
                sel.appendChild(opt);
            }
        }
    })
    .catch(() => {});

initPauseButton();
initLiveProgress();
initTiles();
loadTileCounts();
setInterval(loadTileCounts, 10000);
