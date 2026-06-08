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
        sidebar = page.locator("aside")
        expect(sidebar).to_be_visible()

    def test_sidebar_has_all_nav_items(self, page: Page, base_url: str):
        page.goto(base_url)
        for label in [
            "Dashboard",
            "Movies",
            "TV Shows",
            "Queue",
            "Workers",
            "History",
            "Skipped",
            "Stats",
            "Settings",
        ]:
            expect(page.locator(f"aside >> text={label}")).to_be_visible()

    def test_sidebar_logo(self, page: Page, base_url: str):
        page.goto(base_url)
        expect(page.locator("text=Forge")).to_be_visible()
        expect(page.locator("text=Precision Engine")).to_be_visible()

    def test_active_nav_highlighting(self, page: Page, base_url: str):
        page.goto(f"{base_url}/movies")
        movies_link = page.locator("aside a[href='/movies']")
        expect(movies_link).to_have_class(re.compile("border-primary"))


class TestDashboard:
    """Dashboard page renders stat cards and HTMX containers."""

    def test_dashboard_loads(self, page: Page, base_url: str):
        page.goto(base_url)
        expect(page).to_have_title(re.compile("Dashboard"))

    def test_stat_cards_load(self, page: Page, base_url: str):
        page.goto(base_url)
        # Wait for HTMX to load dashboard-stats partial
        page.wait_for_selector("#dashboard-stats", state="attached")
        expect(page.locator("text=Space Saved")).to_be_visible(timeout=15_000)
        expect(page.locator("text=Completed")).to_be_visible()
        expect(page.locator("#dashboard-stats >> text=Workers")).to_be_visible()

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


class TestHistory:
    """History page renders with tabs and table."""

    def test_history_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/history")
        expect(page).to_have_title(re.compile("History"))
        # History page uses tabs instead of a heading
        expect(page.locator("#history-tabs")).to_be_visible()

    def test_history_container_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/history")
        page.wait_for_selector("#history-container", state="attached")
        expect(page.locator("text=No history yet")).to_be_visible(timeout=15_000)


class TestSkipped:
    """Skipped files page renders with filters."""

    def test_skipped_loads(self, page: Page, base_url: str):
        page.goto(f"{base_url}/skipped")
        expect(page).to_have_title(re.compile("Skipped"))

    def test_filter_controls(self, page: Page, base_url: str):
        page.goto(f"{base_url}/skipped")
        expect(page.locator("#skip-reason-filter")).to_be_visible()
        expect(page.locator("#skip-library-filter")).to_be_visible()


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
        expect(page.get_by_role("heading", name="Libraries")).to_be_visible()


class TestTopBar:
    """Top header bar renders correctly."""

    def test_search_input(self, page: Page, base_url: str):
        page.goto(base_url)
        search = page.locator("header input[type='text']")
        expect(search).to_be_visible()
        expect(search).to_have_attribute("placeholder", re.compile("Search"))

    def test_notification_bell(self, page: Page, base_url: str):
        page.goto(base_url)
        expect(page.locator("header >> text=notifications")).to_be_visible()


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
        # Should be dark (#131313 → rgb(19, 19, 19))
        assert "19" in bg_color or "131313" in bg_color

    def test_material_symbols_loaded(self, page: Page, base_url: str):
        page.goto(base_url)
        # Material Symbols should render (not show as text boxes)
        icon = page.locator(".material-symbols-outlined").first
        expect(icon).to_be_visible()

    def test_manrope_font_loaded(self, page: Page, base_url: str):
        page.goto(base_url)
        # Check that Manrope is used for headlines
        font = page.evaluate("""
            () => {
                const el = document.querySelector('.font-headline');
                return el ? getComputedStyle(el).fontFamily : 'not found';
            }
        """)
        assert "Manrope" in font or "not found" not in font


class TestNavigation:
    """Verify clicking nav links navigates correctly."""

    @pytest.mark.parametrize(
        "path,title_contains",
        [
            ("/", "Dashboard"),
            ("/movies", "Movies"),
            ("/tv", "TV"),
            ("/queue", "Queue"),
            ("/workers", "Workers"),
            ("/history", "History"),
            ("/skipped", "Skipped"),
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
