/* Settings page — libraries CRUD, transcoding defaults, system/services
 * readouts, queue schedules, and maintenance actions.
 * Worker tokens live on the Workers page (workers.js).
 */

import { esc, showToast } from './toast.js';

/* ---- Libraries ---- */

let _libraryCache = [];

async function loadLibraries() {
    const resp = await fetch('/api/libraries');
    const { data } = await resp.json();
    _libraryCache = data;

    const list = document.getElementById('libraries-list');
    const qual = document.getElementById('quality-display');

    if (!data.length) {
        list.innerHTML = `
            <div class="forge-empty">
                <p class="forge-empty-title">No libraries</p>
                <p class="forge-empty-hint">Add one — the scanner catalogs it, then you queue files from Movies or TV Shows.</p>
            </div>`;
        qual.innerHTML = '<p class="forge-empty-hint text-center py-4">No libraries configured.</p>';
        return;
    }

    let html =
        '<table class="forge-table"><thead><tr><th>Name</th><th>Type</th><th>Path</th><th class="text-right">CRF</th><th>State</th><th class="text-right">Actions</th></tr></thead><tbody>';
    let qualHtml =
        '<table class="forge-table"><thead><tr><th>Library</th><th class="text-right">CRF</th><th>Tier</th></tr></thead><tbody>';
    for (const lib of data) {
        const tier =
            lib.quality_preset <= 18 ? 'Very high' : lib.quality_preset <= 22 ? 'High' : lib.quality_preset <= 26 ? 'Medium' : 'Low';
        const name = esc(lib.name);
        const path = esc(lib.path);
        html += `<tr>
            <td class="text-forge-paper font-semibold">${name}</td>
            <td><span class="forge-pill forge-pill--queued">${esc(lib.media_type)}</span></td>
            <td class="col-mono text-xs max-w-xs truncate text-forge-mute" title="${path}">${path}</td>
            <td class="col-mono text-right text-forge-paper">${lib.quality_preset}</td>
            <td>${lib.enabled ? '<span class="forge-pill forge-pill--complete">on</span>' : '<span class="forge-pill forge-pill--cancelled">off</span>'}</td>
            <td class="text-right">
                <span class="flex justify-end gap-1.5">
                    <button class="forge-btn" data-edit-id="${esc(lib.id)}">Edit</button>
                    <button class="forge-btn" data-scan-id="${esc(lib.id)}" data-scan-name="${name}">Scan</button>
                    <button class="forge-btn forge-btn--danger" data-delete-id="${esc(lib.id)}" data-delete-name="${name}">Delete</button>
                </span>
            </td>
        </tr>`;
        qualHtml += `<tr><td class="text-forge-paper font-semibold">${name}</td><td class="col-mono text-right text-forge-ember">${lib.quality_preset}</td><td class="font-mono text-2xs uppercase tracking-stamp text-forge-mute">${tier}</td></tr>`;
    }
    list.innerHTML = html + '</tbody></table>';
    qual.innerHTML = qualHtml + '</tbody></table>';
}

async function addLibrary() {
    const body = {
        name: document.getElementById('lib-name').value,
        media_type: document.getElementById('lib-type').value,
        path: document.getElementById('lib-path').value,
        quality_preset: parseInt(document.getElementById('lib-quality').value, 10),
        scan_interval_hours: parseInt(document.getElementById('lib-scan-interval').value, 10) || 24,
    };
    const resp = await fetch('/api/libraries', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (resp.ok) {
        document.getElementById('add-lib-modal').close();
        showToast(`Library "${body.name}" added`, 'success');
        loadLibraries();
    } else {
        const err = await resp.json();
        showToast(err.detail || 'Failed to add library', 'error');
    }
}

function editLibrary(id) {
    const lib = _libraryCache.find((l) => l.id === id);
    if (!lib) return;
    document.getElementById('edit-lib-id').value = lib.id;
    document.getElementById('edit-lib-name').value = lib.name;
    document.getElementById('edit-lib-quality').value = lib.quality_preset;
    document.getElementById('edit-lib-scan-interval').value = lib.scan_interval_hours || 24;
    document.getElementById('edit-lib-enabled').checked = !!lib.enabled;
    document.getElementById('edit-lib-auto-scan').checked = !!lib.auto_scan;
    document.getElementById('edit-lib-modal').showModal();
}

async function saveLibrary() {
    const id = document.getElementById('edit-lib-id').value;
    const body = {
        name: document.getElementById('edit-lib-name').value,
        quality_preset: parseInt(document.getElementById('edit-lib-quality').value, 10),
        scan_interval_hours: parseInt(document.getElementById('edit-lib-scan-interval').value, 10) || 24,
        enabled: document.getElementById('edit-lib-enabled').checked,
        auto_scan: document.getElementById('edit-lib-auto-scan').checked,
    };
    const resp = await fetch(`/api/libraries/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (resp.ok) {
        document.getElementById('edit-lib-modal').close();
        showToast('Library updated', 'success');
        loadLibraries();
    } else {
        const err = await resp.json();
        showToast(err.detail || 'Failed to update library', 'error');
    }
}

async function scanLibrary(id, name) {
    try {
        const resp = await fetch(`/api/libraries/${id}/scan?max_files=0`, { method: 'POST' });
        if (resp.ok) {
            showToast(`Scanning ${name}…`, 'info');
        } else {
            const err = await resp.json().catch(() => ({}));
            showToast(err.detail || `Scan failed for ${name}`, 'error');
        }
    } catch (e) {
        showToast(`Scan failed: ${e.message}`, 'error');
    }
}

async function deleteLibrary(id) {
    try {
        const resp = await fetch(`/api/libraries/${id}`, { method: 'DELETE' });
        if (resp.ok) {
            showToast('Library deleted', 'warning');
            loadLibraries();
        } else {
            const err = await resp.json().catch(() => ({}));
            showToast(err.detail || 'Failed to delete library', 'error');
        }
    } catch (e) {
        showToast(`Delete failed: ${e.message}`, 'error');
    }
}

/* ---- Transcoding defaults ---- */

function toggleAv1Warning() {
    const isAv1 = document.getElementById('tune-default-codec').value === 'av1';
    document.getElementById('tune-av1-warning').style.display = isAv1 ? 'block' : 'none';
}

async function loadTuning() {
    try {
        const r = await fetch('/api/settings/tuning');
        if (!r.ok) return;
        const body = await r.json();
        document.getElementById('tune-default-codec').value = body.data.default_codec || 'hevc';
        document.getElementById('tune-target-vmaf').value = body.data.target_vmaf || '';
        document.getElementById('tune-vmaf-floor').value = body.data.vmaf_min_floor || '';
        toggleAv1Warning();
    } catch {
        /* panel stays at defaults */
    }
}

async function saveTuning() {
    const values = {
        default_codec: document.getElementById('tune-default-codec').value,
        target_vmaf: document.getElementById('tune-target-vmaf').value,
        vmaf_min_floor: document.getElementById('tune-vmaf-floor').value,
    };
    try {
        const r = await fetch('/api/settings/tuning', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ values }),
        });
        if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            showToast(err.detail || 'Save failed', 'error');
            return;
        }
        showToast('Transcoding defaults saved', 'success');
    } catch (e) {
        showToast('Save failed: ' + e.message, 'error');
    }
}

/* ---- System + services readouts ---- */

function loadSystemInfo() {
    fetch('/api/system/info')
        .then((r) => r.json())
        .then((data) => {
            document.getElementById('sys-version').textContent = 'v' + data.version;
            document.getElementById('sys-database').textContent = data.database;
            document.getElementById('sys-uptime').textContent = data.uptime;
            if (data.disk && data.disk.total > 0) {
                const totalTB = (data.disk.total / 1099511627776).toFixed(1);
                const usedTB = (data.disk.used / 1099511627776).toFixed(1);
                document.getElementById('sys-disk-text').textContent = `${usedTB} / ${totalTB} TiB`;
                document.getElementById('sys-disk-bar').style.width = data.disk.percent + '%';
            }
            if (data.max_retries) document.getElementById('sys-retries').textContent = data.max_retries;
            if (data.heartbeat_interval) document.getElementById('sys-heartbeat').textContent = data.heartbeat_interval + 's';
            if (data.heartbeat_timeout) document.getElementById('sys-timeout').textContent = data.heartbeat_timeout + 's';
        })
        .catch(() => {});

    fetch('/api/health')
        .then((r) => r.json())
        .then((data) => {
            const dot = document.getElementById('redis-dot');
            const label = document.getElementById('redis-status');
            dot.className = data.redis ? 'forge-dot forge-dot--on' : 'forge-dot forge-dot--err';
            label.textContent = data.redis ? 'Online' : 'Offline';
            label.style.color = data.redis ? 'var(--forge-oxide)' : 'var(--forge-rust)';
        })
        .catch(() => {});
}

/* ---- Maintenance ---- */

async function maintenanceAction({ url, method = 'POST', confirmMsg, successMsg }) {
    if (!confirm(confirmMsg)) return;
    try {
        const resp = await fetch(url, { method });
        if (resp.ok) {
            showToast(successMsg, 'warning');
        } else {
            const err = await resp.json().catch(() => ({}));
            showToast(err.detail || `Action failed (HTTP ${resp.status})`, 'error');
        }
    } catch (e) {
        showToast(`Action failed: ${e.message}`, 'error');
    }
}

/* ---- Schedules ---- */

const DAY_ON = 'forge-btn--ember';

function getSelectedDaysMask() {
    let mask = 0;
    document.querySelectorAll('.sched-day-btn').forEach((btn) => {
        if (btn.classList.contains(DAY_ON)) mask |= parseInt(btn.dataset.bit, 10);
    });
    return mask;
}

async function refreshScheduleStatus() {
    try {
        const r = await fetch('/api/schedules');
        if (!r.ok) return;
        const meta = (await r.json()).meta || {};
        const badge = document.getElementById('schedule-status-badge');
        if (!badge) return;
        if (meta.queue_active_now) {
            badge.textContent = 'Queue active';
            badge.className = 'forge-pill forge-pill--complete ml-auto';
        } else {
            badge.textContent = 'Queue paused';
            badge.className = 'forge-pill forge-pill--cancelled ml-auto';
        }
    } catch {
        /* badge keeps its placeholder */
    }
}

async function addSchedule() {
    const name = document.getElementById('sched-name').value.trim();
    const start = parseInt(document.getElementById('sched-start').value, 10);
    const end = parseInt(document.getElementById('sched-end').value, 10);
    const days_mask = getSelectedDaysMask();
    if (!name) {
        showToast('Schedule name required', 'error');
        return;
    }
    if (!days_mask) {
        showToast('Pick at least one day', 'error');
        return;
    }
    try {
        const r = await fetch('/api/schedules', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, start_hour: start, end_hour: end, days_mask }),
        });
        if (!r.ok) {
            showToast('Failed to add schedule', 'error');
            return;
        }
        showToast('Schedule added', 'success');
        document.getElementById('sched-name').value = '';
        htmx.trigger('#schedules-list', 'load');
        refreshScheduleStatus();
    } catch (e) {
        showToast('Add failed: ' + e.message, 'error');
    }
}

/* The schedules partial re-renders via HTMX and calls these inline. */
window.toggleSchedule = async function toggleSchedule(id, enabled) {
    try {
        const r = await fetch('/api/schedules/' + id, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        });
        if (!r.ok) {
            showToast('Toggle failed', 'error');
            return;
        }
        showToast(enabled ? 'Enabled' : 'Disabled', enabled ? 'success' : 'warning');
        htmx.trigger('#schedules-list', 'load');
        refreshScheduleStatus();
    } catch (e) {
        showToast('Toggle failed: ' + e.message, 'error');
    }
};

window.deleteSchedule = async function deleteSchedule(id) {
    if (!confirm('Delete this schedule?')) return;
    try {
        const r = await fetch('/api/schedules/' + id, { method: 'DELETE' });
        if (!r.ok) {
            showToast('Delete failed', 'error');
            return;
        }
        showToast('Schedule removed', 'warning');
        htmx.trigger('#schedules-list', 'load');
        refreshScheduleStatus();
    } catch (e) {
        showToast('Delete failed: ' + e.message, 'error');
    }
};

/* ---- Wiring ---- */

const on = (id, evt, fn) => document.getElementById(id)?.addEventListener(evt, fn);

on('add-lib-btn', 'click', () => document.getElementById('add-lib-modal').showModal());
on('add-lib-save', 'click', addLibrary);
on('edit-lib-save', 'click', saveLibrary);
on('save-tuning-btn', 'click', saveTuning);
on('tune-default-codec', 'change', toggleAv1Warning);
on('add-schedule-btn', 'click', addSchedule);

on('mx-cancel-all', 'click', () =>
    maintenanceAction({
        url: '/api/jobs/cancel-all',
        confirmMsg: 'Cancel all pending and queued jobs? Workers will finish their current job.',
        successMsg: 'All pending jobs cancelled',
    })
);
on('mx-clear-history', 'click', () =>
    maintenanceAction({
        url: '/api/jobs/clear-completed',
        confirmMsg: 'Clear completed and cancelled jobs from history? This cannot be undone.',
        successMsg: 'History cleared',
    })
);
on('mx-reset-jobs', 'click', () =>
    maintenanceAction({
        url: '/api/jobs/reset?confirm=yes-delete-all',
        method: 'DELETE',
        confirmMsg: 'DELETE ALL JOBS — including in-progress, completed, and history? This cannot be undone.',
        successMsg: 'All jobs deleted',
    })
);

document.addEventListener('click', (e) => {
    const close = e.target.closest('[data-close-dialog]');
    if (close) {
        document.getElementById(close.dataset.closeDialog)?.close();
        return;
    }
    const edit = e.target.closest('[data-edit-id]');
    if (edit) {
        editLibrary(edit.dataset.editId);
        return;
    }
    const scan = e.target.closest('[data-scan-id]');
    if (scan) {
        scanLibrary(scan.dataset.scanId, scan.dataset.scanName);
        return;
    }
    const del = e.target.closest('[data-delete-id]');
    if (del && confirm(`Delete ${del.dataset.deleteName}?`)) {
        deleteLibrary(del.dataset.deleteId);
    }
});

document.querySelectorAll('.sched-day-btn').forEach((btn) => {
    btn.classList.add(DAY_ON);
    btn.addEventListener('click', () => {
        btn.classList.toggle(DAY_ON);
        btn.setAttribute('aria-pressed', btn.classList.contains(DAY_ON));
    });
});

loadLibraries();
loadTuning();
loadSystemInfo();
refreshScheduleStatus();
