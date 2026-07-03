/* File-detail drawer — opens from any file/job row, HTMX-style content
 * fetch from /partials/file-detail.
 *
 * The DOM scaffold lives in base.html (#file-drawer / #file-drawer-overlay /
 * #file-drawer-body). Closed state carries `inert` + aria-hidden so the
 * close button can't take focus while invisible. Deep link: #file=<id>
 * opens on load. Pages refresh their listings on the `tf:catalog-refresh`
 * event fired after any drawer action.
 *
 * openFileDrawer + the action handlers hang on window for row onclick
 * handlers and the server-rendered partial's action buttons.
 */

import { showToast } from './toast.js';

/* Matches the sprite's `loader` icon — the one place JS renders an icon
 * without a Jinja context. */
const LOADER_SVG =
    '<svg class="forge-icon forge-icon--spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M21 12a9 9 0 1 1-6.219-8.56" /></svg>';

let lastFocus = null;
let currentFileId = null;

function els() {
    return {
        drawer: document.getElementById('file-drawer'),
        overlay: document.getElementById('file-drawer-overlay'),
        body: document.getElementById('file-drawer-body'),
        close: document.getElementById('file-drawer-close'),
    };
}

export async function refreshDrawer() {
    const { body } = els();
    if (!currentFileId || !body) return;
    try {
        const resp = await fetch(`/partials/file-detail?file_id=${encodeURIComponent(currentFileId)}`);
        body.innerHTML = await resp.text(); // a 404 renders its own not-found body
    } catch {
        body.innerHTML =
            '<div class="forge-empty"><p class="forge-empty-title">Could not load file detail</p>' +
            '<p class="forge-empty-hint">Check the connection and try again.</p></div>';
    }
}

export async function openFileDrawer(fileId) {
    const { drawer, overlay, body, close } = els();
    if (!drawer) return;
    currentFileId = fileId;
    lastFocus = document.activeElement;
    drawer.classList.add('is-open');
    overlay.classList.add('is-open');
    drawer.removeAttribute('inert');
    drawer.setAttribute('aria-hidden', 'false');
    body.innerHTML = `<div class="forge-empty">${LOADER_SVG}</div>`;
    history.replaceState(null, '', '#file=' + encodeURIComponent(fileId));
    await refreshDrawer();
    if (close) close.focus();
}

export function closeFileDrawer() {
    const { drawer, overlay } = els();
    if (!drawer || !drawer.classList.contains('is-open')) return;
    drawer.classList.remove('is-open');
    overlay.classList.remove('is-open');
    drawer.setAttribute('inert', '');
    drawer.setAttribute('aria-hidden', 'true');
    currentFileId = null;
    if (location.hash.startsWith('#file=')) {
        history.replaceState(null, '', location.pathname + location.search);
    }
    if (lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
    lastFocus = null;
}

function currentFilePath() {
    const el = document.querySelector('#file-drawer-body [data-file-path]');
    return el ? el.dataset.filePath : null;
}

function notifyCatalog() {
    document.dispatchEvent(new CustomEvent('tf:catalog-refresh'));
}

async function drawerQueue(fileId) {
    try {
        const resp = await fetch('/api/media/queue', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_ids: [fileId] }),
        });
        if (!resp.ok) {
            showToast(`Error: ${resp.statusText}`, 'error');
            return;
        }
        showToast('Queued for transcode', 'success');
        await refreshDrawer();
        notifyCatalog();
    } catch {
        showToast('Failed to queue file', 'error');
    }
}

async function drawerExclude() {
    const path = currentFilePath();
    if (!path) return;
    if (!confirm('Never try transcoding this file again?\n\n' + path)) return;
    try {
        const resp = await fetch('/api/exclusions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: path, reason: 'manual' }),
        });
        if (!resp.ok) {
            showToast('Failed to exclude (HTTP ' + resp.status + ')', 'error');
            return;
        }
        showToast("Excluded — won't try again", 'warning');
        await refreshDrawer();
        notifyCatalog();
    } catch (e) {
        showToast('Exclude failed: ' + e.message, 'error');
    }
}

async function drawerUnexclude() {
    const path = currentFilePath();
    if (!path) return;
    try {
        const resp = await fetch('/api/exclusions', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: path }),
        });
        if (!resp.ok) {
            showToast('Failed to remove exclusion', 'error');
            return;
        }
        showToast('Exclusion lifted', 'success');
        await refreshDrawer();
        notifyCatalog();
    } catch (e) {
        showToast('Unexclude failed: ' + e.message, 'error');
    }
}

function initDrawer() {
    const { overlay, close } = els();
    if (overlay) overlay.addEventListener('click', closeFileDrawer);
    if (close) close.addEventListener('click', closeFileDrawer);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeFileDrawer();
    });
    if (location.hash.startsWith('#file=')) {
        openFileDrawer(decodeURIComponent(location.hash.slice(6)));
    }
}

window.openFileDrawer = openFileDrawer;
window.drawerQueue = drawerQueue;
window.drawerExclude = drawerExclude;
window.drawerUnexclude = drawerUnexclude;

initDrawer();
