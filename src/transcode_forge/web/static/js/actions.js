/* Shared row-level actions: exclusions, skip-list, sign-out.
 *
 * Exposed on window for the inline onclick handlers in pre-v2 page bodies
 * and HTMX partials; those migrate to modules in Steps 3–6.
 */

import { showToast } from './toast.js';

/* "Don't try this again" — flag a path so it never queues again, then
 * refresh the listing it was clicked from. */
export async function excludeFile(path, reason, refreshTargetId) {
    if (!confirm("Never try transcoding this file again?\n\n" + path)) return;
    try {
        const resp = await fetch('/api/exclusions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: path, reason: reason || 'manual' }),
        });
        if (!resp.ok) {
            showToast('Failed to exclude (HTTP ' + resp.status + ')', 'error');
            return;
        }
        showToast("Excluded — won't try again", 'warning');
        if (refreshTargetId && window.htmx) {
            htmx.trigger('#' + refreshTargetId, 'refresh');
        }
    } catch (e) {
        showToast('Exclude failed: ' + e.message, 'error');
    }
}

/* Remove a file from the skip list (retry on next scan). Sends a JSON
 * body — the endpoint expects JSON, so the old hx-delete (form-encoded)
 * 422'd. Note the refresh trigger is 'load' here, not 'refresh'. */
export async function unskipFile(path, refreshTargetId) {
    try {
        const resp = await fetch('/api/skipped', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_path: path }),
        });
        if (!resp.ok) {
            showToast('Failed to unskip (HTTP ' + resp.status + ')', 'error');
            return;
        }
        showToast('Removed from skip list — will retry on next scan', 'success');
        if (refreshTargetId && window.htmx) {
            htmx.trigger('#' + refreshTargetId, 'load');
        }
    } catch (e) {
        showToast('Unskip failed: ' + e.message, 'error');
    }
}

export async function forgeLogout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
    } catch (_) {
        /* signing out anyway */
    }
    window.location.href = '/login';
}

window.excludeFile = excludeFile;
window.unskipFile = unskipFile;
window.forgeLogout = forgeLogout;
