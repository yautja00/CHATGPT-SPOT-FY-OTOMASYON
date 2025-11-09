#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, random, re, json, unicodedata, tempfile, uuid, logging, sys, math, base64
from collections import deque, defaultdict
from flask import Flask, request, redirect, jsonify, render_template
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheHandler
from redis_cache import TokenCache

# ====== CONFIG ======

SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private user-read-recently-played user-top-read ugc-image-upload"
DEFAULT_USER = "ahmet"

PRESETS = {
    "night_drive": {"seed_genres": ["chill","synthwave","indie","electropop","downtempo"],
                    "targets": {"energy":0.45,"danceability":0.55,"valence":0.35,"instrumentalness":0.2}},
    "focus": {"seed_genres": ["lofi","ambient","piano","beats","classical"],
              "targets": {"energy":0.25,"danceability":0.35,"valence":0.25,"instrumentalness":0.7}},
    "gym": {"seed_genres": ["edm","trap","hardstyle","big-room","rock"],
            "targets": {"energy":0.85,"danceability":0.7,"valence":0.55}},
    "happy_pop": {"seed_genres": ["pop","dance-pop","indie-pop","turkish-pop"],
                  "targets": {"energy":0.7,"danceability":0.7,"valence":0.8}},
    "melancholy": {"seed_genres": ["sad","indie","singer-songwriter","acoustic"],
                   "targets": {"energy":0.35,"danceability":0.4,"valence":0.2}},
}

TURKISH_QUERIES = [
    'genre:"turkish"','turkish indie','turkish alternative','turkce rock',
    'turkish rap','turkce pop','anatolian rock','arabesk'
]
NON_TR_QUERIES = [
    'genre:"chill"','indie electronic','lofi beats','synthwave','edm',
    'indie rock','ambient instrumental','piano instrumental'
]

# ====== LOGGING ======
logging.basicConfig(stream=sys.stdout, level=logging.INFO)

# ====== HELPERS ======

def _blend_targets(base, add, w):
    out = dict(base)
    add = add or {}
    for k, v in add.items():
        if v is None:
            continue
        bv = out.get(k, 0.5)
        out[k] = max(0.0, min(1.0, (1 - w) * bv + w * float(v)))
    return out

def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFD", s).encode("ascii","ignore").decode("utf-8")
    return " ".join(s.split())

def _filter_diversity(sp, uris, max_per_artist=2, sim_guard=True):
    if not uris:
        return uris
    tids = [u.split(":")[-1] for u in uris]
    meta = sp.tracks(tids)["tracks"]
    kept, count, feats_cache = [], {}, {}

    def feat(tid):
        if tid not in feats_cache:
            try:
                feats_cache[tid] = sp.audio_features([tid])[0]
            except Exception:
                feats_cache[tid] = None
            time.sleep(0.02)
        return feats_cache[tid]

    def cosine(v1, v2):
        a = sum(x*y for x,y in zip(v1,v2))
        n1 = math.sqrt(sum(x*x for x in v1)); n2 = math.sqrt(sum(y*y for y in v2))
        return a / (n1*n2 + 1e-9)

    for t, u in zip(meta, uris):
        if not t: continue
        aid = (t["artists"][0]["id"] if t.get("artists") else "na")
        if count.get(aid, 0) >= max_per_artist: continue
        if sim_guard and kept:
            f1 = feat(t["id"])
            if f1:
                v1 = [f1.get("energy"), f1.get("danceability"), f1.get("valence"), f1.get("instrumentalness")]
                if None not in v1:
                    similar = False
                    for kt in kept[-15:]:
                        f2 = feat(kt["id"])
                        if not f2: continue
                        v2 = [f2.get("energy"), f2.get("danceability"), f2.get("valence"), f2.get("instrumentalness")]
                        if None in v2: continue
                        if cosine(v1, v2) > 0.97:
                            similar = True; break
                    if similar: continue
        kept.append(t); count[aid] = count.get(aid, 0) + 1
    return ["spotify:track:" + x["id"] for x in kept]

def _avg_dict(dicts):
    if not dicts: return {}
    keys=set().union(*[d.keys() for d in dicts])
    out={}
    for k in keys:
        vals=[d[k] for d in dicts if k in d]
        out[k]= sum(vals)/len(vals)
    return out

def _median(nums):
    if not nums: return None
    a=sorted(nums); n=len(a)
    return (a[n//2] if n%2==1 else (a[n//2-1]+a[n//2])/2)

def _pick_user():
    return (request.args.get("user") or request.args.get("u") or DEFAULT_USER).lower()

def _check_secret():
    env = os.getenv("HOOK_SECRET", "")
    provided = request.args.get("key")
    if request.is_json and not provided:
        provided = (request.json or {}).get("key")
    return bool(env) and provided == env

def _b(val, default=False):
    if val is None: return default
    return str(val).lower() in ("1","true","yes","on")

def _chunk(seq, n):
    for i in range(0, len(seq), n): yield seq[i:i+n]

def _uniq(xs):
    seen=set(); out=[]
    for x in xs:
        if x not in seen: seen.add(x); out.append(x)
    return out

# ====== TOKEN CACHE (Redis destekli) ======

class RedisCacheBridge(CacheHandler):
    def __init__(self, user: str):
        self.tc = TokenCache(user)
    def get_cached_token(self):
        return self.tc.get()
    def save_token_to_cache(self, token_info):
        self.tc.set(token_info); return token_info

def _oauth(user: str):
    cid = os.getenv("SPOTIPY_CLIENT_ID")
    secret = os.getenv("SPOTIPY_CLIENT_SECRET")
    redirect_uri = os.getenv("SPOTIPY_REDIRECT_URI")
    if not (cid and secret and redirect_uri):
        raise RuntimeError("Missing Spotify secrets")
    return SpotifyOAuth(
        scope=SCOPES,
        client_id=cid,
        client_secret=secret,
        redirect_uri=redirect_uri,
        cache_handler=RedisCacheBridge(user)
    )

def _get_sp(user: str):
    auth = _oauth(user)
    if not auth.get_cached_token():
        return None
    return Spotify(auth_manager=auth)

# ====== SPOTIFY SEARCH/RECO ======

def _recommend(sp, seeds, targets, size):
    uris=[]
    if size<=0: return uris
    g = seeds[:]; random.shuffle(g)
    batches = [g[i:i+5] for i in range(0,len(g),5)] or [g]
    for subset in batches:
        try:
            kw = {f"target_{k}": v for k,v in (targets or {}).items()}
            rec = sp.recommendations(seed_genres=subset[:5], limit=min(100,size), **kw)
            uris += [t["uri"] for t in rec["tracks"]]
            if len(uris)>=size: break
        except Exception:
            try:
                rec = sp.recommendations(seed_genres=subset[:3], limit=min(50,size))
                uris += [t["uri"] for t in rec["tracks"]]
            except Exception:
                pass
        time.sleep(0.15)
    return uris[:size]

def _search(sp, queries, size, market="TR"):
    uris=[]
    if size<=0: return uris
    qs = queries[:]; random.shuffle(qs)
    for q in qs:
        try:
            res = sp.search(q=q, type="track", limit=min(50,size), market=market)
            uris += [t["uri"] for t in res["tracks"]["items"]]
            if len(uris)>=size: break
        except Exception:
            pass
        time.sleep(0.15)
    return uris[:size]

def _pool(sp, mood, total, ratio_tr, targets_override=None, extra_genres=None):
    preset = PRESETS.get(mood)
    if not preset: raise ValueError(f"Unknown mood '{mood}'")
    seed = preset["seed_genres"][:]
    if extra_genres:
        seed = list(dict.fromkeys(seed + list(extra_genres)))[:5]
    targets = dict(preset.get("targets", {}))
    if targets_override:
        targets.update(targets_override)

    n_tr = int(total*(ratio_tr/100)); n_non = total - n_tr
    non = _recommend(sp, seed, targets, n_non)
    tr  = _search(sp, TURKISH_QUERIES, n_tr, market="TR")
    if len(tr)<n_tr:
        tr += _recommend(sp, ["turkish","anatolian-rock","turkish-pop"], targets, n_tr-len(tr))
    combined = _uniq(non+tr)
    if len(combined)<total:
        combined = _uniq(combined + _search(sp, NON_TR_QUERIES, total-len(combined), market="TR"))
    return combined[:total]

def _ensure_playlist(sp, name, public, desc):
    me = sp.current_user()["id"]
    items=[]; pl=sp.current_user_playlists(limit=50); items+=pl["items"]
    while pl.get("next"): pl=sp.next(pl); items+=pl["items"]
    for p in items:
        if p["name"]==name:
            try: sp.playlist_change_details(p["id"], name=name, public=public, description=desc)
            except Exception: pass
            return p["id"]
    new = sp.user_playlist_create(user=me, name=name, public=public, description=desc)
    return new["id"]

def _replace(sp, pid, uris):
    if not uris: return
    sp.playlist_replace_items(pid, uris[:100])
    for batch in _chunk(uris[100:], 100):
        sp.playlist_add_items(pid, list(batch))

def _append(sp, pid, uris):
    if not uris: return
    for batch in _chunk(uris, 100):
        sp.playlist_add_items(pid, list(batch))

def _title(user, mood):
    names={"gym":"Gym","focus":"Focus Lofi","night_drive":"Night Drive",
           "happy_pop":"Happy Pop","melancholy":"Melancholy"}
    base = (user or "Ahmet").capitalize()
    return f"{base} – {names.get(mood,'Auto Playlist')}"

# ====== PROFILE/ HISTORY (aynen) ======

def _is_our_playlist(name: str, user: str):
    prefix = f"{(user or 'Ahmet').capitalize()} – "
    return isinstance(name, str) and name.startswith(prefix)

def _list_recent_our_playlists(sp, user: str, max_playlists=10):
    items = []
    pl = sp.current_user_playlists(limit=50)
    items += pl["items"]
    while pl.get("next") and len(items) < 200:
        pl = sp.next(pl)
        items += pl["items"]
    ours = [p for p in items if _is_our_playlist(p.get("name",""), user)]
    ours = sorted(ours, key=lambda p: p.get("tracks", {}).get("total", 0), reverse=True)[:max_playlists]
    return ours

def _playlist_track_ids(sp, pid: str, limit=500):
    ids = []
    res = sp.playlist_items(pid, limit=100)
    while True:
        for it in res["items"]:
            tr = it.get("track")
            if tr and tr.get("id"):
                ids.append(tr["id"])
                if len(ids) >= limit:
                    return ids
        if res.get("next"):
            res = sp.next(res)
        else:
            break
    return ids

def _audio_features_bulk(sp, track_ids):
    feats=[]
    for chunk_start in range(0, len(track_ids), 100):
        chunk = track_ids[chunk_start:chunk_start+100]
        feats_chunk = sp.audio_features(chunk) or []
        feats += [f for f in feats_chunk if f]
        time.sleep(0.05)
    return feats

def _audio_profile(sp, track_ids):
    if not track_ids:
        return {}
    feats = _audio_features_bulk(sp, track_ids)
    if not feats:
        return {}
    keys = ["energy","danceability","valence","instrumentalness","tempo"]
    agg = {}
    for k in keys:
        vals = [f.get(k) for f in feats if f.get(k) is not None]
        if not vals: continue
        if k == "tempo":
            vals = [max(60.0, min(200.0, float(v))) for v in vals]
        agg[k] = sum(vals) / len(vals)
    agg["tracks_analyzed"] = len(feats)
    return agg

def _build_profile(sp, user: str):
    pls = _list_recent_our_playlists(sp, user, max_playlists=10)
    all_ids = []
    names = []
    for p in pls:
        pid = p["id"]; names.append(p.get("name",""))
        all_ids += _playlist_track_ids(sp, pid, limit=500)
    all_ids = list(dict.fromkeys(all_ids))
    stats = _audio_profile(sp, all_ids)
    stats["playlists_scanned"] = len(pls)
    stats["playlist_names"] = names

    seeds = []
    if stats.get("energy",0) >= 0.7: seeds += ["edm","dance-pop","electropop","rock"]
    elif stats.get("energy",0) <= 0.35: seeds += ["lofi","ambient","piano","acoustic"]
    else: seeds += ["indie","indie-pop","chill","downtempo"]
    if stats.get("valence",0) >= 0.6: seeds += ["pop"]
    elif stats.get("valence",0) <= 0.3: seeds += ["sad"]
    if stats.get("instrumentalness",0) >= 0.5: seeds += ["instrumental","beats"]
    stats["suggested_seeds"] = list(dict.fromkeys(seeds))[:5]
    return stats

def _fetch_recent(sp, limit=50):
    try:
        res = sp.current_user_recently_played(limit=min(50, limit)) or {}
        items = res.get("items", [])
        ids = [it["track"]["id"] for it in items if it.get("track") and it["track"].get("id")]
        return list(dict.fromkeys(ids))
    except Exception:
        return []

def _fetch_top(sp, time_range="short_term", limit=50):
    try:
        res = sp.current_user_top_tracks(limit=min(50, limit), time_range=time_range) or {}
        items = res.get("items", [])
        ids = [t["id"] for t in items if t and t.get("id")]
        return list(dict.fromkeys(ids))
    except Exception:
        return []

def _audio_agg(sp, track_ids):
    if not track_ids: return {}
    feats=_audio_features_bulk(sp, track_ids)
    if not feats: return {}
    keys = ["energy","danceability","valence","instrumentalness","tempo"]
    out={}
    for k in keys:
        vals = [f.get(k) for f in feats if f.get(k) is not None]
        if not vals: continue
        if k == "tempo":
            vals = [max(60.0, min(200.0, float(v))) for v in vals]
        out[k] = sum(vals)/len(vals)
    out["tracks_analyzed"] = len(feats)
    return out

def _history_profile(sp):
    recent = _fetch_recent(sp, 50)
    top_s  = _fetch_top(sp, "short_term", 50)
    top_m  = _fetch_top(sp, "medium_term", 50)
    top_l  = _fetch_top(sp, "long_term", 50)
    uniq_ids = list(dict.fromkeys(recent + top_s + top_m + top_l))
    agg = _audio_agg(sp, uniq_ids)
    seeds=[]
    e = agg.get("energy", 0.5); v = agg.get("valence", 0.5); instr = agg.get("instrumentalness", 0.0)
    if e>=0.7: seeds += ["edm","dance-pop","electropop","rock"]
    elif e<=0.35: seeds += ["lofi","ambient","piano","acoustic"]
    else: seeds += ["indie","indie-pop","chill","downtempo"]
    if v>=0.6: seeds += ["pop"]
    elif v<=0.3: seeds += ["sad"]
    if instr>=0.5: seeds += ["instrumental","beats"]
    agg["suggested_seeds"] = list(dict.fromkeys(seeds))[:5]
    agg["recent_count"] = len(recent); agg["top_short"]=len(top_s); agg["top_medium"]=len(top_m); agg["top_long"]=len(top_l)
    return agg

# ====== NLP REWRITER (Genişletilmiş Kelime Haznesi) ======

REWRITE_HINTS = [
    # enerji/valans/tempo/ins.
    ("yorgun",  {"energy": -0.25, "valence": -0.05, "instrumentalness": +0.25, "mood":"focus"}),
    ("kahve",   {"energy": +0.12, "instrumentalness": +0.1, "mood":"focus"}),
    ("huzun",   {"valence": -0.3, "energy": -0.1, "mood":"melancholy"}),
    ("uzgun",   {"valence": -0.3, "energy": -0.1, "mood":"melancholy"}),
    ("yalniz",  {"valence": -0.2, "instrumentalness": +0.1, "mood":"melancholy"}),
    ("nostalji",{"valence": -0.08, "mood":"melancholy"}),
    ("gece",    {"energy": +0.05, "danceability": +0.1, "mood":"night_drive"}),
    ("araba",   {"energy": +0.08, "danceability": +0.12, "mood":"night_drive"}),
    ("kosu",    {"energy": +0.28, "danceability": +0.2, "mood":"gym"}),
    ("antreman",{"energy": +0.28, "danceability": +0.2, "mood":"gym"}),
    ("pump",    {"energy": +0.3, "danceability": +0.2, "mood":"gym"}),
    ("mutlu",   {"valence": +0.28, "mood":"happy_pop"}),
    ("bahar",   {"valence": +0.18, "energy": +0.05, "mood":"happy_pop"}),
    ("romantik",{"valence": +0.2, "energy": -0.05, "mood":"happy_pop"}),
    ("yuksek tempo", {"energy": +0.35, "danceability": +0.22}),
    ("yüksek tempo", {"energy": +0.35, "danceability": +0.22}),
    ("dusuk tempo",  {"energy": -0.15}),
    ("agresif", {"energy": +0.35, "valence": -0.05, "mood":"gym"}),
    ("enerjik", {"energy": +0.25, "danceability": +0.1}),
    ("dingin",  {"energy": -0.2, "instrumentalness": +0.15, "mood":"focus"}),
    ("lofi",    {"instrumentalness": +0.25, "energy": -0.1, "mood":"focus"}),
    ("enstrumantal",{"instrumentalness": +0.4, "mood":"focus"}),
    ("study",   {"instrumentalness": +0.2, "mood":"focus"}),
    ("uyku",    {"energy": -0.3, "valence": +0.05, "instrumentalness": +0.25, "mood":"focus"}),
]

def clamp01(x): return max(0.0, min(1.0, x))

def rewrite_text_to_params(text):
    t = (_norm(text))
    out = {"mood": None, "targets": {"energy":0.5,"danceability":0.5,"valence":0.5,"instrumentalness":0.1}, "tr": None}

    for key, eff in REWRITE_HINTS:
        if key in t:
            if "mood" in eff and not out["mood"]:
                out["mood"] = eff["mood"]
            for k,v in eff.items():
                if k == "mood": continue
                if k not in out["targets"]: out["targets"][k] = 0.5
                out["targets"][k] = clamp01(out["targets"][k] + v)

    # boyut ve TR yüzdesi
    m = re.search(r'(\d{2,3})', t); size = int(m.group(1)) if m else 40
    m = re.search(r'tr\s*([0-9]{1,2}|100)', t); tr = int(m.group(1)) if m else None
    private = any(x in t for x in ["private","gizli","prv"])
    public  = any(x in t for x in ["public","acik","pub"])

    if not out["mood"]:
        out["mood"] = "focus" if out["targets"]["instrumentalness"]>=0.4 else "happy_pop"

    return {
        "mood": out["mood"],
        "targets": out["targets"],
        "tr": tr,
        "size": size,
        "public": False if private else (True if public else True)
    }

# ====== RATE LIMIT ======

_RATE = defaultdict(lambda: deque(maxlen=50))
def _ratelimit(key:str, limit=20, window=60):
    q=_RATE[key]; now=time.time()
    while q and now - q[0] > window: q.popleft()
    if len(q) >= limit: return False
    q.append(now); return True

# ====== APP ======

app = Flask(__name__)

@app.before_request
def _set_reqid_and_rl():
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.environ["RID"] = rid
    path = request.path
    safe = path in ("/", "/health", "/authorize", "/callback", "/keepalive")
    if safe: return
    if _check_secret(): return
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "ip?"
    key=f"{ip}:{path}"
    if not _ratelimit(key, limit=20, window=60):
        return jsonify({"error":"Rate limit"}), 429

@app.after_request
def _after(resp):
    rid = request.environ.get("RID","-")
    logging.info(json.dumps({"rid":rid,"path":request.path,"status":resp.status_code}))
    resp.headers["X-Request-ID"]=rid
    return resp

# ====== ROUTES (UI) ======
# --- PROFIL ROUTE (hem /profile hem /profile/) ---
@app.route("/profile")
@app.route("/profile/")
def profile_view():
    user = (request.args.get("user") or DEFAULT_USER).lower()
    sp = _get_sp(user)
    if not sp:
        return jsonify({"error":"Not authorized. Open /authorize?user=<name> first.", "user": user}), 401
    prof = _build_profile(sp, user)
    return jsonify({"ok": True, "user": user, "profile": prof})

@app.route("/")
def ui_root():
    return render_template("index.html")

@app.route("/health")
def health():
    base = request.host_url.rstrip("/")
    return jsonify({
        "ok": True,
        "authorize": f"{base}/authorize?user=ali",
        "nlp_create_example": f"{base}/nlp_create?user=ali&key=YOUR_SECRET"
    })

@app.route("/keepalive")
def keepalive():
    return jsonify({"ok":True,"ts":int(time.time())})

# ====== AUTH ======

@app.route("/authorize")
def authorize():
    user=_pick_user()
    auth=_oauth(user)
    url = auth.get_authorize_url(state=user)
    return redirect(url, 302)

@app.route("/callback")
def callback():
    state_user = request.args.get("state") or DEFAULT_USER
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"Spotify error: {error}", 400
    if not code:
        return "Missing code", 400
    auth=_oauth(state_user)
    try:
        token=auth.get_access_token(code, as_dict=True)
    except Exception as e:
        return f"Token exchange failed: {e}", 400
    if not token:
        return "Token exchange failed", 400
    return f"Linked to Spotify for user '{state_user}'. You can close this tab."

# ====== SUGGESTIONS (isim + açıklama) ======

def _suggest_name_desc(mood:str, qtext:str):
    mood = (mood or "").lower()
    base = _norm(qtext)
    tags=[]
    if "gece" in base or "night" in base: tags.append("Gece")
    if "yagmur" in base or "yagmurlu" in base or "rain" in base: tags.append("Yagmur")
    if "yol" in base or "road" in base or "araba" in base: tags.append("Yol")
    if "huzun" in base or "mel" in base: tags.append("Huzun")
    if "gym" in base or "kosu" in base or "antreman" in base: tags.append("Energy")
    tag = " • ".join(tags[:2]) if tags else None

    choices = {
        "night_drive": [
            ("Geceye Karisan Izler", "Sakin beatler, neon araliklari ve uzun yol hissi."),
            ("Gece Yol Ugultusu", "Synth dokunuşlari ve yavas yavas ivmelenen ritimler."),
            ("Neon ve Sis", "Soguk tonlar, hafif tempo, gece manzaralari.")
        ],
        "focus": [
            ("Derin Odak", "Minimal ritimler, dikkat dagitmayacak dokular."),
            ("Sessiz Dalga", "Lo-fi, ambient ve hafif piyanolar."),
            ("Konsantrasyon Akisi", "Metin yazimi ve calisma anlari icin.")
        ],
        "gym": [
            ("Ritmi Yukselt", "Yuksek enerji, hizli BPM, set aralarinda da götürür."),
            ("Pompa Zamanı", "EDM/Trap vuruslari, motivasyon sabit yuksek."),
            ("Ter ve Bpm", "Agresif drop’lar ve hizli akis.")
        ],
        "happy_pop": [
            ("Gunes Acik", "Parlak melodiler, yuksek moral."),
            ("Keyifli Adimlar", "Dance-pop, indie-pop ve pop tazelik."),
            ("Gulerek Yuru", "Sicak synthler ve umutlu sozler.")
        ],
        "melancholy": [
            ("Perde Arkasi Huzun", "Akustik dokular, dusuk valans."),
            ("Solgun Isik", "Indie/singer-songwriter, yalnizlik tatlari."),
            ("Yavaslayan Zaman", "Dusuk enerji, duygusal akorlar.")
        ]
    }
    arr = choices.get(mood or "happy_pop", choices["happy_pop"])
    # tag ekle
    if tag:
        arr = [(f"{nm} – {tag}", desc) for nm,desc in arr]
    return arr[:3]

@app.route("/suggest_names", methods=["POST"])
def suggest_names():
    if not _check_secret(): return jsonify({"error":"Forbidden"}), 403
    data = request.get_json(force=True, silent=True) or {}
    q = data.get("q","")
    # kaba tahmin
    params = rewrite_text_to_params(q)
    mood = params.get("mood","happy_pop")
    return jsonify({"ok":True,"mood":mood,"suggestions":[{"name":n,"desc":d} for n,d in _suggest_name_desc(mood,q)]})

# ====== NLP CREATE (tek akış) ======

def _extract_data_url_base64(data_url:str):
    """
    data:image/jpeg;base64,.....  -> returns base64 string (no header)
    Only jpeg is allowed by Spotify.
    """
    if not data_url: return None, "empty"
    if not data_url.startswith("data:"):
        return None, "not_data_url"
    head, _, b64 = data_url.partition("base64,")
    if "image/jpeg" not in head and "image/jpg" not in head:
        return None, "not_jpeg"
    if not b64: return None, "no_payload"
    return b64, None

@app.route("/nlp_create", methods=["POST"])
def nlp_create():
    if not _check_secret(): return jsonify({"error":"Forbidden"}), 403
    user = request.args.get("user") or (request.get_json(silent=True) or {}).get("user") or DEFAULT_USER
    sp = _get_sp(user)
    if not sp:
        return jsonify({"error":"Not authorized. Open /authorize?user=<name> first."}), 401

    body = request.get_json(force=True, silent=True) or {}
    q_raw = body.get("q","")
    desired_name = (body.get("name") or "").strip()
    desired_desc = (body.get("description") or "").strip()
    public = bool(body.get("public", True))
    size = int(body.get("size", 40))
    ratio = int(body.get("ratio_tr", 20))
    max_per_artist = int(body.get("max_per_artist", 2))
    sim_guard = bool(body.get("sim_guard", True))
    cover_data_url = body.get("cover_data_url")  # data:image/jpeg;base64,...

    # NLP
    params = rewrite_text_to_params(q_raw)
    mood = params.get("mood","happy_pop")
    if body.get("mood"):  # UI zorla override ederse
        mood = body.get("mood")

@app.errorhandler(404)
def not_found(e):
    if _check_secret():
        routes = sorted([str(r) for r in app.url_map.iter_rules()])
        return jsonify({"error":"not found","path":request.path,"routes":routes}), 404
    return jsonify({"error":"not found","path":request.path}), 404
    # hedefler (dinleme profili ile hafif harman)
    try:
        prof_hist = _history_profile(sp) or {}
    except Exception:
        prof_hist = {}
    targets = _blend_targets(
        {"energy":0.5,"danceability":0.5,"valence":0.5,"instrumentalness":0.1},
        params.get("targets",{}), 0.7
    )
    hist_targets = {
        "energy":          prof_hist.get("energy", 0.5),
        "danceability":    prof_hist.get("danceability", 0.5),
        "valence":         prof_hist.get("valence", 0.5),
        "instrumentalness":prof_hist.get("instrumentalness", 0.0),
    }
    targets = _blend_targets(targets, hist_targets, 0.25)

    # isim ve açıklama
    if not desired_name:
        # varsayılan isim önerilerinden ilki
        desired_name = _suggest_name_desc(mood, q_raw)[0][0]
    if not desired_desc:
        desired_desc = _suggest_name_desc(mood, q_raw)[0][1]

    # havuz
    ratio_eff = ratio if body.get("ratio_tr") is not None else (params.get("tr", 20))
    size_eff = size if body.get("size") is not None else params.get("size", 40)
    uris = _pool(sp, mood, size_eff, ratio_eff, targets_override=targets, extra_genres=None)
    uris = _filter_diversity(sp, uris, max_per_artist=max_per_artist, sim_guard=sim_guard)[:size_eff]

    # playlist oluştur/güncelle
    pid  = _ensure_playlist(sp, desired_name, public, desired_desc)
    _replace(sp, pid, uris)

    # kapak görseli (JPEG base64)
    cover_result = None
    if cover_data_url:
        b64, err = _extract_data_url_base64(cover_data_url)
        if err:
            cover_result = {"ok":False, "reason":err}
        else:
            try:
                # spotipy: playlist_upload_cover_image expects base64-encoded JPEG (no header)
                sp.playlist_upload_cover_image(pid, b64)
                cover_result = {"ok":True}
            except Exception as e:
                cover_result = {"ok":False,"reason":str(e)}

    pl = sp.playlist(pid)
    return jsonify({
        "ok": True,
        "name": pl.get("name"),
        "link": pl["external_urls"]["spotify"],
        "size": len(uris),
        "mood": mood,
        "ratio_tr": ratio_eff,
        "public": public,
        "description": desired_desc,
        "cover": cover_result
    })
