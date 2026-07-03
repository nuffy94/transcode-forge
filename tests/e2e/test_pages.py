"""E2E tests — verify every page loads and key UI elements render in a real browser."""

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.fixture(autouse=True)
def _navigate_base(page: Page, base_url: str) -> None:
    """Set default timeout for all E2E tests."""
    page.set_default_timeout(10_000)


class TestSidebar:
    """Sidebar navigation renders correctly on every page."""

    def test_sidebar_renders(self, page: Page, base_url: str):
        page.goto(base_url)
        # aside.forge-sidebar specifically — the file-detail drawer is a
        # second <aside>, so a bare tag locator is ambiguous.
        expect(page.locator("aside.forge-sidebar")).to_be_visible()

    def test_sidebar_has_all_nav_items(self, page: Page, base_url: str):
        page.goto(base_url)
        for label in [
            "Dashboard",
            "Movies",
            "TV Shows",
            "Queue",
            "Activity",
            "Workers",
            "Stats",
            "Settings",
        ]:
            expect(page.locator(".forge-navlink-label", has_text=label)).to_be_visible()

    def test_sidebar_logo(self, page: Page, base_url: str):
        """The v2 brand lockup: mono eyebrow over the Big Shoulders wordmark."""
        page.goto(base_url)
        expect(page.locator(".forge-brand-word")).to_have_text("FORGE")
        expect(page.locator(".forge-brand-eyebrow")).to_have_text("Transcode")

    def test_active_nav_highlighting(self, page: Page, base_url: str):
        page.goto(f"{base_url}/movies")
        movies_link = page.locator("aside a[href='/movies']")
        expect(movies_link).to_have_class(re.compile("is-active"))

    def test_nav_uses_sprite_icons(self, page: Page, base_url: str):
        """Shell nav renders inline SVG sprite icons (not the Material font)."""
        page.goto(base_url)
        icons = page.locator("aside .forge-navlink svg.forge-icon")
        assert icons.count() >= 9, "every nav row should carry a sprite icon"
        expect(page.locator("aside .material-symbols-outlined")).to_have_count(0)

    def test_nav_has_no_numbered_markers(self, page: Page, base_url: str):
        """v2 killed the 01/02 ledger numbers — nav order carries no meaning."""
        page.goto(base_url)
        expect(page.locator(".forge-navlink-num")).to_have_count(0)


class TestDashboard:
    """Dashboard page renders stat cards and HTMX containers."""

    def test_dashboard_loads(self, page: Page, base_url: str):
        page.goto(base_url)
        expect(page).to_have_title(re.compile("Dashboard"))

    def test_stat_cards_load(self, page: Page, base_url: str):
        page.goto(base_url)
        # Wait for HTMX to load dashboard-stats partial
        page.wait_for_selector("#dashboard-stats", state="attached")
        stats = page.locator("#dashboard-stats")
        expect(stats.get_by_text("Space Reclaimed")).to_be_visible(timeout=15_000)
        expect(stats.get_by_text("Completed")).to_be_visible()
        expect(stats.get_by_text("Workers")).to_be_visible()

    def test_active_transcodes_section(self, page: Page, base_url: str):
        page.goto(base_url)
        expect(page.get_by_role("heading", name="Active Transcodes")).to_be_visible()

    def test_pause_button_exists(self, page: Page, base_url: str):
        page.goto(base_url)
        expect(page.locator("#pause-btn")).to_be_visible()

    def test_recent_activity_section(self, page: Page, base_url: str):
        page.goto(base_url)
        expect(page.get_by_role("heading", name="Recent Activity")).to_be_visible()


class TestMovies:
    """Movies library page renders with filters and table."""

    def test_movies_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/movies")
        expect(page).to_have_title(re.compile("Movies"))

    def test_filter_controls_exist(self, page: Page, base_url: str):
        page.goto(f"{base_url}/movies")
        expect(page.locator("#mv-status")).to_be_visible()
        expect(page.locator("#mv-codec")).to_be_visible()

    def test_view_toggle(self, page: Page, base_url: str):
        page.goto(f"{base_url}/movies")
        expect(page.locator("#view-table")).to_be_visible()
        expect(page.locator("#view-grid")).to_be_visible()

    def test_search_input(self, page: Page, base_url: str):
        page.goto(f"{base_url}/movies")
        expect(page.locator("#mv-search")).to_be_visible()


class TestTV:
    """TV Shows page renders with show/episode views."""

    def test_tv_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/tv")
        expect(page).to_have_title(re.compile("TV"))

    def test_view_tabs_exist(self, page: Page, base_url: str):
        page.goto(f"{base_url}/tv")
        expect(page.locator("#tab-shows")).to_be_visible()
        expect(page.locator("#tab-files")).to_be_visible()


class TestQueue:
    """Queue page renders with controls and job table."""

    def test_queue_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/queue")
        expect(page).to_have_title(re.compile("Queue"))
        expect(page.locator("text=Job Queue")).to_be_visible()

    def test_filter_controls(self, page: Page, base_url: str):
        page.goto(f"{base_url}/queue")
        expect(page.locator("#status-filter")).to_be_visible()
        expect(page.locator("#library-filter")).to_be_visible()

    def test_scan_controls(self, page: Page, base_url: str):
        page.goto(f"{base_url}/queue")
        expect(page.locator("#scan-library")).to_be_visible()
        expect(page.locator("#scan-btn")).to_be_visible()

    def test_pause_button(self, page: Page, base_url: str):
        page.goto(f"{base_url}/queue")
        expect(page.locator("#pause-btn")).to_be_visible()

    def test_empty_queue_message(self, page: Page, base_url: str):
        page.goto(f"{base_url}/queue")
        # HTMX loads jobs partial — wait for it
        page.wait_for_selector("#job-table-container", state="attached")
        expect(page.locator("text=No jobs in queue")).to_be_visible(timeout=10_000)


class TestWorkers:
    """Workers page renders with HTMX containers."""

    def test_workers_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/workers")
        expect(page).to_have_title(re.compile("Workers"))

    def test_workers_container_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/workers")
        page.wait_for_selector("#workers-container", state="attached")
        # Should show "No workers registered" since test env has no workers
        expect(page.locator("text=No workers registered")).to_be_visible(timeout=15_000)


class TestActivity:
    """Activity — the merged History+Skipped ledger with two facets."""

    def test_activity_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/activity")
        expect(page).to_have_title(re.compile("Activity"))
        expect(page.locator("#activity-tabs")).to_be_visible()

    def test_outcomes_container_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/activity")
        page.wait_for_selector("#outcomes-container", state="attached")
        expect(page.locator("text=No outcomes yet")).to_be_visible(timeout=15_000)

    def test_facet_switch_shows_skip_filters(self, page: Page, base_url: str):
        page.goto(f"{base_url}/activity")
        page.click("#tab-skips")
        expect(page.locator("#skip-reason-filter")).to_be_visible()
        expect(page.locator("#skip-library-filter")).to_be_visible()
        expect(page.locator("#outcomes-view")).to_be_hidden()

    def test_skips_deep_link(self, page: Page, base_url: str):
        page.goto(f"{base_url}/activity?view=skips")
        expect(page.locator("#skips-view")).to_be_visible()
        expect(page.locator("#outcomes-view")).to_be_hidden()

    def test_old_routes_redirect(self, page: Page, base_url: str):
        page.goto(f"{base_url}/history")
        expect(page).to_have_url(re.compile("/activity"))
        page.goto(f"{base_url}/skipped")
        expect(page).to_have_url(re.compile(r"/activity\?view=skips"))


class TestStats:
    """Stats page renders with HTMX stats container."""

    def test_stats_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/stats")
        expect(page).to_have_title(re.compile("Statistic"))

    def test_stats_container_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/stats")
        page.wait_for_selector("#stats-container", state="attached")
        expect(page.locator("text=Space Saved")).to_be_visible(timeout=15_000)


class TestSettings:
    """Settings page renders with configuration sections."""

    def test_settings_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/settings")
        expect(page).to_have_title(re.compile("Settings"))

    def test_settings_sections(self, page: Page, base_url: str):
        page.goto(f"{base_url}/settings")
        expect(page.locator("#tab-libraries")).to_be_visible()


class TestTopBar:
    """Top header bar renders the live status strip (no search box / bell)."""

    def test_status_strip(self, page: Page, base_url: str):
        page.goto(base_url)
        expect(page.locator("header >> text=Online")).to_be_visible()
        expect(page.locator("#forge-clock")).to_be_attached()

    def test_section_crumb(self, page: Page, base_url: str):
        """The header carries the active section as a stamped crumb."""
        page.goto(f"{base_url}/queue")
        expect(page.locator(".forge-crumb")).to_contain_text("Queue")


class TestHTMXPolling:
    """Verify HTMX partial loading works (containers populate after load)."""

    def test_dashboard_stats_poll(self, page: Page, base_url: str):
        page.goto(base_url)
        # dashboard-stats has hx-trigger="load, every 10s"
        # Should populate within a few seconds
        stats_container = page.locator("#dashboard-stats")
        expect(stats_container).not_to_be_empty(timeout=15_000)

    def test_health_indicator_poll(self, page: Page, base_url: str):
        page.goto(base_url)
        # health-indicator has hx-trigger="load, every 30s"
        health = page.locator("#health-indicator")
        expect(health).not_to_be_empty(timeout=15_000)


class TestDesignSystem:
    """Verify the Industrial Foundry design system is applied."""

    def test_dark_background(self, page: Page, base_url: str):
        page.goto(base_url)
        bg_color = page.evaluate("getComputedStyle(document.body).backgroundColor")
        # Forge warm graphite #0d0b08 → rgb(13, 11, 8)
        assert "13, 11, 8" in bg_color or "0d0b08" in bg_color

    def test_heat_seam_present(self, page: Page, base_url: str):
        """The v2 signature — the molten seam along the top edge."""
        page.goto(base_url)
        expect(page.locator(".forge-seam")).to_be_attached()

    def test_sprite_icons_render(self, page: Page, base_url: str):
        """Rebuilt pages use the inline SVG sprite. (Replaced the old
        Material-Symbols assertion in Step 4 — the dashboard is de-iconed;
        the font survives only on not-yet-rebuilt pages until Step 7.)"""
        page.goto(base_url)
        assert page.locator("svg.forge-icon").count() >= 10
        expect(page.locator("#dashboard-stats .material-symbols-outlined")).to_have_count(0)

    def test_display_font_loaded(self, page: Page, base_url: str):
        page.goto(base_url)
        # The FORGE wordmark uses Big Shoulders Display (font-display).
        font = page.evaluate("""
            () => {
                const el = document.querySelector('.font-display');
                return el ? getComputedStyle(el).fontFamily : 'not found';
            }
        """)
        assert "Big Shoulders" in font


class TestNavigation:
    """Verify clicking nav links navigates correctly."""

    @pytest.mark.parametrize(
        "path,title_contains",
        [
            ("/", "Dashboard"),
            ("/movies", "Movies"),
            ("/tv", "TV"),
            ("/queue", "Queue"),
            ("/activity", "Activity"),
            ("/workers", "Workers"),
            ("/stats", "Statistic"),
            ("/settings", "Settings"),
        ],
    )
    def test_page_navigation(self, page: Page, base_url: str, path: str, title_contains: str):
        page.goto(f"{base_url}{path}")
        expect(page).to_have_title(re.compile(title_contains))
        # No console errors
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.wait_for_load_state("networkidle")
        # Allow font loading errors (Google Fonts CDN in test env)
        real_errors = [e for e in errors if "font" not in e.lower()]
        assert len(real_errors) == 0, f"Console errors on {path}: {real_errors}"
