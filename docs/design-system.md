# Forge Console v2 — design system

The reference for every page rebuild (Steps 3–6 of the redesign) and for
anyone touching the web UI afterwards. Source of truth for the CSS is
`assets/css/forge.css` (built to `static/css/app.css` by
`scripts/build_css.py` — never hand-edit the built file).

## The idea

v1 was a poster about a forge; v2 is the instrument panel bolted to it.
A dense ops console (Grafana/Linear class) on warm graphite, where the
character lives in the data itself:

- **Stamped labels** — IBM Plex Mono, 9–10px, uppercase, `tracking-stamp`
  (0.18em). The connective voice of the console.
- **Instrument numerals** — every number renders in IBM Plex Mono with
  `tabular-nums` and slashed zeros. Real values, never zero-padded
  (`45`, not `0045` — padding lies about scale).
- **Temperature-coded status** — ember means working, oxide means good,
  steel means waiting, rust means failed, brass means caution, mute means
  inert. One vocabulary everywhere.
- **The heat seam** — the signature. A 2px molten line along the top edge
  of the viewport (`.forge-seam`), hottest at the brand corner. The same
  gradient metal fills every progress meter.
- **Big Shoulders Display is reserved** for the FORGE wordmark and rare
  display moments. It does not carry data, headings on rebuilt pages, or
  numerals.

Killed in v2 (do not reintroduce): corner brackets / tick marks,
"SECTION 01" stamps, zero-padded numerals, poster-scale page titles,
numbered nav markers, entrance animation on new pages.

## Rules that gate PRs

1. **No one-off styles.** Every visual decision lands in
   `assets/css/forge.css` as a token or component class, or it doesn't
   land. If a page needs something new, add a component.
2. **Dynamic class names built in JS use component classes only** —
   never utilities. The Tailwind scanner cannot see runtime-assembled
   strings (`forge-pill--${status}` works because every modifier is a
   hand-written component class; `bg-${color}-500` would silently vanish).
3. **Data-contract hooks are load-bearing** (tests/test_view_consistency.py,
   tests/qa/test_sweep.py, live WS updates). Keep exactly:
   - `forge-stat-value` — the stat's label text must PRECEDE the value
     element in DOM order, and the value's text must be the bare integer
     (`<span class="forge-stat-value">45</span>`).
   - `data-job-id` and `data-progress-bar` — exactly ONE per job row.
   - `data-progress-pct`, `data-worker-id` — WS live-update targets.
   - `#nav-queue-badge` — blank when zero (the hide-empty listener lives
     in `static/js/app.js`).
   - Toasts: `data-toast-type`, `role="alert"` on errors, errors persist
     until clicked (`static/js/toast.js`).
   - `jobs-queued` joins this list when Step 4 adds it to scheduler-info.
4. **Functional motion only** — live pulses, meter shimmer, 120–240ms
   hover/slide transitions. No entrance stagger on v2 pages
   (`forge-rise` is legacy; dies in Step 7). Everything animated is
   disabled under `prefers-reduced-motion`.
5. **Element-qualified form rules** (`input.forge-input`, …) must keep
   their tag prefix — they out-specify the Tailwind forms plugin.

## Tokens

Utilities come from `@theme` (`bg-forge-*`, `text-forge-*`); component CSS
uses the matching `--forge-*` vars.

| Token | Value | Use |
|---|---|---|
| `forge-bg` | `#0d0b08` | page field (locked — E2E asserts it) |
| `forge-well` | `#0a0806` | recessed slots: table heads, meter troughs, inputs |
| `forge-surface` | `#15120e` | sidebar, drawers |
| `forge-panel` | `#1c1813` | panels, cards |
| `forge-panel-hi` | `#252018` | raised hover surfaces |
| `forge-rule` | `#3a342a` | hairline borders |
| `forge-rule-bright` | `#5a4f3e` | emphasized borders |
| `forge-ember` | `#ff7a1a` | heat: running, active, primary actions |
| `forge-ember-deep` / `forge-ember-hot` | `#cc4f00` / `#ffb86b` | meter gradient ends, hover heat |
| `forge-steel` / `forge-steel-hi` | `#7d8da3` / `#a4b4ca` | waiting / queued |
| `forge-oxide` | `#7eba78` | success |
| `forge-rust` | `#dc6743` | failure (tuned for AA over its tints) |
| `forge-brass` | `#d9b35c` | caution |
| `forge-paper` | `#ede7d8` | primary text |
| `forge-ink` | `#c8c0ad` | body text |
| `forge-mute` | `#9a8f7d` | labels, secondary text (the single mute — v1's #857d6c drift is gone) |
| `forge-faint` | `#6b6350` | **decoration and disabled ONLY — never text** (fails AA) |

Type: `font-display` (Big Shoulders — wordmark only), `font-body`
(IBM Plex Sans, 13px in components), `font-mono` (IBM Plex Mono — data,
labels). `tracking-stamp` = 0.18em. `text-2xs` = 10px. Motion:
`--forge-ease` / `ease-forge` = `cubic-bezier(0.22, 1, 0.36, 1)`.
Radii: none — v2 components are square.

## Icons

Inline SVG sprite, Lucide-derived (ISC), in
`templates/partials/_icons.html`:

```jinja
{% from "partials/_icons.html" import icon %}
{{ icon('queue') }}                          {# 16px default #}
{{ icon('flame', 'forge-icon--lg') }}        {# 13/16/18/22 via --sm/--lg/--xl #}
{{ icon('loader', 'forge-icon--sm forge-icon--spin') }}
```

Unknown names fail the render loudly. Icons are `aria-hidden` — pair with
visible text or an `aria-label` on the control. The Material Symbols font
is legacy (pages drop it as they rebuild; the CDN link + font config die
in Step 7).

Material → sprite mapping for the rebuild steps:

| Material name(s) | Sprite |
|---|---|
| local_fire_department | `flame` |
| dashboard / movie / tv / queue_play_next / engineering / history / block / leaderboard / settings (nav) | `dashboard` `film` `tv` `queue` `workers` `history` `ban` `stats` `settings` |
| menu / logout | `menu` `log-out` |
| settings-as-spinner, hourglass_empty | `loader` (+ `forge-icon--spin`) |
| pause / play_arrow / close / add / add_circle | `pause` `play` `x` `plus` `circle-plus` |
| delete / delete_sweep / edit / refresh / search | `trash` `sweep` `edit` `refresh` `search` |
| chevron_left / chevron_right / expand_more | `chevron-left` `chevron-right` `chevron-down` |
| warning / info / check_circle / cancel / skip_next / shield / help | `alert` `info` `check-circle` `x-circle` `skip` `shield` `help` |
| database / theaters / local_movies / movie_filter / tv_off / apartment | `database` `clapper` `clapper` `movie-off` `tv-off` `building` |
| view_list / list / grid_view / inbox / inventory_2 / flash_off | `list` `list` `grid` `inbox` `archive` `zap-off` |
| vpn_key / key / vpn_key_off | `key` |
| containerization / terminal / speed / hub / folder_off | `container` `terminal` `gauge` `network` `folder-x` |
| toggle_on / toggle_off / schedule / compress | `toggle-on` `toggle-off` `clock` `shrink` |

## Components

All in `@layer components` in `assets/css/forge.css`. Shell pieces
(`forge-seam`, `forge-brand-*`, `forge-navgroup`, `forge-navlink`,
`forge-navbadge`, `forge-header`, `forge-crumb`) belong to `base.html` —
pages don't use them.

**Panel** — flat graphite plate.
```html
<div class="forge-panel">
  <div class="forge-panel-hd">
    <span class="forge-panel-title">Active transcodes</span>
    <!-- optional: live dot, actions pushed right with ml-auto -->
  </div>
  ...body (p-4 or a forge-table)...
</div>
```
`forge-panel--ember` = molten left spine + warm wash, for the ONE hero
surface a page earns. `.forge-panel-hd` + `.forge-panel-title` is the v2
section-heading language — rebuilt pages use it instead of display-font
headings.

**Stat readout** — label above, mono value, optional unit/meta:
```html
<p class="forge-stat-label">Queued</p>
<div class="flex items-baseline gap-1">
  <span class="forge-stat-value text-2xl">45</span>
  <span class="forge-stat-unit">jobs</span>
</div>
<p class="forge-stat-meta">across 2 libraries</p>
```
The component sets no font-size — the page picks the scale (`text-xl` to
`text-4xl`; the dashboard hero may go larger). DOM-shape rules from the
contract section apply.

**Table** — dense ledger:
```html
<div class="forge-scroll max-h-[70vh]">
  <table class="forge-table forge-table--sticky">
    <thead><tr>
      <th class="is-sortable is-sorted-desc">Size</th>...
    </tr></thead>
    <tbody><tr>...<td class="col-mono">3.1 GiB</td>...</tr></tbody>
  </table>
</div>
```
`--sticky` needs the `.forge-scroll` wrapper. Sort state via
`is-sortable` / `is-sorted-asc` / `is-sorted-desc` on `th` (JS toggles).

**Status** — one system, two forms:
- Pills: `forge-pill forge-pill--{complete|running|queued|pending|failed|skipped|cancelled|caution}`
- Dots: `forge-dot forge-dot--{on|hot|off|err}` (+ `forge-pulse` when live)

Mapping: running→ember, complete→oxide, queued→steel, pending/skipped→mute,
failed→rust, cancelled→mute dimmed. Dot inside a pill is fine
(`<span class="forge-pill forge-pill--running"><span class="forge-dot forge-dot--hot forge-pulse"></span>Transcoding</span>`).
Tinted pills use solid backgrounds on purpose (AA over any row striping) —
keep it that way.

**Progress meter** — molten metal in a trough:
```html
<div class="forge-meter"><div class="forge-meter-fill" style="width: 37%"
     data-progress-bar data-progress-pct="37"></div></div>
```
The fill keeps a 3px minimum so 0% reads as a lit pilot light — pair it
with an honest status label ("starting", "awaiting capacity"), never a
bare "0%". `forge-meter-ticks` adds 25/50/75 gauge marks below.

**Buttons** — `forge-btn` (quiet), `forge-btn--ember` (primary),
`forge-btn--danger`; 28px tall, mono uppercase. `forge-iconbtn`
(+`--danger`) for 26px row actions — give it a `title`/`aria-label`.

**Forms** — `forge-input` / `forge-select` / `forge-textarea` /
`forge-check` on their matching elements. Every input keeps a real
`<label>` (axe gate). Search inputs keep the `pl-8` clearance pattern.

**Tabs** — `forge-tab` + `is-active`, in a bordered row.

**Tile button** — `forge-tile`: a stat readout that doubles as a filter
shortcut (queue status strip). JS toggles `is-active` + `aria-pressed`;
the tile is a shortcut into a real form control, never the only control.

**Dialog** — `<dialog class="forge-dialog">` with a `forge-panel` inside.

**Drawer** — right-side detail scaffold (Step 3's file drawer consumes):
```html
<div class="forge-drawer-overlay" id="drawer-overlay"></div>
<aside class="forge-drawer" role="dialog" aria-modal="true" aria-label="File detail">
  <div class="forge-drawer-hd">
    <span class="forge-panel-title">File detail</span>
    <button class="forge-iconbtn ml-auto" aria-label="Close">…</button>
  </div>
  <div class="forge-drawer-bd">…</div>
</aside>
```
Toggle `is-open` on both. Esc/overlay dismiss + focus management are the
consumer's job (Step 3's `drawer.js`).

**Toast** — via `showToast(message, type)` from `static/js/toast.js`
(global `window.showToast` during the transition). Never hand-build toast
markup.

**Banner** — `forge-banner` + `--critical`/`--warn` with `forge-banner-hd`
/ `-title` / `-x` / `-list` (preflight uses it; reuse for page-level
warnings).

**Empty state** — every empty container invites action:
```html
<div class="forge-empty">
  {{ icon('inbox') }}
  <p class="forge-empty-title">No jobs in queue</p>
  <p class="forge-empty-hint">Queue files from Movies or TV Shows, or run a scan.</p>
</div>
```

**Pagination** — `forge-pager` bar: `forge-pager-info` left ("1–50 of
312"), `forge-pager-btns` with `forge-pager-btn` chevrons right.

## JS modules

ES modules under `static/js/`, loaded once via `app.js`
(`<script type="module">` in base.html): `toast.js`, `actions.js`
(exclude/unexclude/unskip/logout), `clock.js`. During the transition they
also hang on `window` for old inline handlers — rebuilt pages import
instead, and add their own module (`catalog.js`, `queue.js`, …) rather
than inline `<script>` blobs (thin glue only).

## Legacy (alive until Step 7 — don't adopt, don't remove yet)

The Material Symbols CDN link + `.material-symbols-outlined` config, the
`@theme` Material alias tokens (`--color-primary`, …), the legacy-compat
CSS block (`status-badge`, `codec-badge`, `tf-checkbox`, `status-dot`,
`tonal-shift-*`, `stagger-in`, old toast keyframes), and `forge-rise`.
Each rebuilt page sheds its usages; Step 7 deletes the lot.
