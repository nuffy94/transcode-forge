/* Shared operations helpers for the Dashboard and Queue pages:
 * the queue pause control and WebSocket live progress.
 */

import { showToast } from './toast.js';

/* Pause/resume button (#pause-btn). The button carries pause-icon /
 * play-icon / pause-label role spans; the resume label is derived from
 * the pause label ("Pause queue" → "Resume queue"). */
export function initPauseButton() {
    const btn = document.getElementById('pause-btn');
    if (!btn) return;
    const label = btn.querySelector('[data-role="pause-label"]');
    const pauseText = label ? label.textContent.trim() : 'Pause';
    const resumeText = pauseText.replace('Pause', 'Resume');

    const apply = (paused) => {
        btn.querySelector('[data-role="pause-icon"]')?.classList.toggle('hidden', paused);
        btn.querySelector('[data-role="play-icon"]')?.classList.toggle('hidden', !paused);
        if (label) label.textContent = paused ? resumeText : pauseText;
    };

    btn.addEventListener('click', async () => {
        try {
            const resp = await fetch('/api/queue/status');
            if (!resp.ok) {
                showToast('Error fetching queue status', 'error');
                return;
            }
            const { paused } = await resp.json();
            const action = await fetch(paused ? '/api/queue/resume' : '/api/queue/pause', {
                method: 'POST',
            });
            if (!action.ok) {
                showToast('Error changing queue state', 'error');
                return;
            }
            apply(!paused);
            showToast(paused ? 'Queue resumed' : 'Queue paused', paused ? 'success' : 'warning');
        } catch (e) {
            showToast(`Failed to change queue state: ${e.message}`, 'error');
        }
    });

    fetch('/api/queue/status')
        .then((r) => r.json())
        .then(({ paused }) => apply(paused))
        .catch(() => {});
}

/* Live progress over WebSocket. Rows are matched by data-job-id and
 * carry data-progress-bar / data-progress-pct (contract C.1). */
export function initLiveProgress() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    function connect() {
        const ws = new WebSocket(`${proto}//${location.host}/ws/updates`);
        ws.onmessage = (evt) => {
            const data = JSON.parse(evt.data);
            const row = document.querySelector(`[data-job-id="${data.job_id}"]`);
            if (!row) return;
            const pct = Math.round(data.progress * 100);
            const barEl = row.querySelector('[data-progress-bar]');
            const pctEl = row.querySelector('[data-progress-pct]');
            if (barEl) barEl.style.width = pct + '%';
            // At 0% the server renders "starting" — don't fight it with "0%".
            if (pctEl && pct > 0) pctEl.textContent = pct + '%';
        };
        ws.onclose = () => setTimeout(connect, 5000);
    }
    connect();
}
