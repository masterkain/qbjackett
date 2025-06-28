# jackett.py

# VERSION: 2.0
# AUTHORS: Diego de las Heras (ngosang@hotmail.es)
# CONTRIBUTORS: ukharley, hannsen, Alexander Georgievskiy, qb-rewrite[bot], Kain

import json
import os
import re
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
CONFIG_FILE = "jackett.json"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), CONFIG_FILE)
CONFIG_DEFAULTS = {
    "api_key": "YOUR_API_KEY_HERE",
    "url": "http://127.0.0.1:9117",
    "tracker_first": False,
    "thread_count": 20,
    "deduplicate": True,  # New: Enable or disable result deduplication
}
PRINTER_LOCK = Lock()


def load_configuration():
    """Load configuration from JSON file or create it with defaults."""
    config = CONFIG_DEFAULTS.copy()
    # Ensure config file exists before trying to read
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=4, sort_keys=True)

    try:
        with open(CONFIG_PATH, "r") as f:
            user_config = json.load(f)
            # Add any new default keys to user config
            updated = False
            for key, value in CONFIG_DEFAULTS.items():
                if key not in user_config:
                    user_config[key] = value
                    updated = True
            config.update(user_config)
            if updated:
                with open(CONFIG_PATH, "w") as f:
                    json.dump(config, f, indent=4, sort_keys=True)
    except (json.JSONDecodeError, TypeError):
        config["malformed"] = True

    if not all(key in config for key in ["api_key", "url", "tracker_first", "thread_count"]):
        config["malformed"] = True

    return config


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
        try:
            helpers.enable_socks_proxy(is_enabled)
        except AttributeError:
            pass


proxy_manager = ProxyManager()
proxy_manager.enable(False)


# --- Main Plugin Class ---
class jackett(object):
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
        self.api_key = CONFIG_DATA["api_key"]
        self.tracker_first = CONFIG_DATA.get("tracker_first", False)
        self.thread_count = CONFIG_DATA.get("thread_count", 20)
        self.deduplicate = CONFIG_DATA.get("deduplicate", True)
        self.is_malformed = CONFIG_DATA.get("malformed", False)
        self.info_hash_re = re.compile(r"xt=urn:btih:([a-fA-F0-9]{40})")

    def download_torrent(self, download_url):
        if download_url.startswith("magnet:"):
            self._safe_print_link(download_url, download_url)
            return
        proxy_manager.enable(True)
        response_content = self._fetch_url(download_url)
        proxy_manager.enable(False)
        if response_content and response_content.startswith("magnet:"):
            self._safe_print_link(response_content, download_url)
        else:
            print(helpers.download_file(download_url))

    def search(self, what, cat="all"):
        search_query = unquote(what)
        category_ids = self.supported_categories.get(cat.lower())

        if self.is_malformed:
            return self._handle_error("malformed configuration file", search_query)
        if self.api_key == "YOUR_API_KEY_HERE":
            return self._handle_error("API key is not configured", search_query)

        indexers = self._get_configured_indexers(search_query)
        if not indexers:
            return

        search_args = [(search_query, category_ids, idx) for idx in indexers]

        # --- Collect Results ---
        all_results = []
        if self.thread_count > 1 and len(search_args) > 1:
            with Pool(min(len(search_args), self.thread_count)) as pool:
                results_from_threads = pool.starmap(self._search_indexer, search_args)
            # Flatten the list of lists into a single list
            all_results = [item for sublist in results_from_threads for item in sublist]
        else:
            all_results = self._search_indexer(search_query, category_ids, "all")

        if not all_results:
            return

        # --- Deduplicate and Print ---
        if self.deduplicate:
            unique_torrents = {}
            for result in all_results:
                info_hash = self._get_info_hash_from_magnet(result["link"])
                # If no info_hash, we can't deduplicate it, so use its link as a unique key
                key = info_hash if info_hash else result["link"]

                if key not in unique_torrents or result["seeds"] > unique_torrents[key]["seeds"]:
                    unique_torrents[key] = result

            for torrent in unique_torrents.values():
                self._safe_print(torrent)
        else:
            # Print all results without deduplication
            for result in all_results:
                self._safe_print(result)

    def _get_configured_indexers(self, context_query):
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
        """Searches an indexer and *returns* a list of parsed result dicts."""
        params = [("apikey", self.api_key), ("q", query)]
        if category_ids:
            params.append(("cat", ",".join(category_ids)))
        api_url = f"{self.url}/api/v2.0/indexers/{indexer_id}/results/torznab/api?{urlencode(params)}"

        xml_data = self._fetch_url(api_url)
        if not xml_data:
            return []

        found_items = []
        try:
            root = ET.fromstring(xml_data)
            channel = root.find("channel")
            if channel is not None:
                for item in channel.findall("item"):
                    parsed = self._parse_item(item)
                    if parsed:
                        found_items.append(parsed)
        except ET.ParseError:
            pass  # Ignore parse errors for individual indexers
        return found_items

    def _parse_item(self, item):
        """Parses an <item> element and returns a result dict, or None."""
        try:
            title = item.findtext("title")
            if not title:
                return None

            tracker = item.findtext("jackettindexer")
            name = f"[{tracker}] {title}" if self.tracker_first else f"{title} [{tracker}]"

            torznab_ns = "{http://torznab.com/schemas/2015/feed}"
            magnet_el = item.find(f'./{torznab_ns}attr[@name="magneturl"]')
            link = magnet_el.get("value") if magnet_el is not None else item.findtext("link")
            if not link:
                return None

            size = item.findtext("size", default="-1") + " B"
            seeds_el = item.find(f'./{torznab_ns}attr[@name="seeders"]')
            peers_el = item.find(f'./{torznab_ns}attr[@name="peers"]')
            seeds = int(seeds_el.get("value")) if seeds_el is not None else -1
            peers = int(peers_el.get("value")) if peers_el is not None else -1
            leech = (peers - seeds) if (seeds != -1 and peers != -1) else -1

            pub_date = -1
            pub_date_str = item.findtext("pubDate")
            if pub_date_str:
                try:
                    dt_object = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %z")
                    pub_date = int(dt_object.timestamp())
                except ValueError:
                    pass

            return {
                "name": name.replace("|", "%7C"),
                "link": link,
                "size": size,
                "seeds": seeds,
                "leech": leech,
                "engine_url": self.url,
                "desc_link": item.findtext("comments") or item.findtext("guid", ""),
                "pub_date": pub_date,
            }
        except Exception:
            return None

    def _get_info_hash_from_magnet(self, magnet_link):
        """Extracts the info hash from a magnet link using regex."""
        if not magnet_link or not magnet_link.startswith("magnet:"):
            return None
        match = self.info_hash_re.search(magnet_link)
        return match.group(1).lower() if match else None

    def _fetch_url(self, url):
        try:
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            response = opener.open(url, timeout=20)
            return response.read().decode("utf-8", "ignore")
        except urllib.request.HTTPError as e:
            return e.url if e.code == 302 else None
        except Exception:
            return None

    def _handle_error(self, error_msg, search_query):
        error_result = {
            "seeds": -1,
            "size": -1,
            "leech": -1,
            "engine_url": self.url,
            "link": self.url,
            "desc_link": "https://github.com/qbittorrent/search-plugins/wiki/How-to-configure-Jackett-plugin",
            "name": f"Jackett: {error_msg}! Conf: '{CONFIG_PATH}'. Search: '{search_query}'",
        }
        self._safe_print(error_result)

    def _safe_print_link(self, magnet, torrent_url):
        with PRINTER_LOCK:
            print(f"{magnet} {torrent_url}")

    def _safe_print(self, data):
        with PRINTER_LOCK:
            prettyPrinter(data)


if __name__ == "__main__":
    engine = jackett()
    print(f"Testing Jackett plugin. Deduplication: {'Enabled' if engine.deduplicate else 'Disabled'}")
    engine.search("ubuntu server", "software")
