/* Toast system — console messages.
 *
 * Contract (QA and screen readers depend on it, see tests/qa/test_sweep.py):
 *   - every toast carries data-toast-type
 *   - errors get role="alert" and PERSIST until dismissed by click — a
 *     transient auto-fading error can be missed by a screenshot or a
 *     Playwright assertion
 *   - everything else auto-dismisses after 5s
 *
 * showToast is exposed on window because pre-v2 page bodies call it from
 * inline handlers; those pages migrate to modules in Steps 3–6.
 */

const AUTO_DISMISS_MS = 5000;
const KNOWN_TYPES = ['success', 'error', 'warning', 'info'];

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/* Escape text for pasting into HTML: safe as element text AND inside a
 * quoted attribute. The old textContent/innerHTML trick covered text only
 * (browsers escape quotes when serialising attributes, not text nodes), so
 * a filename with a double quote could close an aria-label and add an
 * onfocus handler in the admin tab (ledger R-008). Guarded by
 * tests/qa/test_escaping.py. */
export function esc(text) {
    return String(text).replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/* Turn a parsed JSON error body into a readable string. FastAPI 422s put an
 * ARRAY of {loc, msg, type} objects in `detail`; passing that straight to a
 * toast renders "[object Object]". Fall back to the caller's default. */
export function detailText(body, fallback = 'Something went wrong') {
    const d = body && body.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) {
        const msgs = d.map((e) => (e && e.msg ? e.msg : null)).filter(Boolean);
        if (msgs.length) return msgs.join('; ');
    }
    return fallback;
}

/* <dialog>.showModal() puts the dialog in the browser TOP LAYER and makes
 * everything outside its subtree INERT — the global fixed container
 * renders under the backdrop, invisible and unclickable, so errors fired
 * while a modal is open looked like silent failures (qa ledger:
 * settings-duplicate-path-409-behind-modal). Toasts therefore mount
 * INSIDE the open modal (children of the dialog stay interactive and
 * paint above its backdrop; position:fixed keeps the corner placement).
 * When the dialog closes, surviving toasts migrate back to the global
 * container so persistent errors are never lost with it. */
function toastHost() {
    const global = document.getElementById('toast-container');
    let modal = null;
    try {
        modal = document.querySelector('dialog:modal');
    } catch (_e) {
        return global; // no :modal support → pre-existing behavior
    }
    if (!modal) return global;
    let host = modal.querySelector('.forge-toast-host');
    if (!host) {
        host = document.createElement('div');
        host.className = 'forge-toast-host';
        modal.appendChild(host);
        modal.addEventListener('close', () => {
            while (host.firstChild) global.appendChild(host.firstChild);
        });
    }
    return host;
}

export function showToast(message, type = 'info') {
    const container = toastHost();
    if (!container) return;
    const kind = KNOWN_TYPES.includes(type) ? type : 'info';

    const toast = document.createElement('div');
    toast.className = `forge-toast forge-toast--${kind} forge-slide-in`;
    toast.dataset.toastType = kind;
    toast.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    toast.innerHTML = `
        <span class="forge-toast-tag">${kind}</span>
        <span class="forge-toast-msg">${esc(message)}</span>
        <button class="forge-toast-x" aria-label="Dismiss">&times;</button>
    `;
    toast.querySelector('.forge-toast-x').addEventListener('click', () => toast.remove());
    container.appendChild(toast);

    if (kind !== 'error') {
        setTimeout(() => {
            toast.classList.add('forge-fade-out');
            setTimeout(() => toast.remove(), 300);
        }, AUTO_DISMISS_MS);
    }
}

/* HTMX bridge — server-triggered toasts arrive as a `showToast` event
 * (HX-Trigger response header). */
export function initToastBridge() {
    document.body.addEventListener('showToast', (evt) => {
        const d = evt.detail || {};
        showToast(d.message || 'Action completed', d.type || 'info');
    });
}

window.showToast = showToast;
