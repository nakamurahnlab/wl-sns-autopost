#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WARDROBE LAB -> Instagram 自動投稿（GitHub Actions 用）

記事の選び方は Threads と同じ（post_threads.py の関数を使う）。
  --mode new      公開から36時間以内の未投稿記事を1本（1日1本まで）
  --mode backfill 未投稿の過去記事を古い順に1本（1日1本まで）

画像は make_image.py で 1080x1350 を作り、images/ に置いて push し、
公開URL（raw.githubusercontent.com）を Instagram に渡す。

Secrets: IG_ACCESS_TOKEN（Instagram ログインの長期トークン）
状態:    ig_state.json / 延長したトークンは ig_token.enc（暗号化）
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import post_threads as T
from make_image import make_image

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE, "ig_state.json")
TOKEN_PATH = os.path.join(BASE, "ig_token.enc")
IMG_DIR = os.path.join(BASE, "images")
GRAPH = "https://graph.instagram.com"
SITE = "https://wardrobe-lab.jp"
FEED = SITE + "/blogs/news.atom"
REPO = os.environ.get("GITHUB_REPOSITORY", "nakamurahnlab/wl-sns-autopost")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
JST = T.JST
log = T.log

BASE_TAGS = ["#ワードローブラボ", "#メンズファッション", "#大人の着こなし", "#大人コーデ"]
# Threads 用の文字タグを Instagram 用に読み替える
TAG_ALIAS = {"#WARDROBELAB": None, "#コーデ": "#メンズコーデ"}


# ---------------------------------------------------------------- 取得
def fetch_bytes(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": T.UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def absolutize(u):
    u = u.replace("&amp;", "&")
    return "https:" + u if u.startswith("//") else u


def page_meta(url):
    h = T.fetch(url)
    def meta(prop):
        m = re.search(r'<meta[^>]+property="%s"[^>]+content="([^"]*)"' % prop, h) or \
            re.search(r'<meta[^>]+content="([^"]*)"[^>]+property="%s"' % prop, h)
        return T.html.unescape(m.group(1)).strip() if m else ""
    title = meta("og:title").split("–")[0].split(" | ")[0].strip()
    return {"title": title, "body": meta("og:description"), "image": absolutize(meta("og:image")),
            "url": url, "handle": T.handle_of(url)}


def get_logo():
    try:
        h = T.fetch(SITE + "/")
        m = re.search(r'((?:https:)?//[^"\'\s]*?wl-logo\.png[^"\'\s]*)', h)
        if m:
            return fetch_bytes(absolutize(m.group(1)))
    except Exception as e:  # noqa: BLE001
        log("ロゴ取得失敗（文字ロゴで代用）: %s" % e)
    return None


# ---------------------------------------------------------------- トークン
def load_token():
    key = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not key:
        return None
    cfg = {"access_token": key, "token_updated": "", "_key": key}
    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, "rb") as f:
                saved = json.loads(T._openssl(["-d"], f.read(), key).decode())
            if saved.get("access_token"):
                cfg.update(access_token=saved["access_token"],
                           token_updated=saved.get("token_updated", ""))
        except Exception as e:  # noqa: BLE001
            log("ig_token.enc を読めなかったので Secrets のトークンを使う: %s" % e)
    return cfg


def refresh_token(cfg):
    upd = cfg.get("token_updated")
    if upd:
        try:
            if datetime.now(JST) - datetime.strptime(upd, "%Y-%m-%d").replace(tzinfo=JST) < timedelta(days=20):
                return cfg
        except ValueError:
            pass
    try:
        res = json.loads(T.fetch(GRAPH + "/refresh_access_token?grant_type=ig_refresh_token&access_token="
                                 + urllib.parse.quote(cfg["access_token"])))
        if res.get("access_token"):
            cfg["access_token"] = res["access_token"]
            cfg["token_updated"] = datetime.now(JST).strftime("%Y-%m-%d")
            data = json.dumps({"access_token": cfg["access_token"],
                               "token_updated": cfg["token_updated"]}).encode()
            with open(TOKEN_PATH, "wb") as f:
                f.write(T._openssl([], data, cfg["_key"]))
            log("Instagram トークンを更新した（有効期限 %s 秒）" % res.get("expires_in"))
    except RuntimeError as e:
        log("⚠ Instagram トークン更新に失敗: %s" % e)
    return cfg


# ---------------------------------------------------------------- 文面
def build_caption(article):
    _key, _topic, tags = T.pick_category(article)
    lead = T.first_sentence(article.get("body", ""), limit=140)
    out_tags = []
    for t in tags + BASE_TAGS:
        t = TAG_ALIAS.get(t, t)
        if t and t not in out_tags:
            out_tags.append(t)
    lines = [article["title"]]
    if lead:
        lines += ["", lead]
    lines += ["", "記事はプロフィールのリンク（wardrobe-lab.jp）から読めます。",
              "", " ".join(out_tags)]
    return "\n".join(lines)[:2200]


# ---------------------------------------------------------------- 画像を公開URLに置く
def git(*args):
    r = subprocess.run(["git"] + list(args), cwd=BASE, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), r.stderr.strip()[:300]))
    return r.stdout


def publish_image_file(path):
    """images/ を commit・push して raw URL を返す。古い画像（30日超）は消す"""
    os.makedirs(IMG_DIR, exist_ok=True)
    cutoff = (datetime.now(JST) - timedelta(days=30)).strftime("%Y%m%d")
    for name in os.listdir(IMG_DIR):
        if name[:8].isdigit() and name[:8] < cutoff:
            os.remove(os.path.join(IMG_DIR, name))
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("add", "-A", "images")
    if git("status", "--porcelain", "images").strip():
        git("commit", "-m", "chore: add instagram image")
        git("pull", "--rebase")
        git("push")
    rel = os.path.relpath(path, BASE).replace(os.sep, "/")
    url = "https://raw.githubusercontent.com/%s/%s/%s" % (REPO, BRANCH, rel)
    for _ in range(10):  # 反映待ち
        try:
            if len(fetch_bytes(url)) > 1000:
                return url
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    return url


# ---------------------------------------------------------------- Instagram API
def ig_user_id(token):
    me = json.loads(T.fetch(GRAPH + "/me?fields=user_id,username&access_token=" + urllib.parse.quote(token)))
    log("Instagram アカウント: @%s" % me.get("username"))
    return me.get("user_id") or me["id"]


def publish(token, image_url, caption):
    uid = ig_user_id(token)
    res = json.loads(T.fetch("%s/%s/media" % (GRAPH, uid),
                             data={"image_url": image_url, "caption": caption, "access_token": token}))
    cid = res["id"]
    for _ in range(12):
        st = json.loads(T.fetch("%s/%s?fields=status_code&access_token=%s"
                                % (GRAPH, cid, urllib.parse.quote(token))))
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError("コンテナ作成失敗: %s" % st)
        time.sleep(5)
    out = json.loads(T.fetch("%s/%s/media_publish" % (GRAPH, uid),
                             data={"creation_id": cid, "access_token": token}))
    return out.get("id")


# ---------------------------------------------------------------- 本体
def main():
    dry = "--dry-run" in sys.argv
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "new"
    cfg = load_token()
    if not cfg and not dry:
        log("IG_ACCESS_TOKEN が未設定のため、今日はスキップ")
        return 0

    state = T.load_json(STATE_PATH, {"posted": {}, "sitemap": [], "sitemap_at": ""})
    posted = state.setdefault("posted", {})
    today = datetime.now(JST).strftime("%Y-%m-%d")
    done_key = "last_%s_date" % mode
    log("Instagram モード: %s" % mode)
    if state.get(done_key) == today and not dry:
        log("今日はこのモードで投稿済みなので何もしない")
        return 0

    try:
        entries = T.parse_feed(T.fetch(FEED))
    except RuntimeError as e:
        log("⚠ フィード取得失敗: %s" % e)
        entries = []

    if mode == "new":
        picked = T.pick_new(entries, posted)
    elif mode == "backfill":
        picked = T.pick_backfill(state, SITE, entries, posted)
    else:
        log("不明なモード: %s" % mode)
        return 1
    if picked is None:
        log("投稿する記事なし")
        T.save_json(STATE_PATH, state)
        return 0

    article = page_meta(picked["url"])
    if not article["title"]:
        article["title"] = picked["title"]
    if not article["image"]:
        log("✗ アイキャッチ画像が見つからない: %s" % picked["url"])
        return 1
    caption = build_caption(article)
    log("対象: %s" % article["title"])
    log("キャプション:\n%s" % caption)

    img = make_image(fetch_bytes(article["image"]), article["title"], get_logo())
    name = "%s-%s%s.jpg" % (datetime.now(JST).strftime("%Y%m%d"),
                            hashlib.sha1(article["handle"].encode()).hexdigest()[:10],
                            "-preview" if dry else "")
    path = os.path.join(IMG_DIR, name)
    os.makedirs(IMG_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(img)
    url = publish_image_file(path)
    log("画像: %s" % url)

    if dry:
        log("--dry-run のため投稿しない")
        return 0

    cfg = refresh_token(cfg)
    try:
        mid = publish(cfg["access_token"], url, caption)
    except RuntimeError as e:
        log("✗ 投稿失敗: %s" % e)
        return 1

    now = datetime.now(JST).isoformat()
    posted[article["handle"]] = now
    state[done_key] = today
    state["last"] = {"handle": article["handle"], "url": article["url"], "media_id": mid,
                     "at": now, "mode": mode, "image": url}
    T.save_json(STATE_PATH, state)
    log("✓ Instagram 投稿完了 id=%s %s" % (mid, article["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
