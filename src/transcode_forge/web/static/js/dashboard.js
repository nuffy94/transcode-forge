/* Dashboard page — pause control, live progress, and cancel actions on
 * the active-transcodes rows (delegated: the rows re-render every 3s).
 */

import { showToast } from './toast.js';
import { initPauseButton, initLiveProgress } from './ops.js';

function initCancelDelegation() {
    document.addEventListener('click', async (e) => {
        const btn = e.target.closest('[data-action="cancel-job"]');
        if (!btn) return;
        try {
            const resp = await fetch(`/api/jobs/${btn.dataset.id}/cancel`, { method: 'POST' });
            if (!resp.ok) {
                showToast(`Error: ${resp.statusText}`, 'error');
                return;
            }
            showToast('Job cancelled', 'warning');
            htmx.ajax('GET', '/partials/active-transcodes', {
                target: '#active-transcodes',
                swap: 'morph:innerHTML',
            });
        } catch {
            showToast('Failed to cancel job', 'error');
        }
    });
}

initPauseButton();
initLiveProgress();
initCancelDelegation();
