"""namecanon: 把「中文译名」艺人名规范化回 MusicBrainz 实体的规范名。

背景/规则 (2026-10-07 用户确认):
- 西方等非东亚艺人被标成中文译名时（例：女神卡卡 / Lady Gaga、比莉艾莉什 / Billie Eilish、
  查莉 XCX / Charli XCX），统一改用 MB 实体的规范名。
- 中国 / 香港 / 台湾 / 日本 / 韩国 / 澳门 艺人保持原名不动 —— 简繁只管繁→简（simptrans），
  不把王菲改成 Faye Wong、不把放課後ティータイム改成 Sakurakou K-ON Bu。
- 判定依据 = MB 实体的 (a) 规范名是否含 CJK、(b) 实体国籍/地区是否在 skip_countries 内、
  (c) 本地名必须整体等于实体名或别名之一。只用「credit 是拉丁名」当信号会误伤一半以上的
  中/日艺人（王菲/Faye Wong、张学友/Jacky Cheung），因此必须查实体国籍。

安全闸门（2026-10-07 实测事故）:
- 专辑级只做整串替换，且要求本地名整体命中别名集合；否则 `Hardwell feat. 蔡依林`、
  `Luis Fonsi feat. 林俊杰`、`Lena Raine & 谷岡久美` 这类混合署名会被换成主艺人、丢掉中文合作者。
- 曲目级只在段内出现该实体的中文别名时替换该别名（feat. 场景），不做整串替换。

数据来源: MB artist API（按 mb_albumartistid 查；无 ID 时用别名精确匹配反查），结果缓存到
cache_file，缓存命中不发网络请求（每周流程常态为离线）。
触发: import_task 钩子 + `beet namecanon [query]`（每周 mbsync → simptrans 之后调用）。
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from beets import ui
from beets.plugins import BeetsPlugin

try:  # simptrans 的依赖, 用于繁简归一后匹配别名
    from zhconv import convert as _zh_convert
except Exception:  # pragma: no cover
    _zh_convert = None

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
PLACEHOLDER = re.compile(r"^\s*\[.*\]\s*$")  # MB 特殊艺人 [unknown]/[anonymous]/[dialogue]
# 非拉丁字母(西里尔/希腊/阿拉伯/韩文/中文...)：用户规则 = 非东亚艺人一律用拉丁字母名
NONLATIN_LETTER = re.compile(r"[^\W\d_A-Za-z\u00c0-\u024f]", re.UNICODE)
MB_API = "https://musicbrainz.org/ws/2/"
UA = "beets-namecanon/1.0 ( homelab )"
SEP = "\x00"

ALBUM_NAME_FIELDS = ("albumartist", "albumartists")
ALBUM_SORT_FIELDS = ("albumartist_sort", "albumartists_sort")
ITEM_NAME_FIELDS = ("artist", "artists", "albumartist", "albumartists")
ITEM_SORT_FIELDS = ("artist_sort", "artists_sort", "albumartist_sort", "albumartists_sort")

SKIP_AREA_NAMES = {
    "china", "taiwan", "hong kong", "japan", "south korea", "republic of korea",
    "korea", "macao", "macau",
}
WESTERN_LANGS = {
    "eng", "deu", "fra", "spa", "ita", "por", "nld", "swe", "nor", "dan", "fin",
    "pol", "rus", "ces", "ell", "lat", "cat", "hun", "tur", "ron", "ukr",
}


def has_cjk(s):
    return bool(s and CJK.search(s))


def has_nonlatin(s):
    """含非拉丁字母(中文/日文/西里尔/希腊/韩文...)"""
    return bool(s and NONLATIN_LETTER.search(s))


def is_latin(s):
    return bool(s) and not NONLATIN_LETTER.search(s)


def _as_text(v):
    """flex 字段在 Python API 里可能是 list；统一转成 '\x00' 串处理。"""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return SEP.join("" if x is None else str(x) for x in v)
    return v if isinstance(v, str) else str(v)


def _retype(orig, text):
    """按原类型写回（原来是 list 就写回 list）。"""
    return text.split(SEP) if isinstance(orig, (list, tuple)) else text


def _norm_conv(s):
    """繁→简（保留原大小写与空格，仅做繁简归一）。"""
    if not s or not _zh_convert:
        return s
    try:
        return _zh_convert(s, "zh-cn")
    except Exception:
        return s


def _norm_name(s):
    """别名匹配用的归一化：繁→简 + 去空白 + 小写（MB 别名常见繁体，本地是简体）。"""
    if not s:
        return ""
    return _norm_conv(s).strip().lower()


class NameCanonPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add({
            "cache_file": "/data/beets/no_mbid_analysis/artist_canon.json",
            "skip_countries": ["CN", "TW", "HK", "JP", "KR", "MO"],
            "move": True,
            "request_delay": 1.1,
            "search_fallback": True,
        })
        self.register_listener("import_task", self.on_import)
        self._cache = None
        self._dirty = False
        self._last_call = 0.0
        self._n_since_save = 0

    # ---------------- cache ----------------
    @property
    def cache(self):
        if self._cache is None:
            path = os.path.expanduser(self.config["cache_file"].get())
            try:
                with open(path, encoding="utf-8") as fh:
                    self._cache = json.load(fh)
            except Exception:
                self._cache = {}
        return self._cache

    def save_cache(self):
        if not self._dirty:
            return
        path = os.path.expanduser(self.config["cache_file"].get())
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.cache, fh, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, path)
            self._dirty = False
        except Exception as exc:
            self._log.warning("namecanon: cannot save cache: {}", exc)

    # ---------------- MusicBrainz ----------------
    def _get(self, path, params):
        delay = float(self.config["request_delay"].get())
        url = MB_API + path + "?" + urllib.parse.urlencode(params)
        for attempt in range(4):
            wait = delay - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=45) as resp:
                    self._last_call = time.time()
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                self._last_call = time.time()
                if exc.code in (429, 503) and attempt < 3:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
        return None

    def artist_info(self, mbid):
        """按 MBID 取实体信息(带缓存)。"""
        if not mbid:
            return None
        if mbid in self.cache:
            return self.cache[mbid]
        try:
            data = self._get(f"artist/{mbid}", {"inc": "aliases", "fmt": "json"})
        except Exception as exc:
            self._log.warning("namecanon: MB lookup failed for {}: {}", mbid, exc)
            return None
        info = self._slim(data)
        if info:
            self.cache[mbid] = info
            self._dirty = True
            self._n_since_save += 1
            if self._n_since_save >= 50:
                self.save_cache()
                self._n_since_save = 0
        return info

    def artist_by_alias(self, name):
        """无 MBID 时: 用别名精确匹配反查实体(要求本地名确为其别名之一)。"""
        if not self.config["search_fallback"].get():
            return None
        key = "search:" + name
        if key in self.cache:
            mbid = self.cache[key]
            return self.cache.get(mbid) if mbid else None
        result = None
        try:
            res = self._get("artist", {"query": f'alias:"{name}" OR artist:"{name}"',
                                       "limit": "3", "fmt": "json"})
            for cand in (res or {}).get("artists", []):
                if cand.get("score") != 100:
                    continue
                info = self.artist_info(cand["id"])
                if not info:
                    continue
                names = [a[0] for a in info.get("aliases", [])] + [info.get("name")]
                if name in names:
                    result = info
                    break
        except Exception as exc:
            self._log.warning("namecanon: MB search failed for {}: {}", name, exc)
        self.cache[key] = (result or {}).get("mbid", "")
        self._dirty = True
        return result

    def _slim(self, data):
        if not data or not data.get("id"):
            return None
        area = data.get("area") or {}
        return {
            "mbid": data["id"],
            "name": data.get("name") or "",
            "sort_name": data.get("sort-name") or data.get("name") or "",
            "type": data.get("type"),
            "country": (data.get("country") or "").upper(),
            "area": area.get("name") or "",
            "area_codes": [c.upper() for c in area.get("iso-3166-1-codes", [])],
            "aliases": [[a.get("name"), (a.get("locale") or ""), bool(a.get("primary"))]
                        for a in data.get("aliases", [])],
        }

    # ---------------- decision ----------------
    def _skip_country(self, info):
        skip = {c.upper() for c in self.config["skip_countries"].as_str_seq()}
        if (info.get("country") or "") in skip:
            return True
        if any(c in skip for c in info.get("area_codes", [])):
            return True
        return (info.get("area") or "").strip().lower() in SKIP_AREA_NAMES

    def _alias_names(self, info):
        """实体的所有可用名字(规范名/排序名/别名), 小写去空格。"""
        names = {(info.get("name") or "").strip().lower(),
                 (info.get("sort_name") or "").strip().lower()}
        for entry in info.get("aliases", []):
            if entry and entry[0]:
                names.add(entry[0].strip().lower())
        names.discard("")
        return names

    def preferred_name(self, info):
        """规范化目标名: 规范名是拉丁字母就用它; 非拉丁(西里尔等)优先取 primary en 别名。"""
        canon = info.get("name") or ""
        if not canon or is_latin(canon):
            return canon
        cands = [a for a in info.get("aliases", [])
                 if a and a[0] and is_latin(a[0]) and (a[1] or "").lower().startswith("en")]
        for a in cands:
            if a[2]:
                return a[0]
        return cands[0][0] if cands else canon

    def sort_name_for(self, info, target):
        sn = info.get("sort_name") or ""
        return sn if is_latin(sn) else target

    def canonical_for(self, local, info):
        """返回应改成的规范名；不需要改则返回 None。

        门槛(全部满足): MB 实体规范名不含 CJK、非占位符、国籍不在 skip 名单、
        本地名整体等于实体名或其别名之一(繁简/大小写归一)。
        目标名: 规范名为拉丁则用它，否则用 primary en 别名
        (柴可夫斯基 Пётр Ильич Чайковский → Pyotr Ilyich Tchaikovsky)。
        """
        if not has_nonlatin(local) or not info:
            return None
        canon = info.get("name") or ""
        if not canon or has_cjk(canon) or canon == local:
            return None
        if PLACEHOLDER.match(canon):
            return None
        if self._skip_country(info):
            return None
        if _norm_name(local) not in {_norm_name(n) for n in self._alias_names(info)}:
            return None
        target = self.preferred_name(info)
        return target if target and target != local else None

    def _flag_unresolved(self, album, local):
        """无 MBID 时判断是否值得报给人工（避免把中/日艺人全列出来）。"""
        if (album.get("language") or "").lower() in WESTERN_LANGS:
            return True
        credit = album.get("albumartists_credit") or ""
        return bool(credit) and not has_cjk(credit)

    # ---------------- apply ----------------
    def _save_item(self, it, stats):
        try:
            it.store()
            it.write()
            it.store()
            return True
        except Exception as exc:
            stats["write_failed"] = stats.get("write_failed", 0) + 1
            self._log.warning("namecanon: item {} write failed: {}", it.id, exc)
            return False

    def fix_album(self, album, stats):
        """规范化一张专辑(专辑级 + 曲目级)；返回改动处数。"""
        n = 0
        local = _as_text(album.get("albumartist"))
        info = None
        if has_nonlatin(local):
            mbid = album.get("mb_albumartistid") or ""
            if not mbid:
                for it in album.items():
                    mbid = it.get("mb_albumartistid") or ""
                    if mbid:
                        break
            info = self.artist_info(mbid) if mbid else self.artist_by_alias(local)
        new = self.canonical_for(local, info)
        if new:
            sort = self.sort_name_for(info, new)
            for f in ALBUM_NAME_FIELDS:
                orig = album.get(f)
                text = _as_text(orig)
                if text and local in text:
                    album[f] = _retype(orig, text.replace(local, new))
                    n += 1
            for f in ALBUM_SORT_FIELDS:
                if album.get(f):
                    album[f] = sort
            album.store()
            for it in album.items():
                if local not in (_as_text(it.get("artist")) + _as_text(it.get("albumartist"))):
                    continue
                for f in ITEM_NAME_FIELDS:
                    orig = it.get(f)
                    text = _as_text(orig)
                    if text and local in text:
                        it[f] = _retype(orig, text.replace(local, new))
                for f in ITEM_SORT_FIELDS:
                    if it.get(f):
                        it[f] = sort
                if self._save_item(it, stats):
                    n += 1
            if self.config["move"].get():
                try:
                    album.move()
                except Exception as exc:
                    self._log.warning("namecanon: album {} move failed: {}", album.id, exc)
            stats["changed_albums"] += 1
            stats["details"].append((album.id, local, new, album.get("album") or ""))
        else:
            stats["skipped"] += 1
            if has_nonlatin(local) and not info and self._flag_unresolved(album, local):
                stats["unresolved"].append((album.id, local, album.get("album") or ""))
        # 曲目级独立判定: 覆盖合辑 / 混合署名里被译名的曲目
        for it in album.items():
            n += self.fix_item(it, stats)
        return n

    def fix_item(self, it, stats):
        """曲目级: 按 mb_artistids 逐段处理（支持多艺人合作），把各实体的非拉丁名换成拉丁名。"""
        art = _as_text(it.get("artist"))
        arts_raw = _as_text(it.get("artists"))
        if not (has_nonlatin(art) or has_nonlatin(arts_raw)):
            return 0
        arts = arts_raw.split(SEP)
        ids = [x for x in _as_text(it.get("mb_artistids")).split(SEP) if x]
        if not ids and it.get("mb_artistid"):
            ids = [it.get("mb_artistid")]
        repls = set()  # (源串, 目标名)
        for idx, sid in enumerate(ids):
            info = self.artist_info(sid)
            canon = (info or {}).get("name") or ""
            if not info or not canon or has_cjk(canon) or self._skip_country(info):
                continue
            if PLACEHOLDER.match(canon):  # 占位艺人不动
                continue
            target = self.preferred_name(info)
            if not target:
                continue
            seg = arts[idx] if idx < len(arts) else art
            if has_nonlatin(seg) and _norm_name(seg) != _norm_name(target):
                repls.add((seg, target))
            # 该实体的其它非拉丁别名(MB 别名常为繁体)也一并替换
            for a in info.get("aliases", []):
                if not (a and a[0] and has_nonlatin(a[0])):
                    continue
                for variant in (a[0], _norm_conv(a[0])):
                    if variant and _norm_name(variant) != _norm_name(target):
                        repls.add((variant, target))
        if not repls:
            return 0
        pairs = sorted(repls, key=lambda x: -len(x[0]))
        changed = 0
        for f in ("artist", "artists", "artist_sort"):
            orig = it.get(f)
            text = _as_text(orig)
            if not text or not has_nonlatin(text):
                continue
            new = text
            for src, tgt in pairs:
                if src in new:
                    new = new.replace(src, tgt)
            if new != text:
                it[f] = _retype(orig, new)
                changed += 1
        if changed:
            self._save_item(it, stats)
        return changed

    # ---------------- hooks ----------------
    def on_import(self, session, task):
        if task.album is not None:
            stats = {"changed_fields": 0, "skipped": 0, "unresolved": [],
                     "write_failed": 0, "changed_albums": 0, "details": []}
            stats["changed_fields"] = self.fix_album(task.album, stats)
            self.save_cache()
            for aid, old, new, alb in stats["details"]:
                self._log.info("namecanon: [{0}] {1} → {2} ({3})", aid, old, new, alb)

    def commands(self):
        cmd = ui.Subcommand(
            "namecanon",
            help="normalize translated (Chinese) artist names to the MusicBrainz canonical name")
        cmd.func = self.run
        return [cmd]

    def run(self, lib, opts, args):
        query = ui.decargs(args)
        stats = {"changed_fields": 0, "skipped": 0, "unresolved": [], "write_failed": 0,
                 "changed_albums": 0, "details": []}
        for album in lib.albums(query):
            stats["changed_fields"] += self.fix_album(album, stats)
        self.save_cache()
        ui.print_(f"namecanon: 规范化 {stats['changed_fields']} 处 / {stats['changed_albums']} 张专辑"
                  f" (未判定 {len(stats['unresolved'])} 张)")
        for aid, old, new, alb in stats["details"]:
            ui.print_(f"   [{aid}] {old} → {new}  ({alb})")
        if stats["unresolved"]:
            ui.print_("   ⚠️ 无 MBID 无法判定(疑似中文译名, 请人工确认):")
            for aid, name, alb in stats["unresolved"][:8]:
                ui.print_(f"      [{aid}] {name} - {alb}")
