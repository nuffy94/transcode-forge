/* Header clock — 24h local time, monospaced, ticks every second. */

export function startClock() {
    const el = document.getElementById('forge-clock');
    if (!el) return;
    const pad = (n) => String(n).padStart(2, '0');
    const tick = () => {
        const d = new Date();
        el.textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    };
    tick();
    setInterval(tick, 1000);
}
