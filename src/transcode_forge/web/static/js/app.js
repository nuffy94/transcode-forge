/* Forge Console shell entry — loaded once from base.html as an ES module.
 * Wires the shared shell behaviors; page-specific JS stays with its page.
 */

import { esc, initToastBridge } from './toast.js';
import './actions.js';
import { startClock } from './clock.js';

/* Sidebar queue badge: blank-when-zero. The partial returns the count or
 * an empty body; hide the chip entirely when it's empty (contract —
 * tests/test_view_consistency.py). */
function initQueueBadge() {
    document.body.addEventListener('htmx:afterSwap', (evt) => {
        const t = evt.detail && evt.detail.target;
        if (t && t.id === 'nav-queue-badge') {
            t.style.display = t.textContent.trim() ? '' : 'none';
        }
    });
}

/* Preflight banner — surface startup config problems (bad library path,
 * missing ffmpeg) instead of letting scans fail silently. */
function initPreflight() {
    fetch('/api/health/preflight')
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
            if (!data || !data.issues || !data.issues.length) return;
            const el = document.getElementById('preflight-banner');
            if (!el) return;
            const critical = data.issues.some((i) => i.level === 'critical');
            const tone = critical ? 'forge-banner--critical' : 'forge-banner--warn';
            const items = data.issues.map((i) => `<li>${esc(i.message)}</li>`).join('');
            el.innerHTML = `
                <div class="forge-banner ${tone}">
                    <div class="forge-banner-hd">
                        <span class="forge-banner-title">Configuration ${critical ? 'error' : 'warning'}</span>
                        <button class="forge-banner-x" aria-label="Dismiss">&times;</button>
                    </div>
                    <ul class="forge-banner-list">${items}</ul>
                </div>`;
            el.querySelector('.forge-banner-x').addEventListener('click', () => {
                el.innerHTML = '';
            });
        })
        .catch(() => {});
}

initToastBridge();
startClock();
initQueueBadge();
initPreflight();
