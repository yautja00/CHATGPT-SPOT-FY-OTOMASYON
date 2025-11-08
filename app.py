#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, random, re, json, unicodedata
from flask import Flask, request, redirect, jsonify, render_template
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private user-read-recently-played user-top-read"
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

# --- blend helper ---
def _blend_targets(base, add, w):
    out = dict(base)
    add = add or {}
    for k, v in add.items():
        if v is None: 
            continue
        bv = out.get(k, 0.5)
        out[k] = max(0.0, min(1.0, (1 - w) * bv + w * float(v)))
    return out

# ---------- yardımcılar ----------
def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFD", s).encode("ascii","ignore").decode("utf-8")
    return " ".join(s.split())
# --- diversity filter (artist limiti + benzerlik korumasi) ---
def _filter_diversity(sp, uris, max_per_artist=2, sim_guard=True):
    if not uris: 
        return uris
    tids = [u.split(":")[-1] for u in uris]
    meta = sp.tracks(tids)["tracks"]
    kept = []
    count = {}
    feats_cache = {}

    def feat(tid):
        if tid not in feats_cache:
            try:
                feats_cache[tid] = sp.audio_features([tid])[0]
            except Exception:
                feats_cache[tid] = None
            time.sleep(0.02)
        return feats_cache[tid]

    import math
    def cosine(v1, v2):
        a = sum(x*y for x,y in zip(v1,v2))
        n1 = math.sqrt(sum(x*x for x in v1)); n2 = math.sqrt(sum(y*y for y in v2))
        return a / (n1*n2 + 1e-9)

    for t, u in zip(meta, uris):
        if not t: 
            continue
        aid = (t["artists"][0]["id"] if t.get("artists") else "na")
        if count.get(aid, 0) >= max_per_artist:
            continue
        # benzerlik korumasi (son 15 ile karsilastir)
        if sim_guard and kept:
            f1 = feat(t["id"])
            if f1:
                v1 = [f1.get("energy"), f1.get("danceability"), f1.get("valence"), f1.get("instrumentalness")]
                if None not in v1:
                    similar = False
                    for kt in kept[-15:]:
                        f2 = feat(kt["id"])
                        if not f2: 
                            continue
                        v2 = [f2.get("energy"), f2.get("danceability"), f2.get("valence"), f2.get("instrumentalness")]
                        if None in v2: 
                            continue
                        if cosine(v1, v2) > 0.97:
                            similar = True
                            break
                    if similar:
                        continue
        kept.append(t)
        count[aid] = count.get(aid, 0) + 1

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

def _oauth(user: str):
    cid = os.getenv("SPOTIPY_CLIENT_ID")
    secret = os.getenv("SPOTIPY_CLIENT_SECRET")
    redirect_uri = os.getenv("SPOTIPY_REDIRECT_URI")
    if not (cid and secret and redirect_uri):
        raise RuntimeError("Missing Spotify secrets")
    cache_dir = os.getenv("SPOTIPY_CACHE_DIR", None)
    cache_path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"token_cache_{(user or DEFAULT_USER).lower()}")
    else:
        cache_path = f"token_cache_{(user or DEFAULT_USER).lower()}"
    return SpotifyOAuth(scope=SCOPES, client_id=cid, client_secret=secret,
                        redirect_uri=redirect_uri, cache_path=cache_path)

def _get_sp(user: str):
    auth = _oauth(user)
    if not auth.get_cached_token():
        return None
    return Spotify(auth_manager=auth)

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
    # seed’leri genişlet
    seed = preset["seed_genres"][:]
    if extra_genres:
        seed = list(dict.fromkeys(seed + list(extra_genres)))[:5]
    # targets override
    targets = dict(preset.get("targets", {}))
    if targets_override:
        targets.update(targets_override)
    # böl
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

def _title(user, mood):
    names={"gym":"Gym","focus":"Focus Lofi","night_drive":"Night Drive",
           "happy_pop":"Happy Pop","melancholy":"Melancholy"}
    base = (user or "Ahmet").capitalize()
    return f"{base} – {names.get(mood,'Auto Playlist')}"

def _make(sp, name, mood, size, ratio_tr, public, targets_override=None, extra_genres=None):
    # havuzu oluştur
    uris = _pool(sp, mood, size, ratio_tr, targets_override=targets_override, extra_genres=extra_genres)

    # ÇEŞİTLİLİK FİLTRESİ: aynı sanatçı ve kopya vibe'ı azalt
    uris = _filter_diversity(sp, uris, max_per_artist=2, sim_guard=True)[:size]

    desc = f"Auto-generated • mood={mood} • TR={ratio_tr}%"
    pid  = _ensure_playlist(sp, name, public, desc)
    _replace(sp, pid, uris)
    pl = sp.playlist(pid)
    return {"ok": True,
            "name": pl.get("name"),
            "link": pl["external_urls"]["spotify"],
            "size": len(uris),
            "mood": mood,
            "ratio_tr": ratio_tr,
            "public": public}

# Zekalı mapping (opsiyonel dosya)
try:
    with open("mood_mapping.json","r",encoding="utf-8") as f:
        MOODMAP = json.load(f)
except Exception:
    MOODMAP = {}

app = Flask(__name__)

@app.route("/")
def ui_root():
    return render_template("index.html")

@app.route("/health")
def health():
    base = request.host_url.rstrip("/")
    return jsonify({
        "ok": True,
        "authorize": f"{base}/authorize?user=ali",
        "quick_example": f"{base}/quick/gym40?user=ali&key=YOUR_SECRET",
        "nlp_example": f"{base}/nlp?user=ali&key=YOUR_SECRET&q=yagmurlu huzunlu aksam 30 private"
    })

@app.route("/authorize")
def authorize():
    user=_pick_user()
    auth=_oauth(user)
    url = auth.get_authorize_url(state=user)
    return redirect(url, 302)

@app.route("/callback")
def callback():
    user = request.args.get("state") or DEFAULT_USER
    code = request.args.get("code")
    if not code: return "Missing code", 400
    auth=_oauth(user)
    token=auth.get_access_token(code, as_dict=True)
    if not token: return "Token exchange failed", 400
    return f"Linked to Spotify for user '{user}'. You can close this tab."

@app.route("/make_playlist")
def make_playlist():
    user=_pick_user(); sp=_get_sp(user)
    if not sp: return jsonify({"error":"Not authorized. Open /authorize?user=<name> first."}), 401
    name=(request.args.get("name") or _title(user, request.args.get("mood","night_drive"))).strip()
    mood=request.args.get("mood","night_drive").strip()
    size=max(1,min(300,int(request.args.get("size",40))))
    ratio=max(0,min(100,int(request.args.get("ratio_tr",30))))
    public=_b(request.args.get("public","0"), False)
    try: return jsonify(_make(sp,name,mood,size,ratio,public))
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/hook", methods=["GET","POST"])
def hook():
    if not _check_secret(): return jsonify({"error":"Forbidden"}),403
    user=_pick_user(); sp=_get_sp(user)
    if not sp: return jsonify({"error":"Not authorized. Open /authorize?user=<name> first."}), 401
    data=request.args if request.method=="GET" else (request.json or {})
    name=(data.get("name") or _title(user, data.get("mood","night_drive"))).strip()
    mood=(data.get("mood") or "night_drive").strip()
    size=max(1,min(300,int(data.get("size",40))))
    ratio=max(0,min(100,int(data.get("ratio_tr",30))))
    public=_b(data.get("public","0"), False)
    try: return jsonify(_make(sp,name,mood,size,ratio,public))
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/nlp")
def nlp():
    if not _check_secret(): 
        return jsonify({"error":"Forbidden"}), 403

    user = _pick_user()
    sp = _get_sp(user)
    if not sp:
        return jsonify({"error":"Not authorized. Open /authorize?user=<name> first."}), 401

    q_raw = request.args.get("q") or ""
    q = q_raw.lower()
    qn = _norm(q_raw)

    # --- mevcut mapping mantigi ---
    hits = [k for k in MOODMAP.keys() if k in qn]
    smart_used = False
    extra_genres = []
    smart_targets = {}
    smart_tr = None
    smart_mood = None

    if hits:
        smart_used = True
        smart_mood = MOODMAP[hits[0]].get("mood")
        smart_targets = _avg_dict([MOODMAP[h].get("targets",{}) for h in hits])
        tr_candidates = [MOODMAP[h].get("tr") for h in hits if MOODMAP[h].get("tr") is not None]
        smart_tr = _median(tr_candidates)
        for h in hits:
            extra_genres += MOODMAP[h].get("extra_genres", [])
        extra_genres = list(dict.fromkeys(extra_genres))[:5]

    # --- legacy kelime kontrolu (fallback) ---
    mood = None
    if "focus" in q: mood="focus"
    elif "night" in q or "nd" in q or "gece" in q: mood="night_drive"
    elif "happy" in q or "pop" in q: mood="happy_pop"
    elif "mel" in q or "huzun" in q or "mood" in q: mood="melancholy"
    elif "gym" in q or "spor" in q: mood="gym"
    if smart_used and smart_mood:
        mood = smart_mood

    # --- sayisal parametreler ---
    import re
    size = 40
    m = re.search(r'(\d{2,3})', q); 
    if m: size = max(1, min(300, int(m.group(1))))
    ratio = None
    m = re.search(r'tr\s*([0-9]{1,2}|100)', q)
    if m: ratio = max(0, min(100, int(m.group(1))))
    public = True
    if "private" in q or "gizli" in q or "prv" in q: public = False
    if "public" in q or "acik" in q or "pub" in q: public = True

    # --- REWRITER: kural tabanli yorumlayici (varsa) ---
    try:
        params_rw = rewrite_text_to_params(q_raw)
    except Exception:
        params_rw = {"mood": None, "targets": {}, "tr": None, "size": size, "public": public}

    # --- dinleme gecmisi profili (varsa) ---
    try:
        prof_hist = _history_profile(sp) or {}
    except Exception:
        prof_hist = {}

    # --- hedefleri harmanla: mapping > rewriter > history ---
    # baslangic tabani
    targets = {"energy":0.5, "danceability":0.5, "valence":0.5, "instrumentalness":0.1}
    # mapping agirlikli
    if smart_targets:
        targets = _blend_targets(targets, smart_targets, 0.8)
    # rewriter katkisi
    targets = _blend_targets(targets, (params_rw or {}).get("targets", {}), 0.4)
    # history’den hafif destek
    hist_targets = {
        "energy":          prof_hist.get("energy", 0.5),
        "danceability":    prof_hist.get("danceability", 0.5),
        "valence":         prof_hist.get("valence", 0.5),
        "instrumentalness":prof_hist.get("instrumentalness", 0.0),
    }
    targets = _blend_targets(targets, hist_targets, 0.2)

    # mood yoksa tahmin et
    if not mood:
        mood = params_rw.get("mood") or ("focus" if targets.get("instrumentalness",0) >= 0.4 else "happy_pop")

    # ratio (TR %) yoksa akilli varsayim
    if ratio is None:
        ratio = params_rw.get("tr") if params_rw.get("tr") is not None else (smart_tr if smart_tr is not None else 20)

    # isim
    name = _title(user, mood)

    try:
        return jsonify(_make(sp, name, mood, size, ratio, public,
                             targets_override=targets,
                             extra_genres=(extra_genres if smart_used else None)))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/quick/<code>")
def quick(code):
    if not _check_secret(): return jsonify({"error":"Forbidden"}),403
    user=_pick_user(); sp=_get_sp(user)
    if not sp: return jsonify({"error":"Not authorized. Open /authorize?user=<name> first."}),401

    c = code.lower()
    mood="gym"; public=True; ratio=30; size=40
    if   c.startswith("gym"):   mood="gym";        c=c[3:]
    elif c.startswith("focus"): mood="focus";      c=c[5:]
    elif c.startswith("nd") or c.startswith("night"):
         mood="night_drive";    c=c[2:] if c.startswith("nd") else c[5:]
    elif c.startswith("happy"): mood="happy_pop";  c=c[5:]
    elif c.startswith("mel"):   mood="melancholy"; c=c[3:]

    digits="".join(ch for ch in c if c.isdigit())
    if digits: size=max(1,min(300,int(digits)))
    if "prv" in c: public=False
    if "pub" in c: public=True
    if "tr" in c:
        i=c.index("tr")+2; num=""
        while i<len(c) and c[i].isdigit():
            num+=c[i]; i+=1
        if num: ratio=max(0,min(100,int(num)))

    name=_title(user, mood)
    try: return jsonify(_make(sp,name,mood,size,ratio,public))
    except Exception as e: return jsonify({"error":str(e)}),500
# ===== Mood Memory: profile + recommend-from-profile =====

def _is_our_playlist(name: str, user: str):
    # Uygulamanın oluşturduğu ad şablonu: "Ahmet – Focus Lofi" vb.
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
    # en yeni üstte kalsın
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

def _audio_profile(sp, track_ids):
    # Spotify audio features → energy, danceability, valence, tempo (bpm), instrumentalness
    if not track_ids:
        return {}
    feats = []
    for chunk_start in range(0, len(track_ids), 100):
        chunk = track_ids[chunk_start:chunk_start+100]
        feats_chunk = sp.audio_features(chunk) or []
        feats += [f for f in feats_chunk if f]
        time.sleep(0.05)
    if not feats:
        return {}
    keys = ["energy","danceability","valence","instrumentalness","tempo"]
    agg = {}
    for k in keys:
        vals = [f.get(k) for f in feats if f.get(k) is not None]
        if not vals:
            continue
        if k == "tempo":
            # tempo normalizasyonu için 60-200 aralığına kırp
            vals = [max(60.0, min(200.0, float(v))) for v in vals]
        agg[k] = sum(vals) / len(vals)
    agg["tracks_analyzed"] = len(feats)
    return agg

def _build_profile(sp, user: str):
    pls = _list_recent_our_playlists(sp, user, max_playlists=10)
    all_ids = []
    names = []
    for p in pls:
        pid = p["id"]
        names.append(p.get("name",""))
        all_ids += _playlist_track_ids(sp, pid, limit=500)
    all_ids = list(dict.fromkeys(all_ids))  # uniq
    stats = _audio_profile(sp, all_ids)
    stats["playlists_scanned"] = len(pls)
    stats["playlist_names"] = names
    # Profilden “seed genre” önerisi:
    # energy/danceability/valence değerlerine göre basit bir liste
    seeds = []
    if stats.get("energy",0) >= 0.7:
        seeds += ["edm","dance-pop","electropop","rock"]
    elif stats.get("energy",0) <= 0.35:
        seeds += ["lofi","ambient","piano","acoustic"]
    else:
        seeds += ["indie","indie-pop","chill","downtempo"]
    if stats.get("valence",0) >= 0.6:
        seeds += ["pop"]
    elif stats.get("valence",0) <= 0.3:
        seeds += ["sad"]
    if stats.get("instrumentalness",0) >= 0.5:
        seeds += ["instrumental","beats"]
    stats["suggested_seeds"] = list(dict.fromkeys(seeds))[:5]
    return stats

@app.route("/profile")
def profile_view():
    user = _pick_user()
    sp = _get_sp(user)
    if not sp:
        return jsonify({"error":"Not authorized. Open /authorize?user=<name> first."}), 401
    prof = _build_profile(sp, user)
    return jsonify({"ok": True, "user": user, "profile": prof})

@app.route("/profile_reco")
def profile_reco():
    if not _check_secret(): 
        return jsonify({"error":"Forbidden"}), 403
    user = _pick_user()
    sp = _get_sp(user)
    if not sp:
        return jsonify({"error":"Not authorized. Open /authorize?user=<name> first."}), 401

    # profil + tohum + hedefler
    prof = _build_profile(sp, user)
    seeds = prof.get("suggested_seeds") or ["indie","chill","pop"]
    targets = {
        "energy": prof.get("energy", 0.5),
        "danceability": prof.get("danceability", 0.5),
        "valence": prof.get("valence", 0.5),
        "instrumentalness": prof.get("instrumentalness", 0.0)
    }

    # parametreler
    size = max(1, min(300, int(request.args.get("size", 40))))
    ratio = max(0, min(100, int(request.args.get("ratio_tr", 20))))
    public = _b(request.args.get("public", "0"), False)

    # mood ismini “Profile Mix” gibi kullanalım
    mood = "focus" if targets.get("instrumentalness",0) >= 0.4 else ("gym" if targets.get("energy",0) >= 0.7 else "happy_pop")
    name = f"{(user or 'Ahmet').capitalize()} – Profile Mix"

    # _pool’u tohum ve hedef override ile çağır
    uris = _pool(sp, mood, size, ratio, targets_override=targets, extra_genres=seeds)
    pid = _ensure_playlist(sp, name, public, desc="Auto-generated • from Mood Memory")
    _replace(sp, pid, uris)
    pl = sp.playlist(pid)
    return jsonify({
        "ok": True,
        "user": user,
        "created": pl["external_urls"]["spotify"],
        "size": len(uris),
        "seeds_used": seeds,
        "targets_used": targets,
        "profile_snapshot": prof
    })
# ===== Listening History Profile (recently-played + top-tracks) =====

def _fetch_recent(sp, limit=50):
    # Son dinlenen 50 parça (Spotify 50’ye kadar verir)
    try:
        res = sp.current_user_recently_played(limit=min(50, limit)) or {}
        items = res.get("items", [])
        ids = [it["track"]["id"] for it in items if it.get("track") and it["track"].get("id")]
        return list(dict.fromkeys(ids))
    except Exception:
        return []

def _fetch_top(sp, time_range="short_term", limit=50):
    # Kişisel top parça listeleri: short_term (4 hafta), medium_term (6 ay), long_term (yıllar)
    try:
        res = sp.current_user_top_tracks(limit=min(50, limit), time_range=time_range) or {}
        items = res.get("items", [])
        ids = [t["id"] for t in items if t and t.get("id")]
        return list(dict.fromkeys(ids))
    except Exception:
        return []

def _audio_agg(sp, track_ids):
    if not track_ids: return {}
    feats=[]
    for i in range(0, len(track_ids), 100):
        chunk = track_ids[i:i+100]
        feats_chunk = sp.audio_features(chunk) or []
        feats += [f for f in feats_chunk if f]
        time.sleep(0.05)
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
    # Tohum tür çıkarımı – çok basit sezgisel
    seeds=[]
    e = agg.get("energy", 0.5); d = agg.get("danceability", 0.5); v = agg.get("valence", 0.5)
    instr = agg.get("instrumentalness", 0.0)
    if e>=0.7: seeds += ["edm","dance-pop","electropop","rock"]
    elif e<=0.35: seeds += ["lofi","ambient","piano","acoustic"]
    else: seeds += ["indie","indie-pop","chill","downtempo"]
    if v>=0.6: seeds += ["pop"]
    elif v<=0.3: seeds += ["sad"]
    if instr>=0.5: seeds += ["instrumental","beats"]
    agg["suggested_seeds"] = list(dict.fromkeys(seeds))[:5]
    agg["recent_count"] = len(recent); agg["top_short"]=len(top_s); agg["top_medium"]=len(top_m); agg["top_long"]=len(top_l)
    return agg

@app.route("/listening_profile")
def listening_profile():
    user = _pick_user()
    sp = _get_sp(user)
    if not sp:
        return jsonify({"error":"Not authorized. Open /authorize?user=<name> first."}), 401
    prof = _history_profile(sp)
    return jsonify({"ok": True, "user": user, "profile": prof})

@app.route("/history_reco")
def history_reco():
    if not _check_secret(): 
        return jsonify({"error":"Forbidden"}), 403
    user = _pick_user()
    sp = _get_sp(user)
    if not sp:
        return jsonify({"error":"Not authorized. Open /authorize?user=<name> first."}), 401

    prof = _history_profile(sp)
    seeds = prof.get("suggested_seeds") or ["indie","chill","pop"]
    targets = {
        "energy": prof.get("energy", 0.5),
        "danceability": prof.get("danceability", 0.5),
        "valence": prof.get("valence", 0.5),
        "instrumentalness": prof.get("instrumentalness", 0.0)
    }
    size  = max(1, min(300, int(request.args.get("size", 40))))
    ratio = max(0, min(100, int(request.args.get("ratio_tr", 20))))
    public = _b(request.args.get("public", "0"), False)

    mood = "focus" if targets.get("instrumentalness",0) >= 0.4 else ("gym" if targets.get("energy",0) >= 0.7 else "happy_pop")
    name = f"{(user or 'Ahmet').capitalize()} – History Mix"

    uris = _pool(sp, mood, size, ratio, targets_override=targets, extra_genres=seeds)
    pid = _ensure_playlist(sp, name, public, desc="Auto-generated • from Listening History")
    _replace(sp, pid, uris)
    pl = sp.playlist(pid)
    return jsonify({
        "ok": True,
        "created": pl["external_urls"]["spotify"],
        "size": len(uris),
        "seeds_used": seeds,
        "targets_used": targets,
        "profile_snapshot": prof
    })
# ===== AI Mood Rewriter (Rule-based v1) =====

REWRITE_HINTS = [
    # (aranan_kelime, etkisi)
    ("yorgun",  {"energy": -0.2, "valence": -0.05, "instrumentalness": +0.2, "mood":"focus"}),
    ("kahve",   {"energy": +0.1, "instrumentalness": +0.1, "mood":"focus"}),
    ("huzun",   {"valence": -0.25, "energy": -0.1, "mood":"melancholy"}),
    ("uzgun",   {"valence": -0.25, "energy": -0.1, "mood":"melancholy"}),
    ("nostalji",{"valence": -0.05, "mood":"melancholy"}),
    ("gece",    {"energy": +0.05, "danceability": +0.1, "mood":"night_drive"}),
    ("araba",   {"energy": +0.05, "danceability": +0.1, "mood":"night_drive"}),
    ("kosu",    {"energy": +0.25, "danceability": +0.2, "mood":"gym"}),
    ("koşu",    {"energy": +0.25, "danceability": +0.2, "mood":"gym"}),
    ("gym",     {"energy": +0.25, "danceability": +0.2, "mood":"gym"}),
    ("mutlu",   {"valence": +0.25, "mood":"happy_pop"}),
    ("romantik",{"valence": +0.2, "energy": -0.05, "mood":"happy_pop"}),
    ("yuksek tempo", {"energy": +0.3, "danceability": +0.2}),
    ("yüksek tempo", {"energy": +0.3, "danceability": +0.2}),
]

def clamp01(x): return max(0.0, min(1.0, x))

def rewrite_text_to_params(text):
    t = (_norm(text))
    # başlangıç defaultları
    out = {"mood": None, "targets": {"energy":0.5,"danceability":0.5,"valence":0.5,"instrumentalness":0.1}, "tr": None}
    # anahtar kelime bazlı etkiler
    for key, eff in REWRITE_HINTS:
        if key in t:
            if "mood" in eff and not out["mood"]:
                out["mood"] = eff["mood"]
            for k,v in eff.items():
                if k == "mood": continue
                if k not in out["targets"]: out["targets"][k] = 0.5
                out["targets"][k] = clamp01(out["targets"][k] + v)
    # explicit sayılar
    m=re.search(r'(\\d{2,3})', t); size = int(m.group(1)) if m else 40
    m=re.search(r'tr\\s*([0-9]{1,2}|100)', t); tr = int(m.group(1)) if m else None
    private = any(x in t for x in ["private","gizli","prv"])
    public  = any(x in t for x in ["public","acik","pub"])
    if not out["mood"]:
        # mapping'lerden bir hit yoksa, düşük riskli varsayılan
        out["mood"] = "focus" if out["targets"]["instrumentalness"]>=0.4 else "happy_pop"
    return {
        "mood": out["mood"],
        "targets": out["targets"],
        "tr": tr,
        "size": size,
        "public": False if private else (True if public else True)
    }

@app.route("/rewrite")
def rewrite_endpoint():
    q = request.args.get("q") or ""
    params = rewrite_text_to_params(q)
    return jsonify({"ok": True, "rewritten": params})

# NLP içinde kullanmak istersen: mapping hit yoksa rewriter'a düş
# (Bunu /nlp route'unda, mapping bulamadığı durumda params = rewrite_text_to_params(q_raw) ile override edebilirsin.)

if __name__ == "__main__":
    port=int(os.environ.get("PORT","5000"))
    app.run(host="0.0.0.0", port=port)
