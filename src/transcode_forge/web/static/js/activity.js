/* Activity page — facet switching between the two honest ledgers
 * (encode outcomes / scan skips), KPI numbers, and header sorting.
 * Filters are declarative HTMX on the selects; JS only handles what
 * markup can't: facet state, stats fill, sort inputs, dropdowns.
 */

const OUTCOMES_DESC_FIRST = ['space_saved', 'completed_at'];
const SKIPS_DESC_FIRST = ['file_size'];

function switchView(view, push) {
    const outcomes = view !== 'skips';
    document.getElementById('outcomes-view').classList.toggle('hidden', !outcomes);
    document.getElementById('skips-view').classList.toggle('hidden', outcomes);
    for (const [id, active] of [
        ['tab-outcomes', outcomes],
        ['tab-skips', !outcomes],
    ]) {
        const tab = document.getElementById(id);
        tab.classList.toggle('is-active', active);
        tab.setAttribute('aria-selected', String(active));
    }
    if (push) {
        history.replaceState(null, '', outcomes ? '/activity' : '/activity?view=skips');
    }
}

/* Header sort handlers (referenced by the sort_th macro, re-rendered on
 * every poll — hence window globals + hidden inputs the containers
 * hx-include). */
function makeSorter(sortId, dirId, containerId, descFirst) {
    return (col) => {
        const s = document.getElementById(sortId);
        const d = document.getElementById(dirId);
        if (s.value === col) {
            d.value = d.value === 'asc' ? 'desc' : 'asc';
        } else {
            s.value = col;
            d.value = descFirst.includes(col) ? 'desc' : 'asc';
        }
        htmx.trigger('#' + containerId, 'refresh');
    };
}
window.sortOutcomes = makeSorter('outcomes-sort', 'outcomes-dir', 'outcomes-container', OUTCOMES_DESC_FIRST);
window.sortSkips = makeSorter('skip-sort', 'skip-dir', 'skipped-container', SKIPS_DESC_FIRST);

/* KPI strip — real numerals off /api/stats. */
function loadStats() {
    fetch('/api/stats')
        .then((r) => r.json())
        .then(({ data }) => {
            const completed = data.completed || 0;
            const failed = data.jobs_by_status?.failed || 0;
            const saved = data.total_space_saved_display || { value: '0.0', unit: 'GiB' };
            const source = data.total_source_bytes || 0;
            const output = data.total_output_bytes || 0;
            const avgPct = source > 0 ? Math.round((1 - output / source) * 100) : 0;

            document.getElementById('stat-completed').textContent = String(completed);
            // Pre-formatted server-side (api/routes/stats.format_size).
            document.getElementById('stat-saved').innerHTML =
                `${saved.value}<span class="forge-stat-unit">${saved.unit}</span>`;
            document.getElementById('stat-avg').innerHTML =
                `${avgPct}<span class="forge-stat-unit">%</span>`;
            document.getElementById('stat-failed').textContent = String(failed);
        })
        .catch(() => {});
}

/* Library dropdowns on both facets */
fetch('/api/libraries')
    .then((r) => r.json())
    .then(({ data }) => {
        for (const sel of [
            document.getElementById('outcomes-library'),
            document.getElementById('skip-library-filter'),
        ]) {
            if (!sel) continue;
            for (const lib of data) {
                const opt = document.createElement('option');
                opt.value = lib.id;
                opt.textContent = lib.name;
                sel.appendChild(opt);
            }
        }
    })
    .catch(() => {});

document.getElementById('tab-outcomes').addEventListener('click', () => switchView('outcomes', true));
document.getElementById('tab-skips').addEventListener('click', () => switchView('skips', true));

loadStats();
