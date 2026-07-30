"""
IDX Class Documentation
==========================

**Class Description**
--------------------

The `IDX` class is a provider for retrieving stock data from the IDX (Indonesian
Stock Exchange) website. It drives Camoufox — a stealth, anti-fingerprinting
Firefox fork on top of Playwright — to load the stock-list page (which sits
behind Cloudflare) and extract the table.

**Class Methods**
----------------

### `__init__`

*   Configures the provider: base URL, whether to retrieve all stocks or just a
    small sample, and headless mode.

### `stocks`

*   Retrieves a list of stock data from the IDX website.
*   Returns a list of `Stock` objects, each containing:
    + `ticker`: The stock ticker symbol.
    + `name`: The stock name.
    + `ipo_date`: The initial public offering date.
    + `market_cap`: The market capitalization (float).
    + `note`: The stock note.

**Notes**
------

*   `stocks` launches Camoufox, navigates to the IDX stock-list page, waits for
    the table to render (detecting Cloudflare challenges), optionally expands the
    page size to load every stock, and reads the rows in a single DOM pass.
"""

import os
import re

from camoufox.sync_api import Camoufox
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from schemas.stock import Stock
from utils.logger_config import logger

_TABLE_SELECTOR = "#vgt-table"
_PER_PAGE_SELECT = "select[name='perPageSelect']"
_NEXT_PAGE_BUTTON = "button.footer__navigation__page-btn:nth-child(4)"

# Single DOM pass that reads every row of the stock table into a list of
# objects. The table has 5 data columns (Kode, Nama, Tanggal Pencatatan, Saham,
# Papan Pencatatan) but vue-good-table sometimes prepends an empty line-number
# column, which intermittently shifts fixed nth-child positions by one. The data
# columns are always the LAST 5 cells, so we slice from the end to stay aligned
# regardless of whether the leading column is present. Rows without a ticker are
# dropped as malformed.
_EXTRACT_ROWS_JS = """
() => {
  const rows = Array.from(document.querySelectorAll('#vgt-table tbody tr'));
  return rows.map(r => {
    const tds = Array.from(r.querySelectorAll('td')).map(td => td.textContent.trim());
    const data = tds.slice(-5);
    return {
      ticker: data[0] || '',
      name: data[1] || '',
      ipo_date: data[2] || '',
      market_cap: data[3] || '',
      note: data[4] || '',
    };
  }).filter(row => row.ticker);
}
"""


def _resolve_headless():
    """
    Resolve the Camoufox headless mode from the IDX_HEADLESS env var.

    * unset / "true" / "1"    -> True (headless)
    * "virtual"               -> "virtual" (Xvfb virtual display; good on Linux
                                  servers where a real display is absent but a
                                  headless-detectable browser gets blocked)
    * "false" / "0" / "no"    -> False (headed, shows a window)
    """
    val = os.environ.get("IDX_HEADLESS", "true").strip().lower()
    if val == "virtual":
        return "virtual"
    return val not in ("0", "false", "no", "off")


class IDX:
    """
    IDX Provider Class
    """

    def __init__(self, is_full_retrieve=True, is_second_page=False):
        """
        Initializes the IDX provider and sets the base URL for the IDX website.
        """
        logger.info("IDX provider initialised")
        self.base_url = "https://idx.co.id"
        self.is_full_retrieve = is_full_retrieve
        self.is_second_page = is_second_page
        self.timeout_ms = 15000

    def _wait_for_table(self, page, url: str) -> None:
        """Wait for the stock table, raising a clear error on a Cloudflare wall."""
        try:
            page.wait_for_selector(_TABLE_SELECTOR, timeout=self.timeout_ms)
        except PlaywrightTimeoutError as exc:
            page_source = page.content().lower()
            if "cloudflare" in page_source and (
                "just a moment" in page_source or "checking your browser" in page_source
            ):
                logger.error(
                    "Blocked by Cloudflare while loading stock list page at %s", url
                )
                raise RuntimeError(
                    "Cloudflare protection blocked automated access to IDX stock list page."
                ) from exc
            raise

    def stocks(self) -> [Stock]:
        """
        Retrieves a list of stock data from the IDX website.

        Returns:
            [Stock]: list of Stock object containing parsed stock data.
        """
        url = f"{self.base_url}/id/data-pasar/data-saham/daftar-saham/"

        logger.info(
            "Launching Camoufox to load the IDX stock list"
        )

        with Camoufox(
            headless=_resolve_headless(),
            humanize=True,
            geoip=True,
        ) as browser:
            page = browser.new_page()

            page.goto(url, wait_until="domcontentloaded")

            # Wait for initial table or detect a Cloudflare challenge.
            self._wait_for_table(page, url)

            # If true retrieve all stocks, otherwise the default first page (~10).
            if self.is_full_retrieve:
                page.wait_for_selector(_PER_PAGE_SELECT, timeout=self.timeout_ms)
                # value "-1" is the "All" option in the rows-per-page dropdown.
                page.select_option(_PER_PAGE_SELECT, "-1")
                self._wait_for_full_table(page, url)

            if self.is_second_page:
                # Page forward twice, matching the previous behaviour.
                for _ in range(2):
                    page.wait_for_selector(_PER_PAGE_SELECT, timeout=self.timeout_ms)
                    page.click(_NEXT_PAGE_BUTTON)
                    self._wait_for_table(page, url)

            # Final settle in case the table is still re-rendering.
            self._wait_for_table(page, url)

            rows = page.evaluate(_EXTRACT_ROWS_JS)

        logger.info(
            "Load IDX page..."
        )
        
        stocks = []
        for row in rows:
            digits = re.sub(r"\D", "", row.get("market_cap", ""))
            stocks.append(
                Stock(
                    ticker=row.get("ticker", ""),
                    name=row.get("name", ""),
                    ipo_date=row.get("ipo_date", ""),
                    market_cap=float(digits) if digits else 0.0,
                    note=row.get("note", ""),
                )
            )

        logger.info(f"Stocks has been retrieved from {url}")
        return stocks

    def _wait_for_full_table(self, page, url: str) -> None:
        """
        After expanding the page size to "All", wait for the table to finish
        re-rendering the full set of rows.
        """
        try:
            page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        except PlaywrightTimeoutError:
            # Pure client-side re-renders may never go network-idle; fall through.
            pass
        self._wait_for_table(page, url)
        # Small settle for the row list to stabilise after the size change.
        page.wait_for_timeout(1500)
