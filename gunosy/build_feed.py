#!/usr/bin/env python3
"""wardrobe-lab.jp の記事を GunosyFeed（RSS 2.0）に変換する。

入力: https://wardrobe-lab.jp/blogs/news.atom（Shopify 標準・最新30件・本文全文入り）
出力: site/gunosy.xml（GitHub Pages で公開）

Gunosy のルール（コンテンツ掲載ガイドライン／GunosyFeed 仕様書 ver3.2.4）に合わせて本文を整える。
- 商品の購買ページへのリンクは不可 → 楽天・Amazon の商品パネル／ボタンは丸ごと削除
- 本文中のリンクは「1段落目と2段落目の間に1つだけ」「自社ドメインのみ」
  → 既存の <a> はすべて外し（文字は残す）、1段落目の後ろに関連記事へのリンクを1本だけ入れる
- <script> と style 属性は不可、table はグノシー非対応 → 表は箇条書きに変換
- 文章はすべて <p> で囲む
- PR／タイアップ記事、自社サービスの告知記事は配信しない（exclude.txt と kicker の「PR」で判定）
"""
import base64
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from bs4 import BeautifulSoup, NavigableString, Tag

SITE = "https://wardrobe-lab.jp"
ATOM_URL = SITE + "/blogs/news.atom"
PAGES_BASE = os.environ.get("PAGES_BASE", "https://nakamurahnlab.github.io/wl-sns-autopost")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "site")
JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; WardrobeLabFeedBuilder/1.0; +https://wardrobe-lab.jp)"

CHANNEL = {
    "title": "WARDROBE LAB",
    "link": SITE + "/",
    # 35文字以内
    "description": "大人の男性のための、服と小物の選び方メディア",
    "copyright": "© WARDROBE LAB / HN Lab Inc.",
}
CREATOR = "WARDROBE LAB編集部"

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# 消す要素（中身ごと）
DROP_SELECTORS = [
    "style", "script", "noscript", "iframe", "svg", "button", "form", "input",
    "h1", ".wl-article-kicker",
    ".wl-product-panel", ".wl-brand-link", ".wl-cta", ".wl-cta-note",
    ".wl-slide-count", ".wl-slider-hint",
]
BLOCK_TAGS = {"p", "h2", "h3", "h4", "ul", "ol", "figure", "blockquote"}
CONTAINER_TAGS = {"div", "section", "article", "header", "footer", "main", "aside", "nav"}
INLINE_KEEP = {"strong", "b", "em", "br"}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def load_excludes():
    path = os.path.join(HERE, "exclude.txt")
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def is_excluded(handle, soup, excludes):
    for pat in excludes:
        if pat.endswith("*") and handle.startswith(pat[:-1]):
            return True
        if handle == pat:
            return True
    kicker = soup.select_one(".wl-article-kicker")
    if kicker and re.search(r"\bPR\b|^PR|タイアップ|提供", kicker.get_text(" ", strip=True)):
        return True
    head = soup.get_text(" ", strip=True)[:300]
    if re.search(r"【PR】|\[PR\]|＜PR＞|<PR>|プロモーション", head):
        return True
    return False


def table_to_list(table, soup):
    rows = table.find_all("tr")
    if not rows:
        return None
    header = []
    first = rows[0]
    if first.find("th") and (table.find("thead") or not first.find("td")):
        header = [c.get_text(" ", strip=True) for c in first.find_all(["th", "td"])]
        rows = rows[1:]
    ul = soup.new_tag("ul")
    for tr in rows:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if not any(cells):
            continue
        li = soup.new_tag("li")
        st = soup.new_tag("strong")
        st.string = cells[0]
        li.append(st)
        parts = []
        for i, val in enumerate(cells[1:], start=1):
            if not val:
                continue
            label = header[i] if i < len(header) and header[i] else ""
            parts.append(f"{label}：{val}" if label else val)
        if parts:
            li.append(soup.new_tag("br"))
            li.append(NavigableString(" ／ ".join(parts)))
        ul.append(li)
    return ul


def clean_inline(node, soup):
    """ブロック要素の中身を整える（a/span は外す、許可タグ以外も外す、属性は消す）。"""
    for t in list(node.find_all(True)):
        if t.name == "img":
            continue
        if t.name in INLINE_KEEP:
            t.attrs = {}
            continue
        if t.name in ("ul", "ol", "li") and node.name in ("ul", "ol", "li"):
            t.attrs = {}
            continue
        t.unwrap()
    return node


def make_img(img, soup):
    src = (img.get("src") or img.get("data-src") or "").strip()
    if not src:
        return None
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        src = SITE + src
    if not src.startswith("https://"):
        return None
    if "rakuten.co.jp" in src or "amazon" in src or "media-amazon" in src:
        return None
    new = soup.new_tag("img", src=src)
    alt = img.get("alt")
    if alt:
        new["alt"] = alt
    fig = soup.new_tag("figure")
    fig.append(new)
    return fig


def flatten(container, soup, out):
    """コンテナを再帰的にたどり、許可されたブロック要素だけを out に並べる。
    コンテナ直下の地の文やインライン要素は <p> にまとめる。"""
    buf = []

    def flush():
        if not buf:
            return
        p = soup.new_tag("p")
        for x in buf:
            p.append(x)
        buf.clear()
        clean_inline(p, soup)
        if p.get_text(strip=True):
            out.append(p)

    for child in list(container.children):
        if isinstance(child, NavigableString):
            if str(child).strip():
                buf.append(NavigableString(str(child).strip()))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name
        if name == "img":
            flush()
            fig = make_img(child, soup)
            if fig:
                out.append(fig)
        elif name in ("p", "h2", "h3", "h4", "blockquote"):
            flush()
            imgs = child.find_all("img")
            for im in imgs:
                fig = make_img(im, soup)
                im.extract()
                if fig:
                    out.append(fig)
            clean_inline(child, soup)
            child.attrs = {}
            if child.get_text(strip=True):
                out.append(child)
        elif name in ("ul", "ol"):
            flush()
            for li in child.find_all("li"):
                # 「<strong>見出し</strong> <span>説明</span>」を2行にする
                st = li.find(["strong", "b"])
                sp = li.find("span")
                if st and sp and st.next_sibling is not None:
                    st.insert_after(soup.new_tag("br"))
            clean_inline(child, soup)
            child.attrs = {}
            for li in child.find_all("li"):
                li.attrs = {}
            if child.get_text(strip=True):
                out.append(child)
        elif name == "figure":
            flush()
            im = child.find("img")
            fig = make_img(im, soup) if im else None
            if fig:
                cap = child.find("figcaption")
                if cap and cap.get_text(strip=True):
                    fc = soup.new_tag("figcaption")
                    fc.string = cap.get_text(" ", strip=True)
                    fig.append(fc)
                out.append(fig)
        elif name == "hr":
            flush()
        elif name in CONTAINER_TAGS:
            flush()
            flatten(child, soup, out)
        else:
            # span / strong / a などのインライン
            buf.append(child)
    flush()


def convert_body(raw_html):
    soup = BeautifulSoup(raw_html, "html.parser")
    for sel in DROP_SELECTORS:
        for t in soup.select(sel):
            t.decompose()
    for table in soup.find_all("table"):
        ul = table_to_list(table, soup)
        wrap = table.find_parent(class_="wl-compare-wrap") or table
        if ul is not None:
            wrap.replace_with(ul)
        else:
            wrap.decompose()
    # 外部リンク（楽天・Amazon・ブランド公式など）はボタン文言ごと消す。
    # 自社リンクは外して文字だけ残す（本文内リンクは後で1本だけ入れ直す）
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        internal = href.startswith("/") and not href.startswith("//") or href.startswith(SITE)
        cls = " ".join(a.get("class") or [])
        in_text = a.parent is not None and a.parent.name in ("p", "li", "strong", "em", "b", "span", "h2", "h3")
        if "button" in cls or "thumb" in cls or (not internal and not in_text):
            a.decompose()
        else:
            a.unwrap()

    out = []
    root = BeautifulSoup("<div></div>", "html.parser").div
    for c in list(soup.contents):
        root.append(c)
    flatten(root, soup, out)

    cleaned = []
    for el in out:
        text = el.get_text(" ", strip=True) if el.name != "figure" else "img"
        if el.name == "p":
            if re.match(r"^\[?画像[：:]", text):
                continue  # 差し替え前のプレースホルダ
            if "アフィリエイト" in text or "広告" in text and "含む" in text:
                # 配信版は商品リンクを外しているので、広告表記の注記は残さない
                kept = [s for s in re.split(r"(?<=。)", el.get_text("", strip=False))
                        if s.strip() and not re.search(r"アフィリエイト|広告", s)]
                if not "".join(kept).strip():
                    continue
                el.clear()
                el.append(NavigableString("".join(kept).strip().lstrip("※")))
        cleaned.append(el)
    # ul/ol は <p> に展開する（Gunosyのバリデータは <li> 内の文も「<p>で囲まれていない」と注意を出す）
    final = []
    for el in cleaned:
        if el.name in ("ul", "ol"):
            for n, li in enumerate(el.find_all("li", recursive=False), start=1):
                p = BeautifulSoup("<p></p>", "html.parser").p
                if not li.find(["strong", "b"]):
                    p.append(NavigableString(f"{n}. " if el.name == "ol" else "・"))
                for c in list(li.contents):
                    if isinstance(c, NavigableString):
                        c = NavigableString(str(c).strip())
                    p.append(c)
                if p.get_text(strip=True):
                    final.append(p)
        else:
            final.append(el)
    return final


def rfc822(iso):
    dt = datetime.fromisoformat(iso)
    return format_datetime(dt.astimezone(JST))


def strip_query(url):
    return url.split("?", 1)[0]


def guess_type(url):
    u = url.lower().split("?")[0]
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def main():
    excludes = load_excludes()
    atom = fetch(ATOM_URL)
    root = ET.fromstring(atom)
    entries = []
    for e in root.findall("a:entry", ATOM_NS):
        link = e.find("a:link[@rel='alternate']", ATOM_NS).get("href")
        handle = link.rstrip("/").split("/blogs/news/")[-1]
        content = e.findtext("a:content", default="", namespaces=ATOM_NS)
        soup = BeautifulSoup(content, "html.parser")
        entries.append({
            "handle": handle,
            "link": link,
            "title": e.findtext("a:title", default="", namespaces=ATOM_NS).strip(),
            "published": e.findtext("a:published", namespaces=ATOM_NS),
            "updated": e.findtext("a:updated", namespaces=ATOM_NS),
            "content": content,
            "excluded": is_excluded(handle, soup, excludes),
            "first_img": next((strip_query(i.get("src")) for i in soup.find_all("img")
                               if (i.get("src") or "").startswith(("https://", "//"))
                               and "rakuten" not in i.get("src") and "amazon" not in i.get("src")), None),
        })

    ok = [x for x in entries if not x["excluded"]]
    skipped = [x["handle"] for x in entries if x["excluded"]]

    items_xml = []
    for i, x in enumerate(ok):
        body = convert_body(x["content"])
        related = [y for y in ok if y is not x][:3]
        # 本文内リンクは「1段落目と2段落目の間に1本だけ」（ガイドライン2.3.1⑤）
        if related:
            r = related[0]
            first_p = next((k for k, el in enumerate(body) if el.name == "p"), None)
            if first_p is not None:
                link_html = (f'<p>あわせて読みたい：<a href="{html.escape(r["link"])}">'
                             f'{html.escape(r["title"])}</a></p>')
                body.insert(first_p + 1, BeautifulSoup(link_html, "html.parser").p)
        body_html = "\n".join(str(el) for el in body)
        body_html = body_html.replace("]]>", "]]&gt;")

        parts = [
            "<item>",
            f"<title>{html.escape(x['title'], quote=True)}</title>",
            f"<link>{html.escape(x['link'])}</link>",
            f"<guid isPermaLink=\"false\">wardrobe-lab-{html.escape(x['handle'])}</guid>",
            f"<pubDate>{rfc822(x['published'])}</pubDate>",
            f"<gnf:modified>{rfc822(x['updated'] or x['published'])}</gnf:modified>",
            f"<dc:creator>{CREATOR}</dc:creator>",
            '<media:status state="active" />',
        ]
        if x["first_img"]:
            img = x["first_img"]
            if img.startswith("//"):
                img = "https:" + img
            if len(img) < 256:
                parts.append(f'<enclosure url="{html.escape(img)}" type="{guess_type(img)}" length="0" />')
        for r in related:
            parts.append(f'<gnf:relatedLink title="{html.escape(r["title"], quote=True)}" '
                         f'link="{html.escape(r["link"])}" />')
        parts.append(f"<content:encoded><![CDATA[{body_html}]]></content:encoded>")
        parts.append("</item>")
        items_xml.append("\n".join(parts))

    now = format_datetime(datetime.now(JST))
    feed = f"""<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0" xmlns:gnf="http://assets.gunosy.com/media/gnf" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
<title>{CHANNEL['title']}</title>
<link>{CHANNEL['link']}</link>
<description>{CHANNEL['description']}</description>
<image>
<url>{PAGES_BASE}/logo-square.png</url>
<title>{CHANNEL['title']}</title>
<link>{CHANNEL['link']}</link>
</image>
<gnf:wide_image_link>{PAGES_BASE}/logo-wide.png</gnf:wide_image_link>
<language>ja</language>
<copyright>{html.escape(CHANNEL['copyright'])}</copyright>
<ttl>60</ttl>
<lastBuildDate>{now}</lastBuildDate>
{chr(10).join(items_xml)}
</channel>
</rss>
"""
    ET.fromstring(feed.encode("utf-8"))  # 壊れたXMLは出さない
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "gunosy.xml"), "w", encoding="utf-8") as f:
        f.write(feed)
    # チャンネル用のロゴ（正方形・横長44px）を assets.json から書き出す
    for name, b64 in json.load(open(os.path.join(HERE, "assets.json"))).items():
        with open(os.path.join(OUT_DIR, name), "wb") as f:
            f.write(base64.b64decode(b64))
    print(f"items: {len(ok)} / skipped (PR・除外): {len(skipped)} {skipped}")


if __name__ == "__main__":
    sys.exit(main())
