#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WARDROBE LAB -> Threads 自動投稿

2つのモードで動く（1日あたり各1本まで）。
  --mode new      新着記事（公開から36時間以内・未投稿）を1本投稿。なければ何もしない。
                  平日18:00公開／休日10:00公開に合わせ、毎日10:30と18:30に実行する。
                  その日すでに新着を投稿していたら何もしない（祝日も自動で対応できる）。
  --mode backfill まだThreadsに出していない過去記事を古い順に1本投稿。全部出し終えたら何もしない。

GitHub Actions で動かす前提。
  - THREADS_USER_ID / THREADS_ACCESS_TOKEN はリポジトリの Secrets から環境変数で渡す
  - 延長したトークンは token.enc（THREADS_ACCESS_TOKEN を鍵に暗号化）としてリポジトリに保存
  - 投稿済みの記録は state.json（ワークフローがコミットする）
ローカルで動かす場合は、同じディレクトリの config.json でも可。
標準ライブラリ＋openssl コマンドのみ。Python 3.9+。
"""

import json
import os
import re
import sys
import time
import html
import random
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "post.log")
TOKEN_PATH = os.path.join(BASE, "token.enc")

JST = timezone(timedelta(hours=9))
GRAPH = "https://graph.threads.net/v1.0"
UA = "wl-threads/1.0 (+https://wardrobe-lab.jp)"

# ---------------------------------------------------------------- カテゴリ定義
# (カテゴリキー, Threadsのトピックタグ, 本文に添える文字タグ, 判定キーワード)
# 判定はハンドル(URLの末尾)とタイトルの両方に対して行い、上から順に最初に当たったもの。
CATEGORIES = [
    ("shoecare", "靴磨き", ["#靴磨き", "#靴のお手入れ"],
     ["shoecare", "shoe-polish", "shoes-care", "shoes-yearend", "boots-care",
      "suede-shoes-care", "靴磨き", "靴のお手入れ"]),
    ("clothcare", "衣類ケア", ["#衣類ケア", "#長く着る"],
     ["clothcare", "wardrobe-swap", "wardrobe-inventory", "wardrobe-sort",
      "pilling", "static", "cashmere-care", "wool-coat-care", "denim-wash",
      "coat-knit-yearend", "knit-one-year",
      "衣替え", "毛玉", "静電気", "洗い方", "棚卸", "収納"]),
    ("beauty", "メンズ美容", ["#メンズ美容", "#香水"],
     ["fragrance", "skincare", "haircare", "beauty", "dryer", "shaver", "grooming",
      "香り", "香水", "スキンケア", "ヘアケア", "ドライヤー", "シェーバー", "髭剃り"]),
    ("outer", "メンズファッション", ["#アウター", "#秋冬コーデ"],
     ["coat", "outer", "jacket", "down", "blouson", "gilet", "shearling", "tweed",
      "harrington", "trench", "chesterfield", "balmacaan", "mods", "pea-coat",
      "コート", "アウター", "ジャケット", "ダウン", "ブルゾン", "ジレ", "ベスト"]),
    ("shoes", "革靴", ["#革靴", "#足元"],
     ["shoes", "boots", "sneaker", "sole", "socks", "loafer",
      "靴", "ブーツ", "スニーカー", "ソール", "靴下", "ローファー"]),
    ("denim", "デニム", ["#デニム", "#ジーンズ"],
     ["denim", "jeans", "デニム", "ジーンズ"]),
    ("pants", "メンズファッション", ["#パンツ", "#大人カジュアル"],
     ["pants", "trousers", "chino", "slacks", "corduroy",
      "パンツ", "スラックス", "チノ"]),
    ("casual", "メンズファッション", ["#パーカー", "#大人カジュアル"],
     ["sweat", "hoodie", "loungewear", "スウェット", "パーカー", "ルームウェア"]),
    ("shirt", "シャツ", ["#シャツ", "#大人カジュアル"],
     ["shirt", "flannel", "シャツ", "フランネル"]),
    ("inner", "メンズファッション", ["#インナー", "#秋冬コーデ"],
     ["innerwear", "thermal", "インナー", "肌着"]),
    ("tops", "ニット", ["#ニット", "#秋冬コーデ"],
     ["knit", "turtleneck", "highneck", "cardigan", "tops", "layering",
      "ニット", "タートル", "ハイネック", "カーディガン", "重ね着", "レイヤード"]),
    ("watch", "腕時計", ["#腕時計", "#時計好き"],
     ["watch", "時計"]),
    ("accessory", "メンズファッション", ["#小物", "#秋冬コーデ"],
     ["muffler", "stole", "scarf", "beanie", "hats", "gloves", "tie-casual",
      "accessories", "マフラー", "ストール", "帽子", "手袋", "ニット帽", "ネクタイ"]),
    ("bag", "レザー", ["#革小物", "#バッグ"],
     ["bag", "backpack", "tote", "wallet", "belt", "small-goods", "leather-goods",
      "バッグ", "財布", "ベルト", "革小物"]),
    ("jewelry", "アクセサリー", ["#アクセサリー", "#ギフト"],
     ["jewelry", "gift", "リング", "ネックレス", "ギフト", "プレゼント"]),
    ("eyewear", "メガネ", ["#メガネ", "#アイウェア"],
     ["eyeglasses", "eyewear", "メガネ", "眼鏡", "サングラス"]),
]
DEFAULT_CATEGORY = ("seasonal", "メンズファッション", ["#大人の着こなし", "#コーデ"])


# ---------------------------------------------------------------- 小道具
def log(msg):
    line = "[%s] %s" % (datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fetch(url, data=None, timeout=30, retries=3):
    body = urllib.parse.urlencode(data).encode() if data else None
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last = "HTTP %s: %s" % (e.code, detail)
            if e.code < 500:
                raise RuntimeError(last)
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError("fetch failed: %s (%s)" % (url, last))


def strip_tags(s):
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t　]+", " ", s).strip()


# ---------------------------------------------------------------- 記事の取得
def parse_feed(xml):
    """Shopify の Atom フィードから最近の記事を新しい順で返す"""
    out = []
    for m in re.finditer(r"<entry>(.*?)</entry>", xml, re.S):
        e = m.group(1)
        t = re.search(r"<title>(.*?)</title>", e, re.S)
        l = re.search(r'<link[^>]*href="([^"]+)"', e)
        p = re.search(r"<published>(.*?)</published>", e)
        c = re.search(r"<summary[^>]*>(.*?)</summary>", e, re.S) or \
            re.search(r"<content[^>]*>(.*?)</content>", e, re.S)
        if not (t and l):
            continue
        url = l.group(1).split("?")[0]
        out.append({
            "title": strip_tags(t.group(1)),
            "url": url,
            "handle": url.rstrip("/").rsplit("/", 1)[-1],
            "published": p.group(1) if p else "",
            "body": strip_tags(c.group(1))[:2000] if c else "",
        })
    out.sort(key=lambda x: x["published"], reverse=True)
    return out


def parse_sitemap(site):
    """公開中の記事URLを全部取る（過去記事プール用）"""
    urls = []
    try:
        idx = fetch(site + "/sitemap.xml")
    except RuntimeError as e:
        log("sitemap index 取得失敗: %s" % e)
        return urls
    subs = re.findall(r"<loc>([^<]*sitemap_(?:articles|blogs)[^<]*)</loc>", idx)
    for s in subs:
        try:
            xml = fetch(html.unescape(s))
        except RuntimeError as e:
            log("sitemap 取得失敗 %s: %s" % (s, e))
            continue
        for u in re.findall(r"<loc>([^<]+)</loc>", xml):
            u = html.unescape(u).split("?")[0]
            if re.search(r"/blogs/[^/]+/[^/]+$", u):
                urls.append(u)
    return sorted(set(urls))


def fetch_article_meta(url):
    """過去記事を再投稿するとき用に、ページからタイトルと説明文を取る"""
    try:
        h = fetch(url)
    except RuntimeError as e:
        log("記事ページ取得失敗 %s: %s" % (url, e))
        return None
    t = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', h) or \
        re.search(r"<title>(.*?)</title>", h, re.S)
    d = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', h) or \
        re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', h)
    if not t:
        return None
    title = html.unescape(t.group(1)).split("–")[0].split(" | ")[0].strip()
    return {
        "title": title,
        "url": url,
        "handle": url.rstrip("/").rsplit("/", 1)[-1],
        "body": html.unescape(d.group(1)).strip() if d else "",
    }


# ---------------------------------------------------------------- 文面を組む
def pick_category(article):
    hay = (article["handle"] + " " + article["title"]).lower()
    for key, topic, tags, words in CATEGORIES:
        for w in words:
            if w.lower() in hay:
                return key, topic, tags
    return DEFAULT_CATEGORY


def first_sentence(text, limit=90):
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[。！？])", text)
    s = ""
    for p in parts:
        if len(s) + len(p) > limit:
            break
        s += p
    if not s:
        s = text[:limit]
    return s.strip()


def build_text(article, recycled=False):
    _key, topic, tags = pick_category(article)
    lead = first_sentence(article.get("body", ""))
    lines = [article["title"]]
    if lead:
        lines.append("")
        lines.append(lead)
    lines.append("")
    lines.append(article["url"])
    lines.append("")
    lines.append(" ".join(tags + ["#WARDROBELAB"]))
    text = "\n".join(lines)
    # Threadsの本文上限は500字。超えたらリード文を削る
    while len(text) > 490 and lead:
        lead = lead[: max(0, len(lead) - 20)]
        lines = [article["title"]]
        if lead:
            lines += ["", lead]
        lines += ["", article["url"], "", " ".join(tags + ["#WARDROBELAB"])]
        text = "\n".join(lines)
    return text[:500], topic


# ---------------------------------------------------------------- 設定とトークン保存
def _openssl(args, data, key):
    env = dict(os.environ, WL_KEY=key)
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-a",
                        "-pass", "env:WL_KEY"] + args,
                       input=data, capture_output=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[:200])
    return r.stdout


def load_saved_token(key):
    """token.enc があれば復号して {"access_token", "token_updated"} を返す"""
    if not key or not os.path.exists(TOKEN_PATH):
        return None
    try:
        with open(TOKEN_PATH, "rb") as f:
            raw = _openssl(["-d"], f.read(), key)
        return json.loads(raw.decode("utf-8"))
    except (RuntimeError, ValueError, OSError) as e:
        log("token.enc を読めなかったので Secrets のトークンを使う: %s" % e)
        return None


def save_token(cfg):
    key = cfg.get("_key")
    if not key:  # ローカル実行（config.json）の場合
        save_json(CONFIG_PATH, {k: v for k, v in cfg.items() if not k.startswith("_")})
        return
    data = json.dumps({"access_token": cfg["access_token"],
                       "token_updated": cfg.get("token_updated", "")}).encode()
    with open(TOKEN_PATH, "wb") as f:
        f.write(_openssl([], data, key))


def load_config():
    uid = os.environ.get("THREADS_USER_ID", "").strip()
    tok = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if uid and tok:
        cfg = {"user_id": uid, "access_token": tok, "token_updated": "", "_key": tok}
        saved = load_saved_token(tok)
        if saved and saved.get("access_token"):
            cfg["access_token"] = saved["access_token"]
            cfg["token_updated"] = saved.get("token_updated", "")
        return cfg
    return load_json(CONFIG_PATH, None)


# ---------------------------------------------------------------- Threads API
def refresh_token(cfg):
    """長期トークン(60日)を延長する。20日以上経っていたら更新。"""
    updated = cfg.get("token_updated")
    if updated:
        try:
            d = datetime.strptime(updated, "%Y-%m-%d").replace(tzinfo=JST)
            if datetime.now(JST) - d < timedelta(days=20):
                return cfg
        except ValueError:
            pass
    url = ("https://graph.threads.net/refresh_access_token"
           "?grant_type=th_refresh_token&access_token=" +
           urllib.parse.quote(cfg["access_token"]))
    try:
        res = json.loads(fetch(url))
        if res.get("access_token"):
            cfg["access_token"] = res["access_token"]
            cfg["token_updated"] = datetime.now(JST).strftime("%Y-%m-%d")
            save_token(cfg)
            log("アクセストークンを更新した（有効期限 %s 秒）" % res.get("expires_in"))
    except RuntimeError as e:
        log("⚠ トークン更新に失敗: %s" % e)
    return cfg


def publish(cfg, text, topic):
    uid = cfg["user_id"]
    token = cfg["access_token"]
    link = re.search(r"https://\S+", text)
    params = {
        "media_type": "TEXT",
        "text": text,
        "access_token": token,
    }
    if link:
        params["link_attachment"] = link.group(0)
    if topic and cfg.get("use_topic_tag", True):
        params["topic_tag"] = topic

    try:
        res = json.loads(fetch("%s/%s/threads" % (GRAPH, uid), data=params))
    except RuntimeError as e:
        if "topic_tag" in params and ("topic" in str(e).lower() or "param" in str(e).lower()):
            log("topic_tag が通らなかったので外して再試行: %s" % e)
            params.pop("topic_tag")
            res = json.loads(fetch("%s/%s/threads" % (GRAPH, uid), data=params))
        else:
            raise
    cid = res["id"]
    time.sleep(5)
    out = json.loads(fetch("%s/%s/threads_publish" % (GRAPH, uid),
                           data={"creation_id": cid, "access_token": token}))
    return out.get("id")


# ---------------------------------------------------------------- 本体
def handle_of(url):
    return url.rstrip("/").rsplit("/", 1)[-1]


def parse_time(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def pick_new(entries, posted):
    """公開から36時間以内の未投稿記事のうち最新1本"""
    limit = datetime.now(JST) - timedelta(hours=36)
    for e in entries:  # 新しい順
        t = parse_time(e.get("published", ""))
        if t and t < limit:
            break
        if e["handle"] not in posted:
            return e
    return None


def pick_backfill(state, site, entries, posted):
    """未投稿の過去記事を古い順に1本。直近36時間の記事は新着枠に任せる"""
    stale = (not state.get("sitemap") or not state.get("sitemap_at") or
             state["sitemap_at"] < (datetime.now(JST) - timedelta(days=3)).isoformat())
    if stale:
        urls = parse_sitemap(site)
        if urls:
            state["sitemap"] = urls
            state["sitemap_at"] = datetime.now(JST).isoformat()
            log("サイトマップ更新: %d本" % len(urls))
    recent = set()
    pub = {}
    limit = datetime.now(JST) - timedelta(hours=36)
    for e in entries:
        t = parse_time(e.get("published", ""))
        if t:
            pub[e["handle"]] = e["published"]
            if t >= limit:
                recent.add(e["handle"])
    pool = [u for u in state.get("sitemap", [])
            if handle_of(u) not in posted and handle_of(u) not in recent]
    log("未投稿の過去記事: 残り%d本" % len(pool))
    # フィードにある記事は公開日時で、それ以外はサイトマップ順（古い→新しい）のまま
    pool.sort(key=lambda u: pub.get(handle_of(u), ""))
    for u in pool[:5]:
        meta = fetch_article_meta(u)
        if meta and meta["title"]:
            return meta
    return None


def main():
    dry = "--dry-run" in sys.argv
    mode = "new"
    if "--mode" in sys.argv:
        mode = sys.argv[sys.argv.index("--mode") + 1]
    cfg = load_config()
    if not cfg and not dry:
        log("Threads のトークンが未設定のため、今日はスキップ（Secrets に THREADS_USER_ID と THREADS_ACCESS_TOKEN を登録すると投稿が始まる）")
        return 0
    cfg = cfg or {}
    site = cfg.get("site", "https://wardrobe-lab.jp").rstrip("/")
    feed_url = cfg.get("feed", site + "/blogs/news.atom")

    state = load_json(STATE_PATH, {"posted": {}, "sitemap": [], "sitemap_at": ""})
    posted = state.setdefault("posted", {})
    today = datetime.now(JST).strftime("%Y-%m-%d")
    done_key = "last_%s_date" % mode
    log("モード: %s" % mode)

    if state.get(done_key) == today and not dry:
        log("今日はこのモードで投稿済みなので何もしない")
        return 0

    try:
        entries = parse_feed(fetch(feed_url))
        log("フィード取得: %d本" % len(entries))
    except RuntimeError as e:
        log("⚠ フィード取得失敗: %s" % e)
        entries = []

    if mode == "new":
        article = pick_new(entries, posted)
        if article is None:
            log("新着の未投稿記事なし")
            return 0
        meta = fetch_article_meta(article["url"])
        if meta and meta.get("body"):
            article["body"] = meta["body"]
        recycled = False
    elif mode == "backfill":
        article = pick_backfill(state, site, entries, posted)
        if article is None:
            log("過去記事はすべて投稿済み")
            save_json(STATE_PATH, state)
            return 0
        recycled = True
    else:
        log("不明なモード: %s" % mode)
        return 1

    text, topic = build_text(article, recycled)
    log("対象: %s%s" % (article["title"], "（過去記事）" if recycled else "（新着）"))
    log("トピックタグ: %s" % topic)
    log("本文 %d字:\n%s" % (len(text), text))

    if dry:
        log("--dry-run のため投稿しない")
        return 0

    cfg = refresh_token(cfg)
    try:
        pid = publish(cfg, text, topic)
    except RuntimeError as e:
        log("✗ 投稿失敗: %s" % e)
        return 1

    now = datetime.now(JST).isoformat()
    posted[article["handle"]] = now
    state[done_key] = today
    state["last"] = {"handle": article["handle"], "url": article["url"],
                     "post_id": pid, "at": now, "mode": mode}
    save_json(STATE_PATH, state)
    log("✓ 投稿完了 id=%s %s" % (pid, article["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
