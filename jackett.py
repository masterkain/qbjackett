# jackett.py

import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from http.cookiejar import CookieJar
from multiprocessing.dummy import Pool as ThreadPool
from threading import Lock
from urllib.parse import unquote, urlencode

import helpers
from novaprinter import prettyPrinter

CONFIG_FILE = "jackett.json"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), CONFIG_FILE)
CONFIG_DEFAULTS = {
    "api_key": "YOUR_API_KEY_HERE",
    "url": "http://127.0.0.1:9117",
    "tracker_first": False,
    "thread_count": 20,
}
PRINTER_LOCK = Lock()


class Config:
    def __init__(self):
        self.data = CONFIG_DEFAULTS.copy()
        self._load()

    def _load(self):
        try:
            with open(CONFIG_PATH, "r") as f:
                user_data = json.load(f)
                self.data.update(user_data)
        except json.JSONDecodeError:
            self.data["malformed"] = True
        except FileNotFoundError:
            self._save()

        required_keys = {"api_key", "url", "tracker_first"}
        if not required_keys.issubset(self.data):
            self.data["malformed"] = True

    def _save(self):
        with open(CONFIG_PATH, "w") as f:
            json.dump(self.data, f, indent=4, sort_keys=True)


class ProxyManager:
    def __init__(self):
        self.http_proxy = os.getenv("http_proxy", "")
        self.https_proxy = os.getenv("https_proxy", "")

    def enable(self, enable: bool):
        if enable:
            os.environ["http_proxy"] = self.http_proxy
            os.environ["https_proxy"] = self.https_proxy
        else:
            os.environ.pop("http_proxy", None)
            os.environ.pop("https_proxy", None)

        try:
            helpers.enable_socks_proxy(enable)
        except AttributeError:
            pass


class Jackett:
    def __init__(self):
        self.config = Config()
        self.url = self.config.data["url"].rstrip("/")
        self.api_key = self.config.data["api_key"]
        self.thread_count = self.config.data["thread_count"]
        self.tracker_first = self.config.data["tracker_first"]
        self.proxy = ProxyManager()

        self.supported_categories = {
            "all": None,
            "anime": ["5070"],
            "books": ["8000"],
            "games": ["1000", "4000"],
            "movies": ["2000"],
            "music": ["3000"],
            "software": ["4000"],
            "tv": ["5000"],
        }

    def search(self, what, cat="all"):
        what = unquote(what)
        category = self.supported_categories.get(cat.lower(), None)

        if self.config.data.get("malformed"):
            return self._error("Malformed config", what)

        if self.api_key == "YOUR_API_KEY_HERE":
            return self._error("Missing API key", what)

        indexers = self._get_indexers(what)
        if not indexers:
            return

        args = [(what, category, idx) for idx in indexers]
        with ThreadPool(min(len(args), self.thread_count)) as pool:
            pool.starmap(self._search_indexer, args)

    def _get_indexers(self, what):
        params = urlencode({"apikey": self.api_key, "t": "indexers", "configured": "true"})
        url = f"{self.url}/api/v2.0/indexers/all/results/torznab/api?{params}"
        xml_data = self._fetch_url(url, what)
        if not xml_data:
            return []

        try:
            root = ET.fromstring(xml_data)
            return [i.attrib["id"] for i in root.findall("indexer")]
        except ET.ParseError:
            self._error("Invalid XML from indexers", what)
            return []

    def _search_indexer(self, query, category, indexer):
        params = [("apikey", self.api_key), ("q", query)]
        if category:
            params.append(("cat", ",".join(category)))
        query_str = urlencode(params)
        url = f"{self.url}/api/v2.0/indexers/{indexer}/results/torznab/api?{query_str}"

        xml_data = self._fetch_url(url, query)
        if not xml_data:
            return

        try:
            root = ET.fromstring(xml_data)
            channel = root.find("channel")
            if channel is None:
                self._error(f"No <channel> element in response from {indexer}", query)
                return
            for item in channel.findall("item"):
                self._parse_item(item, indexer)
        except ET.ParseError:
            self._error(f"XML parse error on {indexer}", query)

    def _parse_item(self, item, indexer):
        try:
            title = item.findtext("title", default="")
            if not title:
                return

            tracker = item.findtext("jackettindexer", default="")
            name = f"[{tracker}] {title}" if self.tracker_first else f"{title} [{tracker}]"

            link_el = item.find('./{http://torznab.com/schemas/2015/feed}attr[@name="magneturl"]')
            link = link_el.attrib["value"] if link_el is not None else item.findtext("link", default="")
            if not link:
                return

            size = item.findtext("size", default="-1") + " B"
            seeds = item.find('./{http://torznab.com/schemas/2015/feed}attr[@name="seeders"]')
            leech = item.find('./{http://torznab.com/schemas/2015/feed}attr[@name="peers"]')
            seed_val = int(seeds.attrib["value"]) if seeds is not None else -1
            leech_val = int(leech.attrib["value"]) - seed_val if leech is not None else -1

            pub_date_str = item.findtext("pubDate", default="")
            try:
                pub_date = int(datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %z").timestamp())
            except Exception:
                pub_date = -1

            desc = item.findtext("comments") or item.findtext("guid", default="")

            result = {"name": name.replace("|", "%7C"), "link": link, "size": size, "seeds": seed_val, "leech": leech_val, "engine_url": self.url, "desc_link": desc, "pub_date": pub_date}
            self._safe_print(result)
        except Exception as e:
            self._safe_print({"name": f"Error parsing item: {e}", "link": self.url, "desc_link": "", "engine_url": self.url, "seeds": -1, "leech": -1, "size": -1})

    def _fetch_url(self, url, context):
        try:
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            with opener.open(url) as response:
                return response.read().decode("utf-8")
        except urllib.request.HTTPError as e:
            if e.code == 302:
                return e.url
        except Exception:
            self._error("Request failed", context)
        return None

    def _error(self, message, context):
        self._safe_print(
            {
                "name": f"Jackett: {message}. Search: '{context}'",
                "link": self.url,
                "desc_link": "https://github.com/qbittorrent/search-plugins/wiki/How-to-configure-Jackett-plugin",
                "engine_url": self.url,
                "seeds": -1,
                "leech": -1,
                "size": -1,
            }
        )

    def _safe_print(self, data):
        with PRINTER_LOCK:
            prettyPrinter(data)


if __name__ == "__main__":
    Jackett().search("ubuntu server", "software")
