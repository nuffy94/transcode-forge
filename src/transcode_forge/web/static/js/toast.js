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

export function esc(text) {
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

export function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
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
