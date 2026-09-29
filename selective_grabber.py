#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 guibg
"""
selective_grabber.py — "Torrentio caseiro" para a stack Sonarr + qBittorrent + Prowlarr.

PROBLEMA
  Episódios antigos de anime quase só existem em packs/batches bem-semeados
  (ex.: [Judas], [Anime Time], [Lia]). O Sonarr não enxerga esses packs na busca
  por episódio e, mesmo se enxergasse, baixaria o pack inteiro (dezenas/centenas
  de GB). Já os releases individuais desses episódios costumam estar mortos.

SOLUÇÃO (por episódio monitorado e sem arquivo)
  1. procura no Prowlarr o melhor release que CONTÉM o episódio (prioriza seeders);
  2. adiciona o magnet direto no qBittorrent (categoria + tag próprias);
  3. assim que os metadados chegam, marca APENAS o(s) arquivo(s) do episódio para
     baixar (filePrio) e pula o resto do pack;
  4. quando o arquivo termina, importa no Sonarr via ManualImport (modo "copy",
     que faz HARDLINK — o torrent segue semeando, sem duplicar espaço).

CONFIG   /config/config.json   (ver config.example.json)
ESTADO   /config/state.json

Uso: python3 selective_grabber.py [--once]
"""
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.cookiejar import CookieJar

CFG_PATH = os.environ.get("SELECTIVE_GRABBER_CONFIG", "/config/config.json")
STATE_PATH = os.environ.get("SELECTIVE_GRABBER_STATE", "/config/state.json")


def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "[selective-grabber]", *a, flush=True)


def http(url, method="GET", headers=None, data=None, timeout=90):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            try:
                return r.status, (json.loads(body) if body else None)
            except Exception:
                return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, (json.loads(body) if body else None)
        except Exception:
            return e.code, body
    except Exception as e:
        return 0, str(e)


# --------------------------------------------------------------------------- clientes
class Sonarr:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.h = {"X-Api-Key": key}

    def get(self, path):
        return http(self.url + path, headers=self.h)[1]

    def post(self, path, obj):
        return http(self.url + path, method="POST",
                    headers={**self.h, "Content-Type": "application/json"},
                    data=json.dumps(obj).encode())

    def episodes(self, series_id):
        return self.get(f"/api/v3/episode?seriesId={series_id}") or []

    def queue_episode_ids(self):
        q = self.get("/api/v3/queue?pageSize=250") or {}
        return {r.get("episodeId") for r in q.get("records", []) if r.get("episodeId")}

    def manualimport(self, folder):
        q = urllib.parse.urlencode({"folder": folder, "filterExistingFiles": "false"})
        return self.get("/api/v3/manualimport?" + q) or []

    def command(self, obj):
        return self.post("/api/v3/command", obj)


class Prowlarr:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.h = {"X-Api-Key": key}

    def search(self, query, categories=None, limit=100):
        q = {"query": query, "limit": limit}
        if categories:
            q["categories"] = categories
        st, d = http(self.url + "/api/v1/search?" + urllib.parse.urlencode(q),
                     headers=self.h, timeout=120)
        return d if isinstance(d, list) else []


class Qbit:
    def __init__(self, url, user, pw):
        self.url = url.rstrip("/")
        self.cj = CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.op.addheaders = [("Referer", self.url + "/")]
        try:
            r = self.op.open(urllib.request.Request(
                self.url + "/api/v2/auth/login",
                data=urllib.parse.urlencode({"username": user, "password": pw}).encode(),
                method="POST"), timeout=30)
            log("qBittorrent:", r.read().decode().strip())
        except Exception as e:
            log("qBittorrent login FALHOU:", e)

    def _req(self, path, data=None, method="GET"):
        try:
            r = self.op.open(urllib.request.Request(self.url + path, data=data, method=method), timeout=60)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return 0, str(e).encode()

    def _json(self, path):
        st, b = self._req(path)
        try:
            return json.loads(b)
        except Exception:
            return None

    def post(self, path, params):
        return self._req(path, data=urllib.parse.urlencode(params).encode(), method="POST")

    def torrents(self):
        return self._json("/api/v2/torrents/info") or []

    def files(self, h):
        return self._json(f"/api/v2/torrents/files?hash={h}") or []

    def add(self, link, category, tags):
        return self.post("/api/v2/torrents/add", {"urls": link, "category": category, "tags": tags})

    def set_prio(self, h, ids, prio):
        if ids:
            self.post("/api/v2/torrents/filePrio",
                      {"hash": h, "id": "|".join(str(i) for i in ids), "priority": prio})

    def delete(self, h, delete_files=True):
        self.post("/api/v2/torrents/delete",
                  {"hashes": h, "deleteFiles": "true" if delete_files else "false"})


# --------------------------------------------------------------------------- matching
FOREIGN = re.compile(
    r"(?i)vostfr|vosta|sub[ _.-]?ita|\bita\b|italian|sub[ _.-]?esp|castellano|latino|\besp\b|"
    r"spanish|french|\bfr\b|turk|\btr\b|german|\bger\b|portugu|\bpor\b|korean|\bkor\b|chinese|\bchi\b")


def _nums(s):
    return [int(x) for x in re.findall(r"(?<![0-9A-Za-z])(\d{2,4})(?![0-9A-Za-z])", s)]


def _ranges(s):
    return [(int(a), int(b)) for a, b in re.findall(r"(?<!\d)(\d{2,4})\s*[-~]\s*(\d{2,4})(?!\d)", s)]


def has_episode(text, absn, season=None, episode=None, allow_ranges=True):
    """True se `text` referencia o episódio.

    allow_ranges=True  -> usado em TÍTULOS de release (ex.: "... 0001-1071 ..." cobre o ep).
    allow_ranges=False -> usado em NOMES DE ARQUIVO (um arquivo = um episódio; um range
                          no caminho do pack casaria com tudo e não deve valer).
    """
    if season is not None and episode is not None:
        if re.search(rf"(?i)\bS{season:02d}E{episode:02d}\b", text) or \
           re.search(rf"(?<!\d){season}x{episode:02d}(?!\d)", text):
            return True
    if absn is not None:
        if absn in _nums(text):
            return True
        if allow_ranges:
            for a, b in _ranges(text):
                if a <= absn <= b:
                    return True
    return False


def magnet_hash(link):
    m = re.search(r"btih:([0-9a-fA-F]{40})", link or "")
    return m.group(1).lower() if m else None


def pick_link(r, prowlarr_url):
    for k in ("guid", "magnetUrl", "downloadUrl"):
        v = r.get(k)
        if v and isinstance(v, str) and v.startswith("magnet:"):
            return v
    netloc = urllib.parse.urlparse(prowlarr_url).netloc
    for k in ("magnetUrl", "downloadUrl"):
        v = r.get(k)
        if v and isinstance(v, str) and v.startswith("http"):
            return v.replace("://localhost", "://" + netloc).replace("://127.0.0.1", "://" + netloc)
    return None


def choose(releases, ep, series, tried):
    """Escolhe o release com mais seeders que contém o episódio (desempate por grupo)."""
    absn = ep.get("absoluteEpisodeNumber")
    sn, en = ep.get("seasonNumber"), ep.get("episodeNumber")
    pref = [g.lower() for g in series.get("preferred_groups", [])]
    min_seed = series.get("min_seeders", 1)
    best, best_score = None, None
    for r in releases:
        t = r.get("title") or ""
        if not t or r.get("guid") in tried:
            continue
        if not has_episode(t, absn, sn, en):
            continue
        if FOREIGN.search(t):
            continue
        seeds = r.get("seeders") or 0
        if seeds < min_seed:
            continue
        grp = 1 if any(g in t.lower() for g in pref) else 0
        score = (seeds, grp)
        if best_score is None or score > best_score:
            best_score, best = score, r
    return best


# --------------------------------------------------------------------------- estado
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- ações
def select_files(qbit, h, info, timeout, zero_others=True):
    """Espera os metadados e marca só o arquivo do episódio para baixar.

    zero_others=True  -> zera todos os outros arquivos (use ao ADICIONAR um pack novo,
                         senão o pack inteiro baixaria).
    zero_others=False -> apenas habilita o arquivo do episódio, sem mexer nos demais
                         (use em packs já inicializados, p/ não desabilitar episódios
                         que já havíamos habilitado antes).
    """
    def idx(f):
        return f.get("index", f.get("id"))

    for _ in range(timeout):
        files = qbit.files(h)
        if files:
            target = [f for f in files if has_episode(f["name"], info.get("abs"),
                                                       info.get("season"), info.get("episode"),
                                                       allow_ranges=False)]
            if not target:
                log(f"nenhum arquivo do episódio em '{info['title'][:45]}' -> removendo torrent")
                qbit.delete(h)
                return False
            keep = {idx(f) for f in target}
            if zero_others:
                qbit.set_prio(h, [idx(f) for f in files if idx(f) not in keep], 0)
            qbit.set_prio(h, list(keep), 1)
            info["selected"] = True
            log(f"selecionado {[f['name'][-38:] for f in target]} "
                f"({len(keep)}/{len(files)} arquivos) em '{info['title'][:35]}'")
            return True
        time.sleep(1)
    return False


def do_import(sonarr, qbit, info):
    t = next((x for x in qbit.torrents() if x["hash"].lower() == info["hash"].lower()), None)
    if not t:
        return False
    files = qbit.files(info["hash"])
    target = [f for f in files if has_episode(f["name"], info.get("abs"),
                                              info.get("season"), info.get("episode"),
                                              allow_ranges=False)]
    if not target:
        return False
    tf = target[0]
    save_path = (t.get("save_path") or "").rstrip("/")
    content = t.get("content_path") or (save_path + "/" + t.get("name", ""))
    # files[].name é relativo ao save_path (e já inclui a pasta-raiz do torrent)
    exact = save_path + "/" + tf["name"]
    folder = content if len(files) > 1 else os.path.dirname(exact)
    cands = sonarr.manualimport(folder)
    chosen = next((c for c in cands if c.get("path") == exact), None)
    if not chosen:
        chosen = next((c for c in cands if has_episode(c["path"].split("/")[-1], info.get("abs"),
                                                        info.get("season"), info.get("episode"),
                                                        allow_ranges=False)), None)
    if not chosen and len(cands) == 1:
        chosen = cands[0]
    if not chosen:
        log(f"import: sem candidato p/ epId {info['episode_id']} (folder={folder})")
        return False
    entry = dict(chosen)
    entry["seriesId"] = info["series_id"]
    entry["episodeIds"] = [info["episode_id"]]
    st, _ = sonarr.command({"name": "ManualImport", "importMode": "copy", "files": [entry]})
    log(f"import disparado ({st}) epId {info['episode_id']}: {exact.split('/')[-1][:50]}")
    return st in (200, 201, 202)


# --------------------------------------------------------------------------- ciclo
def run_once(cfg, state):
    sonarr = Sonarr(cfg["sonarr_url"], cfg["sonarr_api_key"])
    prowlarr = Prowlarr(cfg["prowlarr_url"], cfg["prowlarr_api_key"])
    qbit = Qbit(cfg["qbittorrent_url"], cfg["qbittorrent_user"], cfg["qbittorrent_password"])
    cat = cfg.get("category", "anime")
    tag = cfg.get("tag", "selective-grabber")
    retry_s = cfg.get("retry_hours", 6) * 3600
    now = time.time()

    grabbed = state.setdefault("grabbed", {})
    tried = state.setdefault("tried", {})

    # cache de episódios por série
    ep_cache = {}
    for s in cfg.get("series", []):
        eps = sonarr.episodes(s["sonarr_series_id"])
        ep_cache[s["sonarr_series_id"]] = {e["id"]: e for e in eps}

    torrents = {t["hash"].lower(): t for t in qbit.torrents()}
    files_cache = {}

    def tfiles(h):
        if h not in files_cache:
            files_cache[h] = qbit.files(h)
        return files_cache[h]

    def target_file(h, info):
        for f in tfiles(h):
            if has_episode(f["name"], info.get("abs"), info.get("season"),
                           info.get("episode"), allow_ranges=False):
                return f
        return None

    # 1) acompanha o que já foi pego: seleciona / importa / limpa mortos
    for key, info in list(grabbed.items()):
        h = (info.get("hash") or "").lower()
        t = torrents.get(h)
        ep = ep_cache.get(info.get("series_id"), {}).get(info.get("episode_id"))
        if ep and ep.get("hasFile"):
            del grabbed[key]
            continue
        if not t:
            log(f"torrent sumiu p/ {key} -> re-tentando")
            del grabbed[key]
            continue
        if not info.get("selected"):
            init = state.setdefault("packs", {})
            if select_files(qbit, h, info, cfg.get("selection_timeout_seconds", 60),
                            zero_others=not init.get(h)):
                init[h] = True
        if info.get("selected") and not info.get("imported"):
            tf = target_file(h, info)
            if tf and tf.get("progress", 0) >= 1.0:
                if do_import(sonarr, qbit, info):
                    info["imported"] = True
        if not info.get("imported") and now - info.get("ts", now) > retry_s:
            tf = target_file(h, info)
            if not tf or tf.get("progress", 0) < 0.02:
                log(f"{key}: sem progresso há {cfg.get('retry_hours', 6)}h -> removendo p/ tentar outro release")
                qbit.delete(h)
                tried.setdefault(key, []).append(info.get("guid"))
                del grabbed[key]

    # 2) pega episódios faltantes
    queued = sonarr.queue_episode_ids()
    for s in cfg.get("series", []):
        sid = s["sonarr_series_id"]
        eps = list(ep_cache.get(sid, {}).values())
        wanted = [e for e in eps if e.get("monitored") and not e.get("hasFile") and e.get("id") not in queued]
        prio = set(s.get("priority_abs", []))
        wanted.sort(key=lambda e: (0 if e.get("absoluteEpisodeNumber") in prio else 1,
                                   e.get("seasonNumber", 0), e.get("episodeNumber", 0)))
        grabs = 0
        for e in wanted:
            if grabs >= cfg.get("max_grabs_per_run", 2):
                break
            key = f"{sid}:{e['id']}"
            if key in grabbed:
                continue
            absn, sn, en = e.get("absoluteEpisodeNumber"), e.get("seasonNumber"), e.get("episodeNumber")

            # já temos algum torrent nosso com esse episódio? só habilita o arquivo
            existing = None
            for t in torrents.values():
                if tag in (t.get("tags") or ""):
                    if any(has_episode(f["name"], absn, sn, en, allow_ranges=False) for f in tfiles(t["hash"])):
                        existing = t
                        break
            if existing:
                info = {"hash": existing["hash"], "abs": absn, "season": sn, "episode": en,
                        "series_id": sid, "episode_id": e["id"], "title": existing.get("name", ""),
                        "guid": None, "ts": now, "selected": False}
                grabbed[key] = info
                init = state.setdefault("packs", {})
                eh = existing["hash"].lower()
                if select_files(qbit, eh, info, 10, zero_others=not init.get(eh)):
                    init[eh] = True
                grabs += 1
                continue

            releases = []
            for q in (s.get("title", ""), s.get("title", "") + " batch"):
                releases += prowlarr.search(q, s.get("search_categories"), 100)
            pick = choose(releases, e, s, set(tried.get(key, [])))
            if not pick:
                continue
            link = pick_link(pick, cfg["prowlarr_url"])
            h = magnet_hash(link)
            if not link or not h:
                continue
            log(f"pega {key} (abs {absn}): {pick.get('title','')[:55]} | seeds={pick.get('seeders')}")
            qbit.add(link, cat, tag)
            torrents = {t["hash"].lower(): t for t in qbit.torrents()}
            info = {"hash": h, "abs": absn, "season": sn, "episode": en, "series_id": sid,
                    "episode_id": e["id"], "title": pick.get("title", ""), "guid": pick.get("guid"),
                    "ts": now, "selected": False}
            grabbed[key] = info
            if select_files(qbit, h, info, cfg.get("selection_timeout_seconds", 60), zero_others=True):
                state.setdefault("packs", {})[h] = True
            grabs += 1

    save_json(STATE_PATH, state)


def main():
    once = "--once" in sys.argv
    cfg = load_json(CFG_PATH, None)
    if not cfg:
        log("config não encontrada:", CFG_PATH)
        sys.exit(1)
    state = load_json(STATE_PATH, {})
    poll = cfg.get("poll_seconds", 300)
    log("iniciado. poll =", poll, "s | config =", CFG_PATH)
    while True:
        try:
            run_once(cfg, state)
        except Exception as e:
            log("erro no ciclo:", repr(e))
        if once:
            break
        time.sleep(poll)


if __name__ == "__main__":
    main()
