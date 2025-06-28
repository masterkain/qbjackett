# jackett.py

# VERSION: 1.0
# AUTHORS: Diego de las Heras (ngosang@hotmail.es)
# CONTRIBUTORS: ukharley, hannsen, Alexander Georgievskiy, qb-rewrite[bot], Kain

import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from http.cookiejar import CookieJar
from multiprocessing.dummy import Pool
from threading import Lock
from urllib.parse import unquote, urlencode

# qBittorrent-specific imports
import helpers
from novaprinter import prettyPrinter

# --- Configuration Section ---
# This part is run at the module level when qBittorrent loads the plugin.

CONFIG_FILE = "jackett.json"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), CONFIG_FILE)
CONFIG_DEFAULTS = {
    "api_key": "YOUR_API_KEY_HERE",
    "url": "http://127.0.0.1:9117",
    "tracker_first": False,
    "thread_count": 20,
}
# Using a global lock is standard practice for plugins to avoid race conditions with prettyPrinter
PRINTER_LOCK = Lock()


def load_configuration():
    """Load configuration from JSON file or create it with defaults."""
    config = CONFIG_DEFAULTS.copy()
    try:
        with open(CONFIG_PATH, "r") as f:
            user_config = json.load(f)
            config.update(user_config)
    except json.JSONDecodeError:
        config["malformed"] = True
    except FileNotFoundError:
        # Create the file if it doesn't exist
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=4, sort_keys=True)

    # Validate essential keys and add missing ones
    if not all(key in config for key in ["api_key", "url", "tracker_first", "thread_count"]):
        config["malformed"] = True
        # Save back to add any missing keys from defaults
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=4, sort_keys=True)

    return config


# Load config globally so it's available for the class definition
CONFIG_DATA = load_configuration()


# --- Proxy Management ---


class ProxyManager:
    """Manages enabling/disabling system-wide proxy settings for requests."""

    def __init__(self):
        self.http_proxy = os.getenv("http_proxy", "")
        self.https_proxy = os.getenv("https_proxy", "")

    def enable(self, is_enabled: bool):
        """Toggles the proxy settings on or off."""
        if is_enabled:
            os.environ["http_proxy"] = self.http_proxy
            os.environ["https_proxy"] = self.https_proxy
        else:
            os.environ.pop("http_proxy", None)
            os.environ.pop("https_proxy", None)

        # Handle SOCKS proxy for qBittorrent compatibility
        try:
            helpers.enable_socks_proxy(is_enabled)
        except AttributeError:
            # Older qBittorrent versions may not have this helper
            pass


# Initialize the proxy manager at the module level, disabled by default
proxy_manager = ProxyManager()
proxy_manager.enable(False)


# --- Main Plugin Class ---
# The class MUST be named 'jackett' (lowercase) to be recognized by qBittorrent.


class jackett(object):
    """
    qBittorrent search plugin for Jackett.
    This class is instantiated by qBittorrent and its methods are called directly.
    """

    # These are class-level attributes that qBittorrent inspects.
    url = CONFIG_DATA["url"].rstrip("/")
    name = "Jackett"
    supported_categories = {
        "all": None,
        "anime": ["5070"],
        "books": ["8000"],
        "games": ["1000", "4000"],
        "movies": ["2000"],
        "music": ["3000"],
        "software": ["4000"],
        "tv": ["5000"],
    }

    def __init__(self):
        """
        The __init__ method is called when the plugin is loaded.
        We can load config values into instance attributes for cleaner access.
        """
        self.api_key = CONFIG_DATA["api_key"]
        self.tracker_first = CONFIG_DATA.get("tracker_first", False)
        self.thread_count = CONFIG_DATA.get("thread_count", 20)
        self.is_malformed = CONFIG_DATA.get("malformed", False)

    def download_torrent(self, download_url):
        """
        This method is called by qBittorrent to download a .torrent file or get a magnet link.
        It is a required method for the plugin to function correctly.
        """
        # Some indexers return a magnet link directly in the .torrent file response
        if download_url.startswith("magnet:"):
            self._safe_print_link(download_url, download_url)
            return

        proxy_manager.enable(True)
        response_content = self._fetch_url(download_url)
        proxy_manager.enable(False)

        if response_content and response_content.startswith("magnet:"):
            self._safe_print_link(response_content, download_url)
        else:
            # If not a magnet, pass to the helper to download the file
            print(helpers.download_file(download_url))

    def search(self, what, cat="all"):
        """
        The main search method called by qBittorrent.
        """
        search_query = unquote(what)
        category_ids = self.supported_categories.get(cat.lower())

        if self.is_malformed:
            self._handle_error("malformed configuration file", search_query)
            return

        if self.api_key == "YOUR_API_KEY_HERE":
            self._handle_error("API key is not configured", search_query)
            return

        indexers = self._get_configured_indexers(search_query)
        if not indexers:
            # Error is handled inside _get_configured_indexers
            return

        # Prepare arguments for each thread
        search_args = [(search_query, category_ids, indexer_id) for indexer_id in indexers]

        # Use a thread pool to search across all indexers concurrently
        if self.thread_count > 1 and len(search_args) > 1:
            with Pool(min(len(search_args), self.thread_count)) as pool:
                pool.starmap(self._search_indexer, search_args)
        else:
            # Fallback to sequential search for a single indexer or if multithreading is disabled
            self._search_indexer(search_query, category_ids, "all")

    def _get_configured_indexers(self, context_query):
        """Fetches the list of configured indexer IDs from the Jackett API."""
        params = urlencode({"apikey": self.api_key, "t": "indexers", "configured": "true"})
        api_url = f"{self.url}/api/v2.0/indexers/all/results/torznab/api?{params}"

        xml_data = self._fetch_url(api_url)
        if not xml_data:
            self._handle_error("could not connect to Jackett to get indexer list", context_query)
            return []
        try:
            root = ET.fromstring(xml_data)
            return [indexer.attrib["id"] for indexer in root.findall("indexer")]
        except ET.ParseError:
            self._handle_error("failed to parse Jackett indexer list (invalid XML)", context_query)
            return []

    def _search_indexer(self, query, category_ids, indexer_id):
        """Performs a search on a specific Jackett indexer (or 'all')."""
        params = [("apikey", self.api_key), ("q", query)]
        if category_ids:
            params.append(("cat", ",".join(category_ids)))

        api_url = f"{self.url}/api/v2.0/indexers/{indexer_id}/results/torznab/api?{urlencode(params)}"

        xml_data = self._fetch_url(api_url)
        if not xml_data:
            # We don't show a global error here, as one indexer failing is common
            print(f"DEBUG: No response from indexer '{indexer_id}' for query '{query}'")
            return
        try:
            root = ET.fromstring(xml_data)
            channel = root.find("channel")
            if channel is None:
                return  # No results or malformed XML, quietly ignore
            for item in channel.findall("item"):
                self._parse_and_print_item(item)
        except ET.ParseError:
            print(f"DEBUG: XML parse error for indexer '{indexer_id}'")

    def _parse_and_print_item(self, item):
        """Parses an <item> element from the XML feed and prints it."""
        try:
            title = item.findtext("title")
            if not title:
                return

            tracker = item.findtext("jackettindexer")
            name = f"[{tracker}] {title}" if self.tracker_first else f"{title} [{tracker}]"

            # Torznab spec for magnet link
            torznab_ns = "{http://torznab.com/schemas/2015/feed}"
            magnet_el = item.find(f'./{torznab_ns}attr[@name="magneturl"]')
            link = magnet_el.get("value") if magnet_el is not None else item.findtext("link")
            if not link:
                return

            size = item.findtext("size", default="-1") + " B"
            seeds_el = item.find(f'./{torznab_ns}attr[@name="seeders"]')
            peers_el = item.find(f'./{torznab_ns}attr[@name="peers"]')
            seeds = int(seeds_el.get("value")) if seeds_el is not None else -1
            peers = int(peers_el.get("value")) if peers_el is not None else -1
            leech = (peers - seeds) if (seeds != -1 and peers != -1) else -1

            pub_date_str = item.findtext("pubDate")
            pub_date = -1
            if pub_date_str:
                try:
                    # Format: Wed, 21 Dec 2022 10:33:04 +0000
                    dt_object = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %z")
                    pub_date = int(dt_object.timestamp())
                except ValueError:
                    pass  # Ignore if date format is unexpected

            result = {
                "name": name.replace("|", "%7C"),  # Escape pipe character
                "link": link,
                "size": size,
                "seeds": seeds,
                "leech": leech,
                "engine_url": self.url,
                "desc_link": item.findtext("comments") or item.findtext("guid", ""),
                "pub_date": pub_date,
            }
            self._safe_print(result)
        except Exception as e:
            # This can catch unexpected parsing errors for a single item
            print(f"DEBUG: Error parsing item: {e}")

    def _fetch_url(self, url):
        """Generic URL fetching method with cookie and error handling."""
        try:
            # Using a cookie jar is important for sites that use redirects
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            response = opener.open(url, timeout=20)
            return response.read().decode("utf-8", "ignore")
        except urllib.request.HTTPError as e:
            # Handle redirects which are reported as HTTPError in urllib
            if e.code == 302:
                return e.url
            return None
        except Exception:
            return None

    def _handle_error(self, error_msg, search_query):
        """Formats and prints a standard error message."""
        error_result = {
            "seeds": -1,
            "size": -1,
            "leech": -1,
            "engine_url": self.url,
            "link": self.url,
            "desc_link": "https://github.com/qbittorrent/search-plugins/wiki/How-to-configure-Jackett-plugin",
            "name": f"Jackett: {error_msg}! Right-click and open description. Conf: '{CONFIG_PATH}'. Search: '{search_query}'",
        }
        self._safe_print(error_result)

    def _safe_print_link(self, magnet, torrent_url):
        """
        Specialized thread-safe printer for download_torrent to output a magnet link.
        """
        with PRINTER_LOCK:
            # The format is "magnet_link url_of_torrent_file"
            print(f"{magnet} {torrent_url}")

    def _safe_print(self, data):
        """Thread-safe wrapper for prettyPrinter."""
        with PRINTER_LOCK:
            prettyPrinter(data)


# This part is for direct execution of the script for testing purposes.
if __name__ == "__main__":
    engine = jackett()
    print(f"Testing Jackett plugin. URL: {engine.url}, API Key set: {engine.api_key != 'YOUR_API_KEY_HERE'}")
    # You can change the search query and category for your tests
    engine.search("ubuntu server", "software")
    # To test a download, you would need a valid URL from a search result
    # engine.download_torrent("magnet:?xt=urn:btih:...")
