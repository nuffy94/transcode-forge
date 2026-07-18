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
            // Phase transitions move the station highlight between polls;
            // label colors and readout copy catch up on the next 3s morph.
            if (data.phase && row.dataset.phase !== data.phase) {
                row.dataset.phase = data.phase;
                const order = ['search', 'encode', 'verify', 'gauge', 'swap'];
                const cur = order.indexOf(data.phase);
                row.querySelectorAll('.forge-station').forEach((st) => {
                    if (st.classList.contains('forge-station--off')) return;
                    const i = order.indexOf(st.dataset.station);
                    st.classList.remove(
                        'forge-station--done',
                        'forge-station--active',
                        'forge-station--todo',
                        'forge-station--timed'
                    );
                    if (i < cur) st.classList.add('forge-station--done');
                    else if (i === cur) {
                        st.classList.add('forge-station--active');
                        if (data.phase !== 'encode') st.classList.add('forge-station--timed');
                    } else st.classList.add('forge-station--todo');
                });
            }
            // Within-phase progress on the active timed station (gauge %,
            // search probe count). The span is server-rendered empty, so
            // there's always a target; query AFTER the class move above.
            if (data.phase && data.phase !== 'encode') {
                const detailEl = row.querySelector('.forge-station--active [data-phase-detail]');
                if (detailEl) {
                    if (typeof data.phase_pct === 'number') {
                        detailEl.textContent = Math.round(data.phase_pct * 100) + '%';
                    } else if (data.phase_detail) {
                        detailEl.textContent = data.phase_detail;
                    }
                }
            }
        };
        ws.onclose = () => setTimeout(connect, 5000);
    }
    connect();
}
