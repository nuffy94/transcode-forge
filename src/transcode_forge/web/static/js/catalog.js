/* Catalog pages (Movies + TV Shows) — listing, filters, sort, selection,
 * bulk queue, and file-detail drawer wiring.
 *
 * One module serves both pages; each init is gated on its root element.
 * All interaction goes through event delegation on data-action /
 * data-file-id attributes — no inline handlers. Dynamic class names use
 * component classes only (forge-pill--*, codec-*): the Tailwind scanner
 * cannot see runtime-assembled strings.
 */

import { esc, showToast } from './toast.js';

const GIB = 1073741824;

const PILL_CLASS = {
    complete: 'forge-pill--complete',
    transcoding: 'forge-pill--running',
    queued: 'forge-pill--queued',
    pending: 'forge-pill--pending',
    failed: 'forge-pill--failed',
    skipped: 'forge-pill--skipped',
    cancelled: 'forge-pill--cancelled',
    needs_transcode: 'forge-pill--queued',
};

function pill(status) {
    const cls = PILL_CLASS[status] || 'forge-pill--pending';
    return `<span class="forge-pill ${cls}">${esc((status || 'unknown').replace('_', ' '))}</span>`;
}

function codecBadge(codec) {
    const cls = codec === 'h264' ? 'codec-h264' : codec === 'hevc' ? 'codec-hevc' : 'codec-other';
    return `<span class="codec-badge ${cls}">${esc(codec || '?')}</span>`;
}

function gib(bytes) {
    return bytes ? (bytes / GIB).toFixed(1) : '';
}

function emptyState() {
    const tpl = document.getElementById('tpl-empty');
    return tpl ? tpl.innerHTML : '<div class="forge-empty"><p class="forge-empty-title">Nothing here</p></div>';
}

function canQueue(f) {
    return f.video_codec === 'h264' && !['queued', 'transcoding', 'complete'].includes(f.transcode_status);
}

function sortIndicator(state, col) {
    if (state.col !== col) return 'is-sortable';
    return `is-sortable ${state.dir === 'desc' ? 'is-sorted-desc' : 'is-sorted-asc'}`;
}

function pagerButtons(containerId, current, totalPages, onPageAttr) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (totalPages <= 1) {
        el.innerHTML = '';
        return;
    }
    const cp = Math.min(Math.max(current, 1), totalPages);
    let html = '';
    for (let p = Math.max(1, cp - 3); p <= Math.min(totalPages, cp + 3); p++) {
        const active = p === cp ? ' !border-forge-ember !text-forge-ember' : '';
        html += `<button class="forge-pager-btn font-mono text-2xs font-semibold w-6${active}" data-action="${onPageAttr}" data-page="${p}" aria-label="Page ${p}">${p}</button>`;
    }
    el.innerHTML = html;
}

async function postQueue(ids, codecSelectId) {
    const codec = document.getElementById(codecSelectId)?.value || '';
    const body = JSON.stringify(codec ? { file_ids: ids, codec } : { file_ids: ids });
    const resp = await fetch('/api/media/queue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
    });
    if (!resp.ok) throw new Error(resp.statusText);
    return resp.json();
}

/* ================================================================
 * Movies
 * ================================================================ */

const mv = {
    page: 1,
    view: localStorage.getItem('tf-view') || 'table',
    sort: { col: 'file_size', dir: 'desc' },
    selected: new Set(),
};

// Columns that read "high first" sort descending on first click; the rest A→Z.
const DESC_FIRST = ['file_size', 'file_modified_at', 'duration', 'scanned_at'];

function setView(view) {
    mv.view = view;
    localStorage.setItem('tf-view', view);
    const t = document.getElementById('view-table');
    const g = document.getElementById('view-grid');
    if (t) t.style.color = view === 'table' ? 'var(--forge-ember)' : '';
    if (g) g.style.color = view === 'grid' ? 'var(--forge-ember)' : '';
    // The sort select is the grid view's sort control; tables sort by header.
    document.getElementById('mv-sort')?.classList.toggle('hidden', view !== 'grid');
    loadMovies();
}

function loadMovies(page) {
    // No arg = a filter/sort/search change or a view switch → back to page 1.
    mv.page = page || 1;
    const params = new URLSearchParams({
        status: document.getElementById('mv-status').value,
        codec: document.getElementById('mv-codec').value,
        sort: mv.sort.col,
        dir: mv.sort.dir,
        search: document.getElementById('mv-search').value,
        page: mv.page,
        per_page: mv.view === 'grid' ? 48 : 50,
    });
    for (const [k, v] of [...params]) {
        if (!v) params.delete(k);
    }

    fetch(`/api/media/movies?${params}`)
        .then((r) => r.json())
        .then((data) => {
            // Past the last page (the set shrank under us)? Snap back.
            const totalPages = Math.ceil((data.meta.total || 0) / data.meta.per_page);
            if (!data.data.length && data.meta.total > 0 && mv.page > totalPages) {
                return loadMovies(totalPages);
            }
            return mv.view === 'grid' ? renderMovieGrid(data) : renderMovieTable(data);
        })
        .catch(() => showToast('Failed to load movies', 'error'));
}

/* The listing endpoint carries no aggregates — the stat strip reads the
 * real per-status counts from /api/media/stats (filter-independent). */
async function loadMovieStats() {
    try {
        const resp = await fetch('/api/media/stats');
        const map = (await resp.json()).data?.movies || {};
        const total = Object.values(map).reduce((a, b) => a + b, 0);
        setText('stat-total', total);
        setText('stat-hevc', map.complete || 0);
        setText('stat-pending', (map.needs_transcode || 0) + (map.pending || 0));
        setText('stat-queued', (map.queued || 0) + (map.transcoding || 0));
    } catch {
        /* tiles keep their placeholder */
    }
}

function movieHead() {
    const th = (label, col, cls = '') =>
        `<th class="${sortIndicator(mv.sort, col)} ${cls}" data-action="mv-sort" data-col="${col}">${label}</th>`;
    return `<thead><tr>
        <th class="w-10"><input type="checkbox" class="forge-check" aria-label="Select all files" data-role="mv-select-all"></th>
        ${th('Movie', 'filename')}
        ${th('Codec', 'video_codec')}
        ${th('Resolution', 'resolution')}
        ${th('Size', 'file_size', 'text-right')}
        ${th('Date', 'file_modified_at')}
        ${th('Status', 'transcode_status')}
        <th class="text-right">Actions</th>
    </tr></thead>`;
}

function renderMovieTable(data) {
    const { data: files, meta } = data;
    const start = files.length ? (meta.page - 1) * meta.per_page + 1 : 0;
    const end = (meta.page - 1) * meta.per_page + files.length;
    setText('mv-count', files.length ? `${start}–${end} of ${meta.total}` : `0 of ${meta.total}`);

    const content = document.getElementById('movies-content');
    if (!files.length) {
        content.innerHTML = emptyState();
        pagerButtons('movies-pagination-buttons', 1, 0, 'mv-page');
        return;
    }

    let html = `<table class="forge-table">${movieHead()}<tbody>`;
    for (const f of files) {
        const checked = mv.selected.has(f.id) ? 'checked' : '';
        html += `<tr data-file-id="${esc(f.id)}" class="cursor-pointer">
            <td><input type="checkbox" class="forge-check file-select" aria-label="Select ${esc(f.filename)}" value="${esc(f.id)}" ${checked} data-role="mv-select"></td>
            <td><span class="text-forge-paper">${esc(f.filename)}</span></td>
            <td>${codecBadge(f.video_codec)}</td>
            <td><span class="font-mono text-2xs uppercase tracking-wider text-forge-steel">${esc(f.resolution || '')}</span></td>
            <td class="col-mono text-right">${gib(f.file_size)}<span class="text-forge-mute"> GiB</span></td>
            <td class="col-mono text-xs text-forge-mute">${esc(f.file_modified_at ? f.file_modified_at.substring(0, 10) : '')}</td>
            <td>${pill(f.transcode_status)}</td>
            <td class="text-right">${canQueue(f) ? `<button class="forge-btn" data-action="queue-file" data-id="${esc(f.id)}">Queue</button>` : ''}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    content.innerHTML = html;
    pagerButtons('movies-pagination-buttons', mv.page, Math.ceil(meta.total / meta.per_page), 'mv-page');
}

function renderMovieGrid(data) {
    const { data: files, meta } = data;
    setText('mv-count', `${meta.total} files`);

    const content = document.getElementById('movies-content');
    if (!files.length) {
        content.innerHTML = emptyState();
        pagerButtons('movies-pagination-buttons', 1, 0, 'mv-page');
        return;
    }

    let html = '<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-2.5 p-3">';
    for (const f of files) {
        const checked = mv.selected.has(f.id) ? 'checked' : '';
        const title = esc(f.filename.replace(/\.[^.]+$/, '').replace(/\./g, ' '));
        html += `<div class="forge-panel relative p-2.5 flex flex-col gap-1.5 cursor-pointer group" data-file-id="${esc(f.id)}">
            <input type="checkbox" class="absolute top-2 right-2 forge-check file-select opacity-0 group-hover:opacity-100 checked:opacity-100 transition-opacity" aria-label="Select ${esc(f.filename)}" value="${esc(f.id)}" ${checked} data-role="mv-select">
            <p class="text-xs text-forge-paper truncate font-medium" title="${esc(f.filename)}">${title}</p>
            <div class="flex items-center justify-between">
                ${codecBadge(f.video_codec)}
                <span class="font-mono text-2xs text-forge-mute tabular-nums">${gib(f.file_size) || '?'} GiB</span>
            </div>
            <div class="flex items-center justify-between">
                <span class="font-mono text-2xs text-forge-mute uppercase">${esc(f.resolution || '')}</span>
                ${pill(f.transcode_status)}
            </div>
        </div>`;
    }
    html += '</div>';
    content.innerHTML = html;
    pagerButtons('movies-pagination-buttons', mv.page, Math.ceil(meta.total / meta.per_page), 'mv-page');
}

function updateBulkBar() {
    const bar = document.getElementById('bulk-bar');
    if (!bar) return;
    const count = mv.selected.size;
    setText('bulk-count', count);
    bar.style.display = count > 0 ? 'inline-flex' : 'none';
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = String(value);
}

async function queueFiles(ids, noun) {
    if (!ids.length) return;
    try {
        const data = await postQueue(ids, 'queue-codec');
        showToast(`Queued ${data.queued} ${noun}${data.queued !== 1 ? 's' : ''}`, 'success');
        return true;
    } catch (err) {
        showToast(`Failed to queue: ${err.message}`, 'error');
        return false;
    }
}

function initMovies() {
    const on = (id, evt, fn) => document.getElementById(id)?.addEventListener(evt, fn);

    on('view-table', 'click', () => setView('table'));
    on('view-grid', 'click', () => setView('grid'));
    on('mv-status', 'change', () => loadMovies());
    on('mv-codec', 'change', () => loadMovies());
    on('mv-sort', 'change', (e) => {
        const [col, dir] = e.target.value.split(':');
        mv.sort = { col, dir };
        loadMovies();
    });
    on('mv-search', 'input', () => {
        clearTimeout(window._mvSearchTimer);
        window._mvSearchTimer = setTimeout(() => loadMovies(), 300);
    });
    on('queue-codec', 'change', () => {
        const codec = document.getElementById('queue-codec').value;
        const warn = document.getElementById('codec-warning');
        if (warn) warn.style.display = codec === 'av1' ? 'flex' : 'none';
    });
    on('bulk-queue', 'click', async () => {
        if (await queueFiles([...mv.selected], 'file')) {
            mv.selected.clear();
            updateBulkBar();
            loadMovies(mv.page);
            loadMovieStats();
        }
    });
    on('bulk-clear', 'click', () => {
        mv.selected.clear();
        document.querySelectorAll('.file-select').forEach((cb) => (cb.checked = false));
        updateBulkBar();
    });

    document.addEventListener('change', (e) => {
        const t = e.target;
        if (t.dataset?.role === 'mv-select') {
            if (t.checked) mv.selected.add(t.value);
            else mv.selected.delete(t.value);
            updateBulkBar();
        } else if (t.dataset?.role === 'mv-select-all') {
            document.querySelectorAll('.file-select').forEach((cb) => {
                cb.checked = t.checked;
                if (t.checked) mv.selected.add(cb.value);
                else mv.selected.delete(cb.value);
            });
            updateBulkBar();
        }
    });

    document.addEventListener('tf:catalog-refresh', () => {
        loadMovies(mv.page);
        loadMovieStats();
    });
    loadMovieStats();
    setView(mv.view);
}

/* ================================================================
 * TV Shows
 * ================================================================ */

const tv = {
    page: 1,
    sort: { col: 'show_name', dir: 'asc' },
};

function initTv() {
    const on = (id, evt, fn) => document.getElementById(id)?.addEventListener(evt, fn);

    on('tab-shows', 'click', () => switchTvView('shows'));
    on('tab-files', 'click', () => switchTvView('files'));
    on('shows-search', 'input', applyShowFilters);
    on('shows-sort', 'change', applyShowFilters);
    on('shows-hide-done', 'change', applyShowFilters);
    on('tv-status', 'change', () => loadTvFiles());
    on('tv-search', 'input', () => {
        clearTimeout(window._tvSearchTimer);
        window._tvSearchTimer = setTimeout(() => loadTvFiles(), 300);
    });
    on('queue-codec', 'change', () => {
        const codec = document.getElementById('queue-codec').value;
        const warn = document.getElementById('codec-warning');
        if (warn) warn.style.display = codec === 'av1' ? 'inline-flex' : 'none';
    });
    on('tv-queue-selected', 'click', async () => {
        const ids = [...document.querySelectorAll('.tv-select:checked')].map((cb) => cb.value);
        if (await queueFiles(ids, 'episode')) {
            await loadTvFiles(tv.page);
        }
    });

    document.addEventListener('change', (e) => {
        if (e.target.classList?.contains('tv-select') || e.target.dataset?.role === 'tv-select-all') {
            if (e.target.dataset?.role === 'tv-select-all') {
                e.target
                    .closest('table')
                    ?.querySelectorAll('.tv-select')
                    .forEach((cb) => (cb.checked = e.target.checked));
            }
            updateTvSelectionCount();
        }
    });

    document.addEventListener('tf:catalog-refresh', () => {
        loadShows();
        if (!document.getElementById('files-view').classList.contains('hidden')) loadTvFiles(tv.page);
    });

    loadShows();
}

function switchTvView(view) {
    document.getElementById('shows-view').classList.toggle('hidden', view !== 'shows');
    document.getElementById('files-view').classList.toggle('hidden', view === 'shows');
    document.getElementById('tab-shows').classList.toggle('is-active', view === 'shows');
    document.getElementById('tab-files').classList.toggle('is-active', view !== 'shows');
    if (view === 'files') loadTvFiles();
}

async function loadShows() {
    const resp = await fetch('/api/media/tv/shows');
    const { data } = await resp.json();
    const shows = data || [];

    // The endpoint carries no aggregate block — derive the strip from the
    // show rows themselves so the numbers can never disagree with the list.
    const episodes = shows.reduce((a, s) => a + (s.episode_count || 0), 0);
    const done = shows.reduce((a, s) => a + (s.transcoded_count || 0), 0);
    const pending = shows.reduce((a, s) => a + (s.needs_transcode_count || 0), 0);
    setText('stat-shows', shows.length);
    setText('stat-episodes', episodes);
    const eff = document.getElementById('stat-efficiency');
    if (eff) {
        eff.innerHTML = `${episodes ? Math.round((done / episodes) * 100) : 0}<span class="forge-stat-unit">%</span>`;
    }
    setText('stat-pending', pending);

    window._tvShowsCache = shows;
    applyShowFilters();
}

function applyShowFilters() {
    const all = window._tvShowsCache || [];
    const q = (document.getElementById('shows-search')?.value || '').trim().toLowerCase();
    const hideDone = document.getElementById('shows-hide-done')?.checked ?? true;
    const sortKey = document.getElementById('shows-sort')?.value || 'todo_desc';

    const filtered = all.filter((show) => {
        if (hideDone && (show.needs_transcode_count || 0) === 0) return false;
        if (q && !show.show_name.toLowerCase().includes(q)) return false;
        return true;
    });

    const sorters = {
        todo_desc: (a, b) =>
            (b.needs_transcode_count || 0) - (a.needs_transcode_count || 0) ||
            a.show_name.localeCompare(b.show_name),
        size_desc: (a, b) => (b.total_size || 0) - (a.total_size || 0),
        size_asc: (a, b) => (a.total_size || 0) - (b.total_size || 0),
        episodes_desc: (a, b) => (b.episode_count || 0) - (a.episode_count || 0),
        name_asc: (a, b) => a.show_name.localeCompare(b.show_name),
        name_desc: (a, b) => b.show_name.localeCompare(a.show_name),
    };
    const sorted = [...filtered].sort(sorters[sortKey] || sorters.todo_desc);

    setText('shows-visible-count', `${sorted.length} of ${all.length} shows`);

    const target = document.getElementById('shows-list');
    if (!sorted.length) {
        const head = !all.length ? 'No TV shows' : hideDone ? 'All caught up' : 'No matches';
        const body = !all.length
            ? 'Add a TV library in Settings, then scan it from the Queue page.'
            : hideDone
              ? 'Every show is fully transcoded. Untick "Hide completed" to browse them.'
              : 'No show names match your filter.';
        target.innerHTML = `<div class="forge-panel"><div class="forge-empty">
            <p class="forge-empty-title">${head}</p>
            <p class="forge-empty-hint">${body}</p>
        </div></div>`;
        return;
    }

    target.innerHTML = sorted.map(renderShowRow).join('');
}

function renderShowRow(show) {
    const total = show.episode_count || 0;
    const done = show.transcoded_count || 0;
    const todo = show.needs_transcode_count || 0;
    const donePct = total > 0 ? (done / total) * 100 : 0;
    const todoPct = total > 0 ? (todo / total) * 100 : 0;
    const sizeGiB = gib(show.total_size) || '0';
    const showId = show.show_name.replace(/[^a-zA-Z0-9]/g, '_');
    const isDone = todo === 0;

    return `
    <div class="forge-panel ${isDone ? 'opacity-70' : ''}">
        <div class="flex items-center gap-4 px-4 py-2.5 cursor-pointer hover:bg-forge-panel-hi/40 transition-colors" data-action="toggle-show" data-show-id="${showId}" data-show-name="${esc(show.show_name)}">
            <div class="flex-1 min-w-0">
                <span class="text-[13px] font-semibold text-forge-paper">${esc(show.show_name)}</span>
                <span class="font-mono text-2xs text-forge-mute ml-2 tabular-nums">${total} eps · ${sizeGiB} GiB</span>
            </div>
            <div class="hidden md:flex flex-col gap-1 w-64">
                <div class="flex justify-between font-mono text-2xs uppercase tracking-stamp">
                    <span class="text-forge-oxide tabular-nums">${done} done</span>
                    <span class="${isDone ? 'text-forge-mute' : 'text-forge-ember-hot'} tabular-nums">${todo} to do</span>
                </div>
                <div class="relative h-1.5 bg-forge-well border border-forge-rule overflow-hidden">
                    <div class="absolute inset-y-0 left-0" style="width:${donePct}%; background: var(--forge-oxide);"></div>
                    <div class="absolute inset-y-0" style="left:${donePct}%; width:${todoPct}%; background: var(--forge-ember);"></div>
                </div>
            </div>
            ${isDone ? '' : `<button class="forge-btn forge-btn--ember whitespace-nowrap" data-action="queue-show" data-show-name="${esc(show.show_name)}">Queue ${todo}</button>`}
            <span class="text-forge-mute" id="expand-${showId}">▾</span>
        </div>
        <div id="content-${showId}" class="hidden bg-forge-well/60 border-t border-forge-rule"></div>
    </div>`;
}

function toggleShow(el) {
    const showId = el.dataset.showId;
    const showName = el.dataset.showName;
    const content = document.getElementById('content-' + showId);
    if (!content) return;
    content.classList.toggle('hidden');
    if (!content.classList.contains('hidden') && !content.dataset.loaded) {
        htmx.ajax('GET', '/partials/tv-episodes?show=' + encodeURIComponent(showName), {
            target: content,
            swap: 'innerHTML',
        });
        content.dataset.loaded = 'true';
    }
}

async function fetchAllShowEpisodeIds(showName) {
    // The API caps per_page at 200 — page through; long-running shows exceed one page.
    const ids = [];
    let page = 1;
    let total = Infinity;
    while (ids.length < total) {
        const resp = await fetch(
            `/api/media/tv?show=${encodeURIComponent(showName)}&status=needs_transcode&per_page=200&page=${page}`
        );
        if (!resp.ok) throw new Error(resp.statusText);
        const { data, meta } = await resp.json();
        if (!data.length) break;
        ids.push(...data.map((f) => f.id));
        total = meta.total;
        page += 1;
    }
    return ids;
}

async function queueShow(showName, btn) {
    if (btn) btn.disabled = true;
    try {
        const ids = await fetchAllShowEpisodeIds(showName);
        if (!ids.length) {
            showToast('Nothing to queue for this show', 'info');
            return;
        }
        if (await queueFiles(ids, 'episode')) await loadShows();
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function loadTvFiles(page) {
    tv.page = page || 1;
    const params = new URLSearchParams({
        status: document.getElementById('tv-status').value,
        sort: tv.sort.col,
        dir: tv.sort.dir,
        search: document.getElementById('tv-search').value,
        page: tv.page,
        per_page: 50,
    });
    for (const [k, v] of [...params]) {
        if (!v) params.delete(k);
    }
    const resp = await fetch(`/api/media/tv?${params}`);
    const data = await resp.json();
    const totalPages = Math.ceil((data.meta.total || 0) / data.meta.per_page);
    if (!data.data.length && data.meta.total > 0 && tv.page > totalPages) {
        return loadTvFiles(totalPages);
    }
    renderTvTable(data);
}

function renderTvTable(data) {
    const { data: files, meta } = data;
    const start = files.length ? (meta.page - 1) * meta.per_page + 1 : 0;
    const end = (meta.page - 1) * meta.per_page + files.length;
    setText('tv-count', files.length ? `${start}–${end} of ${meta.total}` : `0 of ${meta.total}`);

    const target = document.getElementById('tv-table');
    if (!files.length) {
        target.innerHTML = emptyState();
        pagerButtons('tv-pagination-buttons', 1, 0, 'tv-page');
        return;
    }

    const th = (label, col, cls = '') =>
        `<th class="${sortIndicator(tv.sort, col)} ${cls}" data-action="tv-sort" data-col="${col}">${label}</th>`;
    let html = `<table class="forge-table"><thead><tr>
        <th class="w-10"><input type="checkbox" class="forge-check" aria-label="Select all episodes" data-role="tv-select-all"></th>
        ${th('Show', 'show_name')}
        ${th('Episode', 'filename')}
        ${th('Codec', 'video_codec')}
        ${th('Size', 'file_size', 'text-right')}
        ${th('Status', 'transcode_status')}
        <th class="text-right">Actions</th>
    </tr></thead><tbody>`;

    for (const f of files) {
        const ep =
            f.season && f.episode
                ? `S${String(f.season).padStart(2, '0')}E${String(f.episode).padStart(2, '0')}`
                : '';
        html += `<tr data-file-id="${esc(f.id)}" class="cursor-pointer">
            <td><input type="checkbox" class="forge-check tv-select" aria-label="Select ${esc(f.filename)}" value="${esc(f.id)}"></td>
            <td><span class="text-forge-paper">${esc(f.show_name || '')}</span></td>
            <td><span class="font-mono text-xs text-forge-ink">${esc(ep || f.filename)}</span></td>
            <td>${codecBadge(f.video_codec)}</td>
            <td class="col-mono text-right">${gib(f.file_size)}<span class="text-forge-mute"> GiB</span></td>
            <td>${pill(f.transcode_status)}</td>
            <td class="text-right">${canQueue(f) ? `<button class="forge-btn" data-action="queue-file" data-id="${esc(f.id)}">Queue</button>` : ''}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    target.innerHTML = html;
    updateTvSelectionCount();
    pagerButtons('tv-pagination-buttons', tv.page, Math.ceil(meta.total / meta.per_page), 'tv-page');
}

function updateTvSelectionCount() {
    const n = document.querySelectorAll('.tv-select:checked').length;
    setText('tv-selected-count', n);
    const btn = document.getElementById('tv-queue-selected');
    if (btn) {
        btn.disabled = n === 0;
        btn.textContent = n > 0 ? `Queue selected (${n})` : 'Queue selected';
    }
}

/* ================================================================
 * Shared delegation: actions, sorting, row → drawer
 * ================================================================ */

function cycleSort(state, col, reload) {
    if (state.col === col) {
        state.dir = state.dir === 'asc' ? 'desc' : 'asc';
    } else {
        state.col = col;
        state.dir = DESC_FIRST.includes(col) ? 'desc' : 'asc';
    }
    reload();
}

document.addEventListener('click', async (e) => {
    const actionEl = e.target.closest('[data-action]');
    if (actionEl) {
        const a = actionEl.dataset.action;
        if (a === 'queue-file') {
            e.stopPropagation();
            if (await queueFiles([actionEl.dataset.id], 'file')) {
                document.dispatchEvent(new CustomEvent('tf:catalog-refresh'));
            }
        } else if (a === 'mv-page') {
            loadMovies(Number(actionEl.dataset.page));
        } else if (a === 'tv-page') {
            loadTvFiles(Number(actionEl.dataset.page));
        } else if (a === 'mv-sort') {
            cycleSort(mv.sort, actionEl.dataset.col, () => loadMovies());
        } else if (a === 'tv-sort') {
            cycleSort(tv.sort, actionEl.dataset.col, () => loadTvFiles());
        } else if (a === 'toggle-show') {
            if (!e.target.closest('button')) toggleShow(actionEl);
        } else if (a === 'queue-show') {
            e.stopPropagation();
            queueShow(actionEl.dataset.showName, actionEl);
        }
    }
    // Row click → file-detail drawer is handled globally by drawer.js.
});

if (document.getElementById('movies-content')) initMovies();
if (document.getElementById('shows-list')) initTv();
