"""Postmuxarr — backend (anciennement Remuxer).

Deux usages :
- Remux : pilote bdinfo, 7z, mkvmerge, mkvpropedit, mediainfo et qBittorrent pour produire des remux Blu-ray.
- Repost : reprend des releases déjà en seed (inventaire lu dans qBittorrent) — Blu-ray Original, remux, rip, mHD,
  WEB-DL —, vérifie HD-Forever par son API, puis cross-seede ou prépare l'upload (torrent + MediaInfo ou BDInfo
  + userscript). Les fichiers des autres seeds ne sont jamais modifiés.

Tous les traitements longs sont des jobs exécutés côté serveur, un à la fois :
le navigateur peut être fermé, le travail continue.
"""
import base64
import collections
import hashlib
import hmac
import html
import http.cookiejar
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/config.yml"))


def _paths_cfg() -> dict:
    """Section paths de config.yml, lue au démarrage : un restart suffit après modification."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("paths") or {}
    except (OSError, yaml.YAMLError):
        return {}


_PATHS = _paths_cfg()
SOURCE_ROOT = Path(os.environ.get("SOURCE_ROOT", "/volume1/media/downloads")).resolve()
# config.yml (paths.output) prioritaire sur la variable d'environnement
OUTPUT_DIR = Path(_PATHS.get("output") or os.environ.get("OUTPUT_DIR", "/volume1/media/remux")).resolve()
START_DIR = Path(_PATHS.get("start") or SOURCE_ROOT).resolve()
PLEX_DIR = Path(os.environ.get("PLEX_DIR", "/volume1/media/films")).resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
EXTRACT_DIR = OUTPUT_DIR / ".extract"
BDINFO_DIR = DATA_DIR / "bdinfo"
REPOST_TORRENTS = DATA_DIR / "torrents"   # .torrent des reposts : jamais à côté des fichiers d'un autre seed
APP_DIR = Path(__file__).parent
try:  # numéro de version : app/VERSION, mis à jour par scripts/release.sh (le tag Git vX.Y.Z doit correspondre)
    APP_VERSION = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip() or "dev"
except OSError:
    APP_VERSION = "dev"
DB_PATH = DATA_DIR / "jobs.db"
LOG_LINES = 400
BIG_FILE = 50 * 1024 * 1024               # en dessous : nfo, sample, srt…
TMDB_BASE = os.environ.get("TMDB_BASE", "https://api.themoviedb.org/3")
# sans locale UTF-8, mkvmerge -J tronque sa sortie au premier caractère accentué
ENV = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}

app = FastAPI(title="Postmuxarr", version=APP_VERSION)
db_lock = threading.Lock()


class Cancelled(Exception):
    pass


# =========================================================================== utilitaires

def load_config() -> dict:
    """config.yml, puis les connexions réglées dans l'interface par-dessus."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return apply_conn(cfg, conn_settings())


def conn_settings() -> dict:
    """Connexions enregistrées depuis l'onglet Réglages ({} si aucune, ou base pas encore créée)."""
    try:
        rows = db_rows("SELECT value FROM settings WHERE key='connections'")
    except Exception:
        return {}
    return json.loads(rows[0]["value"]) if rows else {}


def apply_conn(cfg: dict, c: dict) -> dict:
    """Réglages de l'interface prioritaires ; secrets vides = repli sur .env (QBT_API_KEY, QBT_PASS)."""
    if not c:
        return cfg
    q = dict(cfg.get("qbittorrent") or {})
    for src, dst in (("qbt_url", "url"), ("qbt_user", "username"), ("path_map", "path_map"),
                     ("qbt_category", "category"), ("qbt_tags", "tags")):
        if src in c:
            q[dst] = c[src]
    for src, dst in (("qbt_key", "api_key"), ("qbt_pass", "password")):
        if c.get(src):
            q[dst] = c[src]
    r = dict(cfg.get("repost") or {})
    if "repost_category" in c:
        r["category"] = c["repost_category"]
    return {**cfg, "qbittorrent": q, "repost": r}


SVC_SECRETS = {"tmdb_key": "TMDB_API_KEY", "hdf_key": "HDF_API_KEY"}


def svc() -> dict:
    """Clés et services réglés dans l'onglet Réglages ({} si aucun, ou base pas encore créée)."""
    try:
        rows = db_rows("SELECT value FROM settings WHERE key='services'")
    except Exception:
        return {}
    return json.loads(rows[0]["value"]) if rows else {}


def tmdb_key() -> str:
    return (svc().get("tmdb_key") or os.environ.get("TMDB_API_KEY", "")).strip()


def hdf_key() -> str:
    return (svc().get("hdf_key") or os.environ.get("HDF_API_KEY", "")).strip().strip("'\"").strip()  # guillemets recopiés


def within(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def media_roots() -> list:
    """Dossiers du NAS partagés avec qBittorrent (clés de qbittorrent.path_map) : là où vivent les seeds."""
    pm = ((load_config().get("qbittorrent") or {}).get("path_map") or {})
    return [Path(h).resolve() for h in pm]


def readable(p: str, wide: bool = False) -> Path:
    """Chemin lisible : sources, dossier de sortie, extractions ; wide = aussi tout le stockage de qBittorrent."""
    path = Path(p).resolve()
    roots = [SOURCE_ROOT, OUTPUT_DIR] + (media_roots() if wide else [])
    if not any(within(path, r) for r in roots):
        raise HTTPException(400, f"Chemin hors des dossiers autorisés : {p}")
    if not path.exists():
        raise HTTPException(404, f"Introuvable : {p}")
    return path


def editable(p: Path) -> bool:
    """Seuls les MKV du dossier remux (hors extractions et hardlinks de repost) peuvent être modifiés en place."""
    return (p.suffix.lower() == ".mkv" and within(p, OUTPUT_DIR) and not within(p, EXTRACT_DIR)
            and not within(p, repost_cfg()["link_dir"]))


def run_json(cmd: list, timeout: int = 180) -> dict:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise HTTPException(500, f"{cmd[0]} : sortie illisible ({r.stderr.strip()[:300]})")


def identify(path: Path) -> dict:
    return run_json(["mkvmerge", "-J", str(path)])


def mediainfo(path: Path) -> list:
    try:
        j = run_json(["mediainfo", "--Output=JSON", str(path)], timeout=300)
        return j.get("media", {}).get("track", []) or []
    except Exception:
        return []


def guess_title(name: str):
    m = re.search(r"^(.*?)[.\s_(\[-]+((?:19|20)\d{2})(?=[.\s_)\]-]|$)", name)
    if m:
        return m.group(1).replace(".", " ").replace("_", " ").strip(), m.group(2)
    return name.replace(".", " ").replace("_", " ").strip(), ""


def disc_meta_title(root: Path) -> str:
    """Titre gravé dans le disque (BDMV/META/DL/bdmt_fra.xml…), souvent plus lisible que le nom du dossier
    (« JE_SAIS_RIEN_MAIS_@JD »). Français d'abord, puis anglais, puis le premier venu."""
    dl = root / "BDMV" / "META" / "DL"
    try:
        files = sorted(dl.glob("bdmt_*.xml"), key=lambda f: {"bdmt_fra.xml": 0, "bdmt_eng.xml": 1}.get(f.name.lower(), 2))
        for f in files:
            m = re.search(r"<di:name>\s*([^<]+?)\s*</di:name>", f.read_text(encoding="utf-8", errors="replace"))
            if m and len(m.group(1)) > 2 and not re.fullmatch(  # titres génériques des disques : ignorés
                    r"(?i)(blu-?ray|bd|dvd)?\W*(disc|disque|title|titre|volume|vol)?\W*\d*", m.group(1)):
                return html.unescape(m.group(1))
    except OSError:
        pass
    return ""


def title_hint(path: Path):
    """(titre, année) d'un disque ou d'une release : titre gravé dans le disque s'il existe, sinon le nom,
    débarrassé des mots parasites des étiquettes de disque (« @JD », « DISC1 »…)."""
    root = path if path.is_dir() else None
    name = path.name if root else path.stem
    title, year = guess_title(name)
    meta = disc_meta_title(root) if root else ""
    if meta:
        t2, y2 = guess_title(meta)
        return t2 or title, year or y2
    words = [w for w in title.split() if re.fullmatch(r"[\w'’\-:,&!?]+", w) and not re.fullmatch(r"(?i)disc\d*|d\d", w)]
    return " ".join(words) or title, year


def disc_key(path: Path) -> str:
    return hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:16]


def disc_root(src: Path) -> Path:
    """Dossier du disque (contenant BDMV) pour une playlist, sinon le chemin lui-même."""
    return src.parents[2] if src.suffix.lower() == ".mpls" else src


def mediainfo_text(mkv: Path, source_label: str = "", header: str = "") -> str:
    """Sortie mediainfo sans le chemin dans « Complete name », précédée d'un en-tête (REPOST) ou de la release source."""
    r = subprocess.run(["mediainfo", str(mkv)], capture_output=True, text=True, timeout=600, env=ENV)
    txt = re.sub(r"^(Complete name\s*:\s*).*/", r"\1", r.stdout, flags=re.M).strip()
    top = header.strip() or (f"Source : {source_label.strip()}" if source_label.strip() else "")
    return (f"{top}\n\n" if top else "") + txt + "\n"


def release_meta(mkv: Path) -> dict:
    rows = db_rows("SELECT source_label, tmdb_id FROM releases WHERE path=?", (str(mkv),))
    return rows[0] if rows else {"source_label": "", "tmdb_id": None}


def remember(mkv: Path, label: str, tmdb_id):
    if label or tmdb_id:
        db_exec("INSERT OR REPLACE INTO releases (path, source_label, tmdb_id) VALUES (?,?,?)", (str(mkv), label, tmdb_id))


def chapter_name(template: str, n: int) -> str:
    return re.sub(r"<NUM:(\d+)>", lambda m: str(n).zfill(int(m.group(1))), template).replace("<NUM>", str(n))


def token(pat: str, text: str):
    return re.search(rf"(?<![A-Za-z0-9])(?:{pat})(?![A-Za-z0-9])", text, re.I)


def release_safe(s: str) -> str:
    """Même règle que les noms de release de l'onglet Remux : ni accents, ni apostrophes (→ point), ni espaces."""
    for a, b in (("œ", "oe"), ("Œ", "Oe"), ("æ", "ae"), ("Æ", "Ae"), ("ß", "ss")):
        s = s.replace(a, b)
    s = "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))
    s = re.sub(r"[’'\s_]+", ".", s)
    s = re.sub(r"[^A-Za-z0-9.\-]", "", s)
    s = re.sub(r"(?<!\.)\.\.(?!\.)", ".", s)
    s = re.sub(r"\.{4,}", "...", s)
    return s.strip(".")


def is_series(name: str) -> bool:
    return bool(token(r"S\d{1,2}E\d{1,3}|S\d{2}|Saison\.?\d+|Season\.?\d+", name))


# Catégories de repost : clé → valeur « Média » de HDF. Le rip et le mHD sont des encodes, le reste est intact.
REL_KINDS = {"original": "Blu-ray Original", "remux": "Blu-ray Remux", "rip": "Blu-ray Rip", "mhd": "mHD", "webdl": "WEB-DL"}
STRICT_KINDS = ("original", "remux")  # doublon bloquant (sauf team d'exception) ; les autres : alerte, arbitrage manuel


# ------------------------------------------------------------------ règles d'upload HD-Forever (nom et structure)
HDF_EXEMPT_TEAM = "HDForever"             # dérogation au doublon : reposts de la team interne HDForever seulement
MHD_TEAMS = ("XSHD", "Psaro", "RiFiFi")   # seules teams autorisées en mHD (règles d'upload 4.5)
HDF_BANNED = [  # (motif cherché après l'année, raison) — règles d'upload 3 et 5
    (r"DVD(?:Rip|R|5|9|SCR)?|DVD-?REMUX|NTSC|PAL", "DVD : interdit (HD seulement)"),
    (r"(?:HD)?CAM|(?:HD)?TS|TELESYNC|TC|TELECINE|SCR|SCREENER|R5", "CAM, TS, TC, Screener, R5 : interdits"),
    (r"TVRip|HDTV(?:Rip)?|PDTV|SDTV", "TVRip, HDTV : interdits"),
    (r"WEB-?Rip", "WEBRip : interdit"),
    (r"Upscaled?|AI\.?Upscaled?", "vidéo upscalée : interdite"),
    (r"MP3|WMA", "audio MP3 ou WMA : interdit"),
]
SEASON_EP = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})E(\d{1,3})(?![0-9])", re.I)

# ------------------------------------------------------------------ critères modifiables (onglet Réglages)
# Valeurs par défaut : règles d'upload HDF d'octobre 2026. Seules les différences sont enregistrées (settings.hdf_rules).
RULES_DEFAULT = {
    "release_tag": "",                       # vide = group de config.yml (stocké à part : settings.release_tag)
    "exempt_team": "HDForever",              # règle 1.2 : reposts tolérés malgré un doublon (défaut : repost.team)
    "size_tolerance": 0.10,                  # doublon remux/FULL : taille à ± 10 % (défaut : repost.size_tolerance)
    "banned": [
        {"terms": ["DVD", "DVDRip", "DVDR", "DVD-R", "DVD5", "DVD9", "DVDSCR", "DVD-REMUX", "DVDREMUX", "NTSC", "PAL"],
         "reason": "DVD : interdit (HD seulement)"},
        {"terms": ["CAM", "HDCAM", "TS", "HDTS", "TELESYNC", "TC", "TELECINE", "SCR", "SCREENER", "R5"],
         "reason": "CAM, TS, TC, Screener, R5 : interdits"},
        {"terms": ["TVRip", "HDTV", "HDTVRip", "PDTV", "SDTV"], "reason": "TVRip, HDTV : interdits"},
        {"terms": ["WEBRip", "WEB-Rip"], "reason": "WEBRip : interdit"},
        {"terms": ["Upscale", "Upscaled", "AI.Upscale", "AI.Upscaled"], "reason": "vidéo upscalée : interdite"},
        {"terms": ["MP3", "WMA"], "reason": "audio MP3 ou WMA : interdit"},
    ],
    "refuse_720p": True,                     # règle 4.4 (encodes) et 3 (WEB-DL)
    "mhd_teams": ["XSHD", "Psaro", "RiFiFi"],  # règle 4.5
    # « résolution codec » : [débit min kbps, CRF max, débit min animation, CRF max animation] — règles 4.3, 4.6
    "enc": {"1080p x264": [8000, 19, 7000, 21], "1080p x265": [4500, 20, 4500, 22], "1080p AV1": [4000, 24, 4000, 24],
            "2160p x265": [18000, 18, 8000, 20], "2160p AV1": [12000, 22, 12000, 22]},
    "mhd": {"x264": [3000, 23, 3000, 24], "x265": [3000, 22, 3000, 23]},  # mHD 1080p, règle 4.5
    "web": {"1080p AVC": [3500, 2500], "1080p HEVC": [2500, 2500], "1080p AV1": [1800, 1800],
            "2160p HEVC": [11000, 8000], "2160p AV1": [8000, 8000]},     # WEB-DL : [min, min animation], règle 5
    "web_audio": {"AAC": 128, "AC3": 192, "EAC3": 192},                   # WEB-DL : débit audio minimum
    "transcode": {"5.1": 640, "7.1 EAC3": 768, "par canal": 96},           # encodes : AC3/EAC3 transcodé, règle 4.3
    "x264": {"subme": 7, "me_range": 16, "bframes": 6, "ref": 4, "level": 4.1},  # règles 1.3 et 4.3
    "ten_bits": ["x265", "AV1"],
    "audio_formats": ["TrueHD", "DTS-HD", "DTS", "EAC3", "AC3", "PCM", "FLAC", "AAC"],
    "aac_only_web": True,                    # AAC : WEB-DL, commentaires et audiodescriptions seulement
    "chapters": True,                        # chapitres obligatoires, sauf WEB-DL
    "extra_langs": ["en"],                   # langues audio admises sans alerte en plus de la VF et de la VO
}
_RULES = {"at": 0.0, "v": None}


def rules_base() -> dict:
    """Valeurs par défaut, complétées par config.yml (repost.team, repost.size_tolerance)."""
    rc = load_config().get("repost") or {}
    base = json.loads(json.dumps(RULES_DEFAULT))
    base["exempt_team"] = str(rc.get("team") or base["exempt_team"])
    base["size_tolerance"] = float(rc.get("size_tolerance", base["size_tolerance"]))
    return base


def hdf_rules() -> dict:
    """Critères en vigueur : valeurs par défaut + réglages enregistrés (cache de 5 s, vidé à chaque enregistrement)."""
    if _RULES["v"] is not None and time.time() - _RULES["at"] < 5:
        return _RULES["v"]
    r = rules_base()
    rows = db_rows("SELECT value FROM settings WHERE key='hdf_rules'")
    for k, v in (json.loads(rows[0]["value"]) if rows else {}).items():
        if k in r:
            r[k] = {**r[k], **v} if isinstance(r[k], dict) and isinstance(v, dict) else v
    tag = db_rows("SELECT value FROM settings WHERE key='release_tag'")
    r["release_tag"] = tag[0]["value"] if tag else ""
    _RULES.update(at=time.time(), v=r)
    return r


def clean_hdf_rules(d: dict, base: dict) -> dict:
    """Réglages reçus de la page, typés comme les valeurs par défaut ; vide = valeur par défaut."""
    def num(v, dflt):
        if v is None or str(v).strip() == "":
            return dflt
        return float(str(v).replace(",", "."))
    out = {}
    for k, dv in base.items():
        if k not in d or k == "release_tag":
            continue
        v = d[k]
        try:
            if isinstance(dv, bool):
                out[k] = bool(v)
            elif isinstance(dv, (int, float)):
                out[k] = num(v, dv)
            elif isinstance(dv, str):
                out[k] = str(v or "").strip()[:60] or dv
            elif k == "banned":
                out[k] = [{"terms": [str(t).strip()[:40] for t in r.get("terms") or [] if str(t).strip()][:40],
                           "reason": str(r.get("reason") or "").strip()[:200]} for r in v if isinstance(r, dict)][:40]
                out[k] = [r for r in out[k] if r["terms"]]
            elif isinstance(dv, list):
                out[k] = [str(x).strip()[:40] for x in v if str(x).strip()][:40]
            elif isinstance(dv, dict):
                out[k] = {kk: ([num(x, dv[kk][i]) for i, x in enumerate(v[kk][:len(dv[kk])])] if isinstance(dv[kk], list)
                               else num(v[kk], dv[kk])) for kk in dv if kk in v}
        except (TypeError, ValueError, AttributeError, KeyError):
            raise HTTPException(400, f"Valeur illisible dans les réglages : {k}")
    return {k: v for k, v in out.items() if v != base[k]}


def season_of(name: str):
    """Numéro de saison d'un nom (S01, S01E02, Saison 1…), None sinon."""
    m = token(r"S(\d{1,2})(?:E\d{1,3})?|Saison\.?(\d{1,2})|Season\.?(\d{1,2})", name or "")
    return int(next(g for g in m.groups() if g)) if m else None


def series_title(name: str):
    """(titre, année) d'un nom de série : ce qui précède la saison (« The.Office.US.S02.1080p… » → « The Office US »)."""
    m = re.search(r"^(.*?)[.\s_-]+(?:S\d{1,2}(?:E\d{1,3})?|Saison\.?\d+|Season\.?\d+)(?![A-Za-z0-9])", name or "", re.I)
    return guess_title(m.group(1) if m else name or "")


def files_shape(names: list, sizes: list) -> str:
    """Forme d'un torrent d'après ses fichiers (règle HDF 1.5 : un film, une saison complète, ou un disque) :
    mkv = un seul gros MKV, hors épisode ; season = plusieurs épisodes MKV d'une même saison ; episode = épisode seul ;
    pack = plusieurs saisons ; disc = dossier BDMV ou ISO ; other = le reste (plusieurs films, archives, AVI…)."""
    names = [(n or "").replace("\\", "/") for n in names]
    if any("BDMV/" in "/" + n for n in names):
        return "disc"
    big = [n for n, s in zip(names, sizes) if (s or 0) >= BIG_FILE]
    if len(big) == 1 and big[0].lower().endswith(".iso"):
        return "disc"
    if not big or not all(n.lower().endswith(".mkv") for n in big):
        return "other"
    eps = [SEASON_EP.search(posixpath.basename(n)) for n in big]
    if len(big) == 1:
        return "episode" if eps[0] else "mkv"
    if not all(eps):
        return "other"
    return "season" if len({int(m.group(1)) for m in eps}) == 1 else "pack"


def hdf_eligibility(name: str, shape: str, kind: str):
    """Repost possible sur HDF d'après le nom et la structure (règles d'upload 1.5, 3, 4.4, 4.5, 5 ; critères de
    l'onglet Réglages) : (« ok » | « check » | « refused », raisons). Ni les doublons ni le fichier ne sont vérifiés ici."""
    R = hdf_rules()
    bad, check = [], []
    if shape == "episode":
        bad.append("épisode seul : saison complète obligatoire")
    elif shape == "pack":
        bad.append("plusieurs saisons : un torrent par saison")
    elif shape == "other":
        bad.append("contenu non admis : film ou saison en MKV, ou disque BDMV/ISO")
    elif not shape:
        check.append("contenu inconnu (torrent absent de qBittorrent)")
    m = re.search(r"[.\s(\[_-](?:19|20)\d{2}[.\s)\]_-]+(.+)$", name or "")
    tail = m.group(1) if m else name or ""
    for row in R["banned"]:
        terms = [t for t in row.get("terms") or [] if t]
        if terms and token("|".join(re.escape(t) for t in terms), tail):
            bad.append(row.get("reason") or "terme interdit : " + ", ".join(terms))
    res = rel_res(name or "")
    if not res:
        check.append("résolution absente du nom" + (" : à lire dans le BDInfo" if shape == "disc" else " (SD interdit)"))
    elif res == "720p" and kind != "original" and R["refuse_720p"]:
        bad.append("720p interdit, sauf exception du staff si aucune source 1080p n'existe")
    if kind == "mhd":
        team = name.rsplit("-", 1)[1].strip() if "-" in name else ""
        if team.lower() not in {t.lower() for t in R["mhd_teams"]}:
            bad.append(f"mHD : seules les teams {', '.join(R['mhd_teams'])} sont autorisées")
    elif not kind:
        check.append("catégorie HDF indéterminée d'après le nom")
    bad = list(dict.fromkeys(bad))
    return ("refused" if bad else "check" if check else "ok"), bad + check


def link_target(ref: Path) -> Path:
    """Dossier des hardlinks des reposts (repost.link_dir) : sous un dossier de qBittorrent, même volume que ref."""
    link_dir = repost_cfg()["link_dir"]
    if not any(within(link_dir, r) for r in media_roots()):
        raise HTTPException(400, f"repost.link_dir ({link_dir}) doit être sous un dossier de qbittorrent.path_map")
    try:
        link_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(500, f"Impossible de créer {link_dir} ({e.strerror}) : "
                                 f"sudo mkdir -p {link_dir} && sudo chown 1026:100 {link_dir}")
    if os.stat(link_dir).st_dev != os.stat(ref).st_dev:
        raise HTTPException(400, f"{link_dir} n'est pas sur le même volume que la release : hardlink impossible")
    return link_dir


def season_dir(p: Path) -> bool:
    """Dossier de saison (épisodes MKV), par opposition à un disque Blu-ray en dossier."""
    try:
        return p.is_dir() and not has_disc(p) and any(x.suffix.lower() == ".mkv" for x in p.iterdir())
    except OSError:
        return False


def first_episode(p: Path) -> Path:
    return sorted(x for x in p.iterdir() if x.suffix.lower() == ".mkv")[0]


def rel_kind(name: str, disc: bool = False) -> str:
    """Catégorie d'une release d'après son nom (et sa forme : dossier BDMV ou ISO = disque complet).
    « webrip » = refusé par HDF ; « » = indéterminée (à choisir à la main), DVD compris (HDF : HD seulement)."""
    if disc:
        return "original"
    m = re.search(r"[.\s(\[_-](?:19|20)\d{2}[.\s)\]_-]+(.+)$", name)
    name = m.group(1) if m else name  # après l'année : un titre (« Light », « Web », « Complete »…) ne compte pas
    if token(r"DVD(?:Rip|R|5|9)?|NTSC|PAL|VOB", name) and not token(r"HD-?DVD", name):
        return ""
    if token(r"COMPLETE|BD25|BD50|BD66|BD100|BDMV|ISO|m2ts", name):
        return "original"
    if token(r"WEB-?Rip", name):
        return "webrip"
    if token(r"REMUX", name):
        return "remux"
    if token(r"mHD|HDLight|4KLight|Light", name):
        return "mhd"
    if token(r"WEB-?DL|WEB", name):
        return "webdl"
    if token(r"HD-?DVD", name):
        return ""
    if token(r"Blu-?Ray|BDRip|BRRip", name):
        if token(r"x26[45]|[HX]\.?26[45]|10bit|BDRip|BRRip", name):
            return "rip"
        if token(r"AVC|HEVC|VC-?1|MPEG-?2", name):  # codec vidéo du disque, sans remux ni encodeur : disque complet
            return "original"
        return "rip"
    return ""


def rel_res(name: str) -> str:
    m = token(r"720p|1080p|1080i|2160p", name)
    return m.group(0).lower() if m else "2160p" if token(r"4K|UHD", name) else ""


def rel_label(p: Path) -> str:
    """Nom d'une release sur disque : sans extension pour un fichier, tel quel pour un dossier."""
    return p.name if p.is_dir() else p.stem


# =========================================================================== codecs

AUDIO_CODECS = [  # (fragment du codec mkvmerge, nom de piste, nom de release) — l'ordre compte
    ("DTS-HD Master Audio", "DTS-HD MA", "DTS.HD.MA"),
    ("DTS-HD High Resolution", "DTS-HD HRA", "DTS.HD.HRA"),
    ("TrueHD", "TrueHD", "TrueHD"),
    ("E-AC-3", "DD+", "DDP"),
    ("AC-3", "AC3", "AC3"),
    ("DTS", "DTS", "DTS"),
    ("PCM", "LPCM", "LPCM"),
    ("FLAC", "FLAC", "FLAC"),
    ("AAC", "AAC", "AAC"),
    ("Opus", "Opus", "Opus"),
]
VIDEO_CODECS = [("AVC", "AVC"), ("HEVC", "HEVC"), ("VC-1", "VC-1"), ("MPEG-1/2", "MPEG-2"), ("AV1", "AV1")]
SUB_FORMATS = [("PGS", "PGS"), ("SubRip", "SRT"), ("SRT", "SRT"), ("VobSub", "VobSub"),
               ("TextST", "TextST"), ("ASS", "ASS"), ("SSA", "SSA")]


def lookup(table, codec):
    for row in table:
        if row[0].lower() in codec.lower():
            return row[1:]
    return None


def channels_str(mi: dict, n) -> str:
    layout = (mi.get("ChannelLayout") or "").split()
    if layout:
        lfe = sum(1 for x in layout if "LFE" in x.upper())
        return f"{len(layout) - lfe}.{lfe}"
    try:
        n = int(n or mi.get("Channels") or 0)
    except ValueError:
        n = 0
    return {6: "5.1", 8: "7.1", 7: "6.1"}.get(n, f"{n}.0" if n else "")


def match_mediainfo(tracks: list, mi_tracks: list):
    """Associe chaque piste mkvmerge à sa piste mediainfo (PID/numéro, sinon ordre par type)."""
    kinds = {"video": "Video", "audio": "Audio", "subtitles": "Text"}
    by_id = {}
    for m in mi_tracks:
        mid = str(m.get("ID", "")).split(" ")[0].split("-")[0]
        if mid.isdigit():
            by_id.setdefault((m.get("@type"), int(mid)), m)
    order = {k: [m for m in mi_tracks if m.get("@type") == v] for k, v in kinds.items()}
    seen = {k: 0 for k in kinds}
    res = {}
    for t in tracks:
        ty = t.get("type")
        if ty not in kinds:
            continue
        num = t.get("properties", {}).get("number")
        m = by_id.get((kinds[ty], num)) if num is not None else None
        if m is None and seen[ty] < len(order[ty]):
            m = order[ty][seen[ty]]
        seen[ty] += 1
        res[t["id"]] = m or {}
    return res


# =========================================================================== qBittorrent

class Qbt:
    def __init__(self, cfg: dict):
        q = cfg.get("qbittorrent") or {}
        self.url = (q.get("url") or "").rstrip("/")
        self.api_key = (q.get("api_key") or os.environ.get("QBT_API_KEY", "")).strip()   # qBittorrent >= 5.2.0
        self.user = q.get("username") or ""
        self.password = q.get("password") or os.environ.get("QBT_PASS", "")
        self.path_map = q.get("path_map") or {}
        self.category = q.get("category") or ""
        tags = q.get("tags") or []
        self.tags = ",".join(tags) if isinstance(tags, list) else str(tags)
        if not self.url:
            raise RuntimeError("qBittorrent non configuré : onglet Réglages (ou section qbittorrent de config.yml)")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _headers(self) -> dict:
        return {"Referer": self.url, "Origin": self.url,
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})}

    def _req(self, path, data=None, method=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(self.url + "/api/v2/" + path, data=body, method=method or ("POST" if body else "GET"),
                                     headers=self._headers())
        with self.opener.open(req, timeout=60) as r:
            return r.read()

    def json(self, path):
        return json.loads(self._req(path) or b"null")

    def login(self):
        if self.api_key:  # authentification sans état : pas de login/logout
            try:
                return self._req("app/version").decode()
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise RuntimeError("Clé API qBittorrent refusée (QBT_API_KEY)")
                raise
        if self.user or self.password:
            r = self._req("auth/login", {"username": self.user, "password": self.password}).decode()
            if "Ok" not in r:
                raise RuntimeError("Connexion à qBittorrent refusée (identifiants ?)")
        return self._req("app/version").decode()

    def trackers(self, h: str) -> list:
        return json.loads(self._req("torrents/trackers?hash=" + h))

    def reannounce(self, h: str):
        self._req("torrents/reannounce", {"hashes": h})

    def add_torrent(self, data: bytes, fields: dict) -> str:
        """Ajout d'un .torrent (multipart) : renvoie la réponse brute de qBittorrent (« Ok. » ou « Fails. »)."""
        b = uuid.uuid4().hex
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()]
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="torrents"; filename="repost.torrent"\r\n'
                     'Content-Type: application/x-bittorrent\r\n\r\n'.encode() + data + b"\r\n")
        parts.append(f"--{b}--\r\n".encode())
        req = urllib.request.Request(self.url + "/api/v2/torrents/add", data=b"".join(parts), method="POST",
                                     headers={**self._headers(), "Content-Type": f"multipart/form-data; boundary={b}"})
        with self.opener.open(req, timeout=60) as r:
            return r.read().decode(errors="replace").strip()

    def map_path(self, p: Path) -> str:
        """Chemin vu par le NAS → chemin vu par qBittorrent."""
        s = str(p)
        for host, cont in sorted(self.path_map.items(), key=lambda kv: -len(kv[0])):
            if s == host or s.startswith(host.rstrip("/") + "/"):
                return cont.rstrip("/") + s[len(host.rstrip("/")):]
        return s

    def unmap_path(self, s: str) -> str:
        """Chemin vu par qBittorrent → chemin vu par le NAS (et par ce conteneur)."""
        for host, cont in sorted(self.path_map.items(), key=lambda kv: -len(kv[1])):
            c = cont.rstrip("/")
            if s == c or s.startswith(c + "/"):
                return host.rstrip("/") + s[len(c):]
        return s


def add_ok(raw: str) -> tuple:
    """Réponse de torrents/add : « Ok. » (qBittorrent < 5) ou JSON (>= 5). Renvoie (succès, hashes ajoutés)."""
    raw = (raw or "").strip()
    if raw.lower().startswith("ok"):
        return True, []
    try:
        j = json.loads(raw)
    except ValueError:
        return False, []
    if not isinstance(j, dict):
        return False, []
    ok = not j.get("failure_count") and (j.get("success_count") or 0) + (j.get("pending_count") or 0) > 0
    return ok, [str(x) for x in j.get("added_torrent_ids") or []]


def qbt() -> Qbt:
    try:
        q = Qbt(load_config())
        q.login()
        return q
    except Exception as e:
        raise HTTPException(502, f"qBittorrent : {e}")


def info_span(data: bytes) -> tuple:
    """Position (début, fin) du dictionnaire info d'un .torrent, sans dépendance externe."""
    def parse(i):
        c = data[i:i + 1]
        if c == b"i":
            return data.index(b"e", i) + 1
        if c in (b"l", b"d"):
            i += 1
            while data[i:i + 1] != b"e":
                if c == b"d":
                    j = parse(i)
                    key = data[data.index(b":", i) + 1:j]
                    v0 = j
                    i = parse(j)
                    if key == b"info":
                        raise StopIteration((v0, i))
                else:
                    i = parse(i)
            return i + 1
        n = data.index(b":", i)
        return n + 1 + int(data[i:n])
    try:
        parse(0)
    except StopIteration as s:
        return s.value
    raise ValueError("dictionnaire info introuvable")


def bdecode(data: bytes):
    """Décodage bencode minimal (clés et chaînes en bytes)."""
    def dec(i):
        c = data[i:i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1:j]), j + 1
        if c == b"l":
            out, i = [], i + 1
            while data[i:i + 1] != b"e":
                v, i = dec(i)
                out.append(v)
            return out, i + 1
        if c == b"d":
            out, i = {}, i + 1
            while data[i:i + 1] != b"e":
                k, i = dec(i)
                out[k], i = dec(i)
            return out, i + 1
        n = data.index(b":", i)
        j = n + 1 + int(data[i:n])
        return data[n + 1:j], j
    return dec(0)[0]


def torrent_files(data: bytes) -> dict:
    """Fichiers d'un .torrent v1 ou hybride : {chemin relatif au dossier de destination : taille}."""
    info = bdecode(data)[b"info"]
    name = info[b"name"].decode("utf-8", "replace")
    if b"files" in info:  # multi-fichiers : tout est sous un dossier racine portant le nom du torrent
        return {posixpath.join(name, *(x.decode("utf-8", "replace") for x in f[b"path"])): f[b"length"]
                for f in info[b"files"] if b"attr" not in f or b"p" not in f[b"attr"]}  # sans les fichiers de bourrage
    if b"length" in info:
        return {name: info[b"length"]}
    raise ValueError("torrent v2 pur : liste de fichiers non lue")


def info_hash(data: bytes, fmt: str = "v1") -> str:
    """Identifiant du torrent dans qBittorrent : SHA-1 du dictionnaire info (v1, hybride), SHA-256 tronqué (v2 pur)."""
    a, b = info_span(data)
    if fmt == "v2":
        return hashlib.sha256(data[a:b]).hexdigest()[:40]
    return hashlib.sha1(data[a:b]).hexdigest()


# =========================================================================== API : config / navigation

def hdf_tracker(cfg: dict) -> dict:
    """Tracker de config.yml qui correspond à HD-Forever : celui qui a une upload_url."""
    trs = (cfg.get("torrent") or {}).get("trackers") or []
    return next((t for t in trs if t.get("upload_url")), trs[0] if trs else {})


def repost_cfg() -> dict:
    r = load_config().get("repost") or {}
    return {"category": r.get("category", "repost") or "",
            "hdf_api": (r.get("hdf_api") or "https://hdf.world/api.php").rstrip("/"),
            # hardlinks au nom nettoyé : le fichier de l'autre seed garde son nom
            "link_dir": Path(r.get("link_dir") or "/volume1/media/repost"),
            # doublon HDF : remux de même résolution et mêmes langues, taille à ± size_tolerance près
            "size_tolerance": hdf_rules()["size_tolerance"],
            # dérogation HDF au doublon : reposts de la team interne HDForever seulement, pas le tag de tes remux
            "team": hdf_rules()["exempt_team"],
            # userscripts des autres trackers : durée du cache des recherches HDF par film
            "lookup_ttl": int(r.get("lookup_ttl", 6 * 3600))}


# =========================================================================== accès (authentification facultative)

AUTH_OFF = os.environ.get("POSTMUXARR_NO_AUTH", "").strip().lower() in ("1", "true", "yes", "on")  # secours
_AUTH = {"at": 0.0, "v": None, "ok": set()}


def auth_cfg() -> dict:
    try:
        rows = db_rows("SELECT value FROM settings WHERE key='auth'")
    except Exception:
        return {}
    return json.loads(rows[0]["value"]) if rows else {}


def auth_state() -> dict:
    if _AUTH["v"] is None or time.time() - _AUTH["at"] > 5:
        _AUTH.update(v=auth_cfg(), at=time.time())
    return _AUTH["v"]


def auth_reset():
    _AUTH.update(v=None, ok=set())


def pw_hash(pw: str, salt: str = "") -> tuple:
    salt = salt or secrets.token_hex(16)
    return salt, hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


@app.middleware("http")
async def require_auth(request, call_next):
    """Identifiant + mot de passe (HTTP Basic) ou clé API (en-tête X-Api-Key, userscripts), si la protection est
    activée dans Réglages. Identifiants déjà vérifiés gardés en mémoire : le hachage n'est fait qu'une fois."""
    a = auth_state()
    if AUTH_OFF or not (a.get("user") and a.get("hash")):
        return await call_next(request)
    key = request.headers.get("x-api-key", "") or (
        request.query_params.get("key", "") if request.url.path.startswith("/userscripts/") else "")
    if key and a.get("api_key") and same(key, a["api_key"]):
        return await call_next(request)
    h = request.headers.get("authorization", "")
    tag = hashlib.sha256(h.encode()).hexdigest()
    if h and tag in _AUTH["ok"]:
        return await call_next(request)
    if h[:6].lower() == "basic ":
        try:
            u, _, p = base64.b64decode(h[6:]).decode("utf-8").partition(":")
        except Exception:
            u = p = ""
        if u and same(u, a["user"]) and same(pw_hash(p, a["salt"])[1], a["hash"]):
            _AUTH["ok"].add(tag)
            return await call_next(request)
    return Response("Authentification requise", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Postmuxarr", charset="UTF-8"'})


def auth_payload() -> dict:
    a = auth_cfg()
    on = bool(a.get("user") and a.get("hash"))
    return {"enabled": on, "user": a.get("user", ""), "api_key": a.get("api_key", "") if on else "", "forced_off": AUTH_OFF}


class AuthSpec(BaseModel):
    user: str = ""
    password: str = ""            # vide = inchangé (si déjà défini)


@app.get("/api/auth")
def api_auth_get():
    return auth_payload()


@app.put("/api/auth")
def api_auth_put(spec: AuthSpec):
    a, user = auth_cfg(), spec.user.strip()
    if not user or ":" in user or len(user) > 60:
        raise HTTPException(400, "Identifiant invalide (sans « : », 60 caractères au plus)")
    if spec.password:
        if len(spec.password) < 8:
            raise HTTPException(400, "Mot de passe trop court : 8 caractères minimum")
        salt, h = pw_hash(spec.password)
    elif a.get("hash"):
        salt, h = a["salt"], a["hash"]
    else:
        raise HTTPException(400, "Mot de passe requis pour activer la protection")
    new = {"user": user, "salt": salt, "hash": h, "api_key": a.get("api_key") or secrets.token_urlsafe(24)}
    db_exec("INSERT OR REPLACE INTO settings (key, value) VALUES ('auth', ?)", (json.dumps(new),))
    auth_reset()
    return auth_payload()


@app.post("/api/auth/apikey")
def api_auth_apikey():
    a = auth_cfg()
    if not a.get("hash"):
        raise HTTPException(409, "Protection désactivée : pas de clé API")
    a["api_key"] = secrets.token_urlsafe(24)
    db_exec("INSERT OR REPLACE INTO settings (key, value) VALUES ('auth', ?)", (json.dumps(a),))
    auth_reset()
    return auth_payload()


@app.delete("/api/auth")
def api_auth_delete():
    db_exec("DELETE FROM settings WHERE key='auth'")
    auth_reset()
    return auth_payload()


# =========================================================================== userscripts (servis avec adresse et clé)

USERSCRIPTS = APP_DIR / "userscripts"


def userscript_meta(f: Path) -> dict:
    head = f.read_text(encoding="utf-8").split("// ==/UserScript==")[0]
    get = lambda k: (re.search(rf"^//\s*@{k}\s+(.+)$", head, re.M) or [None, ""])[1].strip()  # noqa: E731
    return {"file": f.name, "name": get("name"), "description": get("description"), "version": get("version")}


def userscript_key() -> str:
    a = auth_cfg()
    return a.get("api_key", "") if a.get("hash") else ""


@app.get("/api/userscripts")
def api_userscripts():
    key = userscript_key()
    q = "?key=" + urllib.parse.quote(key) if key else ""
    return [{**userscript_meta(f), "url": f"/userscripts/{f.name}{q}"} for f in sorted(USERSCRIPTS.glob("*.user.js"))]


@app.get("/userscripts/{name}")
def userscript(name: str, request: Request):
    """Userscript prêt à installer : adresse de Postmuxarr (celle du navigateur) et clé API remplies. Accessible avec la
    clé en paramètre (?key=…) quand la protection est active : Violentmonkey s'en sert pour les mises à jour."""
    f = USERSCRIPTS / name
    if not re.fullmatch(r"[\w.-]+\.user\.js", name) or not f.is_file():
        raise HTTPException(404, "Userscript introuvable")
    base, key = str(request.base_url).rstrip("/"), userscript_key()
    q = "?key=" + urllib.parse.quote(key) if key else ""
    js = (f.read_text(encoding="utf-8").replace("__POSTMUXARR_URL__", base)
          .replace("__POSTMUXARR_HOST__", request.url.hostname or "localhost").replace("__API_KEY__", key)
          .replace("__SELF_URL__", f"{base}/userscripts/{name}{q}"))
    return PlainTextResponse(js, media_type="text/javascript; charset=utf-8")


@app.get("/api/version")
def api_version():
    return {"version": APP_VERSION}


@app.get("/api/config")
def api_config():
    cfg = load_config()
    cfg["version"] = APP_VERSION
    tag = db_rows("SELECT value FROM settings WHERE key='release_tag'")
    if tag:
        cfg["group"] = tag[0]["value"]  # tag choisi dans l'interface, prioritaire sur config.yml
    hdf_host = urllib.parse.urlparse(hdf_tracker(cfg).get("announce") or "").hostname or ""
    q = cfg.get("qbittorrent") or {}
    cfg["qbittorrent"] = {"url": q.get("url"), "configured": bool(q.get("url"))}  # jamais le mot de passe
    for t in (cfg.get("torrent") or {}).get("trackers") or []:
        t.pop("announce", None)  # l'URL contient la passkey : elle reste côté serveur
    cfg["tmdb"] = bool(tmdb_key())
    cfg["paths"] = {"sources": str(SOURCE_ROOT), "output": str(OUTPUT_DIR), "start": str(START_DIR), "plex": str(PLEX_DIR)}
    rc = repost_cfg()
    cfg["repost"] = {"arr": bool(arr_list()), "hdf_api": bool(hdf_key()),
                     "category": rc["category"], "hdf_host": hdf_host, "link_dir": str(rc["link_dir"]), "team": rc["team"]}
    return cfg


def has_disc(d: Path, depth: int = 2) -> bool:
    """Le dossier est un Blu-ray (BDMV/PLAYLIST) ou en contient un, ou un ISO, à 2 niveaux au plus."""
    if (d / "BDMV" / "PLAYLIST").is_dir():
        return True
    if depth <= 0:
        return False
    try:
        for e in d.iterdir():
            if e.name.startswith((".", "@", "#")):
                continue
            if e.is_file() and e.suffix.lower() == ".iso":
                return True
            if e.is_dir() and e.name.upper() not in ("BDMV", "CERTIFICATE") and has_disc(e, depth - 1):
                return True
    except OSError:
        return False
    return False


class TagSpec(BaseModel):
    tag: str


@app.post("/api/settings/tag")
def api_settings_tag(spec: TagSpec):
    """Tag des remux choisi dans l'interface, prioritaire sur group de config.yml."""
    tag = release_safe(spec.tag.strip())
    if not tag or len(tag) > 40:
        raise HTTPException(400, "Tag invalide")
    db_exec("INSERT OR REPLACE INTO settings (key, value) VALUES ('release_tag', ?)", (tag,))
    _RULES["v"] = None
    return {"tag": tag}


def rules_payload() -> dict:
    base = rules_base()
    return {"rules": hdf_rules(), "defaults": {**base, "release_tag": ""}, "config_group": load_config().get("group") or ""}


class ConnSpec(BaseModel):
    url: str = ""
    username: str = ""
    password: str = ""            # vide = inchangé
    api_key: str = ""             # vide = inchangé
    path_map: list[list[str]] = []
    category: str = ""
    tags: list[str] = []
    repost_category: str = ""


def conn_merged(spec: ConnSpec) -> dict:
    url = spec.url.strip().rstrip("/")
    if url and not re.match(r"^https?://[^\s/]+", url):
        raise HTTPException(400, "Adresse de qBittorrent invalide : http://IP:port")
    pm = {}
    for row in spec.path_map:
        if len(row) != 2 or not all(str(x).strip() for x in row):
            continue
        h, c = (str(x).strip().rstrip("/") or "/" for x in row)
        if not (h.startswith("/") and c.startswith("/")):
            raise HTTPException(400, f"Chemins absolus attendus : {h} → {c}")
        pm[h] = c
    old = conn_settings()
    return {"qbt_url": url, "qbt_user": spec.username.strip(), "path_map": pm,
            "qbt_category": spec.category.strip(), "qbt_tags": [t.strip() for t in spec.tags if t.strip()],
            "repost_category": spec.repost_category.strip(),
            "qbt_key": spec.api_key.strip() or old.get("qbt_key", ""), "qbt_pass": spec.password or old.get("qbt_pass", "")}


def conn_payload() -> dict:
    cfg, c = load_config(), conn_settings()
    q = cfg.get("qbittorrent") or {}
    tags = q.get("tags") or []
    env_key, env_pass = os.environ.get("QBT_API_KEY", "").strip(), os.environ.get("QBT_PASS", "")
    return {"url": q.get("url") or "", "username": q.get("username") or "", "path_map": q.get("path_map") or {},
            "category": q.get("category") or "",
            "tags": tags if isinstance(tags, list) else [t.strip() for t in str(tags).split(",") if t.strip()],
            "repost_category": (cfg.get("repost") or {}).get("category", "repost") or "",
            "has_key": bool(c.get("qbt_key") or env_key), "key_source": "réglages" if c.get("qbt_key") else ".env" if env_key else "",
            "has_pass": bool(c.get("qbt_pass") or env_pass), "pass_source": "réglages" if c.get("qbt_pass") else ".env" if env_pass else "",
            "source": "réglages" if c else "config.yml"}


@app.get("/api/connections")
def api_connections_get():
    return conn_payload()


@app.put("/api/connections")
def api_connections_put(spec: ConnSpec):
    db_exec("INSERT OR REPLACE INTO settings (key, value) VALUES ('connections', ?)", (json.dumps(conn_merged(spec)),))
    return conn_payload()


@app.post("/api/connections/reset")
def api_connections_reset():
    """Retour à config.yml et .env (secrets saisis dans l'interface effacés)."""
    db_exec("DELETE FROM settings WHERE key='connections'")
    return conn_payload()


@app.post("/api/connections/test")
def api_connections_test(spec: ConnSpec):
    """Essai sans enregistrer : connexion, puis torrents dont Postmuxarr voit le dossier (correspondance des chemins)."""
    try:
        q = Qbt(apply_conn(load_config(), conn_merged(spec)))
        version = q.login()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    paths = [{"host": h, "cont": c, "exists": Path(h).is_dir()} for h, c in (q.path_map or {}).items()]
    try:
        ts = q.json("torrents/info") or []
    except Exception as e:
        return {"ok": True, "version": version, "paths": paths, "error": f"liste des torrents illisible : {e}"}
    seen = {}
    for t in ts:
        p = q.unmap_path(t.get("save_path") or "")
        if p not in seen:
            seen[p] = Path(p).is_dir()
    visible = sum(1 for t in ts if seen.get(q.unmap_path(t.get("save_path") or "")))
    missing = sorted({t.get("save_path") or "" for t in ts if not seen.get(q.unmap_path(t.get("save_path") or ""))})[:5]
    return {"ok": True, "version": version, "paths": paths, "visible": visible, "total": len(ts), "missing": missing}


class ArrSpec(BaseModel):
    id: str = ""                  # vide = nouvelle instance
    kind: str = "radarr"          # radarr ou sonarr
    name: str = ""
    url: str = ""
    key: str = ""                 # vide = clé inchangée


class SvcSpec(BaseModel):
    tmdb_key: str = ""            # secrets : vide = inchangé
    hdf_key: str = ""
    arr: list[ArrSpec] = []       # instances de Radarr et Sonarr, dans l'ordre de la page
    tracker_names: dict[str, str] = {}   # adresse du tracker → nom affiché (vide = automatique)
    clear: list[str] = []         # secrets saisis dans l'interface à effacer (retour à .env)


def svc_merged(spec: SvcSpec) -> dict:
    old, prev, arr = svc(), {a["id"]: a for a in arr_list()}, []
    for a in spec.arr[:30]:
        if a.kind not in ARR_KINDS:
            raise HTTPException(400, f"Type d'instance inconnu : {a.kind}")
        u, name = a.url.strip().rstrip("/"), a.name.strip()[:40] or a.kind.capitalize()
        if not u and not a.key.strip():
            continue  # ligne laissée vide
        if not re.match(r"^https?://[^\s/]+", u):
            raise HTTPException(400, f"Adresse invalide pour {name} : http://IP:port")
        p = prev.get(a.id) or {}
        arr.append({"id": a.id if p else secrets.token_hex(4), "kind": a.kind, "name": name, "url": u,
                    "key": a.key.strip() or p.get("key", "")})
    out = {"arr": arr, "tracker_names": {str(h).strip().lower()[:200]: str(n).strip()[:40]
                                         for h, n in list(spec.tracker_names.items())[:200]
                                         if str(h).strip() and str(n).strip()}}
    for k in SVC_SECRETS:
        v = getattr(spec, k).strip()
        out[k] = "" if k in spec.clear else (v or old.get(k, ""))
    return out


def svc_payload() -> dict:
    s = svc()
    sec = {k: {"set": bool(s.get(k) or os.environ.get(e, "").strip()),
               "source": "réglages" if s.get(k) else ".env" if os.environ.get(e, "").strip() else ""}
           for k, e in SVC_SECRETS.items()}
    hosts = sorted({r["host"] for r in db_rows("SELECT DISTINCT host FROM torrent_tracker") if r["host"]})
    return {**sec, "arr": [{"id": a["id"], "kind": a["kind"], "name": a["name"], "url": a["url"], "key_set": bool(a["key"])}
                           for a in arr_list()],
            "tracker_names": s.get("tracker_names") or {}, "hosts": hosts}


@app.get("/api/services")
def api_services_get():
    return svc_payload()


@app.put("/api/services")
def api_services_put(spec: SvcSpec):
    db_exec("INSERT OR REPLACE INTO settings (key, value) VALUES ('services', ?)", (json.dumps(svc_merged(spec)),))
    _ARR["at"] = 0.0  # Radarr et Sonarr relus au prochain affichage de l'inventaire
    return svc_payload()


@app.post("/api/services/test")
def api_services_test(spec: SvcSpec):
    """Essai sans enregistrer de chaque service renseigné : TMDB, HD-Forever (1 requête, cadence de l'API), chaque
    instance de Radarr et Sonarr (résultat sous son id, avec son nom)."""
    m, out = svc_merged(spec), {}
    tk = m["tmdb_key"] or ("" if "tmdb_key" in spec.clear else tmdb_key())
    if tk:
        h, q = ({"Authorization": f"Bearer {tk}"}, "") if tk.startswith("eyJ") else ({}, "?api_key=" + urllib.parse.quote(tk))
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{TMDB_BASE}/configuration{q}",
                                                               headers={"Accept": "application/json", **h}), timeout=15):
                out["tmdb"] = {"ok": True}
        except urllib.error.HTTPError as e:
            out["tmdb"] = {"ok": False, "error": "clé refusée" if e.code == 401 else f"erreur {e.code}"}
        except Exception as e:
            out["tmdb"] = {"ok": False, "error": f"injoignable : {getattr(e, 'reason', e)}"}
    hk = m["hdf_key"] or ("" if "hdf_key" in spec.clear else hdf_key())
    if hk:
        api = HdfApi.__new__(HdfApi)  # sans __init__ : la clé testée, pas celle enregistrée
        api.key, api.url = hk.strip("'\"").strip(), repost_cfg()["hdf_api"]
        try:
            api.call("browse", searchstr="test", group_results=0, limit=1)
            out["hdf"] = {"ok": True}
        except HTTPException as e:
            out["hdf"] = {"ok": False, "error": str(e.detail).removeprefix("HDF : ")}
    for i, a in enumerate(m["arr"]):
        k = f"arr{i}"
        if not a["key"]:
            out[k] = {"ok": False, "name": a["name"], "error": "clé API manquante"}
            continue
        try:
            st = arr_get(a["url"], a["key"], "system/status", timeout=15) or {}
            if a["kind"] == "radarr" and str(st.get("appName", "Radarr")).lower() != "radarr" or \
               a["kind"] == "sonarr" and str(st.get("appName", "Sonarr")).lower() != "sonarr":
                out[k] = {"ok": False, "name": a["name"], "error": f"c'est un {st.get('appName')}, pas un {a['kind'].capitalize()}"}
            else:
                out[k] = {"ok": True, "name": a["name"], "version": st.get("version") or ""}
        except urllib.error.HTTPError as e:
            out[k] = {"ok": False, "name": a["name"], "error": "clé API refusée" if e.code == 401 else f"erreur {e.code}"}
        except Exception as e:
            out[k] = {"ok": False, "name": a["name"], "error": f"injoignable : {getattr(e, 'reason', e)}"}
    return out


@app.get("/api/rules")
def api_rules_get():
    return rules_payload()


@app.put("/api/rules")
def api_rules_put(d: dict):
    """Enregistre les critères (seulement ce qui diffère des valeurs par défaut) et le tag des remux."""
    db_exec("INSERT OR REPLACE INTO settings (key, value) VALUES ('hdf_rules', ?)",
            (json.dumps(clean_hdf_rules(d, rules_base())),))
    if "release_tag" in d:
        tag = release_safe(str(d.get("release_tag") or "").strip())[:40]
        if tag:
            db_exec("INSERT OR REPLACE INTO settings (key, value) VALUES ('release_tag', ?)", (tag,))
        else:
            db_exec("DELETE FROM settings WHERE key='release_tag'")
    _RULES["v"] = None
    return rules_payload()


@app.post("/api/rules/reset")
def api_rules_reset():
    """Critères par défaut (le tag des remux est conservé)."""
    db_exec("DELETE FROM settings WHERE key='hdf_rules'")
    _RULES["v"] = None
    return rules_payload()


@app.get("/api/browse")
def api_browse(path: str = "", all: bool = False):
    if path:
        base = readable(path)
    elif START_DIR.is_dir() and (within(START_DIR, SOURCE_ROOT) or within(START_DIR, OUTPUT_DIR)):
        base = START_DIR
    else:  # dossier de départ absent ou hors racines : on retombe sur les sources
        base = SOURCE_ROOT
    if not base.is_dir():
        raise HTTPException(400, "Ce n'est pas un dossier")
    roots = {"Téléchargements": SOURCE_ROOT, "Remux": OUTPUT_DIR}
    if EXTRACT_DIR.is_dir():
        roots["Extractions ISO"] = EXTRACT_DIR
    root = max((r for r in roots.values() if within(base, r)), key=lambda r: len(str(r)))
    # côté sources, on ne remuxe que des Blu-ray : dossiers sans disque, MKV et M2TS isolés masqués (sauf all=1).
    # Le dossier Remux garde ses MKV (retouche, torrent).
    discs_only = not all and root != OUTPUT_DIR
    entries, hidden = [], 0
    for e in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if e.name.startswith((".", "@", "#")):
            continue
        if e.is_dir():
            kind = "bdmv" if (e / "BDMV" / "PLAYLIST").is_dir() else "dir"
            if discs_only and kind == "dir" and not has_disc(e):
                hidden += 1
                continue
            entries.append({"name": e.name, "path": str(e), "kind": kind})
        elif discs_only and e.suffix.lower() in (".mkv", ".m2ts"):
            hidden += 1
        elif e.suffix.lower() in (".mkv", ".iso", ".m2ts"):
            entries.append({"name": e.name, "path": str(e), "kind": e.suffix.lower()[1:],
                            "size": e.stat().st_size})
    masked = hidden_paths()
    for x in entries:
        x["masked"] = x["path"] in masked
        x["done"] = x["kind"] in ("bdmv", "iso") and remuxed(Path(x["path"]))
    return {
        "path": str(base),
        "parent": str(base.parent) if base != root else None,
        "is_bdmv": (base / "BDMV" / "PLAYLIST").is_dir(),
        "entries": entries, "hidden": hidden, "discs_only": discs_only,
        "roots": {k: str(v) for k, v in roots.items()},
        "free": shutil.disk_usage(OUTPUT_DIR).free,
    }


def hidden_paths() -> set:
    """Entrées masquées à la main dans la liste des sources (chemins)."""
    rows = db_rows("SELECT value FROM settings WHERE key='hidden_paths'")
    return set(json.loads(rows[0]["value"])) if rows else set()


class MaskSpec(BaseModel):
    path: str
    masked: bool = True


@app.post("/api/browse/mask")
def api_browse_mask(spec: MaskSpec):
    paths = hidden_paths()
    (paths.add if spec.masked else paths.discard)(spec.path)
    db_exec("INSERT OR REPLACE INTO settings (key, value) VALUES ('hidden_paths', ?)", (json.dumps(sorted(paths)),))
    return {"masked": spec.masked}


@app.get("/api/playlists")
def api_playlists(path: str):
    d = readable(path)
    if not (d / "BDMV" / "PLAYLIST").is_dir():
        raise HTTPException(400, "Pas de dossier BDMV/PLAYLIST ici")
    return {"playlists": disc_playlists(d), "encrypted": (d / "AACS").is_dir()}


def disc_playlists(d: Path) -> list:
    """Playlists d'un Blu-ray (mkvmerge), la plus longue d'abord, les boucles (anti-copie, menus) en dernier."""
    pl = d / "BDMV" / "PLAYLIST"
    items = []
    for f in sorted(x for x in pl.iterdir() if x.suffix.lower() == ".mpls"):
        j = identify(f)
        p = j.get("container", {}).get("properties", {})
        items.append({
            "path": str(f), "name": f.name,
            "duration": (p.get("playlist_duration") or 0) / 1e9,
            "size": p.get("playlist_size") or 0,
            "files": [Path(x).name for x in p.get("playlist_file", [])],
            "tracks": len(j.get("tracks", [])),
            "langs": sorted({(t.get("properties") or {}).get("language") or "und"
                             for t in j.get("tracks", []) if t.get("type") == "audio"} - {"und"}),
            "height": next((int(str(t.get("properties", {}).get("pixel_dimensions", "x0")).split("x")[-1] or 0)
                            for t in j.get("tracks", []) if t.get("type") == "video"), 0),
        })
    groups = {}
    for it in items:
        groups.setdefault(tuple(it["files"]), []).append(it["name"])
    for it in items:
        it["same_as"] = [n for n in groups[tuple(it["files"])] if n != it["name"]]
        # playlist en boucle (anti-copie, menu) : le même clip répété des centaines de fois, durée absurde
        it["loop"] = len(it["files"]) > 10 and len(it["files"]) >= 3 * len(set(it["files"]))
    items.sort(key=lambda x: (x["loop"], -x["duration"], x["name"]))
    return items


@app.get("/api/tracks")
def api_tracks(path: str):
    src = readable(path)
    j = identify(src)
    if j.get("errors") or not j.get("tracks"):
        raise HTTPException(422, "mkvmerge ne reconnaît pas cette source : "
                            + "; ".join(j.get("errors") or ["aucune piste (source chiffrée ?)"]))
    props = j.get("container", {}).get("properties", {})
    mi_target = src
    if src.suffix.lower() == ".mpls" and props.get("playlist_file"):
        mi_target = Path(props["playlist_file"][0])
    mi = match_mediainfo(j["tracks"], mediainfo(mi_target))
    # débits mesurés par BDInfo sur la playlist (sous-titres compris, que mediainfo ne donne pas) : seulement si le
    # rapport couvre cette playlist et qu'il a autant de pistes de chaque type que mkvmerge
    bd_rate, bd_note = {}, []
    rep = BDINFO_DIR / f"{disc_key(disc_root(src))}.txt" if src.suffix.lower() == ".mpls" else None
    if rep and not rep.is_file():
        bd_note.append("pas encore de rapport BDInfo pour ce disque")
    elif rep:
        rates, seen = bdinfo_bitrates(rep.read_text(encoding="utf-8", errors="replace"), src.name)
        if rates is None:
            bd_note.append(f"le rapport BDInfo porte sur {', '.join(seen)}, pas sur {src.name}" if seen
                           else "rapport BDInfo illisible (format inattendu)")
        for ty, lab in (("audio", "audio"), ("subtitles", "sous-titres")):
            ts = [t for t in j["tracks"] if t.get("type") == ty]
            got = (rates or {}).get(ty) or []
            if ts and len(got) == len(ts):
                bd_rate.update({t["id"]: r for t, r in zip(ts, got)})
            elif ts and rates is not None:
                bd_note.append(f"BDInfo liste {len(got)} piste(s) {lab}, mkvmerge {len(ts)}")

    # sans rapport BDInfo : débit estimé en lisant ~200 Mo répartis sur les .m2ts de la playlist (quelques secondes)
    est_rate = {}
    if src.suffix.lower() == ".mpls" and props.get("playlist_file"):
        need = {t["id"] for t in j["tracks"] if t.get("type") in ("audio", "subtitles") and t["id"] not in bd_rate}
        rates = {}
        if need:
            try:
                rates = ts_sample_rates([Path(f) for f in dict.fromkeys(props["playlist_file"])],
                                        (props.get("playlist_duration") or 0) / 1e9)
            except OSError:
                rates = {}
        if rates:
            # PID donné par mkvmerge (« number »), sinon numérotation standard du Blu-ray : audio 0x1100+, PGS 0x1200+
            base, rank = {"audio": 0x1100, "subtitles": 0x1200}, {"audio": 0, "subtitles": 0}
            for t in sorted((t for t in j["tracks"] if t.get("type") in base), key=lambda t: t["id"]):
                num = (t.get("properties") or {}).get("number")
                pid = num if isinstance(num, int) and 0x1000 <= num <= 0x1FFF else base[t["type"]] + rank[t["type"]]
                rank[t["type"]] += 1
                if t["id"] in need:
                    est_rate[t["id"]] = rates.get(pid, 0)  # absente de l'échantillon : piste très creuse (forcés)

    tracks = []
    for t in j["tracks"]:
        p, m = t.get("properties", {}), mi.get(t["id"], {})
        codec = t.get("codec", "")
        out = {
            "id": t["id"], "number": p.get("number"), "type": t["type"], "codec": codec,
            "language": p.get("language") or "und",
            "name": p.get("track_name") or "",
            "default": bool(p.get("default_track")), "forced": bool(p.get("forced_track")),
            "original": bool(p.get("flag_original")),
            "compression": p.get("content_encoding_algorithms") or "",
            "bitrate": bd_rate.get(t["id"]) or m.get("BitRate") or est_rate.get(t["id"]),
            "bitrate_est": not (bd_rate.get(t["id"]) or m.get("BitRate")) and t["id"] in est_rate,
            "elements": m.get("ElementCount"),
        }
        if t["type"] == "video":
            dims = p.get("pixel_dimensions") or ""
            h = m.get("Height") or (dims.split("x")[1] if "x" in dims else "")
            scan = (m.get("ScanType") or "").lower()
            out["res"] = f"{h}{'i' if scan in ('interlaced', 'mbaff') else 'p'}" if h else ""
            out["dims"] = dims
            v = lookup(VIDEO_CODECS, codec)
            out["disp"] = out["rel"] = v[0] if v else codec.split("/")[0]
            out["hdr"] = m.get("HDR_Format") or ""
        elif t["type"] == "audio":
            a = lookup(AUDIO_CODECS, codec) or (codec, codec.replace(" ", "."))
            disp, rel = a
            commercial = m.get("Format_Commercial_IfAny") or ""
            if "atmos" in (codec + commercial).lower():
                disp, rel = disp + " Atmos", rel + ".Atmos"
            out.update(disp=disp, rel=rel, channels=channels_str(m, p.get("audio_channels")))
        elif t["type"] == "subtitles":
            s = lookup(SUB_FORMATS, codec)
            out["disp"] = s[0] if s else codec
        tracks.append(out)

    root = disc_root(src)
    title, year = title_hint(root) if src.suffix.lower() == ".mpls" else guess_title(src.stem)
    return {
        "source": str(src), "tracks": tracks, "title": title, "year": year,
        "duration": (props.get("playlist_duration") or 0) / 1e9 or None,
        "chapters": sum(c.get("num_entries", 0) for c in j.get("chapters", [])),
        "size": props.get("playlist_size") or (src.stat().st_size if src.is_file() else 0),
        "free": shutil.disk_usage(OUTPUT_DIR).free,
        "editable": editable(src),
        "release_name": src.stem if editable(src) else None,
        "bdinfo": (BDINFO_DIR / f"{disc_key(root)}.txt").exists(),
        "source_label": release_meta(src)["source_label"] if editable(src) else (root.name if src.suffix.lower() == ".mpls" else src.stem),
        "tmdb_id": release_meta(src)["tmdb_id"] if editable(src) else None,
        "original_language": tmdb_original_language(release_meta(src)["tmdb_id"]) if editable(src) else "",
        "bdinfo_rates": bool(bd_rate), "bdinfo_note": " ; ".join(bd_note),
        "disc": str(root),
    }


def bdinfo_bitrates(report: str, playlist: str):
    """Débits (bit/s) des pistes audio et sous-titres d'une playlist dans le rapport BDInfo, dans l'ordre du
    rapport (celui des PID, comme mkvmerge) : {"audio": [...], "subtitles": [...]} ; vide si la playlist n'y est pas."""
    by, cur, section = {}, "", None
    for line in report.splitlines():
        s = line.strip()
        m = re.match(r"(?i)name:\s*(\S+\.mpls)\b", s)
        if m:
            cur, section = m.group(1).upper(), None
            by.setdefault(cur, {})
            continue
        h = re.fullmatch(r"([A-Za-z][A-Za-z ]*):", s)  # en-tête de section (VIDEO:, AUDIO:, SUBTITLES:, FILES:…)
        if h:
            section = {"AUDIO": "audio", "SUBTITLES": "subtitles", "SUBTITLE": "subtitles"}.get(h.group(1).strip().upper())
            continue
        k = re.search(r"([\d.,]+)\s*kbps", s, re.I) if section else None
        if k and not re.match(r"(?i)codec|-", s):
            by.setdefault(cur, {}).setdefault(section, []).append(float(k.group(1).replace(",", "")) * 1000)
    rates = by.get(playlist.upper()) or (next(iter(by.values())) if len(by) == 1 else None)
    return rates, [k or "?" for k in by]


def ts_sample_rates(files: list, duration: float, chunks: int = 200, size: int = 1 << 20) -> dict:
    """Débit estimé (bit/s) de chaque PID d'une playlist Blu-ray sans tout lire : ~chunks morceaux de size octets
    répartis sur ses .m2ts (paquets de 192 octets : 4 d'en-tête + paquet TS commençant par 0x47), comptés par PID."""
    files = [f for f in files if f.is_file()]
    total = sum(f.stat().st_size for f in files)
    if not total or duration <= 0:
        return {}
    counts, packets = collections.Counter(), 0
    for f in files:
        fs = f.stat().st_size
        n = max(1, round(chunks * fs / total))
        with open(f, "rb") as fh:
            for i in range(n):
                fh.seek((fs * i // n) // 192 * 192)
                buf = fh.read(size)
                o = next((o for o in range(192) if len(buf) > o + 196 and buf[o + 4] == 0x47 and buf[o + 196] == 0x47), None)
                if o is None:
                    continue
                m = (len(buf) - o) // 192 * 192
                sync, hi, lo = buf[o + 4:o + m:192], buf[o + 5:o + m:192], buf[o + 6:o + m:192]
                counts.update(((h & 0x1F) << 8) | l for s, h, l in zip(sync, hi, lo) if s == 0x47)
                packets += m // 192
    if not packets:
        return {}
    scale = total / 192 / packets * 184 * 8 / duration  # paquets réels par paquet lu × charge utile, en bit/s
    return {pid: c * scale for pid, c in counts.items()}


@app.get("/api/bdinfo/report", response_class=PlainTextResponse)
def api_bdinfo_report(path: str):
    f = BDINFO_DIR / f"{disc_key(readable(path))}.txt"
    if not f.exists():
        raise HTTPException(404, "Pas encore de rapport BDInfo pour cette source")
    return f.read_text(encoding="utf-8", errors="replace")


def tmdb_get(path: str, params: dict) -> dict:
    key = tmdb_key()
    if not key:
        raise HTTPException(503, "Clé TMDB absente : onglet Réglages (ou TMDB_API_KEY dans .env)")
    headers = {"Accept": "application/json"}
    if key.startswith("eyJ"):          # jeton de lecture v4
        headers["Authorization"] = f"Bearer {key}"
    else:                              # clé API v3
        params = {**params, "api_key": key}
    req = urllib.request.Request(f"{TMDB_BASE}{path}?{urllib.parse.urlencode(params)}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise HTTPException(502, "Clé TMDB refusée" if e.code == 401 else f"TMDB : erreur {e.code}")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"TMDB injoignable : {e.reason}")


def tmdb_query(q: str, year: str = "", tv: bool = False) -> list:
    path, ykey = ("/search/tv", "first_air_date_year") if tv else ("/search/movie", "year")
    base = {"query": q.strip(), "language": "fr-FR", "include_adult": "false"}
    res = tmdb_get(path, {**base, ykey: year}).get("results", []) if year else []
    if not res:  # année absente ou fausse dans la source : on réessaie sans
        res = tmdb_get(path, base).get("results", [])
    return res


def tmdb_find(q: str, year: str = "", tv: bool = False) -> tuple:
    """Recherche TMDB ; si rien, on raccourcit le titre par la fin (nom tronqué : « JE SAIS RIEN MAIS »).
    Renvoie (résultats, requête qui a répondu)."""
    words = q.split()
    for n in range(len(words), 0, -1):
        part = " ".join(words[:n])
        if n < len(words) and (n < 2 and len(part) < 6):  # un mot court seul : trop vague
            break
        res = tmdb_query(part, year, tv)
        if res:
            return res, part
    return [], q


@app.get("/api/tmdb/search")
def api_tmdb_search(q: str, year: str = "", langs: str = ""):
    ls = [x for x in langs.split(",") if x]
    return tmdb_rank(tmdb_format(tmdb_find(q, year)[0], 20), year, ls, complete=bool(ls))[:8]


TMDB_LANG = {"fr": "fre", "en": "eng", "de": "ger", "it": "ita", "es": "spa", "pt": "por", "ja": "jpn", "nl": "dut",
             "sv": "swe", "da": "dan", "no": "nor", "ru": "rus", "zh": "chi", "ko": "kor", "pl": "pol", "cs": "cze",
             "hu": "hun", "fi": "fin", "el": "gre", "tr": "tur", "he": "heb", "ar": "ara"}
LANG_ALIAS = {"fra": "fre", "deu": "ger", "nld": "dut", "ces": "cze", "ell": "gre", "zho": "chi"}


def name_langs(name: str) -> list:
    """Langues audio qu'un nom de release laisse deviner. Liste incomplète : ne sert pas à écarter un film."""
    toks = {t.upper() for t in re.split(r"[.\s_\-\[\]()]+", name or "")}
    out = {"fre"} if toks & {"FRENCH", "TRUEFRENCH", "VFF", "VFQ", "VFI", "VOF", "VOQ", "VF2", "MULTI", "DUAL"} else set()
    for w, code in (("ENGLISH", "eng"), ("GERMAN", "ger"), ("ITALIAN", "ita"), ("SPANISH", "spa"), ("JAPANESE", "jpn")):
        if w in toks:
            out.add(code)
    return sorted(out)


def tmdb_rank(res: list, year: str = "", langs=(), complete: bool = False) -> list:
    """Résultats TMDB reclassés : même année (±1 ensuite), langue originale parmi les pistes audio (pénalisée si les
    pistes du disque sont connues et qu'aucune n'est dans cette langue), puis film français en cas de doute.
    L'ordre de TMDB départage le reste."""
    have = {LANG_ALIAS.get(x, x) for x in langs or ()}
    year = str(year or "")

    def score(item):
        i, r = item
        s, ry = 0, str(r.get("year") or "")
        if year.isdigit() and ry.isdigit():
            d = abs(int(ry) - int(year))
            s += 4 if d == 0 else 2 if d == 1 else 0
        o = r.get("original_language") or ""
        if have and o:
            s += 3 if TMDB_LANG.get(o, o) in have else -3 if complete else 0
        if o == "fr":
            s += 1
        return -s, i
    return [r for _, r in sorted(enumerate(res), key=score)]


def tmdb_format(res: list, n: int = 8, tv: bool = False) -> list:
    return [{
        "id": r["id"], "title": r.get("title") or r.get("name") or "",
        "original_title": r.get("original_title") or r.get("original_name") or "",
        "original_language": r.get("original_language") or "",
        "year": (r.get("release_date") or r.get("first_air_date") or "")[:4],
        "url": f"https://www.themoviedb.org/{'tv' if tv else 'movie'}/{r['id']}",
        "poster": f"https://image.tmdb.org/t/p/w92{r['poster_path']}" if r.get("poster_path") else None,
    } for r in res[:n]]


@app.get("/api/qbt/test")
def api_qbt_test():
    try:
        return {"ok": True, "version": Qbt(load_config()).login()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# =========================================================================== specs

class TrackSpec(BaseModel):
    id: int
    number: int | None = None
    type: str
    keep: bool = True
    language: str = "und"
    name: str = ""
    default: bool = False
    forced: bool = False
    original: bool = False


class TorrentSpec(BaseModel):
    enabled: bool = False
    trackers: list[str] = []      # noms des trackers de config.yml
    piece_size: int | None = None
    format: str | None = None


class JobSpec(BaseModel):
    source: str
    release_name: str
    tracks: list[TrackSpec]
    chapter_language: str = "fre"
    chapter_template: str = ""
    subs_no_compression: bool = True
    mediainfo: bool = True
    source_label: str = ""
    tmdb_id: int | None = None
    hardlink: bool = True
    bdinfo_copy: bool = True
    cleanup_extract: bool = True
    torrent: TorrentSpec = TorrentSpec()


class RetouchSpec(BaseModel):
    source: str
    release_name: str
    tracks: list[TrackSpec]
    chapter_language: str = "fre"
    chapter_template: str = ""
    rename_chapters: bool = True
    mediainfo: bool = True
    source_label: str = ""
    tmdb_id: int | None = None


class PathSpec(BaseModel):
    path: str
    torrent: TorrentSpec = TorrentSpec()


def check_name(name: str) -> str:
    name = name.strip()
    if not name or name.startswith(".") or "/" in name or "\0" in name or len(name) > 230:
        raise HTTPException(400, "Nom de release invalide")
    return name


def build_remux(spec: JobSpec):
    src = readable(spec.source)
    name = check_name(spec.release_name)
    out = OUTPUT_DIR / f"{name}.mkv"
    if out.resolve() == src:
        raise HTTPException(400, "Même nom que la source : utilise « Appliquer les retouches » ou change le nom")
    kept = [t for t in spec.tracks if t.keep]
    if not any(t.type == "video" for t in kept):
        raise HTTPException(400, "Aucune piste vidéo conservée")

    cmd = ["mkvmerge", "-o", str(out), "--title", name]
    if spec.chapter_language:
        cmd += ["--chapter-language", spec.chapter_language]
    if spec.chapter_template:
        cmd += ["--generate-chapters-name-template", spec.chapter_template]
    for ty, opt, none in (("video", "--video-tracks", "-D"), ("audio", "--audio-tracks", "-A"),
                          ("subtitles", "--subtitle-tracks", "-S")):
        ids = [str(t.id) for t in kept if t.type == ty]
        cmd += [opt, ",".join(ids)] if ids else [none]
    yn = lambda b: "yes" if b else "no"  # noqa: E731
    for t in kept:
        i = t.id
        cmd += ["--language", f"{i}:{t.language}", "--track-name", f"{i}:{t.name}",
                "--default-track-flag", f"{i}:{yn(t.default)}"]
        if t.type in ("audio", "subtitles"):
            cmd += ["--forced-display-flag", f"{i}:{yn(t.forced)}", "--original-flag", f"{i}:{yn(t.original)}"]
        if t.type == "subtitles" and spec.subs_no_compression:
            cmd += ["--compression", f"{i}:none"]
    cmd += ["--track-order", ",".join(f"0:{t.id}" for t in kept), str(src)]
    return cmd, out


def build_retouch(spec: RetouchSpec):
    src = readable(spec.source)
    if not editable(src):
        raise HTTPException(400, f"Retouche possible uniquement sur un MKV de {OUTPUT_DIR}")
    name = check_name(spec.release_name)
    target = OUTPUT_DIR / f"{name}.mkv"
    if target != src and target.exists():
        raise HTTPException(409, f"Le fichier existe déjà : {target.name}")
    order = [t.id for t in spec.tracks]
    if any(not t.keep for t in spec.tracks) or order != sorted(order):
        raise HTTPException(400, "Retirer ou réordonner des pistes demande un remux")
    b = lambda v: "1" if v else "0"  # noqa: E731
    cmd = ["mkvpropedit", str(src), "--edit", "info", "--set", f"title={name}"]
    for t in spec.tracks:
        sel = f"track:@{t.number}" if t.number else f"track:{t.id + 1}"
        cmd += ["--edit", sel, "--set", f"name={t.name}", "--set", f"language={t.language}",
                "--set", f"flag-default={b(t.default)}"]
        if t.type in ("audio", "subtitles"):
            cmd += ["--set", f"flag-forced={b(t.forced)}", "--set", f"flag-original={b(t.original)}"]
    return cmd, src, target


@app.post("/api/preview")
def api_preview(spec: JobSpec):
    cmd, out = build_remux(spec)
    return {"command": shlex.join(cmd), "output": str(out), "exists": out.exists()}


@app.post("/api/preview/retouch")
def api_preview_retouch(spec: RetouchSpec):
    cmd, src, target = build_retouch(spec)
    extra = []
    if spec.rename_chapters:
        extra.append(f"# + chapitres renommés « {spec.chapter_template} », langue {spec.chapter_language}")
    if target != src:
        extra.append(f"# + renommage : {src.name} → {target.name} (et le hardlink Plex s'il existe)")
    return {"command": "\n".join([shlex.join(cmd)] + extra)}


# =========================================================================== base de jobs

def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def db_exec(sql: str, args=()):
    with db_lock:
        con = db()
        try:
            cur = con.execute(sql, args)
            con.commit()
            return cur.lastrowid
        finally:
            con.close()


def db_rows(sql: str, args=()):
    with db_lock:
        con = db()
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BDINFO_DIR.mkdir(parents=True, exist_ok=True)
    REPOST_TORRENTS.mkdir(parents=True, exist_ok=True)
    db_exec("""CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY, created REAL, started REAL, finished REAL,
        status TEXT, name TEXT, source TEXT, output TEXT, spec TEXT, cmd TEXT,
        progress REAL DEFAULT 0, exit_code INTEGER, log TEXT DEFAULT '')""")
    cols = {r["name"] for r in db_rows("PRAGMA table_info(jobs)")}
    if "kind" not in cols:
        db_exec("ALTER TABLE jobs ADD COLUMN kind TEXT DEFAULT 'remux'")
    if "phase" not in cols:
        db_exec("ALTER TABLE jobs ADD COLUMN phase TEXT DEFAULT ''")
    if "mediainfo" not in cols:
        db_exec("ALTER TABLE jobs ADD COLUMN mediainfo TEXT")
    if "tfile" not in cols:  # .torrent rangé ailleurs qu'à côté du MKV (repost)
        db_exec("ALTER TABLE jobs ADD COLUMN tfile TEXT")
    db_exec("CREATE TABLE IF NOT EXISTS releases (path TEXT PRIMARY KEY, source_label TEXT)")
    db_exec("""CREATE TABLE IF NOT EXISTS seeds (
        path TEXT PRIMARY KEY, hash TEXT, until REAL, next REAL, state TEXT, msg TEXT DEFAULT '', tries INTEGER DEFAULT 0)""")
    if "tmdb_id" not in {r["name"] for r in db_rows("PRAGMA table_info(releases)")}:
        db_exec("ALTER TABLE releases ADD COLUMN tmdb_id INTEGER")
    db_exec("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS repost_checks (hash TEXT PRIMARY KEY, status TEXT, result TEXT, at REAL)")
    db_exec("CREATE TABLE IF NOT EXISTS hdf_lookup (key TEXT PRIMARY KEY, result TEXT, at REAL)")
    db_exec("CREATE TABLE IF NOT EXISTS hdf_scan (path TEXT PRIMARY KEY, result TEXT, at REAL)")
    db_exec("CREATE TABLE IF NOT EXISTS torrent_shape2 (hash TEXT PRIMARY KEY, shape TEXT)")  # v2 : saisons
    db_exec("CREATE TABLE IF NOT EXISTS repost_mi (hash TEXT PRIMARY KEY, result TEXT, at REAL)")
    db_exec("CREATE TABLE IF NOT EXISTS torrent_tracker (hash TEXT PRIMARY KEY, host TEXT)")
    db_exec("CREATE TABLE IF NOT EXISTS torrent_big (hash TEXT PRIMARY KEY, big INTEGER)")
    db_exec("CREATE TABLE IF NOT EXISTS arr_link (hash TEXT PRIMARY KEY, tmdb INTEGER, title TEXT, year TEXT, tv INTEGER, src TEXT)")
    # jobs coupés par un redémarrage : on supprime ce qui est partiel
    for j in db_rows("SELECT id, kind, output FROM jobs WHERE status='running'"):
        if j["kind"] in ("remux", None) and j["output"]:
            Path(j["output"]).unlink(missing_ok=True)
        if j["kind"] == "extract" and j["output"]:
            shutil.rmtree(j["output"], ignore_errors=True)
        db_exec("UPDATE jobs SET status='interrupted', finished=? WHERE id=?", (time.time(), j["id"]))


def enqueue(kind: str, name: str, source: str, output: str, spec: dict, cmd: list | None = None) -> int:
    jid = db_exec("INSERT INTO jobs (created, status, kind, name, source, output, spec, cmd) VALUES (?,?,?,?,?,?,?,?)",
                  (time.time(), "queued", kind, name, source, output, json.dumps(spec), json.dumps(cmd or [])))
    worker.wake.set()
    return jid


def job_files(row: dict):
    """(MKV, .torrent) d'un job, chacun None s'il n'existe pas. Le .torrent d'un repost est rangé dans /data."""
    p = Path(row.get("output") or "")
    disc = row.get("kind") == "repost" and (p.is_dir() or (p.suffix.lower() == ".iso" and p.is_file()))
    if not disc and (p.suffix.lower() != ".mkv" or not p.is_file()):
        return None, None
    t = Path(row["tfile"]) if row.get("tfile") else p.with_suffix(".torrent")
    return p, (t if t.is_file() else None)


def torrent_of(mkv: Path) -> Path:
    rows = db_rows("SELECT tfile FROM jobs WHERE output=? AND tfile IS NOT NULL ORDER BY id DESC LIMIT 1", (str(mkv),))
    if rows and Path(rows[0]["tfile"]).is_file():
        return Path(rows[0]["tfile"])
    return mkv.with_suffix(".torrent")


# =========================================================================== worker

class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.wake = threading.Event()
        self.proc = None
        self.current = None
        self.cancel = False
        self.qbt_task = None
        self.log = []
        self._last = 0.0

    # ---------------------------------------------------------------- boucle
    def run(self):
        while True:
            rows = db_rows("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
            if not rows:
                self.wake.wait(3)
                self.wake.clear()
                continue
            job = rows[0]
            self.current, self.cancel, self.log, self.qbt_task = job["id"], False, [], None
            db_exec("UPDATE jobs SET status='running', started=? WHERE id=?", (time.time(), job["id"]))
            try:
                handler = getattr(self, "do_" + (job.get("kind") or "remux"))
                status, rc = handler(job)
            except Cancelled:
                status, rc = "cancelled", None
                self.say("Annulé.")
            except Exception as e:  # ne jamais tuer le worker
                status, rc = "failed", None
                self.say(f"Erreur : {e}")
            db_exec("UPDATE jobs SET status=?, exit_code=?, finished=?, log=?, phase='' WHERE id=?",
                    (status, rc, time.time(), "\n".join(self.log), job["id"]))
            self.proc, self.current = None, None

    # ---------------------------------------------------------------- outils communs
    def say(self, line: str):
        self.log.append(line)
        self.log = self.log[-LOG_LINES:]

    def phase(self, text: str, progress: float = 0):
        self.say(f"— {text}")
        db_exec("UPDATE jobs SET phase=?, progress=?, log=? WHERE id=?",
                (text, progress, "\n".join(self.log), self.current))

    def progress(self, pct: float):
        if time.time() - self._last > 2:
            db_exec("UPDATE jobs SET progress=?, log=? WHERE id=?", (pct, "\n".join(self.log), self.current))
            self._last = time.time()

    def check(self):
        if self.cancel:
            raise Cancelled()

    def run_tool(self, cmd: list, pct_re: str, stdout_file=None, quiet_re: str | None = None) -> int:
        """Exécute un outil, lit sa sortie par segments (\\r, \\n, \\b) et suit la progression."""
        self.check()
        if stdout_file is not None:
            self.proc = subprocess.Popen(cmd, stdout=stdout_file, stderr=subprocess.PIPE, env=ENV)
            stream = self.proc.stderr
        else:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV)
            stream = self.proc.stdout
        rx, quiet = re.compile(pct_re), re.compile(quiet_re) if quiet_re else None
        buf = b""

        def handle(seg: bytes):
            line = seg.decode("utf-8", "replace").strip()
            if not line:
                return
            m = rx.search(line)
            if m:
                self.progress(float(m.group(1)))
            elif not (quiet and quiet.search(line)):
                self.say(line.replace("#GUI#", ""))

        while True:
            chunk = stream.read1(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n\x08]+", buf)
            buf = parts.pop()
            for p in parts:
                handle(p)
        handle(buf)
        rc = self.proc.wait()
        self.check()
        return rc

    # ---------------------------------------------------------------- BDInfo
    def do_bdinfo(self, job):
        src = Path(job["source"])
        report = BDINFO_DIR / f"{disc_key(src)}.txt"
        self.phase("Scan BDInfo (lecture complète du disque)")
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=BDINFO_DIR, suffix=".tmp") as tmp:
            rc = self.run_tool(["bdinfo", str(src), "--main", "--stdout", "--progress"],
                               r"(\d{1,3}(?:\.\d+)?)\s*%", stdout_file=tmp)
        if rc != 0 or Path(tmp.name).stat().st_size == 0:
            Path(tmp.name).unlink(missing_ok=True)
            self.say(f"bdinfo a échoué (code {rc}).")
            return "failed", rc
        os.replace(tmp.name, report)
        self.say(f"Rapport enregistré ({report.stat().st_size // 1024} Ko).")
        return "done", rc

    # ---------------------------------------------------------------- extraction ISO
    def do_extract(self, job):
        iso, dest = Path(job["source"]), Path(job["output"])
        self.phase("Extraction de l'ISO")
        dest.mkdir(parents=True, exist_ok=True)
        rc = self.run_tool(["7z", "x", "-y", "-bsp1", "-bso0", f"-o{dest}", str(iso)], r"^(\d{1,3})%",
                           quiet_re=r"^\d+M\s|Scan|Open")
        if rc != 0 or not (dest / "BDMV" / "PLAYLIST").is_dir():
            shutil.rmtree(dest, ignore_errors=True)
            self.say(f"Extraction impossible (code {rc}) : ISO illisible ou pas un Blu-ray.")
            return "failed", rc
        if (dest / "AACS").is_dir():
            self.say("Attention : dossier AACS présent, le disque est peut-être chiffré (mkvmerge échouera alors).")
        self.say(f"Extrait dans {dest}")
        return "done", rc

    # ---------------------------------------------------------------- remux
    def do_remux(self, job):
        spec = JobSpec(**json.loads(job["spec"]))
        cmd, out = json.loads(job["cmd"]), Path(job["output"])
        self.phase("Multiplexage")
        try:
            rc = self.run_tool([cmd[0], "--gui-mode"] + cmd[1:], r"#GUI#progress (\d+)%")
        except Cancelled:
            out.unlink(missing_ok=True)
            self.say("Fichier partiel supprimé.")
            raise
        if rc >= 2:
            out.unlink(missing_ok=True)
            self.say("Échec de mkvmerge, fichier partiel supprimé.")
            return "failed", rc
        db_exec("UPDATE jobs SET progress=100 WHERE id=?", (self.current,))
        # pistes absentes d'un segment secondaire de la playlist (carton de fin…) : avertissement sans conséquence
        benign = re.compile(r"A track with the ID \d+ was requested but not found in the file")
        warns = [ln for ln in self.log if ln.startswith("warning")]
        warn = rc == 1 and (not warns or not all(benign.search(ln) for ln in warns))
        if rc == 1 and not warn:
            self.say("Avertissements mkvmerge ignorés : pistes absentes d'un segment secondaire de la playlist.")

        src = Path(spec.source)
        disc = disc_root(src)
        if spec.chapter_template and src.suffix.lower() == ".mkv":
            # chapitres déjà nommés dans un MKV source : le gabarit mkvmerge ne s'applique qu'aux MPLS
            warn |= not self.fix_chapters(out, spec.chapter_template, spec.chapter_language)
        remember(out, spec.source_label, spec.tmdb_id)
        if spec.mediainfo:
            self.store_mediainfo(out, spec.source_label)
        if spec.bdinfo_copy:
            rep = BDINFO_DIR / f"{disc_key(disc)}.txt"
            if src.suffix.lower() != ".mpls":
                pass  # source MKV : pas de disque, pas de BDInfo
            elif rep.exists():
                shutil.copyfile(rep, out.with_name(out.stem + ".bdinfo.txt"))
                self.say(f"BDInfo joint : {out.stem}.bdinfo.txt")
            else:
                self.say("Pas de rapport BDInfo pour ce disque (lance-le depuis l'étape 1 si besoin).")
        if spec.hardlink:
            self.link(out)
        if spec.cleanup_extract and within(disc, EXTRACT_DIR) and disc != EXTRACT_DIR:
            top = EXTRACT_DIR / disc.relative_to(EXTRACT_DIR).parts[0]
            shutil.rmtree(top, ignore_errors=True)
            self.say(f"Extraction temporaire supprimée : {top.name}")
        if spec.torrent.enabled:
            warn |= not self.make_torrent(out, spec.torrent)
        return ("warning" if warn else "done"), rc

    # ---------------------------------------------------------------- retouche
    def do_retouch(self, job):
        spec = RetouchSpec(**json.loads(job["spec"]))
        cmd, src, target = build_retouch(spec)
        warn = False
        self.phase("Retouche des en-têtes")
        rc = self.run_tool(cmd, r"(?!x)x")
        if rc >= 2:
            return "failed", rc
        warn |= rc == 1
        if spec.rename_chapters:
            warn |= not self.fix_chapters(src, spec.chapter_template, spec.chapter_language)
        if target != src:
            self.phase("Renommage")
            link = PLEX_DIR / src.name
            same_link = link.exists() and os.path.samefile(link, src)
            os.rename(src, target)
            self.say(f"{src.name} → {target.name}")
            for ext in (".nfo", ".bdinfo.txt", ".torrent"):  # .nfo : fichiers de la V1
                old = src.with_name(src.stem + ext)
                if old.exists():
                    os.rename(old, target.with_name(target.stem + ext))
            if same_link:
                os.rename(link, PLEX_DIR / target.name)
                self.say("Hardlink Plex renommé.")
            db_exec("DELETE FROM releases WHERE path=?", (str(src),))
            db_exec("UPDATE jobs SET output=? WHERE output=?", (str(target), str(src)))
            db_exec("UPDATE seeds SET path=? WHERE path=?", (str(target), str(src)))
        remember(target, spec.source_label, spec.tmdb_id)
        if spec.mediainfo:
            self.store_mediainfo(target, spec.source_label)
        return ("warning" if warn else "done"), rc

    def fix_chapters(self, mkv: Path, template: str, lang: str) -> bool:
        self.phase("Chapitres")
        with tempfile.TemporaryDirectory() as td:
            xml = Path(td) / "ch.xml"
            subprocess.run(["mkvextract", str(mkv), "chapters", str(xml)], capture_output=True, env=ENV)
            if not xml.exists() or xml.stat().st_size == 0:
                self.say("Aucun chapitre dans le fichier.")
                return True
            tree, n = ET.parse(xml), 0
            ietf = {"fre": "fr", "fra": "fr", "eng": "en", "ger": "de", "deu": "de", "ita": "it",
                    "spa": "es", "jpn": "ja", "por": "pt"}.get(lang, lang)
            for atom in tree.getroot().iter("ChapterAtom"):
                n += 1
                for d in atom.findall("ChapterDisplay"):
                    s = d.find("ChapterString")
                    if template and s is not None and re.match(r"^\s*(Chapter|Chapitre)?\s*0*\d*\s*$", s.text or ""):
                        s.text = chapter_name(template, n)
                    for tag, val in (("ChapterLanguage", lang), ("ChapLanguageIETF", ietf)):
                        e = d.find(tag)
                        if e is None:
                            e = ET.SubElement(d, tag)
                        e.text = val
            tree.write(xml, encoding="utf-8", xml_declaration=True)
            r = subprocess.run(["mkvpropedit", str(mkv), "--chapters", str(xml)], capture_output=True, text=True, env=ENV)
            self.say(f"{n} chapitres traités." if r.returncode < 2 else f"Chapitres : {r.stdout.strip()[-300:]}")
            return r.returncode == 0

    # ---------------------------------------------------------------- torrent seul
    def do_torrent(self, job):
        spec = PathSpec(**json.loads(job["spec"]))
        ok = self.make_torrent(Path(spec.path), spec.torrent)
        return ("done" if ok else "failed"), None

    # ---------------------------------------------------------------- repost
    def do_repost(self, job):
        """Release d'un autre seed : lecture seule. MediaInfo (BDInfo pour un disque, 1er épisode pour une saison), puis
        nouveau torrent sur les mêmes données. Hardlinks seulement pour un remux renommé ou une saison à nettoyer."""
        spec = json.loads(job["spec"])
        src, out = Path(spec["mkv"]), Path(job["output"])
        kind = spec.get("rel_kind") or "remux"
        if not src.exists():
            self.say(f"Release introuvable dans le conteneur : {src}")
            return "failed", None
        if spec.get("season"):
            files = [Path(p) for p in spec.get("episodes") or []]
            if out != src:
                self.phase("Hardlinks de la saison (épisodes et .nfo seulement)")
                out.mkdir(parents=True, exist_ok=True)
                for f in files:
                    dst = out / f.name
                    if not dst.exists():
                        os.link(f, dst)
                    elif not os.path.samefile(dst, f):
                        self.say(f"{dst} existe déjà et n'est pas ce fichier : abandon.")
                        return "failed", None
                self.say(f"{len(files)} fichier(s) liés dans {out}" + (f" ({spec.get('extras')} autre(s) laissé(s) de côté)"
                                                                      if spec.get("extras") else ""))
            remember(out, "", spec.get("tmdb_id"))
            eps = sorted(x for x in out.iterdir() if x.suffix.lower() == ".mkv")
            if eps:
                self.store_mediainfo(eps[0], "", header="REPOST")  # MediaInfo du 1er épisode
        else:
            if out != src:  # nom à nettoyer : hardlink renommé, l'original n'est pas touché
                self.phase("Hardlink au nom de la release")
                if not out.exists():
                    out.parent.mkdir(parents=True, exist_ok=True)
                    os.link(src, out)
                    self.say(f"Hardlink créé : {out}")
                elif not os.path.samefile(out, src):
                    self.say(f"{out} existe déjà et n'est pas ce fichier : abandon.")
                    return "failed", None
            remember(out, "", spec.get("tmdb_id"))
            if kind == "original":
                if not self.store_bdinfo(out):
                    return "failed", None
            else:
                self.store_mediainfo(out, "", header="REPOST")  # en tête du MediaInfo, à la place de « Source : »
        REPOST_TORRENTS.mkdir(parents=True, exist_ok=True)
        tfile = REPOST_TORRENTS / f"{rel_label(out)}.torrent"
        ok = self.make_torrent(out, TorrentSpec(**(spec.get("torrent") or {})), tfile=tfile,
                               category=repost_cfg()["category"], keep_path=True)
        if tfile.is_file():
            db_exec("UPDATE jobs SET tfile=? WHERE id=?", (str(tfile), self.current))
        return ("done" if ok else "failed"), None

    def do_crossseed(self, job):
        """Torrent HDF identique : ajouté arrêté, vérifié par qBittorrent, démarré seulement à 100 %."""
        spec = json.loads(job["spec"])
        q, h = qbt(), spec["hash"]
        self.phase("Ajout du torrent HDF (arrêté)")
        cat = repost_cfg()["category"]
        if cat:
            try:
                q._req("torrents/createCategory", {"category": cat})  # sans chemin ; déjà existante : sans effet
            except Exception:
                pass
        # gestion manuelle, arrêté : qBittorrent vérifie les données sur place sans jamais rien télécharger ni écrire
        fields = {"savepath": spec["savepath"], "autoTMM": "false", "skip_checking": "false",
                  "paused": "true", "stopped": "true", "contentLayout": "Original"}
        if cat:
            fields["category"] = cat
        if spec.get("present"):
            self.say("Torrent HDF déjà dans qBittorrent, arrêté (essai précédent) : reprise à la vérification.")
        else:
            r = q.add_torrent(base64.b64decode(spec["torrent"]), fields)
            ok, ids = add_ok(r)
            if not ok:
                self.say(f"qBittorrent refuse l'ajout : {r or 'sans message'}")
                return "failed", None
            if ids and ids[0].lower() != h.lower():
                self.say(f"Attention : qBittorrent annonce le hash {ids[0]}, attendu {h}.")
            self.say(f"Ajouté dans {spec['savepath']} ({spec['files']} fichier(s)), arrêté.")
        self.phase("Vérification des données par qBittorrent")
        info, seen, checked, t_re = None, False, False, 0.0
        for i in range(4 * 3600):  # jusqu'à 4 h pour un gros disque sur un NAS lent
            self.check()
            time.sleep(1)
            rows = q.json("torrents/info?hashes=" + h) or []
            if not rows:
                if seen:
                    self.say("Torrent retiré de qBittorrent pendant la vérification.")
                    return "failed", None
                if i >= 30:
                    self.say("Torrent introuvable dans qBittorrent après l'ajout.")
                    return "failed", None
                continue
            if not seen:  # vérification forcée : ne dépend pas du réglage « vérifier à l'ajout » de qBittorrent
                q._req("torrents/recheck", {"hashes": h})
                t_re = time.time()
                time.sleep(2)
                rows = q.json("torrents/info?hashes=" + h) or rows
            seen, info = True, rows[0]
            st = info.get("state") or ""
            if st.startswith("checking") or st in ("metaDL", "moving", "queuedForChecking", "allocating"):
                checked = True
                self.progress((info.get("progress") or 0) * 100)
                continue
            if not checked and time.time() - t_re < 30:  # vérification pas encore lancée : on ne conclut pas
                continue
            break
        else:
            self.say("Vérification trop longue : torrent laissé arrêté, à contrôler dans qBittorrent.")
            return "warning", None
        if (info.get("progress") or 0) < 1:
            q._req("torrents/delete", {"hashes": h, "deleteFiles": "false"})
            self.say(f"Données différentes ({(info.get('progress') or 0) * 100:.1f} % identiques) : torrent HDF retiré, "
                     "fichiers intacts.")
            return "failed", None
        started = False
        for path in ("torrents/start", "torrents/resume"):  # qBittorrent 5, puis 4
            try:
                q._req(path, {"hashes": h})
                started = True
                break
            except Exception:
                continue
        if not started:
            self.say("Données identiques à 100 %, mais qBittorrent refuse le démarrage : démarre-le à la main.")
            return "warning", None
        self.say("Données identiques à 100 % : seed démarré.")
        chk = check_row(spec["source_hash"])
        if chk:
            chk["status"] = "crossseeded"
            db_exec("UPDATE repost_checks SET status='crossseeded', result=? WHERE hash=?",
                    (json.dumps(chk), spec["source_hash"]))
        return "done", None

    def do_hdfscan(self, job):
        """Verdict HDF de chaque Blu-ray / ISO du dossier ; ~10 s par film (cadence de l'API), cache 6 h."""
        spec = json.loads(job["spec"])
        targets = scan_targets(Path(job["source"]))
        counts = {}
        for i, d in enumerate(targets):
            self.check()
            self.phase(f"{i + 1}/{len(targets)} · {d.name[:60]}")
            self.progress(i / len(targets) * 100)
            try:
                r = scan_disc(d, spec.get("refresh", False))
            except HTTPException as e:
                r = {"name": d.name, "status": "error", "error": str(e.detail)}
            except Exception as e:
                r = {"name": d.name, "status": "error", "error": str(e)}
            db_exec("INSERT OR REPLACE INTO hdf_scan (path, result, at) VALUES (?,?,?)", (str(d), json.dumps(r), time.time()))
            st = "done" if r.get("done") else r["status"]
            counts[st] = counts.get(st, 0) + 1
            self.say(f"{SCAN_LAB.get(st, st)} · {d.name}" + (f" — {r['error']}" if r.get("error") else ""))
        self.say("Bilan : " + ", ".join(f"{SCAN_LAB.get(k, k)} {n}" for k, n in sorted(counts.items())))
        return "done", None

    def do_repostscan(self, job):
        """Vérification HDF de toute la liste filtrée du Repost : ~10 s par film, cache partagé."""
        spec = json.loads(job["spec"])
        q, rels, counts = qbt(), spec["releases"], {}
        for i, hashes in enumerate(rels):
            self.check()
            self.progress(i / len(rels) * 100)
            try:
                src = pick_source(q, hashes)
                self.phase(f"{i + 1}/{len(rels)} · {src['stem'][:60]}")
                chk = check_for(src)
                tmdb_id = ((chk or {}).get("tmdb_id") or release_meta(Path(src["path"]))["tmdb_id"]
                           or (arr_link(hashes) or {}).get("tmdb"))
                if not tmdb_id and src["kind"] in REL_KINDS:
                    tv = src["season"] or is_series(src["stem"])
                    title, year = (series_title if tv else guess_title)(src["stem"])
                    tmdb_id = (tmdb_guess(title, year, name=src["stem"], tv=tv)[0] or {}).get("id")
                st = repost_check(src, tmdb_id, quick=True, refresh=spec.get("refresh", False))["status"]
                name = src["stem"]
            except HTTPException as e:
                st, name = "error", f"{hashes[0][:8]} — {e.detail}"
            except Exception as e:
                st, name = "error", f"{hashes[0][:8]} — {e}"
            counts[st] = counts.get(st, 0) + 1
            self.say(f"{CHECK_LAB.get(st, st)} · {name}")
        self.say("Bilan : " + ", ".join(f"{CHECK_LAB.get(k, k)} {n}" for k, n in sorted(counts.items())))
        return "done", None

    def store_bdinfo(self, disc: Path) -> bool:
        """Rapport BDInfo du disque (dossier ou ISO), réutilisé s'il existe déjà, rangé comme MediaInfo du job."""
        report = BDINFO_DIR / f"{disc_key(disc)}.txt"
        if not report.is_file():
            self.phase("Scan BDInfo (lecture complète du disque)")
            BDINFO_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("wb", delete=False, dir=BDINFO_DIR, suffix=".tmp") as tmp:
                rc = self.run_tool(["bdinfo", str(disc), "--main", "--stdout", "--progress"],
                                   r"(\d{1,3}(?:\.\d+)?)\s*%", stdout_file=tmp)
            if rc != 0 or Path(tmp.name).stat().st_size == 0:
                Path(tmp.name).unlink(missing_ok=True)
                self.say(f"bdinfo a échoué (code {rc}) : disque illisible ou chiffré ?")
                return False
            os.replace(tmp.name, report)
        else:
            self.say("Rapport BDInfo déjà présent : réutilisé.")
        text = report.read_text(encoding="utf-8", errors="replace").strip()
        db_exec("UPDATE jobs SET mediainfo=? WHERE id=?", (f"REPOST\n\n{text}\n", self.current))
        self.say("BDInfo prêt : bouton « MediaInfo » du job.")
        return True

    # ---------------------------------------------------------------- étapes partagées
    def store_mediainfo(self, mkv: Path, label: str, header: str = ""):
        self.phase("MediaInfo")
        try:
            db_exec("UPDATE jobs SET mediainfo=? WHERE id=?", (mediainfo_text(mkv, label, header), self.current))
            self.say("MediaInfo prêt : bouton « MediaInfo » du job.")
        except Exception as e:
            self.say(f"MediaInfo impossible : {e}")

    def link(self, mkv: Path):
        dest = PLEX_DIR / mkv.name
        try:
            if dest.exists():
                self.say(f"Hardlink ignoré, existe déjà : {dest}")
            else:
                os.link(mkv, dest)
                self.say(f"Hardlink créé : {dest}")
        except OSError as e:
            self.say(f"Hardlink impossible : {e}")

    def make_torrent(self, mkv: Path, t: TorrentSpec, tfile: Path | None = None, source: str | None = None,
                     category: str | None = None, keep_path: bool = False) -> bool:
        """Crée le torrent dans qBittorrent et le seede.
        tfile : où écrire le .torrent (défaut : à côté du MKV) ; source : chemin vu par qBittorrent (défaut : path_map) ;
        category : catégorie (défaut : qbittorrent.category) ; keep_path : fichier d'un autre seed, qBittorrent ne doit
        jamais le déplacer (gestion automatique désactivée, catégorie créée sans chemin)."""
        cfg = load_config()
        tc = cfg.get("torrent") or {}
        known = {x["name"]: x for x in tc.get("trackers") or []}
        chosen = [known[n] for n in t.trackers if n in known]
        try:
            q = Qbt(cfg)
            self.phase("Torrent : connexion à qBittorrent")
            self.say(f"qBittorrent {q.login()}")
            params = {
                "sourcePath": source or q.map_path(mkv),
                "format": t.format or tc.get("format", "v1"),
                "private": "true" if tc.get("private", True) else "false",
                "startSeeding": "true" if tc.get("start_seeding", True) else "false",
            }
            piece = t.piece_size if t.piece_size is not None else tc.get("piece_size")
            if piece:
                params["pieceSize"] = str(piece)
            if chosen:
                params["trackers"] = "|".join(x["announce"] for x in chosen)
                sources = {x.get("source") for x in chosen if x.get("source")}
                if len(sources) == 1:
                    params["source"] = sources.pop()
            self.say(f"Chemin vu par qBittorrent : {params['sourcePath']}")
            task = json.loads(q._req("torrentcreator/addTask", params))
            self.qbt_task = (q, task.get("taskID") or task.get("taskId"))
            self.phase("Torrent : hachage")
            while True:
                if self.cancel:
                    q._req("torrentcreator/deleteTask", {"taskID": self.qbt_task[1]})
                    raise Cancelled()
                time.sleep(2)
                st = json.loads(q._req("torrentcreator/status?taskID=" + urllib.parse.quote(self.qbt_task[1])))
                info = st[0] if isinstance(st, list) and st else st
                if not info:
                    continue
                if isinstance(info.get("progress"), (int, float)):
                    self.progress(info["progress"])
                if info.get("status") in ("Finished", "Failed"):
                    break
            data = q._req("torrentcreator/torrentFile?taskID=" + urllib.parse.quote(self.qbt_task[1]))
            q._req("torrentcreator/deleteTask", {"taskID": self.qbt_task[1]})
            tfile = tfile or mkv.with_name(mkv.stem + ".torrent")
            tfile.write_bytes(data)
            self.say(f".torrent enregistré : {tfile.name}")
            if info.get("status") == "Failed":
                self.say(f"Seed non démarré : {info.get('errorMessage') or 'raison inconnue (doublon ?)'}")
                return False
            self.say("Seed démarré dans qBittorrent.")
            h = info_hash(data, params["format"])
            ra = reannounce_cfg()
            if ra["enabled"] and chosen and ra["start"] == "seed":
                watch_seed(mkv, h)
                self.say(f"Reannounce automatique toutes les {ra['interval']} s pendant {ra['window'] // 60} min, "
                         "arrêté dès que le tracker accepte le torrent.")
            elif ra["enabled"] and chosen:
                self.say("Reannounce au signal d'upload du userscript, ou avec le bouton « Réannoncer ».")

            cat = q.category if category is None else category
            ops = []
            if keep_path:  # avant la catégorie : un torrent en gestion manuelle n'est jamais déplacé
                ops.append(("Gestion automatique désactivée", "torrents/setAutoManagement", {"hashes": h, "enable": "false"}))
            if cat:
                try:
                    if keep_path:  # sans chemin : on ne veut surtout pas qu'elle impose un dossier
                        q._req("torrents/createCategory", {"category": cat})
                        self.say(f"Catégorie « {cat} » créée.")
                    else:  # chemin = dossier du MKV : même en gestion automatique, qBittorrent ne le déplace pas
                        q._req("torrents/createCategory", {"category": cat, "savePath": q.map_path(mkv.parent)})
                        self.say(f"Catégorie « {cat} » créée (chemin {q.map_path(mkv.parent)}).")
                except Exception:
                    pass  # existe déjà : on ne touche pas à son réglage
                ops.append((f"Catégorie appliquée : {cat}", "torrents/setCategory", {"hashes": h, "category": cat}))
            if q.tags:
                ops.append((f"Tags appliqués : {q.tags}", "torrents/addTags", {"hashes": h, "tags": q.tags}))
            for what, path, data_ in ops:
                for _ in range(10):  # le torrent met un instant à apparaître dans la session
                    try:
                        q._req(path, data_)
                        self.say(what)
                        break
                    except Exception:
                        time.sleep(1)
                else:
                    self.say(f"Non fait (torrent introuvable dans qBittorrent) : {what}")
                    if keep_path and path == "torrents/setAutoManagement":
                        self.say("Catégorie non appliquée par prudence : vérifie la gestion du torrent dans qBittorrent.")
                        break
            return True
        except Cancelled:
            raise
        except Exception as e:
            self.say(f"Torrent impossible : {e}")
            return False


worker = Worker()


# =========================================================================== reannounce

TRACKER_WORKING = 2  # statut qBittorrent d'un tracker qui a accepté l'annonce


def reannounce_cfg() -> dict:
    r = (load_config().get("torrent") or {}).get("reannounce") or {}
    return {"enabled": r.get("enabled", True), "start": r.get("start", "upload"),
            "window": int(r.get("window", 600)), "interval": max(10, int(r.get("interval", 30)))}


def watch_seed(mkv: Path, h: str, delay: float | None = None):
    """(Re)lance le suivi d'un seed : reannounce régulier jusqu'à ce que le tracker l'accepte."""
    rc, now = reannounce_cfg(), time.time()
    db_exec("INSERT OR REPLACE INTO seeds (path, hash, until, next, state, msg, tries) VALUES (?,?,?,?,?,?,0)",
            (str(mkv), h, now + rc["window"], now + (rc["interval"] if delay is None else delay), "waiting", ""))
    reannouncer.wake.set()


class Reannouncer(threading.Thread):
    """Tant que le torrent n'est pas uploadé sur le site, le tracker le refuse : on réannonce jusqu'au feu vert."""

    def __init__(self):
        super().__init__(daemon=True)
        self.wake = threading.Event()

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:
                pass  # ne jamais tuer le thread
            self.wake.wait(5)
            self.wake.clear()

    @staticmethod
    def status(q: Qbt, h: str):
        """(True, '') si un vrai tracker répond, (False, message) sinon, (None, …) si le torrent a disparu."""
        try:
            trs = [t for t in q.trackers(h) if not str(t.get("url", "")).startswith("**")]  # DHT, PeX, LSD
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "Torrent absent de qBittorrent"
            raise
        if any(t.get("status") == TRACKER_WORKING for t in trs):
            return True, ""
        return False, next((t["msg"] for t in trs if t.get("msg")), "")

    def tick(self):
        now = time.time()
        due = db_rows("SELECT * FROM seeds WHERE state='waiting' AND next<=?", (now,))
        if not due:
            return
        interval = reannounce_cfg()["interval"]
        try:
            q = Qbt(load_config())
            q.login()
        except Exception as e:  # qBittorrent injoignable : on retentera au prochain intervalle
            for s in due:
                db_exec("UPDATE seeds SET next=?, msg=? WHERE path=?", (now + interval, f"qBittorrent : {e}", s["path"]))
            return
        for s in due:
            try:
                ok, msg = self.status(q, s["hash"])
            except Exception as e:
                ok, msg = False, f"qBittorrent : {e}"
            if ok:
                db_exec("UPDATE seeds SET state='ok', msg='' WHERE path=?", (s["path"],))
            elif ok is None:
                db_exec("UPDATE seeds SET state='absent', msg=? WHERE path=?", (msg, s["path"]))
            elif now >= s["until"]:
                db_exec("UPDATE seeds SET state='timeout', msg=? WHERE path=?", (msg, s["path"]))
            else:
                try:
                    q.reannounce(s["hash"])
                except Exception as e:
                    msg = f"Reannounce impossible : {e}"
                db_exec("UPDATE seeds SET next=?, tries=tries+1, msg=? WHERE path=?", (now + interval, msg, s["path"]))


reannouncer = Reannouncer()


@app.on_event("startup")
def startup():
    if not OUTPUT_DIR.is_dir():  # pas de création silencieuse dans le conteneur : le volume doit être monté
        raise RuntimeError(f"Dossier de destination introuvable dans le conteneur : {OUTPUT_DIR} (volume monté ?)")
    init_db()
    worker.start()
    reannouncer.start()


# =========================================================================== API : jobs

def ensure_free(output: Path):
    if db_rows("SELECT id FROM jobs WHERE output=? AND status IN ('queued','running')", (str(output),)):
        raise HTTPException(409, "Un job traite déjà ce fichier")


@app.post("/api/jobs")
def api_create_job(spec: JobSpec):
    cmd, out = build_remux(spec)
    if out.exists():
        raise HTTPException(409, f"Le fichier existe déjà : {out.name}")
    ensure_free(out)
    return {"id": enqueue("remux", spec.release_name, spec.source, str(out), spec.model_dump(), cmd)}


@app.post("/api/retouch")
def api_retouch(spec: RetouchSpec):
    cmd, src, target = build_retouch(spec)
    ensure_free(src)
    return {"id": enqueue("retouch", spec.release_name, str(src), str(src), spec.model_dump(), cmd)}


@app.post("/api/bdinfo")
def api_bdinfo(spec: PathSpec):
    src = readable(spec.path)
    if src.suffix.lower() == ".mpls":
        src = disc_root(src)
    if not (src.suffix.lower() == ".iso" or (src / "BDMV").is_dir()):
        raise HTTPException(400, "BDInfo attend un dossier Blu-ray ou un ISO")
    return {"id": enqueue("bdinfo", f"BDInfo · {src.name}", str(src), str(src), spec.model_dump())}


@app.post("/api/extract")
def api_extract(spec: PathSpec):
    iso = readable(spec.path)
    if iso.suffix.lower() != ".iso":
        raise HTTPException(400, "Ce n'est pas un ISO")
    dest = EXTRACT_DIR / iso.stem
    if (dest / "BDMV" / "PLAYLIST").is_dir():
        raise HTTPException(409, "Déjà extrait : ouvre-le dans « Extractions ISO »")
    if iso.stat().st_size > shutil.disk_usage(OUTPUT_DIR).free:
        raise HTTPException(507, "Espace libre insuffisant pour extraire cet ISO")
    ensure_free(dest)
    return {"id": enqueue("extract", f"Extraction · {iso.name}", str(iso), str(dest), spec.model_dump())}


@app.post("/api/torrent")
def api_torrent(spec: PathSpec):
    mkv = readable(spec.path)
    if not editable(mkv):
        raise HTTPException(400, f"Torrent possible uniquement pour un MKV de {OUTPUT_DIR}")
    spec.torrent.enabled = True
    return {"id": enqueue("torrent", f"Torrent · {mkv.stem}", str(mkv), str(mkv), spec.model_dump())}


@app.get("/api/jobs")
def api_jobs():
    rows = db_rows("SELECT id, kind, created, started, finished, status, phase, name, source, output, tfile, "
                   "progress, exit_code, mediainfo IS NOT NULL AS has_mi FROM jobs ORDER BY id DESC LIMIT 50")
    for r in rows:
        mkv, t = job_files(r) if r["kind"] in ("remux", "retouch", "torrent", "repost") else (None, None)
        r["torrent"] = str(t) if t else None
        # Plex : seulement pour nos propres remux, jamais pour le fichier d'un autre seed
        r["plex"] = plex_state(mkv) if mkv and r["kind"] != "repost" and r["status"] in ("done", "warning") else None
        seed = db_rows("SELECT state, msg, tries FROM seeds WHERE path=?", (str(mkv),)) if mkv else []
        r["seed"] = seed[0] if seed else None
        r.pop("tfile", None)
    return rows


def plex_state(mkv: Path) -> str:
    link = PLEX_DIR / mkv.name
    if not link.exists():
        return "absent"
    return "linked" if os.path.samefile(link, mkv) else "conflict"


def seed_hash(mkv: Path) -> str:
    rows = db_rows("SELECT hash FROM seeds WHERE path=?", (str(mkv),))
    if rows:
        return rows[0]["hash"]
    tfile = torrent_of(mkv)  # pas encore suivi : hash calculé depuis le .torrent
    if not tfile.is_file():
        raise HTTPException(404, "Pas de .torrent pour ce MKV")
    return info_hash(tfile.read_bytes())


class SeedSpec(BaseModel):
    path: str


@app.post("/api/seed/reannounce")
def api_seed_reannounce(spec: SeedSpec):
    """Reannounce immédiat, puis suivi relancé pour une nouvelle fenêtre (utile si l'upload a pris plus d'une heure)."""
    mkv = readable(spec.path, wide=True)
    h = seed_hash(mkv)
    try:
        q = Qbt(load_config())
        q.login()
        q.reannounce(h)
    except Exception as e:
        raise HTTPException(502, f"qBittorrent : {e}")
    watch_seed(mkv, h, delay=15)
    return {"ok": True}


class PlexSpec(BaseModel):
    path: str
    action: str  # "link" ou "unlink"


@app.post("/api/plex")
def api_plex(spec: PlexSpec):
    mkv = readable(spec.path)
    if not editable(mkv):
        raise HTTPException(400, f"Seuls les MKV de {OUTPUT_DIR} sont concernés")
    link, state = PLEX_DIR / mkv.name, plex_state(mkv)
    if spec.action == "link":
        if state == "conflict":
            raise HTTPException(409, f"Un autre fichier porte déjà ce nom dans {PLEX_DIR}")
        if state == "absent":
            os.link(mkv, link)
    elif spec.action == "unlink":
        if state == "conflict":
            raise HTTPException(409, "Le fichier de Plex n'est pas ce remux : suppression refusée")
        if state == "linked":
            link.unlink()  # même inode : le remux reste intact
    else:
        raise HTTPException(400, "Action inconnue")
    return {"plex": plex_state(mkv)}


# =========================================================================== repost : Radarr et Sonarr (facultatifs)
# Leur historique des téléchargements porte le hash de chaque torrent (downloadId) et le film ou la série, avec son ID
# TMDB : correspondance exacte torrent → œuvre, même si la bibliothèque est faite de hardlinks renommés. Lecture seule.
ARR_KINDS = {"radarr": ("movie", False), "sonarr": ("series", True)}   # type → (champ de l'historique, série)
_ARR = {"at": 0.0, "warn": []}


def arr_list() -> list:
    """Instances de Radarr et Sonarr réglées dans l'onglet Réglages : [{id, kind, name, url, key}]. Reprend le réglage
    d'une seule instance de chaque de la version précédente (radarr_url, sonarr_url)."""
    s = svc()
    out = [dict(a) for a in s.get("arr") or [] if isinstance(a, dict) and a.get("kind") in ARR_KINDS and a.get("url")]
    if "arr" not in s:
        out = [{"id": k, "kind": k, "name": k.capitalize(), "url": s[f"{k}_url"], "key": s.get(f"{k}_key") or ""}
               for k in ARR_KINDS if s.get(f"{k}_url")]
    for a in out:
        a["name"] = a.get("name") or a["kind"].capitalize()
    return out


def arr_get(url: str, key: str, path: str, params: dict | None = None, timeout: int = 60):
    q = "?" + urllib.parse.urlencode(params) if params else ""
    req = urllib.request.Request(f"{url}/api/v3/{path}{q}", headers={"X-Api-Key": key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def arr_sync(force: bool = False) -> list:
    """Complète la table arr_link avec l'historique de chaque instance : tout la première fois, puis seulement le
    nouveau ; au plus une fois toutes les 5 min (force : bouton « Actualiser l'inventaire »). Renvoie les erreurs."""
    if not force and time.time() - _ARR["at"] < 300:
        return _ARR["warn"]
    rows = db_rows("SELECT value FROM settings WHERE key='arr_sync'")
    old, state, warn = (json.loads(rows[0]["value"]) if rows else {}), {}, []
    for a in arr_list():
        field, tv = ARR_KINDS[a["kind"]]
        if not a.get("key"):
            warn.append(f"{a['name']} : clé API manquante")
            continue
        st = old.get(a["id"]) or {}
        since = st.get("since") if st.get("url") == a["url"] else None  # nouvelle adresse : tout relire
        try:
            recs = arr_get(a["url"], a["key"], "history/since", {"date": since or "2000-01-01T00:00:00Z",
                                                                 "include" + field.capitalize(): "true"}, timeout=180)
        except urllib.error.HTTPError as e:
            warn.append(f"{a['name']} : " + ("clé API refusée" if e.code == 401 else f"erreur {e.code}"))
            state[a["id"]] = st
            continue
        except Exception as e:
            warn.append(f"{a['name']} injoignable : {getattr(e, 'reason', e)}")
            state[a["id"]] = st
            continue
        out, last, tvdb = [], since or "", {}
        for r in recs if isinstance(recs, list) else []:
            last = max(last, str(r.get("date") or ""))
            h, w = str(r.get("downloadId") or "").lower(), r.get(field) or {}
            if not re.fullmatch(r"[0-9a-f]{40}", h) or not w:
                continue
            tmdb = w.get("tmdbId") or None
            if not tmdb and tv and w.get("tvdbId"):  # Sonarr ancien : ID TMDB retrouvé par l'ID TVDB
                if w["tvdbId"] not in tvdb:
                    try:
                        res = tmdb_get(f"/find/{int(w['tvdbId'])}", {"external_source": "tvdb_id"}).get("tv_results") or []
                        tvdb[w["tvdbId"]] = res[0]["id"] if res else None
                    except Exception:
                        tvdb[w["tvdbId"]] = None
                tmdb = tvdb[w["tvdbId"]]
            out.append((h, tmdb, w.get("title") or "", str(w.get("year") or ""), int(tv), a["name"]))
        if out:
            with db_lock:
                con = db()
                try:
                    con.executemany("INSERT OR REPLACE INTO arr_link (hash, tmdb, title, year, tv, src) VALUES (?,?,?,?,?,?)", out)
                    con.commit()
                finally:
                    con.close()
        state[a["id"]] = {"url": a["url"], "since": last or since}
    db_exec("INSERT OR REPLACE INTO settings (key, value) VALUES ('arr_sync', ?)", (json.dumps(state),))
    _ARR.update(at=time.time(), warn=warn)
    return warn


def arr_link(hashes: list):
    """Film ou série trouvé par Radarr ou Sonarr pour l'un de ces torrents : {tmdb, title, year, tv, src}, sinon None."""
    hs = [h.lower() for h in hashes if re.fullmatch(r"[0-9a-fA-F]{40}", h or "")]
    if not hs:
        return None
    try:
        arr_sync()
    except Exception:
        pass
    rows = db_rows(f"SELECT * FROM arr_link WHERE hash IN ({','.join('?' * len(hs))})", tuple(hs))
    r = next((x for x in rows if x["tmdb"]), rows[0] if rows else None)
    return {"tmdb": r["tmdb"], "title": r["title"], "year": r["year"], "tv": bool(r["tv"]),
            "src": r["src"]} if r else None


class ShapeCache:
    """Forme du contenu de chaque torrent (files_shape), lue dans qBittorrent. Cache par hash : le contenu d'un
    torrent ne change jamais."""

    def __init__(self, q, allq: dict):
        self.q, self.allq, self.new = q, allq, {}
        self.known = {r["hash"]: r["shape"] for r in db_rows("SELECT hash, shape FROM torrent_shape2")}

    def of(self, hashes: list) -> str:
        hs = [h.lower() for h in hashes if h]
        for h in hs:
            if h in self.known:
                return self.known[h]
        for h in hs:
            t = self.allq.get(h)
            if not t or not self.q:
                continue
            try:
                s = self.compute(h, t)
            except Exception:
                continue
            self.known[h] = self.new[h] = s
            return s
        return ""  # absente de qBittorrent

    def compute(self, h: str, t: dict) -> str:
        cp = (t.get("content_path") or "").replace("\\", "/")
        if cp.lower().endswith((".mkv", ".iso")):  # torrent à un seul fichier : pas besoin de la liste
            return files_shape([posixpath.basename(cp)], [BIG_FILE])
        files = self.q.json("torrents/files?hash=" + h) or []
        return files_shape([f.get("name") or "" for f in files], [f.get("size") or 0 for f in files])

    def save(self):
        if not self.new:
            return
        with db_lock:
            con = db()
            try:
                con.executemany("INSERT OR REPLACE INTO torrent_shape2 (hash, shape) VALUES (?,?)", list(self.new.items()))
                con.commit()
            finally:
                con.close()


SAME_SIZE = 500 * 1024 * 1024  # au-delà, deux torrents de même taille exacte sont la même release


def big_size(q, t: dict) -> int:
    """Taille cumulée des gros fichiers d'un torrent (épisodes, MKV, ISO), sans .nfo, sample ni sous-titres à part :
    deux cross-seeds d'une même release ont la même. -1 si la liste des fichiers est illisible (non mémorisé)."""
    if (t.get("content_path") or "").lower().endswith((".mkv", ".iso", ".mp4", ".avi", ".m2ts", ".ts")):
        return t.get("total_size") or t.get("size") or 0  # torrent d'un seul fichier
    try:
        files = q.json("torrents/files?hash=" + t["hash"]) or []
    except Exception:
        return -1
    return sum(f.get("size") or 0 for f in files
               if (f.get("size") or 0) >= BIG_FILE and not re.search(r"(?i)(^|[/._ -])sample([/._ -]|$)", f.get("name") or ""))


def qbt_inventory() -> dict:
    """Inventaire du Repost : torrents de qBittorrent regroupés par contenu (les cross-seeds d'une même release pointent
    sur le même fichier ou dossier), tracker lu dans qBittorrent (en cache : le champ « tracker » est vide pour un
    torrent arrêté). Titre et ID TMDB donnés par Radarr ou Sonarr s'ils connaissent le torrent, sinon déduits du nom."""
    q = qbt()
    warn = arr_sync()
    links = {r["hash"]: r for r in db_rows("SELECT * FROM arr_link")}
    known = {r["hash"]: r["host"] for r in db_rows("SELECT hash, host FROM torrent_tracker")}
    sigs, new_sig = {r["hash"]: r["big"] for r in db_rows("SELECT hash, big FROM torrent_big")}, {}
    new, groups = {}, {}
    for t in q.json("torrents/info") or []:
        h = t["hash"].lower()
        host = urllib.parse.urlparse(t.get("tracker") or "").hostname or known.get(h, "")
        if not host:
            try:
                urls = [x.get("url") or "" for x in q.trackers(h) if not str(x.get("url", "")).startswith("**")]
                host = next((urllib.parse.urlparse(u).hostname for u in urls if urllib.parse.urlparse(u).hostname), "") or ""
            except Exception:
                host = ""
        if host and known.get(h) != host:
            new[h] = host
        # cross-seeds d'une même release : mêmes gros fichiers (taille cumulée à l'octet près), même si chaque tracker a
        # son dossier, un nom un peu différent ou ses propres .nfo ; petits torrents : même chemin de contenu
        big = sigs.get(h)
        if big is None:
            big = big_size(q, t)
            if big >= 0:
                sigs[h] = new_sig[h] = big
        key = (f"size:{big}" if big >= SAME_SIZE
               else t.get("content_path") or posixpath.join(t.get("save_path") or "", t.get("name") or ""))
        groups.setdefault(key, []).append((t, host or "?"))
    if new_sig:
        with db_lock:
            con = db()
            try:
                con.executemany("INSERT OR REPLACE INTO torrent_big (hash, big) VALUES (?,?)", list(new_sig.items()))
                con.commit()
            finally:
                con.close()
    if new:
        with db_lock:
            con = db()
            try:
                con.executemany("INSERT OR REPLACE INTO torrent_tracker (hash, host) VALUES (?,?)", list(new.items()))
                con.commit()
            finally:
                con.close()
    rels = []
    for key, items in groups.items():
        name = re.sub(r"\.(mkv|iso|mp4|avi|m2ts)$", "", items[0][0].get("name") or "", flags=re.I)
        a = next((links[t["hash"].lower()] for t, _ in items if t["hash"].lower() in links), None)
        if a:
            tv, title, year = bool(a["tv"]), a["title"], a["year"]
        else:
            tv = is_series(name)
            title, year = (series_title if tv else guess_title)(name)
        rels.append({
            "id": hashlib.sha1(key.encode()).hexdigest()[:12], "kind": "serie" if tv else "film",
            "title": f"{title} ({year})" if year else title, "name": name,
            "tmdb": a["tmdb"] if a else None, "arr": a["src"] if a else "",
            "size": max(t.get("size") or 0 for t, _ in items),
            "category": sorted({t.get("category") for t, _ in items if t.get("category")}),
            "trackers": [{"host": host, "hash": t["hash"], "name": t.get("name") or "", "seeds": t.get("num_complete")}
                         for t, host in items],
            "hashes": [t["hash"] for t, _ in items]})
    names = {k: v for k, v in (svc().get("tracker_names") or {}).items() if v}
    return {"state": "ready", "built_at": time.time(), "releases": rels, "tracker_names": names, "warnings": warn,
            "source": "qbittorrent"}


def inventory_data():
    return qbt_inventory()


@app.get("/api/repost/inventory")
def api_repost_inventory():
    d = inventory_data()
    if not isinstance(d, dict) or d.get("state") != "ready":
        d = d if isinstance(d, dict) else {}
        return {"state": d.get("state") or "error", "job_id": d.get("job_id"), "error": d.get("error")}
    # qBittorrent : seuls les torrents complets et dont les fichiers sont là sont repostables (1 appel pour tout)
    q = None
    try:
        q = qbt()
        allq = {t["hash"].lower(): t for t in q.json("torrents/info") or []}
        ready = {h for h, t in allq.items() if (t.get("progress") or 0) >= 1 and t.get("state") != "missingFiles"}
    except Exception:
        allq, ready = {}, None  # qBittorrent injoignable : on n'écarte rien, l'erreur viendra à l'ouverture
    shapes = ShapeCache(q, allq)
    mis = {r["hash"]: json.loads(r["result"]) for r in db_rows("SELECT hash, result FROM repost_mi")}
    hidden = []
    rmasked = repost_masked()
    checks = {r["hash"]: r for r in db_rows("SELECT hash, status, at FROM repost_checks")}
    jobs = {}
    for r in db_rows("SELECT id, status, source FROM jobs WHERE kind='repost' ORDER BY id"):
        jobs[r["source"]] = r  # le plus récent l'emporte
    rels = []
    for r in d.get("releases") or []:
        trs = [{"host": t.get("host") or "?", "hash": t.get("hash"), "name": t.get("name") or "", "seeds": t.get("seeds")}
               for t in r.get("trackers") or []]
        hashes = [t["hash"] for t in trs if t["hash"]] or list(r.get("hashes") or [])
        chk = next((checks[h] for h in hashes if h in checks), None)
        job = next((jobs[h] for h in hashes if h in jobs), None)
        name = r.get("name") or ""
        shape = shapes.of(hashes)
        rk = rel_kind(name, shape == "disc")  # catégorie HDF supposée d'après le nom (corrigée dans le détail)
        elig, why = merge_mi(*hdf_eligibility(name, shape, rk), mis, hashes)
        rel = {
            "id": r.get("id"), "kind": r.get("kind"), "title": r.get("title") or "", "name": name,
            "rk": rk, "res": rel_res(name), "shape": shape, "elig": elig, "elig_why": why,
            "size": r.get("size") or 0, "category": r.get("category") or [], "trackers": trs, "hashes": hashes,
            "hdf": chk["status"] if chk else None, "hdf_at": chk["at"] if chk else None,
            "job": {"id": job["id"], "status": job["status"]} if job else None,
            "masked": any((h or "").lower() in rmasked for h in hashes),
            "tmdb": r.get("tmdb"), "arr": r.get("arr") or "",
        }
        if ready is not None and not any((h or "").lower() in ready for h in hashes):
            # inutilisable : téléchargement incomplet, fichiers manquants, ou torrent absent de qBittorrent.
            # Gardée à part avec sa raison : la page lui applique les mêmes règles que la liste.
            ts = [allq[(h or "").lower()] for h in hashes if (h or "").lower() in allq]
            rel["why"] = ("absent de qBittorrent" if not ts
                          else "fichiers manquants" if all(t.get("state") == "missingFiles" for t in ts)
                          else f"incomplet ({max((t.get('progress') or 0) for t in ts) * 100:.1f} %)")
            hidden.append(rel)
        else:
            rels.append(rel)
    shapes.save()
    return {"state": "ready", "built_at": d.get("built_at"), "releases": rels, "source": "qbittorrent",
            "tracker_names": d.get("tracker_names") or {},
            "warnings": list(d.get("warnings") or [])
                        + ([] if ready is not None else ["qBittorrent injoignable : releases incomplètes non écartées."]),
            "hidden_list": hidden}


def repost_masked() -> set:
    # releases masquées à la main dans la liste du Repost : hashes de leurs torrents, en minuscules
    rows = db_rows("SELECT value FROM settings WHERE key='repost_masked'")
    return set(json.loads(rows[0]["value"])) if rows else set()


class RepostMaskSpec(BaseModel):
    hashes: list[str]
    masked: bool = True


@app.post("/api/repost/mask")
def api_repost_mask(spec: RepostMaskSpec):
    hs = {h.lower() for h in spec.hashes if re.fullmatch(r"[0-9a-fA-F]{40}", h or "")}
    if not hs:
        raise HTTPException(400, "Aucun hash de torrent valide")
    cur = repost_masked()
    cur = cur | hs if spec.masked else cur - hs
    db_exec("INSERT OR REPLACE INTO settings (key, value) VALUES ('repost_masked', ?)", (json.dumps(sorted(cur)),))
    return {"masked": spec.masked}


@app.post("/api/repost/inventory/refresh")
def api_repost_inventory_refresh():
    """Inventaire lu dans qBittorrent à chaque affichage ; Radarr et Sonarr relus tout de suite."""
    arr_sync(force=True)
    return {"job_id": None}


@app.get("/api/repost/rules")
def api_repost_rules_get():
    rows = db_rows("SELECT value FROM settings WHERE key='repost_rules'")
    return json.loads(rows[0]["value"]) if rows else None


def clean_rules(node, depth=0):
    """{"match": "all"|"any", "rules": [{"f", "op", "v"} | sous-groupe]} — un seul niveau de sous-groupe."""
    if not isinstance(node, dict):
        return None
    out = {"match": "any" if node.get("match") == "any" else "all", "rules": []}
    for r in (node.get("rules") or [])[:60]:
        if not isinstance(r, dict):
            continue
        if "rules" in r:
            sub = clean_rules(r, depth + 1) if depth == 0 else None
            if sub is not None:
                out["rules"].append(sub)
            continue
        v = r.get("v", "")
        if not isinstance(v, (str, int, float)) or len(str(v)) > 500:
            v = ""
        out["rules"].append({"f": str(r.get("f", ""))[:30], "op": str(r.get("op", ""))[:30], "v": v})
    return out


@app.post("/api/repost/rules")
def api_repost_rules_set(rules: dict):
    clean = clean_rules(rules)
    if clean is None:
        raise HTTPException(400, "Règles illisibles")
    db_exec("INSERT OR REPLACE INTO settings (key, value) VALUES ('repost_rules', ?)", (json.dumps(clean),))
    return {"ok": True}


# =========================================================================== repost : release et HDF

class RepostRef(BaseModel):
    hashes: list[str]
    tmdb_id: int | None = None
    kind: str = ""                # catégorie choisie à la main (clé de REL_KINDS), sinon déduite du nom
    variant: str = ""             # hash de la version à reposter parmi les cross-seeds (vide = la plus complète)


class RepostSpec(BaseModel):
    hashes: list[str]
    tmdb_id: int | None = None
    torrent: TorrentSpec = TorrentSpec()
    force: bool = False           # doublon mort ou release à arbitrer : upload confirmé quand même
    variant: str = ""             # hash de la version à reposter parmi les cross-seeds (vide = la plus complète)


class CrossSpec(BaseModel):
    hashes: list[str]
    torrent_id: int


def pick_source(q: Qbt, hashes: list, kind: str = "", prefer: str = "") -> dict:
    """Le torrent de la release à reposter, complet : un film en MKV (seul ou dans un dossier), une saison complète
    (dossier d'épisodes MKV d'une même saison), ou un disque complet (dossier contenant BDMV, ou ISO) pour un Blu-ray
    Original. kind : catégorie choisie à la main, sinon déduite du nom. Hors remux, rien n'est renommé."""
    hashes = [h for h in hashes if re.fullmatch(r"[0-9a-fA-F]{40}", h or "")]
    if not hashes:
        raise HTTPException(400, "Aucun hash de torrent valide")
    infos = {t["hash"]: t for t in q.json("torrents/info?hashes=" + "|".join(hashes)) or []}
    why, cands = [], []
    for h in hashes:
        t = infos.get(h.lower()) or infos.get(h)
        if not t:
            why.append(f"{h[:8]} absent de qBittorrent")
            continue
        if (t.get("progress") or 0) < 1:
            why.append(f"{t['name']} : téléchargement incomplet")
            continue
        files = q.json("torrents/files?hash=" + t["hash"]) or []
        big = [f for f in files if (f.get("size") or 0) >= BIG_FILE]
        save = t.get("save_path") or ""
        shape = files_shape([f.get("name") or "" for f in files], [f.get("size") or 0 for f in files])
        season, episodes = False, []
        if any("BDMV/" in ("/" + f["name"]).replace("\\", "/") for f in files):
            tops = {f["name"].replace("\\", "/").split("/", 1)[0] for f in files}
            if len(tops) != 1 or all("/" not in f["name"] for f in files) or next(iter(tops)) == "BDMV":
                why.append(f"{t['name']} : disque Blu-ray sans dossier racine, impossible à reposter tel quel")
                continue
            name = next(iter(tops))
            qpath, size, disc, folder = posixpath.join(save, name), sum(f.get("size") or 0 for f in files), True, True
        elif len(big) == 1 and big[0]["name"].lower().endswith(".iso"):
            name, size, disc, folder = posixpath.basename(big[0]["name"]), big[0]["size"], True, "/" in big[0]["name"]
            qpath = posixpath.join(save, big[0]["name"])
        elif shape == "mkv":
            name, size, disc, folder = posixpath.basename(big[0]["name"]), big[0]["size"], False, "/" in big[0]["name"]
            qpath = posixpath.join(save, big[0]["name"])
        elif shape == "season":
            names = [f["name"].replace("\\", "/") for f in big]
            tops = {n.split("/", 1)[0] for n in names}
            if len(tops) != 1 or any("/" not in n for n in names):
                why.append(f"{t['name']} : saison sans dossier racine, impossible à reposter telle quelle")
                continue
            name = next(iter(tops))
            qpath, size, disc, folder, season = posixpath.join(save, name), sum(f["size"] for f in big), False, True, True
            episodes = sorted(q.unmap_path(posixpath.join(save, n)) for n in names)
        else:
            why.append({"episode": f"{t['name']} : épisode seul, HDF n'admet que des saisons complètes",
                        "pack": f"{t['name']} : plusieurs saisons, HDF veut un torrent par saison"}.get(
                shape, f"{t['name']} : {len(big)} gros fichier(s) ; il faut un film en MKV, une saison en MKV, "
                       "un ISO ou un dossier BDMV"))
            continue
        host = q.unmap_path(qpath)
        whole = (disc or season) and not name.lower().endswith(".iso")  # dossier publié en entier
        stem = name if whole else Path(name).stem
        detected = rel_kind(stem, disc)
        k = kind if kind in REL_KINDS and (kind == "original") == disc else detected
        cands.append({"hash": t["hash"], "torrent": t.get("name") or "", "name": name, "stem": stem,
                "clean": release_safe(stem) if k == "remux" else stem,  # seuls nos remux suivent la règle de nommage
                "size": size, "qbt_path": qpath, "path": host, "folder": folder, "disc": disc,
                "disc_dir": disc and not name.lower().endswith(".iso"), "season": season, "episodes": episodes,
                "shape": shape, "kind": k, "kind_detected": detected,
                "exists": Path(host).is_dir() if whole else Path(host).is_file(),
                "_files": files, "_tracker": t.get("tracker") or ""})
    if not cands:
        raise HTTPException(422, "Rien à reposter dans cette release : " + " ; ".join(why[:3]))
    # versions de la même release (cross-seeds regroupés) : par défaut la plus complète (.nfo d'origine compris)
    known = {r["hash"]: r["host"] for r in db_rows("SELECT hash, host FROM torrent_tracker")}
    for c in cands:
        fs, tr = c.pop("_files"), c.pop("_tracker")
        c["host"] = urllib.parse.urlparse(tr).hostname or known.get(c["hash"].lower(), "") or "?"
        c["total"] = sum(f.get("size") or 0 for f in fs)
        c["nfo"] = sum(1 for f in fs if (f.get("name") or "").lower().endswith(".nfo"))
        c["nfiles"] = len(fs)
    pick = next((c for c in cands if prefer and c["hash"].lower() == prefer.lower()), None) \
        or max(cands, key=lambda c: (c["nfo"], c["nfiles"]))
    variants = [{"hash": c["hash"], "host": c["host"], "name": c["name"], "total": c["total"], "big": c["size"],
                 "nfo": c["nfo"], "nfiles": c["nfiles"]} for c in cands]
    # tailles à comparer aux torrents HDF pour reconnaître une release identique : chaque version, entière ou non
    return {**pick, "variants": variants, "sizes": sorted({c["total"] for c in cands} | {c["size"] for c in cands})}


def check_for(src: dict):
    """Vérification HDF d'une release : celle de la version choisie, sinon celle d'une autre version du groupe."""
    for h in [src["hash"]] + [v["hash"] for v in src.get("variants") or []]:
        c = check_row(h)
        if c:
            return c
    return None


def check_row(h: str):
    rows = db_rows("SELECT status, result, at FROM repost_checks WHERE hash=?", (h,))
    if not rows:
        return None
    return {**json.loads(rows[0]["result"]), "status": rows[0]["status"], "at": rows[0]["at"]}


def tmdb_guess(title: str, year: str, langs=None, name: str = "", tv: bool = False):
    """Premier résultat TMDB (film, ou série si tv) après classement, plus la liste pour corriger à la main.
    partial = trouvé seulement sur un début du titre (nom tronqué) : à confirmer."""
    if not tmdb_key() or not title:
        return None, []
    try:
        raw, used = tmdb_find(title, year, tv)
    except HTTPException:
        return None, []
    # année, langue originale présente dans les pistes audio (sinon devinée du nom), œuvre française en cas de doute
    res = tmdb_rank(tmdb_format(raw, 20, tv), year, langs or name_langs(name or title), complete=bool(langs))[:8]
    pick = res[0] if res else None
    if pick and used != title:
        pick = {**pick, "partial": True, "query": used}
    return pick, res


@app.post("/api/repost/release")
def api_repost_release(spec: RepostRef):
    q = qbt()
    src = pick_source(q, spec.hashes, spec.kind, spec.variant)
    check = check_for(src)
    if not spec.kind and check and check.get("kind") and check["kind"] != src["kind"]:
        src = pick_source(q, spec.hashes, check["kind"], spec.variant)  # catégorie corrigée lors de la vérification
    stem = src["stem"]
    arr = arr_link(spec.hashes)  # Radarr ou Sonarr connaît ce torrent : titre et ID TMDB exacts
    series = src["season"] or is_series(stem) or bool(arr and arr["tv"])
    title, year = (arr["title"], arr["year"]) if arr else series_title(stem) if series else guess_title(stem)
    pick, results = tmdb_guess(title, year, name=stem, tv=series)
    known = release_meta(Path(src["path"]))["tmdb_id"]
    if check and check.get("kind", "remux") != src["kind"]:
        check = None  # vérifiée sous une autre catégorie : à refaire
    tmdb_id = (check or {}).get("tmdb_id") or known or (arr or {}).get("tmdb") or (pick or {}).get("id")
    jobs = db_rows("SELECT id, status FROM jobs WHERE kind='repost' AND source=? ORDER BY id DESC LIMIT 1", (src["hash"],))
    elig, why = hdf_eligibility(src["name"], src["shape"] if not src["disc"] else "disc", src["kind"])
    mi = db_rows("SELECT result FROM repost_mi WHERE hash=?", (src["hash"],))
    return {**src, "mi": json.loads(mi[0]["result"]) if mi else None,
            "title": title, "year": year, "series": series, "kinds": REL_KINDS, "elig": elig, "elig_why": why,
            "tmdb_id": tmdb_id, "tmdb_results": results, "arr": arr, "check": check, "job": jobs[0] if jobs else None}


SCAN_LAB = {"absent": "Vaut le coup", "review": "À juger", "dupe": "Doublon", "replaceable": "Sans seeder : note modo ?",
            "ambiguous": "Film ambigu", "error": "Erreur", "done": "Déjà remuxé", "nofilm": "Film introuvable"}


CHECK_LAB = {"absent": "Repostable", "allowed": "Repostable (HDForever)", "review": "À arbitrer", "dupe": "Doublon",
             "replaceable": "Doublon sans seeder", "identical": "Identique", "reseed": "Identique sans seeder",
             "refused": "WEBRip refusé", "unknown": "Catégorie à choisir", "error": "Erreur"}


class HdfApi:
    """API HD-Forever : clé dans l'en-tête X-Api-Key, cadence limitée (6/min en continu) gérée ici pour tout le monde."""
    GAP = 10.5
    lock = threading.Lock()
    last = 0.0

    def __init__(self):
        self.key = hdf_key().strip("'\"").strip()  # guillemets recopiés dans .env
        self.url = repost_cfg()["hdf_api"]
        if not self.key:
            raise HTTPException(503, "Clé API HD-Forever absente : onglet Réglages (ou HDF_API_KEY dans .env)")

    def call(self, endpoint: str, **params) -> dict:
        q = urllib.parse.urlencode({"endpoint": endpoint, **{k: v for k, v in params.items() if v not in (None, "")}})
        with HdfApi.lock:
            for attempt in range(2):
                wait = HdfApi.last + HdfApi.GAP - time.time()
                if wait > 0:
                    time.sleep(wait)
                req = urllib.request.Request(f"{self.url}?{q}", headers={
                    "X-Api-Key": self.key, "Accept": "application/json", "User-Agent": "Postmuxarr"})
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        data = json.load(r)
                    HdfApi.last = time.time()
                    break
                except urllib.error.HTTPError as e:
                    HdfApi.last = time.time()
                    if e.code == 429 and attempt == 0:
                        time.sleep(min(int(e.headers.get("Retry-After") or 30), 90))
                        continue
                    try:
                        msg = json.load(e).get("error")
                    except Exception:
                        msg = ""
                    raise HTTPException(502, f"HDF : {msg or f'erreur {e.code}'}")
                except (urllib.error.URLError, OSError) as e:
                    raise HTTPException(502, f"HDF injoignable : {getattr(e, 'reason', e)}")
        if not isinstance(data, dict) or data.get("status") != "success":
            raise HTTPException(502, f"HDF : {(data or {}).get('error') or 'réponse inattendue'}")
        return data.get("response") or {}

    def download_url(self, torrent_id: int) -> str:
        p = urllib.parse.urlparse(self.url)
        return f"{p.scheme}://{p.netloc}/torrents.php?" + urllib.parse.urlencode(
            {"action": "download", "id": torrent_id, "apikey": self.key})


def lang_sig(name: str):
    """Langues d'une release d'après son nom : (MULTI|FR|VOST, {variantes VF}) ; None si le nom ne dit rien."""
    toks = {t.upper() for t in re.split(r"[.\s_\-\[\]()]+", name or "")}
    fr = {t for t in ("VFF", "VFQ", "VFI", "VOF", "VOQ") if t in toks}
    if "VF2" in toks:
        fr |= {"VFF", "VFQ"}
    if "TRUEFRENCH" in toks:
        fr.add("VFF")
    if "MULTI" in toks:
        kind = "MULTI"
    elif fr or "FRENCH" in toks or "TRUEFRENCH" in toks:
        kind = "FR"
    elif "VOSTFR" in toks:
        kind = "VOST"
    else:
        return None
    return kind, frozenset(fr)


def same_langs(a, b) -> bool:
    if a is None or b is None:  # le nom ne dit rien : dans le doute, mêmes langues
        return True
    return a[0] == b[0] and (not a[1] or not b[1] or a[1] == b[1])


def classify(src: dict, found: list, approx: bool = False, new: bool = False) -> dict:
    """Doublons HDF de notre release, dans sa catégorie (src["kind"], clé de REL_KINDS) et sa résolution.
    Toutes catégories : même taille à l'octet = identical (même release : cross-seed ou reseed).
    Remux et Blu-ray Original : même catégorie, taille à ± size_tolerance, mêmes langues = dupe, bloquant sauf
    team d'exception (HDForever). WEB-DL, rip, mHD : une release de même catégorie = review (alerte, l'utilisateur
    arbitre). Une release par catégorie est admise : les autres catégories ne comptent pas.
    new : nouvelle release (onglet Remux), règle plus stricte : pas d'exception de team, et un remux de même résolution
    qui diffère par la taille ou les langues est à juger (review) au lieu d'être ignoré.
    Genre des éléments : identical, dupe, review, same (même catégorie, langues ou taille différentes), other."""
    rc = repost_cfg()
    kind = src.get("kind") or "remux"
    media = REL_KINDS.get(kind, "")
    res = rel_res(src["stem"])
    ours = lang_sig(src["stem"])
    team = src["stem"].rsplit("-", 1)[1].strip() if "-" in src["stem"] else ""
    exempt = not new and bool(rc["team"]) and team.lower() == rc["team"].lower()  # l'exception ne vaut que pour un repost
    strict = kind in STRICT_KINDS
    season = season_of(src["stem"]) if (src.get("season") or is_series(src["stem"])) else None
    items = []
    for t in found:
        size = t.get("size") or 0
        cat = bool(media) and (t.get("media") or "") == media and (not res or (t.get("encoding") or "").lower() == res)
        if season is not None and season_of(t.get("fileName") or "") != season:
            cat = False  # autre saison de la série : pas un doublon
        close = bool(size) and abs(size - src["size"]) <= rc["size_tolerance"] * max(size, src["size"])
        langs = same_langs(ours, lang_sig(t.get("fileName") or ""))
        same = size == src["size"] or size in src.get("sizes", ()) or (approx and bool(size) and abs(size - src["size"]) <= 0.003 * size)
        if same and new and cat:
            k = "dupe"  # nouvelle release : même taille = même disque déjà remuxé, doublon
        elif same and not new:
            k = "identical"
        elif not cat:
            k = "other"
        elif not strict:
            k = "review"
        else:
            k = "dupe" if close and langs else "review" if new else "same"
        why = [] if k not in ("same", "review") or not strict else [x for x, ok in (("taille différente", close), ("langues différentes", langs)) if not ok]
        items.append({
            "id": t.get("torrentId"), "name": t.get("fileName") or "", "group": t.get("groupName") or "",
            "year": t.get("groupYear"), "size": size, "media": t.get("media") or "", "format": t.get("format") or "",
            "encoding": t.get("encoding") or "", "seeders": t.get("seeders"), "files": t.get("fileCount"),
            "url": t.get("detailsUrl") or "", "kind": k, "why": ", ".join(why),
            "dead": t.get("seeders") == 0,  # l'API ne donne pas la dernière activité : 0 seeder = mort probable
            # candidat au cross-seed : même taille exacte ; la liste des fichiers du .torrent HDF est comparée au
            # moment du cross-seed, et qBittorrent revérifie tout avant de seeder
            "crossseed": size == src["size"] or size in src.get("sizes", ()),
        })
    order = {"identical": 0, "dupe": 1, "review": 2, "same": 3, "other": 4}
    items.sort(key=lambda x: (order[x["kind"]], -(x["seeders"] or 0)))
    ident = [i for i in items if i["kind"] == "identical"]
    dupes = [i for i in items if i["kind"] == "dupe"]
    review = [i for i in items if i["kind"] == "review"]
    # identique sans seeder → on remet le même torrent en seed. Doublon sans seeder → « replaceable » = à vérifier :
    # seule la note modo de la fiche HDF (inactif > 2 mois, reseed demandé sans succès depuis 7 jours) autorise le
    # remplacement, et l'API ne la donne pas. L'upload reste bloqué tant que l'utilisateur ne l'a pas confirmée.
    if kind not in REL_KINDS:
        status = "refused" if kind == "webrip" else "unknown"
    elif ident:
        status = "reseed" if all(i["dead"] for i in ident) else "identical"
    elif dupes and not new:
        status = "allowed" if exempt else "replaceable" if all(i["dead"] for i in dupes) else "dupe"
    elif dupes or review:
        # nouvelle release (règle stricte) : remux de même résolution tous sans seeder → « replaceable », c'est-à-dire
        # à vérifier : seule la note modo « Remplacement autorisé » de leur fiche permet le remplacement
        if new and strict and all(i["dead"] for i in dupes + review):
            status = "replaceable"
        elif any(not i["dead"] for i in dupes):
            status = "dupe"
        else:
            status = "review"
    else:
        status = "absent"
    return {"status": status, "items": items[:40], "team": team, "exempt": exempt, "kind": kind, "media": media,
            "res": res}


def repost_check(src: dict, tmdb_id, quick: bool = False, refresh: bool = False) -> dict:
    """Vérification HDF d'une release (src de pick_source), enregistrée dans repost_checks.
    Complète : tous les torrents de l'œuvre (TMDB), puis le nom de la release si aucun doublon exact n'a été vu.
    quick (vérification de masse) : l'œuvre seule (cache 6 h des films partagé avec les userscripts) ; le nom seulement
    sans TMDB. Série : pas de filtre de catégorie HDF, et seule la même saison compte (classify)."""
    found = {}
    series = src.get("season") or is_series(src["stem"])
    if src["kind"] in REL_KINDS:  # WEBRip (refusé) ou catégorie indéterminée : pas d'appel HDF
        if quick and tmdb_id and not series:
            found = {t.get("torrentId"): t for t in hdf_film(LookupSpec(tmdb=tmdb_id, refresh=refresh))[0]}
        else:
            api = HdfApi()
            if tmdb_id:
                params = {"search_type": "advanced", "tmdbid": tmdb_id, "group_results": 0, "limit": 50}
                if not series:
                    params["filter_cat"] = "1,2"
                for t in api.call("browse", **params).get("results") or []:
                    found[t.get("torrentId")] = t
            if not tmdb_id or (not quick and not any(t.get("size") == src["size"] for t in found.values())):
                for t in api.call("browse", searchstr=src["name"], group_results=0, limit=50).get("results") or []:
                    found[t.get("torrentId")] = t
    result = {**classify(src, list(found.values())), "tmdb_id": tmdb_id, "with_tmdb": bool(tmdb_id), "quick": quick}
    now = time.time()
    db_exec("INSERT OR REPLACE INTO repost_checks (hash, status, result, at) VALUES (?,?,?,?)",
            (src["hash"], result["status"], json.dumps(result), now))
    return {**result, "at": now}


# ------------------------------------------------------------------ repost : analyse MediaInfo (règles HDF du fichier)
# (format, codec) : (débit min kbps, CRF max, débit min animation, CRF max animation) — règles 4.3, 4.6
MI_ENC = {("1080p", "x264"): (8000, 19, 7000, 21), ("1080p", "x265"): (4500, 20, 4500, 22),
          ("1080p", "AV1"): (4000, 24, 4000, 24), ("2160p", "x265"): (18000, 18, 8000, 20),
          ("2160p", "AV1"): (12000, 22, 12000, 22)}
MI_MHD = {"x264": (3000, 23, 3000, 24), "x265": (3000, 22, 3000, 23)}  # règle 4.5 (1080p)
MI_WEB = {("1080p", "AVC"): (3500, 2500), ("1080p", "HEVC"): (2500, 2500), ("1080p", "AV1"): (1800, 1800),
          ("2160p", "HEVC"): (11000, 8000), ("2160p", "AV1"): (8000, 8000)}  # règle 5 : (min, min animation)
MI_AUDIO_OK = {"TrueHD", "DTS-HD", "DTS", "EAC3", "AC3", "PCM", "FLAC", "AAC"}


def _num(v):
    try:
        return float(str(v).split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def mi_afmt(a: dict) -> str:
    f, c, x = a.get("Format") or "", a.get("Format_Commercial_IfAny") or "", a.get("Format_AdditionalFeatures") or ""
    if f.startswith("MLP") or "TrueHD" in c:
        return "TrueHD"
    if f == "DTS":
        return "DTS-HD" if ("XLL" in x or "XBR" in x or "DTS-HD" in c or "DTS:X" in c) else "DTS"
    return {"E-AC-3": "EAC3", "AC-3": "AC3", "MPEG Audio": "MP3"}.get(f, "WMA" if f.startswith("WMA") else f)


def mi_analyze(tracks: list, kind: str, orig: str = "", anim: bool = False, team: str = "") -> dict:
    """Conformité d'un MKV aux règles d'upload HDF vérifiables sur le fichier (critères de l'onglet Réglages).
    checks : [{level: bad|warn|ok, text}]."""
    R = hdf_rules()
    ENC = {tuple(k.split()): v for k, v in R["enc"].items()}
    WEB = {tuple(k.split()): v for k, v in R["web"].items()}
    X, TR = R["x264"], R["transcode"]
    vid = next((t for t in tracks if t.get("@type") == "Video"), {})
    auds = [t for t in tracks if t.get("@type") == "Audio"]
    subs = [t for t in tracks if t.get("@type") == "Text"]
    chapters = any(t.get("@type") == "Menu" for t in tracks)
    checks = []
    say = lambda lvl, txt: checks.append({"level": lvl, "text": txt})  # noqa: E731
    lang = lambda t: (t.get("Language") or "und").split("-")[0].lower()  # noqa: E731
    forced = lambda s: s.get("Forced") == "Yes" or "forc" in (s.get("Title") or "").lower()  # noqa: E731
    extra = lambda a: bool(re.search(r"comment|descri|\bAD\b", a.get("Title") or "", re.I))  # noqa: E731

    # ---- vidéo
    w, h = int(_num(vid.get("Width")) or 0), int(_num(vid.get("Height")) or 0)
    res = "2160p" if w >= 3000 or h >= 1700 else "1080p" if w >= 1800 or h >= 1000 else "720p" if w >= 1200 or h >= 700 else "SD"
    fmt = vid.get("Format") or ""
    lib = (vid.get("Encoded_Library_Name") or vid.get("Encoded_Library") or "").lower()
    st = {}
    for kv in (vid.get("Encoded_Library_Settings") or "").split(" / "):
        if "=" in kv:
            k, v = kv.split("=", 1)
            st[k.strip()] = v.strip()
    enc = "x264" if "x264" in lib else "x265" if "x265" in lib else "AV1" if fmt == "AV1" else ""
    shown = _num(vid.get("BitRate")) or _num(vid.get("BitRate_Nominal"))
    kbps = shown / 1000 if shown else _num(st.get("bitrate"))
    crf = _num(st.get("crf")) if st.get("rc", "crf") == "crf" else None
    depth = int(_num(vid.get("BitDepth")) or 0)
    desc = " ".join(x for x in (enc or fmt, res, f"{kbps:.0f} kbps" if kbps else "", f"CRF {crf:g}" if crf is not None else "") if x)
    if not vid:
        say("bad", "aucune piste vidéo")
    elif not shown:
        say("bad", "débit vidéo absent du MediaInfo : obligatoire (règle 1.8)")
    if vid and res == "SD":
        say("bad", f"définition SD ({w}x{h}) : HD seulement")
    elif res == "720p" and kind in ("rip", "mhd", "webdl") and R["refuse_720p"]:
        say("bad", f"720p ({w}x{h}) : interdit, sauf exception du staff si aucune source 1080p n'existe")

    def meets(r):
        if not r:
            return None
        mk, mc = (r[2], r[3]) if anim else (r[0], r[1])
        return (crf is not None and crf <= mc) or (kbps is not None and kbps >= mk)

    def need(r):
        mk, mc = (r[2], r[3]) if anim else (r[0], r[1])
        return f"{mk:g} kbps minimum ou CRF ≤ {mc:g}" + (" (animation)" if anim else "")

    if kind in ("rip", "mhd") and vid:
        if not enc:
            say("warn", "encodeur et réglages absents du MediaInfo : conformité de l'encode invérifiable")
        if enc == "x264":
            for key, lab in (("subme", "subme"), ("me_range", "me-range"), ("bframes", "bframes"), ("ref", "ref")):
                v = _num(st.get(key))
                if v is not None and v < X[key]:
                    say("bad", f"x264 {lab}={v:g} : {X[key]:g} minimum")
            if st.get("rc") == "abr":
                say("bad", "x264 en ABR 1 passe : interdit")
            prof, lvl = vid.get("Format_Profile") or "", _num(vid.get("Format_Level"))
            if prof and not prof.startswith("High"):
                say("bad", f"profil {prof} : High obligatoire")
            if lvl is not None and lvl < X["level"]:
                say("warn", f"level {lvl:g} : {X['level']:g} minimum")
        if enc == "x265" and st.get("rc") == "abr" and st.get("stats-read", "0") == "0":
            say("bad", "x265 en ABR sans 2e passe (stats-read=0) : interdit")
        if enc in R["ten_bits"] and depth and depth != 10:
            say("bad", f"{enc} en {depth} bits : 10 bits obligatoire")
        rule, mhd = ENC.get((res, enc)), (R["mhd"].get(enc) if res == "1080p" else None)
        mhd_team = team.lower() in {t.lower() for t in R["mhd_teams"]}
        if kind == "rip" and rule:
            if meets(rule):
                say("ok", f"débit d'encode conforme ({desc})")
            elif meets(mhd):
                say("ok" if mhd_team else "bad", f"débit de mHD ({desc}) : " + ("team mHD autorisée" if mhd_team else
                    f"réservé aux teams {', '.join(R['mhd_teams'])} ; encode {res} : {need(rule)}"))
            else:
                say("bad", f"débit trop faible ({desc}) : {need(rule)}")
        elif kind == "rip" and enc and res in ("1080p", "2160p"):
            say("warn", f"{enc} en {res} : codec non prévu par les règles")
        if kind == "mhd" and mhd:
            say("ok" if meets(mhd) else "bad", f"débit mHD ({desc})" + ("" if meets(mhd) else f" : {need(mhd)}"))
    if kind == "webdl" and vid:
        r = WEB.get((res, fmt))
        mk = (r[1] if anim else r[0]) if r else None
        if mk and kbps and kbps < mk:
            say("bad", f"débit vidéo {kbps:.0f} kbps : {mk:g} minimum pour un WEB-DL {res} {fmt}")
        elif mk and kbps:
            say("ok", f"débit WEB-DL conforme ({desc})")

    # ---- audio
    if not auds:
        say("bad", "aucune piste audio")
    allowed = set(R["audio_formats"])
    for a in auds:
        f, L = mi_afmt(a), lang(a)
        abr, ch = (_num(a.get("BitRate")) or 0) / 1000, int(_num(a.get("Channels")) or 0)
        if f not in allowed:
            say("bad", f"audio {f} ({L}) : format non admis")
        elif f == "AAC" and R["aac_only_web"] and kind != "webdl" and not extra(a):
            say("bad", f"audio AAC ({L}) : réservé aux WEB-DL, commentaires et audiodescriptions")
        wa = R["web_audio"].get(f)
        if kind == "webdl" and abr and wa and abr < wa:
            say("bad", f"audio {f} ({L}) à {abr:.0f} kbps : {wa:g} minimum")
        if kind in ("rip", "mhd") and f in ("AC3", "EAC3") and abr and not extra(a):
            mini = TR["7.1 EAC3"] if ch >= 8 and f == "EAC3" else TR["5.1"] if ch >= 6 else TR["par canal"] * max(ch, 1)
            if abr < mini:
                say("warn", f"{f} {ch} canaux ({L}) à {abr:.0f} kbps : {mini:g} minimum pour un transcodage "
                            "(toléré si c'est l'AC3 d'origine du disque)")
    main_auds = [a for a in auds if not extra(a)]
    langs = {lang(a) for a in main_auds}
    for L in sorted(langs):
        fs = [mi_afmt(a) for a in main_auds if lang(a) == L]
        if "DTS-HD" in fs and "DTS" in fs:
            say("bad", f"core DTS en double d'une piste DTS-HD ({L}) : interdit")
        if kind == "remux" and "TrueHD" in fs and "AC3" not in fs:
            say("bad", f"TrueHD ({L}) sans son core AC3 : à conserver")
    if auds and "fr" not in langs:
        say("warn", "pas de VF : obligatoire, sauf VF inexistante, film muet, concert…")
    if orig and orig != "fr":
        if orig not in langs:
            say("warn" if kind == "webdl" else "bad", f"VO ({orig}) absente des pistes audio")
        full = [s for s in subs if lang(s) == "fr" and not forced(s)]
        if not full:
            say("bad", "VO non francophone sans sous-titres français complets")
        elif kind == "remux" and not any((s.get("Format") or "") == "PGS" for s in full):
            say("warn", "sous-titres français complets hors PGS : PGS attendu pour un remux")
        if "fr" in langs and not any(lang(s) == "fr" and forced(s) for s in subs):
            say("warn", "pas de sous-titres français forcés : obligatoires s'ils existent sur la source")
    elif not orig:
        say("warn", "langue originale inconnue (choisis l'œuvre TMDB) : VO et sous-titres non vérifiés")
    others = sorted(langs - {"fr", orig, "und", ""} - {x.lower() for x in R["extra_langs"]})
    if others:
        say("warn", f"autres langues audio ({', '.join(others)}) : seulement si pertinentes")

    # ---- sous-titres, chapitres
    if any((s.get("Format") or "") == "VobSub" for s in subs):
        say("bad", "sous-titres VobSub : interdits")
    if R["chapters"] and not chapters and kind != "webdl":
        say("bad", "chapitres absents : obligatoires (sauf WEB-DL)")

    checks.sort(key=lambda c: {"bad": 0, "warn": 1, "ok": 2}[c["level"]])
    verdict = "refused" if any(c["level"] == "bad" for c in checks) else "check" if any(c["level"] == "warn" for c in checks) else "ok"
    tl = lambda t: f" — {t['Title']}" if t.get("Title") else ""  # noqa: E731
    return {"verdict": verdict, "checks": checks, "summary": {
        "video": " ".join(x for x in (fmt, f"({enc})" if enc and enc != fmt else "", f"{w}x{h}" if w else "",
                                      f"{depth} bits" if depth else "") if x),
        "res": res, "kbps": kbps, "crf": crf, "rc": st.get("rc"), "chapters": chapters,
        "audio": [f"{lang(a)} {mi_afmt(a)} {a.get('Channels') or '?'} can. {round((_num(a.get('BitRate')) or 0) / 1000)} kbps{tl(a)}"
                  for a in auds],
        "subs": [f"{lang(s)} {s.get('Format') or ''}{' forcés' if forced(s) else ''}{tl(s)}" for s in subs]}}


def tmdb_meta(tmdb_id, tv: bool = False) -> dict:
    """Langue originale et genres TMDB (16 = animation) d'un film ou d'une série."""
    out = {"original_language": "", "genres": []}
    if not tmdb_id or not tmdb_key():
        return out
    try:
        d = tmdb_get(f"/{'tv' if tv else 'movie'}/{int(tmdb_id)}", {})
    except Exception:
        return out
    return {"original_language": d.get("original_language") or "", "genres": [g.get("id") for g in d.get("genres") or []]}


def merge_mi(elig: str, why: list, mis: dict, hashes: list):
    """Ajoute le verdict de l'analyse MediaInfo (s'il existe) à l'éligibilité déduite du nom."""
    mi = next((mis[h] for h in hashes if h in mis), None)
    if not mi:
        return elig, why
    rank = {"ok": 0, "check": 1, "refused": 2}
    if rank.get(mi["verdict"], 0) > rank.get(elig, 0):
        elig = mi["verdict"]
    return elig, why + [f"MediaInfo : {c['text']}" for c in mi["checks"] if c["level"] in ("bad", "warn")]


class AnalyzeSpec(BaseModel):
    hashes: list[str]
    kind: str = ""
    tmdb_id: int | None = None
    variant: str = ""             # hash de la version à reposter parmi les cross-seeds (vide = la plus complète)


@app.post("/api/repost/analyze")
def api_repost_analyze(spec: AnalyzeSpec):
    src = pick_source(qbt(), spec.hashes, spec.kind, spec.variant)
    if src["disc"]:
        raise HTTPException(422, "FULL Blu-ray : contenu intact, rien à analyser ici (BDInfo fait au moment du repost)")
    f = Path(src["episodes"][0]) if src["season"] else Path(src["path"])
    if not f.is_file():
        raise HTTPException(404, f"Fichier introuvable dans le conteneur : {f}")
    tracks = mediainfo(f)
    if not tracks:
        raise HTTPException(502, f"MediaInfo n'a rien lu dans {f.name}")
    meta = tmdb_meta(spec.tmdb_id, src["season"] or is_series(src["stem"]))
    team = src["stem"].rsplit("-", 1)[1].strip() if "-" in src["stem"] else ""
    res = mi_analyze(tracks, src["kind"], meta["original_language"], 16 in meta["genres"], team)
    res.update(file=f.name, episodes=len(src["episodes"]) or None, tmdb=bool(spec.tmdb_id), kind=src["kind"],
               at=time.time(), text=mediainfo_text(f))
    db_exec("INSERT OR REPLACE INTO repost_mi (hash, result, at) VALUES (?,?,?)", (src["hash"], json.dumps(res), res["at"]))
    return res


@app.post("/api/repost/check")
def api_repost_check(spec: RepostRef):
    """Vérification complète d'une release, depuis son détail."""
    src = pick_source(qbt(), spec.hashes, spec.kind, spec.variant)
    if src["kind"] not in REL_KINDS:
        raise HTTPException(422, "WEBRip : refusé sur HD-Forever" if src["kind"] == "webrip"
                            else "Catégorie indéterminée d'après le nom : choisis-la dans le détail")
    return repost_check(src, spec.tmdb_id)


class BulkSpec(BaseModel):
    releases: list[list[str]]     # hashes de chaque release de la liste filtrée
    refresh: bool = False


@app.post("/api/repost/checkall")
def api_repost_checkall(spec: BulkSpec):
    rels = [[h for h in r if re.fullmatch(r"[0-9a-fA-F]{40}", h or "")] for r in spec.releases][:2000]
    rels = [r for r in rels if r]
    if not rels:
        raise HTTPException(400, "Aucune release à vérifier")
    HdfApi()  # clé API absente : erreur tout de suite
    if db_rows("SELECT id FROM jobs WHERE kind='repostscan' AND status IN ('queued','running')"):
        raise HTTPException(409, "Une vérification de masse est déjà en cours")
    return {"id": enqueue("repostscan", f"Vérification HDF · {len(rels)} release(s)", "", "",
                          {"releases": rels, "refresh": spec.refresh})}


@app.get("/api/repost/checks")
def api_repost_checks():
    """Statut HDF de chaque release vérifiée (hash → statut), pour rafraîchir la liste sans relire qBittorrent."""
    return {r["hash"]: {"status": r["status"], "at": r["at"]} for r in db_rows("SELECT hash, status, at FROM repost_checks")}


@app.post("/api/repost/crossseed")
def api_repost_crossseed(spec: CrossSpec):
    """La release est déjà sur HDF à l'identique : on seede son torrent sur nos données, sans rien uploader.
    Fichier ou dossier, même méthode : le .torrent HDF doit lister des fichiers qui existent déjà chez nous, au même
    chemin et à la même taille (rien à télécharger). Il est ajouté arrêté ; le job attend la vérification de
    qBittorrent et ne le démarre qu'à 100 %, sinon il le retire (sans toucher aux fichiers)."""
    q = qbt()
    src = pick_source(q, spec.hashes)
    chk = check_for(src)
    item = next((i for i in (chk or {}).get("items", []) if i["id"] == spec.torrent_id), None)
    if not item or not item["crossseed"]:
        raise HTTPException(409, "Ce torrent HDF n'a pas été vérifié comme identique à notre release")
    same = next((v["hash"] for v in src["variants"] if item["size"] in (v["total"], v["big"])), None)
    if same and same != src["hash"]:
        src = pick_source(q, spec.hashes, prefer=same)  # la version qui a exactement le contenu du torrent HDF
    api = HdfApi()
    try:
        with urllib.request.urlopen(urllib.request.Request(api.download_url(spec.torrent_id),
                                                           headers={"User-Agent": "Postmuxarr"}), timeout=30) as r:
            data = r.read()
    except Exception as e:
        raise HTTPException(502, f"Téléchargement du .torrent HDF impossible : {e}")
    if not data.startswith(b"d"):
        raise HTTPException(502, "HDF n'a pas renvoyé un .torrent (clé API ou droits ?)")
    try:
        theirs = torrent_files(data)
    except Exception as e:
        raise HTTPException(422, f".torrent HDF illisible : {e}")
    t = q.json("torrents/info?hashes=" + src["hash"])[0]
    ours = {posixpath.join(t["save_path"], f["name"].replace("\\", "/")): f["size"]
            for f in q.json("torrents/files?hash=" + src["hash"]) or []}
    # dossier de destination : celui de notre torrent (même dossier racine), ou celui du fichier (torrent HDF à 1 fichier)
    savepath = None
    for cand in dict.fromkeys([t["save_path"], posixpath.dirname(src["qbt_path"])]):
        if all(ours.get(posixpath.join(cand, p)) == n for p, n in theirs.items()):
            savepath = cand
            break
    if savepath is None:
        diff = [p for p, n in theirs.items() if ours.get(posixpath.join(t["save_path"], p)) != n][:3]
        raise HTTPException(409, "Contenu différent du torrent HDF (noms ou tailles) : cross-seed impossible sans "
                                 "télécharger. Fichiers en cause : " + ", ".join(diff))
    h = info_hash(data)
    present = q.json("torrents/info?hashes=" + h) or []
    if present and not str(present[0].get("state") or "").startswith(("stopped", "paused")):
        raise HTTPException(409, "Ce torrent HDF est déjà dans qBittorrent, et actif")
    payload = {"hash": h, "torrent": base64.b64encode(data).decode(), "savepath": savepath,
               "source_hash": src["hash"], "torrent_id": spec.torrent_id, "files": len(theirs),
               "present": bool(present)}  # déjà ajouté arrêté (essai précédent) : reprise à la vérification
    return {"id": enqueue("crossseed", f"Cross-seed · {item['name'] or src['stem']}", src["hash"], "", payload)}


@app.post("/api/repost/upload")
def api_repost_upload(spec: RepostSpec):
    q = qbt()
    chk = check_for(pick_source(q, spec.hashes, prefer=spec.variant))
    if not chk:
        raise HTTPException(409, "Vérifie d'abord la release sur HDF")
    src = pick_source(q, spec.hashes, chk.get("kind") or "remux", spec.variant)  # la catégorie vérifiée fait foi
    kind = src["kind"]
    if kind not in REL_KINDS:
        raise HTTPException(422, "WEBRip : refusé sur HD-Forever" if kind == "webrip" else "Catégorie indéterminée")
    path = Path(src["path"])
    if not any(within(path, r) for r in media_roots()) or not src["exists"]:
        raise HTTPException(400, f"Release introuvable dans le conteneur : {src['path']} (qbittorrent.path_map de config.yml ?)")
    st = chk["status"]
    if st in ("identical", "crossseeded", "reseed"):
        raise HTTPException(409, "Déjà sur HDF à l'identique : cross-seede-la ou remets-la en seed plutôt")
    if st in ("dupe", "similar"):  # similar : vérification faite avec l'ancienne règle
        raise HTTPException(409, f"Doublon sur HDF ({REL_KINDS[kind]} de taille proche, mêmes langues) : repost interdit")
    if st == "replaceable" and not spec.force:
        raise HTTPException(409, "Doublon sans seeder : confirme que la fiche HDF autorise le remplacement (note modo)")
    if st == "review" and not spec.force:
        raise HTTPException(409, f"Un {REL_KINDS[kind]} de même résolution est déjà sur HDF : confirme l'upload")
    if st not in ("absent", "allowed", "replaceable", "review"):
        raise HTTPException(409, "Revérifie la release sur HDF")
    out, extra = path, {}
    if src["season"]:
        # HDF : dans le torrent, seulement les épisodes et le .nfo d'origine (ni sample, ni sous-titres à part, ni bonus)
        keep = list(src["episodes"]) + sorted(str(x) for x in path.iterdir() if x.is_file() and x.suffix.lower() == ".nfo")
        kept = set(keep)
        extras = [x for x in path.rglob("*") if x.is_file() and str(x) not in kept]
        if extras or src["clean"] != path.name:
            out = link_target(path) / src["clean"]
            if out.exists() and not out.is_dir():
                raise HTTPException(409, f"{out} existe déjà et n'est pas un dossier")
        extra = {"season": True, "episodes": keep, "extras": len(extras)}
    elif kind == "remux" and src["clean"] != path.stem:  # nos remux : torrent sur un hardlink au nom nettoyé
        out = link_target(path) / f"{src['clean']}.mkv"
        if out.exists() and not os.path.samefile(out, path):
            raise HTTPException(409, f"{out} existe déjà et n'est pas ce fichier")
    ensure_free(out)
    done = db_rows("SELECT id, tfile FROM jobs WHERE kind='repost' AND output=? AND status IN ('done','warning') "
                   "ORDER BY id DESC LIMIT 1", (str(out),))
    if done and done[0]["tfile"] and Path(done[0]["tfile"]).is_file():
        raise HTTPException(409, f"Torrent déjà créé : utilise le job #{done[0]['id']} (bouton « Uploader sur… »)")
    spec.torrent.enabled = True
    payload = {**spec.model_dump(), "mkv": str(path), "qbt_path": src["qbt_path"], "source_hash": src["hash"],
               "rel_kind": kind, **extra}
    return {"id": enqueue("repost", rel_label(out), src["hash"], str(out), payload)}


# =========================================================================== HDF : disponibilité (userscripts des autres trackers)

class LookupRelease(BaseModel):
    name: str
    size: int = 0
    approx: bool = False          # taille arrondie (« 11.76 GiB ») : « identique » à 0,3 % près
    remux: bool = False           # le tracker l'indique comme remux, même si le nom ne le dit pas
    kind: str = ""                # catégorie connue du tracker (clé de REL_KINDS), sinon déduite du nom


class LookupSpec(BaseModel):
    imdb: str = ""                # « tt0079003 » ou « 0079003 »
    tmdb: int | None = None
    title: str = ""               # repli sans identifiant : titre + année
    year: str = ""
    alt: str = ""                 # autre titre (ex. titre français entre parenthèses) : 2e recherche si la 1re ne trouve rien
    new: bool = False             # nouvelle release (onglet Remux) : règle de doublon stricte, sans exception de team
    releases: list[LookupRelease] = []
    refresh: bool = False


def hdf_film(spec: LookupSpec):
    """Torrents HDF d'un film (1 appel API, mis en cache). Renvoie (torrents, date du cache, critère)."""
    imdb = re.sub(r"\D", "", spec.imdb or "")
    if imdb:
        key, params = f"imdb:{imdb}", {"search_type": "advanced", "imdbid": f"tt{imdb}", "filter_cat": "1,2"}
    elif spec.tmdb:
        key, params = f"tmdb:{spec.tmdb}", {"search_type": "advanced", "tmdbid": spec.tmdb, "filter_cat": "1,2"}
    elif spec.title.strip():
        q = f"{spec.title.strip()} {spec.year}".strip()
        key, params = f"title:{q.lower()}", {"searchstr": q}
    else:
        raise HTTPException(400, "Ni IMDb, ni TMDB, ni titre")
    ttl = repost_cfg()["lookup_ttl"]
    rows = db_rows("SELECT result, at FROM hdf_lookup WHERE key=?", (key,))
    if rows and not spec.refresh and time.time() - rows[0]["at"] < ttl:
        return json.loads(rows[0]["result"]), rows[0]["at"], key
    found = HdfApi().call("browse", group_results=0, limit=50, **params).get("results") or []
    now = time.time()
    db_exec("INSERT OR REPLACE INTO hdf_lookup (key, result, at) VALUES (?,?,?)", (key, json.dumps(found), now))
    return found, now, key


@app.post("/api/hdf/lookup")
def api_hdf_lookup(spec: LookupSpec):
    """Le film est-il sur HDF, dans quels formats, et chaque release de la page serait-elle un doublon ?"""
    found, at, key = hdf_film(spec)
    if not found and key.startswith("title:") and spec.alt.strip():
        found, at, key = hdf_film(spec.model_copy(update={"title": spec.alt}))
    groups = {t.get("groupId") for t in found}
    ambiguous = key.startswith("title:") and len(groups) > 1
    torrents = [{
        "name": t.get("fileName") or "", "group": t.get("groupName") or "", "year": t.get("groupYear"),
        "media": t.get("media") or "", "encoding": t.get("encoding") or "", "format": t.get("format") or "",
        "size": t.get("size") or 0, "seeders": t.get("seeders"), "url": t.get("detailsUrl") or "",
    } for t in found]
    torrents.sort(key=lambda t: (t["group"], -int(re.sub(r"\D", "", t["encoding"]) or 0), t["media"], -(t["size"] or 0)))
    releases = []
    for r in spec.releases:
        stem = re.sub(r"\.(mkv|iso)$", "", r.name, flags=re.I)
        kind = r.kind if r.kind in REL_KINDS else "remux" if r.remux else rel_kind(stem)
        if kind not in REL_KINDS:  # WEBRip : refusé ; indéterminée : pas de verdict
            releases.append({"name": r.name, "status": "refused" if kind == "webrip" else "na"})
            continue
        if ambiguous:
            releases.append({"name": r.name, "status": "ambiguous"})
            continue
        src = {"name": r.name, "stem": stem, "size": r.size, "kind": kind,
               "clean": release_safe(stem) if kind == "remux" else stem}
        c = classify(src, found, approx=r.approx, new=spec.new)
        releases.append({"name": r.name, "status": c["status"], "kind": kind, "items": c["items"] if spec.new else None,
                         "matches": [i["name"] for i in c["items"] if i["kind"] in ("identical", "dupe", "review")][:3]})
    return {"torrents": torrents, "releases": releases, "at": at, "ambiguous": ambiguous,
            "films": sorted({f"{t['group']} ({t['year']})" for t in torrents}) if ambiguous else []}


# =========================================================================== HDF : analyse d'un dossier de Blu-ray

def scan_targets(folder: Path) -> list:
    """Blu-ray (dossiers BDMV) et ISO directement dans le dossier."""
    out = []
    for e in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
        if e.name.startswith((".", "@", "#")):
            continue
        if (e.is_dir() and (e / "BDMV" / "PLAYLIST").is_dir()) or (e.is_file() and e.suffix.lower() == ".iso"):
            out.append(e)
    return out


def remuxed(disc: Path) -> bool:
    """Un remux terminé existe déjà pour ce disque (playlist du dossier, ou de son ISO extrait)."""
    key = disc.stem if disc.suffix.lower() == ".iso" else None
    for r in db_rows("SELECT source FROM jobs WHERE kind='remux' AND status IN ('done','warning')"):
        src = Path(r["source"] or "")
        if within(src, disc) or (key and src.suffix.lower() == ".mpls" and disc_root(src).name == key):
            return True
    return False


def scan_disc(disc: Path, refresh: bool = False) -> dict:
    """Verdict « nouvelle release » pour un disque : film (nom → TMDB), playlist principale, puis HDF (règle stricte)."""
    iso = disc.suffix.lower() == ".iso"
    name = disc.stem if iso else disc.name
    title, year = title_hint(disc)
    out = {"name": name, "title": title, "year": year, "iso": iso}
    if remuxed(disc):
        out["done"] = True
    if iso:
        size, height, langs = disc.stat().st_size, 0, None
    else:
        main_pl = next((p for p in disc_playlists(disc) if not p["loop"] and p["duration"] >= 300), None)
        if not main_pl:
            return {**out, "status": "error", "error": "Aucune playlist principale lisible"}
        size, height, langs = main_pl["size"], main_pl["height"], main_pl.get("langs") or None
        out.update(playlist=main_pl["name"], duration=main_pl["duration"])
    res = rel_res(name) or ("2160p" if height > 1100 else "1080p" if height > 720 else "720p" if height else "")
    out.update(size=size, res=res)
    pick, _ = tmdb_guess(title, year, langs=langs, name=name)
    out["tmdb_id"] = (pick or {}).get("id")
    if pick:
        out["tmdb_title"] = f"{pick['title']} ({pick.get('year') or '?'})"
        out["partial"] = bool(pick.get("partial"))
        out["tmdb_query"] = pick.get("query") or title
    elif tmdb_key():
        # pas de film : une recherche HDF par ce titre dirait « absent » à tort
        return {**out, "status": "nofilm", "error": f"Rien sur TMDB pour « {title} »"}
    spec = LookupSpec(tmdb=out["tmdb_id"], title=title, year=year, refresh=refresh)
    found, _, key = hdf_film(spec)
    if key.startswith("title:") and len({t.get("groupId") for t in found}) > 1:
        return {**out, "status": "ambiguous", "count": len(found)}
    stem = name if token(r"REMUX", name) else f"{name}.REMUX"
    stem = stem if rel_res(stem) else f"{stem}.{res}" if res else stem
    c = classify({"name": stem + ".mkv", "stem": stem, "size": size, "kind": "remux", "clean": stem},
                 found, approx=True, new=True)
    return {**out, "status": c["status"], "count": len(found),
            "matches": [{"name": i["name"], "kind": i["kind"], "size": i["size"], "url": i["url"], "why": i["why"]}
                        for i in c["items"] if i["kind"] in ("dupe", "review")][:4]}


class ScanSpec(BaseModel):
    path: str
    refresh: bool = False


@app.post("/api/hdf/scan")
def api_hdf_scan(spec: ScanSpec):
    folder = readable(spec.path)
    if not folder.is_dir():
        raise HTTPException(400, "Ce n'est pas un dossier")
    if not scan_targets(folder):
        raise HTTPException(400, "Aucun Blu-ray ni ISO dans ce dossier")
    HdfApi()  # clé API absente : erreur tout de suite plutôt qu'en fin de job
    if db_rows("SELECT id FROM jobs WHERE kind='hdfscan' AND source=? AND status IN ('queued','running')", (str(folder),)):
        raise HTTPException(409, "Analyse déjà en cours pour ce dossier")
    return {"id": enqueue("hdfscan", f"Analyse HDF · {folder.name}", str(folder), "", spec.model_dump())}


@app.get("/api/hdf/scan")
def api_hdf_scan_results(path: str):
    folder = readable(path)
    out = {}
    for d in scan_targets(folder) if folder.is_dir() else []:
        rows = db_rows("SELECT result, at FROM hdf_scan WHERE path=?", (str(d),))
        if rows:
            out[str(d)] = {**json.loads(rows[0]["result"]), "at": rows[0]["at"]}
    return out


# =========================================================================== HDF (userscript d'upload)

HDF_LANGS = ("VFI", "VFF", "VFQ", "VO", "VOF", "VOQ")
HDF_CODECS = ("AVC", "HEVC", "VC-1", "MPEG-2", "AV1", "x264", "x265", "H264", "H265")
HDF_VERSIONS = [  # (motif dans le nom, après l'année ; valeur de la case « Versions » de HDF)
    (r"remaster(?:ed|isee?)?|restauration(?:\.4k)?|restored", "RM"), (r"directors?\.?cut|dc", "DC"), (r"extended|version\.longue", "VL"),
    (r"uncut", "UC"), (r"unrated", "UR"), (r"2in1", "2in1"), (r"nf", "NF"), (r"amzn", "AMZN"),
    (r"atvp", "ATVP"), (r"cnlp|canal\+?|canalplus", "CNLP"), (r"dsnp", "DSNP"), (r"criterion", "Crit"),
    (r"custom|hybrid", "Cust"), (r"hdr", "HDR"), (r"hdr10(?:\+|plus)", "HDR10+"), (r"dv|dovi", "DV"),
    (r"imax", "IMAX"), (r"bonus", "Bonus"),
]


def tmdb_original_language(tmdb_id, tv: bool = False) -> str:
    """Langue originale du film (ou de la série) sur TMDB (« fr », « en »…), vide si inconnue."""
    if not tmdb_id or not tmdb_key():
        return ""
    try:
        return tmdb_get(f"/{'tv' if tv else 'movie'}/{int(tmdb_id)}", {}).get("original_language") or ""
    except Exception:
        return ""


BD_LANG = {"french": "fre", "english": "eng", "german": "ger", "italian": "ita", "spanish": "spa", "japanese": "jpn"}


def bdinfo_tracks(report: str) -> list:
    """Pistes audio et sous-titres d'un rapport BDInfo, au format de mkvmerge -J (type, properties.language)."""
    tracks, section = [], None
    for line in report.splitlines():
        head = line.strip().rstrip(":").upper()
        if head in ("AUDIO", "SUBTITLES"):
            section = "audio" if head == "AUDIO" else "subtitles"
            continue
        if head in ("FILES", "CHAPTERS", "STREAM DIAGNOSTICS", "VIDEO", "QUICK SUMMARY", "PLAYLIST REPORT"):
            section = None
            continue
        cols = re.split(r"\s{2,}", line.strip())
        if section and len(cols) >= 3 and not set(cols[0]) <= set("-") and cols[0].lower() != "codec":
            lang = cols[1].strip().lower()
            tracks.append({"type": section, "properties": {
                "language": BD_LANG.get(lang, lang[:3] or "und"), "track_name": cols[-1] if section == "audio" else ""}})
    return tracks


def hdf_info(mkv: Path, kind: str = "", report: str = "") -> dict:
    """Ce que le formulaire d'upload de HDF demande, déduit des pistes (MKV, ou rapport BDInfo d'un disque) et du nom."""
    season = season_dir(mkv)
    disc = (mkv.is_dir() and not season) or mkv.suffix.lower() == ".iso"
    name = rel_label(mkv)
    series = season or is_series(name)
    kind = kind or rel_kind(name, disc)
    m = re.search(r"[.\s](?:19|20)\d{2}[.\s](.*)$", name)
    tail = m.group(1) if m else name  # le titre du film n'entre pas dans la détection
    langs, srt, fr_unknown, foreign = set(), False, False, False
    tracks = bdinfo_tracks(report) if disc else identify(first_episode(mkv) if season else mkv).get("tracks", [])
    for t in tracks:
        p = t.get("properties", {})
        lang, tname = p.get("language") or "und", p.get("track_name") or ""
        fr = lang in ("fre", "fra", "fr")
        if t["type"] == "audio":
            tok = next((w for w in re.split(r"[\s.\-_/()]+", tname.upper()) if w in HDF_LANGS), None)
            if tok:
                langs.add(tok)
            elif fr:
                fr_unknown = True  # piste française dont le nom ne dit pas la variante (« French », vide…)
            elif lang != "und":
                foreign = True  # VO décidée plus bas : sur un film français, une piste étrangère est un doublage
        elif t["type"] == "subtitles" and fr and not p.get("forced_track") and "forc" not in tname.lower():
            srt = True  # sous-titres français complets ou SDH (BDInfo ne distingue pas les forcés)
    # releases d'autres teams : les noms de pistes ne disent pas toujours VFF/VFQ, le nom de release si
    for w in re.split(r"[.\s_-]+", tail.upper()):
        if w in HDF_LANGS:
            langs.add(w)
        elif w == "VF2":
            langs |= {"VFF", "VFQ"}
        elif w == "TRUEFRENCH":
            langs.add("VFF")
    meta = release_meta(mkv)
    notes = []
    orig = tmdb_original_language(meta["tmdb_id"], series) if (fr_unknown or foreign) else ""
    if foreign and orig != "fr":
        langs.add("VO")
    if fr_unknown and not langs & {"VFF", "VFQ", "VFI", "VOF", "VOQ"}:
        # « French » sur un film français = VOF. Sur un film étranger, VFF ou VFQ selon le cas : on ne coche pas.
        if orig == "fr" or (not orig and not foreign):
            langs.add("VOF")
        else:
            notes.append("Piste française sans variante sur un film étranger : coche VFF, VFQ ou VFI toi-même.")
    if disc and srt:
        notes.append("Sous-titres français d'après BDInfo, qui ne distingue pas les forcés : vérifie la case.")
    if kind == "webdl" and not any(v in ("NF", "AMZN", "ATVP", "CNLP", "DSNP") for v in
                                   [v for pat, v in HDF_VERSIONS if token(pat, tail)]):
        notes.append("Plateforme WEB inconnue d'après le nom : coche la source si tu la connais.")
    res, codec = rel_res(tail), next((c for c in HDF_CODECS if token(re.escape(c), tail)), "")
    if disc:  # « COMPLETE.BLURAY » ne dit souvent ni la résolution ni le codec : la 1re piste vidéo de BDInfo, si
        video = re.search(r"^VIDEO:\s*$(.*?)^AUDIO:", report, re.M | re.S)
        line = next((ln for ln in (video.group(1) if video else "").splitlines() if re.search(r"\bVideo\b", ln)), "")
        res = res or rel_res(line)
        codec = codec or next((c for k, c in (("AVC", "AVC"), ("HEVC", "HEVC"), ("VC-1", "VC-1"), ("MPEG-2", "MPEG-2"))
                               if k in line), "")
    tfile = torrent_of(mkv)
    return {
        "name": name,
        "tmdb_url": f"https://www.themoviedb.org/{'tv' if series else 'movie'}/{meta['tmdb_id']}" if meta["tmdb_id"] else None,
        "langs": [k for k in HDF_LANGS if k in langs], "srt": srt, "lang_note": " ".join(notes),
        "versions": [v for pat, v in HDF_VERSIONS if token(pat, tail)],
        "codec": codec,
        "res": res,
        "media": REL_KINDS.get(kind, ""),
        "team": name.rsplit("-", 1)[1] if "-" in name else "",
        "torrent_name": tfile.name,
        "torrent": base64.b64encode(tfile.read_bytes()).decode(),
    }


def hdf_job_mkv(row: dict):
    mkv, t = job_files(row)
    return mkv if t else None


@app.get("/api/hdf")
def api_hdf_list():
    """Releases prêtes à uploader (MKV + .torrent), les plus récentes d'abord."""
    seen, out = set(), []
    for r in db_rows("SELECT id, kind, output, tfile FROM jobs WHERE kind IN ('remux','retouch','torrent','repost') "
                     "AND status IN ('done','warning') ORDER BY id DESC LIMIT 100"):
        mkv = hdf_job_mkv(r)
        if mkv and str(mkv) not in seen:
            seen.add(str(mkv))
            seed = db_rows("SELECT state FROM seeds WHERE path=?", (str(mkv),))
            out.append({"id": r["id"], "name": rel_label(mkv), "seed": seed[0]["state"] if seed else None})
    return out[:15]


@app.get("/api/hdf/{jid}")
def api_hdf(jid: int):
    rows = db_rows("SELECT id, kind, output, tfile, mediainfo, spec FROM jobs WHERE id=?", (jid,))
    if not rows:
        raise HTTPException(404, f"Job #{jid} introuvable dans Postmuxarr")
    mkv = hdf_job_mkv(rows[0])
    if not mkv:
        raise HTTPException(404, "Ce job n'a pas de release avec son .torrent")
    repost = rows[0]["kind"] == "repost"
    kind = (json.loads(rows[0]["spec"] or "{}").get("rel_kind") or "") if repost else ""
    report = rows[0]["mediainfo"] or ""
    if not report and (mkv.is_dir() or mkv.suffix.lower() == ".iso"):
        f = BDINFO_DIR / f"{disc_key(mkv)}.txt"
        report = f"REPOST\n\n{f.read_text(encoding='utf-8', errors='replace').strip()}\n" if f.is_file() else ""
        if not report:
            raise HTTPException(404, "Pas de rapport BDInfo pour ce disque : relance le repost")
    info = hdf_info(mkv, kind, report)
    info["mediainfo"] = report or mediainfo_text(
        mkv, "" if repost else release_meta(mkv)["source_label"], "REPOST" if repost else "")
    return info


@app.post("/api/hdf/{jid}/uploaded")
def api_hdf_uploaded(jid: int):
    """Signal du userscript : le site a accepté l'upload, on réannonce jusqu'au feu vert du tracker."""
    rows = db_rows("SELECT kind, output, tfile FROM jobs WHERE id=?", (jid,))
    mkv = hdf_job_mkv(rows[0]) if rows else None
    if not mkv:
        raise HTTPException(404, f"Job #{jid} sans MKV ni .torrent")
    watch_seed(mkv, seed_hash(mkv), delay=10)  # le temps que le site enregistre le torrent auprès du tracker
    return {"ok": True}


@app.get("/api/download")
def api_download(path: str):
    f = Path(path).resolve()
    if f.suffix.lower() != ".torrent" or not (within(f, OUTPUT_DIR) or within(f, REPOST_TORRENTS)) or not f.is_file():
        raise HTTPException(404, ".torrent introuvable")
    return FileResponse(f, media_type="application/x-bittorrent", filename=f.name)


@app.get("/api/jobs/{jid}/mediainfo", response_class=PlainTextResponse)
def api_job_mediainfo(jid: int):
    rows = db_rows("SELECT mediainfo FROM jobs WHERE id=?", (jid,))
    if not rows or not rows[0]["mediainfo"]:
        raise HTTPException(404, "Pas de MediaInfo pour ce job")
    return rows[0]["mediainfo"]


@app.get("/api/mediainfo", response_class=PlainTextResponse)
def api_mediainfo(path: str, label: str = ""):
    mkv = readable(path)
    if mkv.suffix.lower() != ".mkv":
        raise HTTPException(400, "MediaInfo : un fichier MKV est attendu")
    return mediainfo_text(mkv, label)


@app.get("/api/jobs/{jid}")
def api_job(jid: int):
    rows = db_rows("SELECT * FROM jobs WHERE id=?", (jid,))
    if not rows:
        raise HTTPException(404, "Job introuvable")
    j = rows[0]
    cmd = json.loads(j.pop("cmd") or "[]")
    j["command"] = shlex.join(cmd) if cmd else ""
    j.pop("spec", None)
    j.pop("mediainfo", None)
    return j


@app.post("/api/jobs/{jid}/cancel")
def api_cancel(jid: int):
    if worker.current == jid:
        worker.cancel = True
        if worker.proc and worker.proc.poll() is None:
            worker.proc.terminate()
        return {"ok": True}
    db_exec("UPDATE jobs SET status='cancelled', finished=? WHERE id=? AND status='queued'", (time.time(), jid))
    return {"ok": True}


class RemoveSpec(BaseModel):
    path: str


@app.post("/api/remux/remove")
def api_remux_remove(spec: RemoveSpec):
    """Supprime un remux raté pour le refaire : MKV, .torrent (et son seed dans qBittorrent), BDInfo joint,
    hardlink Plex, et les jobs qui le concernent. Jamais un fichier de repost (autre seed)."""
    mkv = readable(spec.path)
    if not editable(mkv):
        raise HTTPException(400, f"Suppression possible uniquement pour un MKV de {OUTPUT_DIR}")
    if db_rows("SELECT id FROM jobs WHERE output=? AND status IN ('queued','running')", (str(mkv),)):
        raise HTTPException(409, "Un job traite ce fichier : annule-le d'abord")
    removed = []
    h = None
    try:
        h = seed_hash(mkv)
    except Exception:
        pass  # pas de .torrent : rien à retirer de qBittorrent
    if h:
        try:
            q = Qbt(load_config())
        except RuntimeError:
            q = None  # qBittorrent non configuré
        if q:
            try:
                q.login()
                if q.json("torrents/info?hashes=" + h):
                    q._req("torrents/delete", {"hashes": h, "deleteFiles": "false"})
                    removed.append("seed qBittorrent")
            except Exception as e:  # on ne supprime rien tant que qBittorrent seede peut-être le fichier
                raise HTTPException(502, f"qBittorrent : {e} — rien n'a été supprimé")
    if plex_state(mkv) == "linked":
        (PLEX_DIR / mkv.name).unlink()  # même inode : seul le lien disparaît
        removed.append("hardlink Plex")
    others = sorted({torrent_of(mkv), mkv.with_suffix(".torrent")}) + [
        mkv.with_name(mkv.stem + ".bdinfo.txt"), mkv.with_name(mkv.stem + ".nfo")]
    for f in others:
        if f.is_file() and (within(f, OUTPUT_DIR) or within(f, REPOST_TORRENTS)):
            f.unlink()
            removed.append(f.name)
    mkv.unlink()
    removed.insert(0, mkv.name)
    for table, col in (("seeds", "path"), ("releases", "path"), ("jobs", "output")):
        db_exec(f"DELETE FROM {table} WHERE {col}=?", (str(mkv),))
    return {"removed": removed}


@app.delete("/api/jobs/{jid}")
def api_delete(jid: int):
    if worker.current == jid:
        raise HTTPException(409, "Job en cours : annule-le d'abord")
    db_exec("DELETE FROM jobs WHERE id=?", (jid,))
    return {"ok": True}


app.mount("/", StaticFiles(directory=APP_DIR / "static", html=True), name="static")
