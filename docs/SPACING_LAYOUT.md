# Spacing & Layout Guide

Consistent spacing and layout patterns for Transcode Forge UI.

## Spacing Scale

Based on Tailwind's 4px unit system (multiples of 4px):

| Tailwind | Pixels | Usage |
|----------|--------|-------|
| gap-1 | 4px | Tight grouping (rarely used) |
| gap-2 | 8px | Small gaps between items |
| gap-3 | 12px | Menu item gaps |
| gap-4 | 16px | Standard gap between components |
| gap-6 | 24px | Large gap between sections |
| gap-8 | 32px | Extra large section spacing |

### Padding Scale

| Tailwind | Pixels | Usage |
|----------|--------|-------|
| p-2 | 8px | Minimal padding |
| p-3 | 12px | Card content (tight) |
| p-4 | 16px | Standard card padding, button padding |
| p-6 | 24px | Page content padding |
| p-8 | 32px | Extra padding (modals, large sections) |

### Margin Scale

| Tailwind | Pixels | Usage |
|----------|--------|-------|
| mt-2 | 8px | Small top margin |
| mt-4 | 16px | Standard top margin |
| mt-6 | 24px | Large top margin |
| mb-2 | 8px | Small bottom margin |
| mb-4 | 16px | Standard bottom margin |
| mb-8 | 32px | Large bottom margin |

---

## Layout Specifications

### Page Layout

```
┌─────────────────────────────────────────┐
│  Navbar (mobile only, lg:hidden)        │  Height: 56px
├─────────────────┬───────────────────────┤
│                 │ Main Content Area     │
│   Sidebar       │ p-6 (desktop)         │
│   w-64          │ p-4 (mobile)          │
│   bg-base-200   │ bg-base-300           │
│                 │                       │
│                 │                       │
│                 │                       │
│                 │                       │
└─────────────────┴───────────────────────┘
```

**Key Measurements**:
- **Sidebar Width**: 256px (w-64)
- **Sidebar Logo Padding**: 16px (p-4)
- **Content Padding Desktop**: 24px (p-6)
- **Content Padding Mobile**: 16px (p-4)
- **Border between sidebar & header**: 1px solid base-300

### Sidebar Navigation

```
┌─ Sidebar (256px) ──────────────────────┐
│ ┌─ Logo Section ─────────────────────┐ │
│ │ Transcode Forge        p-4         │ │  Height: ~80px
│ │ v0.1.0                            │ │
│ └────────────────────────────────────┘ │  border-b
│                                        │
│ ┌─ Menu ─────────────────────────────┐ │
│ │ OVERVIEW                 text-xs   │ │  opacity-50
│ │ ◉ Dashboard       py-3 px-2        │ │  tracking-wider
│ │ □ Movies          active class     │ │
│ │ □ TV Shows        applies bg       │ │
│ │                                     │ │
│ │ PROCESSING                          │ │
│ │ □ Queue                             │ │
│ │ □ Workers                           │ │
│ │ □ History                           │ │
│ │                                     │ │
│ │ SYSTEM                              │ │
│ │ □ Stats                             │ │
│ │ □ Settings                          │ │
│ └────────────────────────────────────┘ │  flex-1 (grows)
│                                        │
│ ┌─ Health Footer ────────────────────┐ │
│ │ ● Online                  p-4      │ │  border-t
│ └────────────────────────────────────┘ │
└────────────────────────────────────────┘
```

**Sidebar Spacing**:
- **Logo section padding**: 16px (p-4)
- **Menu padding**: 16px (p-4)
- **Menu items vertical padding**: 12px (py-3)
- **Menu items horizontal padding**: 8px (px-2)
- **Menu gap**: 4px (gap-1)
- **Menu title font**: text-xs, uppercase, tracking-wider, opacity-50
- **Icon size**: h-5 w-5

---

## Card & Container Spacing

### Standard Card

```
┌─ Card (bg-base-100, shadow-sm) ────────────┐
│                                            │
│ ┌─ Card Body (p-4) ──────────────────────┐ │
│ │                                        │ │
│ │ Card Title (text-base, font-bold)     │ │
│ │                                        │ │  margin-bottom: 1rem (implicit)
│ │ ┌──────────────────────────────────┐  │ │
│ │ │ Card content area                │  │ │
│ │ │                                  │  │ │
│ │ │                                  │  │ │
│ │ └──────────────────────────────────┘  │ │
│ │                                        │ │  margin-top: 1rem
│ │ ┌─ Card Actions (justify-end gap-2) ┐ │ │
│ │ │ [Button] [Button]                 │ │ │
│ │ └──────────────────────────────────┘ │ │
│ └────────────────────────────────────────┘ │
└────────────────────────────────────────────┘
```

**Card Spacing**:
- **Card Padding**: 16px (p-4)
- **Title Bottom Margin**: 16px (mb-4, implicit from DaisyUI)
- **Content Padding**: Included in card-body
- **Actions Top Margin**: 16px (mt-4)
- **Actions Gap**: 8px (gap-2)
- **Card Border Radius**: 8px (rounded-lg, implicit from DaisyUI)
- **Card Shadow**: subtle (shadow-sm)

### Grid Spacing

**Dashboard Stats Grid** (4-column on desktop):
```html
<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
  <div class="stat card bg-base-100">...</div>
</div>
```

- **Mobile**: 1 column, gap-6 (24px)
- **Tablet (md)**: 2 columns, gap-6 (24px)
- **Desktop (lg)**: 4 columns, gap-6 (24px)
- **Between sections**: gap-6 (24px)

**Movie/TV Library Grid** (responsive):
```html
<div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4">
  <div class="card">...</div>
</div>
```

- **Mobile (xs)**: 2 columns
- **Small (sm)**: 3 columns
- **Medium (md)**: 4 columns
- **Large (lg)**: 5 columns
- **X-Large (xl)**: 6 columns
- **Gap**: 16px (gap-4)
- **Card Height**: h-72 (288px, for poster aspect ratio)

---

## Table Spacing

### Table Layout

```
┌──────────────────────────────────────────────────────────────┐
│ ┌─ Header (bg-base-200) ────────────────────────────────────┐│
│ │ File              Progress           Status      Worker   ││ 44px
│ │ px-4 py-3                                                 ││
│ └───────────────────────────────────────────────────────────┘│
│ ┌─ Row 1 (hover:bg-base-200) ──────────────────────────────┐│
│ │ Avatar.2009...    ███████████████  ✓ Complete   worker-1   ││ 48px
│ │ px-4 py-3                                               ││
│ └───────────────────────────────────────────────────────────┘│
│ ┌─ Row 2 (striped) ────────────────────────────────────────┐│
│ │ Inception.2010... ██████████░░░    ⟳ Transcoding  worker-2  ││ 48px
│ │ px-4 py-3                                               ││
│ └───────────────────────────────────────────────────────────┘│
│ ┌─ Row 3 (hover:bg-base-200) ──────────────────────────────┐│
│ │ Interstellar.2014 █░░░░░░░░░░░░░  ○ Queued      —       ││ 48px
│ │ px-4 py-3                                               ││
│ └───────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
```

**Table Spacing**:
- **Table Class**: table table-sm table-zebra
- **Header Cell Padding**: 12px 16px (py-3 px-4)
- **Header Height**: 44px
- **Row Height**: 48-52px
- **Row Cell Padding**: 12px 16px (py-3 px-4)
- **Striped alternation**: Every other row bg-base-100 vs base-300
- **Row Hover**: hover:bg-base-200
- **Text Size**: text-sm (14px) by default

### Progress Bar in Table

```
┌─────────────────────────────────────────┐
│ ████████████░░░░░░░░░░░  65%           │ 24px height
└─────────────────────────────────────────┘
```

- **Progress Height**: 24px (default DaisyUI)
- **Cell Padding**: 12px 16px

---

## Typography Spacing

### Heading Hierarchy

```
┌─────────────────────────────────────────┐
│ Dashboard (text-3xl, font-bold)         │ 28px
│ mb-2                                    │
│ Dashboard Stats and Activity            │ text-sm, base-content/70
│ mb-8                                    │
│ ┌─────────────────────────────────────┐ │
│ │ [Stat Cards Grid]                   │ │
│ └─────────────────────────────────────┘ │
│ gap-6                                   │
│ ┌─────────────────────────────────────┐ │
│ │ Active Transcodes (text-base, bold) │ │ card
│ │ gap-2 (inner)                       │ │
│ └─────────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

**Heading Spacing**:
- **Page Title (H1)**: text-3xl font-bold, mb-2 (8px below)
- **Subtitle below title**: text-sm text-base-content/70, mb-8 (32px below)
- **Card Title (H3)**: text-base font-bold, implicit gap in card layout
- **Section Title (H2)**: text-xl font-bold, mb-4 (16px below)

---

## Button Spacing

### Standalone Buttons

```
┌─────────────────────────────────────────┐
│ [Button] gap-2 [Button] gap-2 [Button]  │ 8px horizontal gap
└─────────────────────────────────────────┘
```

- **Button Size sm**: px-4 py-2 (text 14px)
- **Button Size md**: px-4 py-3 (text 14px)
- **Gap between buttons**: gap-2 (8px) or gap-4 (16px)

### Button Groups

```
┌────────────────────┐  ┌────────────────────┐
│  Primary Action    │  │  Secondary Action  │
└────────────────────┘  └────────────────────┘
     gap-4 (16px)
```

---

## Form Spacing

```
┌─────────────────────────────────────────┐
│ Label                                   │  text-sm font-semibold
│ mb-2                                    │  8px gap
│ ┌───────────────────────────────────────┐│
│ │ [Input Field]                         ││  h-10 (40px)
│ └───────────────────────────────────────┘│
│ mb-4                                    │  16px gap
│ Help text (text-xs)                     │  text-base-content/60
│ mb-6                                    │  24px gap to next field
│ Label                                   │
└─────────────────────────────────────────┘
```

- **Field vertical spacing**: mb-6 (24px)
- **Label to input**: mb-2 (8px)
- **Input height**: h-10 (40px)
- **Input padding**: px-4 py-2
- **Helper text**: text-xs, mb-2, text-base-content/60

---

## Modal/Overlay Spacing

```
┌──────────────────────────────────────────┐
│ ┌─ Modal Box ──────────────────────────┐ │
│ │ Title (text-lg, font-bold)           │ │  p-6
│ │ py-4 (24px top/bottom)               │ │
│ │ Modal content...                     │ │
│ │                                      │ │
│ │ py-4 (24px top/bottom)               │ │
│ │ ┌──────────────────────────────────┐ │ │
│ │ │ [Cancel]  gap-4  [Confirm]       │ │ │  modal-action
│ │ └──────────────────────────────────┘ │ │
│ └──────────────────────────────────────┘ │
└──────────────────────────────────────────┘
```

- **Modal Box Padding**: 24px (p-6)
- **Title Bottom Spacing**: 16px (py-4)
- **Content Padding**: 16px top/bottom (py-4)
- **Action Gap**: 16px (gap-4)
- **Button Group**: flex justify-end

---

## Responsive Breakpoints

Tailwind CSS breakpoints (used throughout):

| Prefix | Min Width | Usage |
|--------|-----------|-------|
| (none) | 0px | Mobile first (base styles) |
| sm | 640px | Small phones |
| md | 768px | Tablets |
| lg | 1024px | Desktops (sidebar appears) |
| xl | 1280px | Large desktops |
| 2xl | 1536px | Ultra-wide (rarely used) |

**Example Responsive Patterns**:
```html
<!-- Grid scales at breakpoints -->
<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4">

<!-- Hide on mobile -->
<div class="hidden lg:block">Sidebar</div>

<!-- Show only on mobile -->
<div class="lg:hidden">Mobile Menu</div>

<!-- Padding changes -->
<main class="p-4 lg:p-6">

<!-- Font size scaling -->
<h1 class="text-2xl lg:text-3xl">
```

---

## Animation & Transition Spacing

### HTMX Swap Animations

```css
/* From app.css */
.htmx-settling {
    opacity: 0;
    /* immediate (no transition) */
}
.htmx-added {
    transition: opacity 0.3s ease-in;
    opacity: 1;
}
```

- **Fade-in duration**: 300ms (0.3s)
- **Easing**: ease-in

### Pulsing Animation

```css
@keyframes pulse-green {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}
.pulse-green {
    animation: pulse-green 2s ease-in-out infinite;
}
```

- **Duration**: 2 seconds
- **Opacity range**: 1.0 → 0.4 → 1.0
- **Easing**: ease-in-out

### Standard Transitions

```html
<div class="transition-colors duration-200 hover:bg-base-200">
```

- **transition-colors**: Smooth color changes
- **duration-200**: 200ms transition (0.2s)
- **duration-300**: 300ms transition (0.3s) for longer animations

---

## Summary Table: Common Spacing Patterns

| Pattern | Spacing | Usage |
|---------|---------|-------|
| Page padding | 24px (p-6) desktop, 16px (p-4) mobile | Main content area |
| Sidebar width | 256px (w-64) | Sidebar |
| Card padding | 16px (p-4) | Card body |
| Section gap | 24px (gap-6) | Between major sections |
| Component gap | 16px (gap-4) | Between components |
| Tight gap | 8px (gap-2) | Buttons, badge rows |
| Row height | 48-52px | Tables, lists |
| Column padding | 16px (px-4) | Table cells |
| Header height | 56px | Navbar, top bar |
| Border radius | 8px (rounded-lg) | Cards, buttons, inputs |
| Shadow | shadow-sm | Subtle depth, hover shadow-md |

---

**Last Updated**: 2026-03-23
**Used by**: All Transcode Forge UI templates
