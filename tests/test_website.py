"""공개 사이트의 링크·자산과 비밀 파일 게시 방지를 확인한다."""

import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1] / "site" / "public"


class Document(HTMLParser):
    def __init__(self, path):
        super().__init__()
        self.links = []
        self.ids = set()
        self.headings = 0
        self.feed(path.read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "h1":
            self.headings += 1
        for name in ("href", "src"):
            if name in attrs:
                self.links.append(attrs[name])


class WebsiteTest(unittest.TestCase):
    def test_pages_assets_and_fragment_links_exist(self):
        pages = list(ROOT.rglob("*.html"))
        self.assertEqual(len(pages), 3)
        for path in pages:
            doc = Document(path)
            self.assertEqual(doc.headings, 1, str(path))
            self.assertIn("/privacy/", doc.links)
            self.assertIn("/terms/", doc.links)
            for link in doc.links:
                url = urlsplit(link)
                if url.scheme or url.netloc:
                    continue
                target = ROOT / unquote(url.path).lstrip("/") if url.path.startswith("/") else path.parent / unquote(url.path)
                if not url.path:
                    target = path
                if target.is_dir():
                    target /= "index.html"
                self.assertTrue(target.is_file(), f"{path}: {link}")
                if url.fragment:
                    self.assertIn(unquote(url.fragment), Document(target).ids, link)

    def test_public_directory_contains_only_static_assets(self):
        for path in ROOT.rglob("*"):
            self.assertFalse(path.is_symlink(), str(path))
            self.assertFalse(path.name.startswith("."), str(path))
            if path.is_file():
                self.assertIn(path.suffix, {".html", ".css", ".png", ".txt", ".xml"})
                self.assertLess(path.stat().st_size, 1_000_000)


if __name__ == "__main__":
    unittest.main()
