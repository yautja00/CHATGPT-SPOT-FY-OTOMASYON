#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, random, re, json, unicodedata
from flask import Flask, request, redirect, jsonify
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private"
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

# ---------- yardımcılar ----------
def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFD", s).encode("ascii","ignore").decode("utf-8")
    return " ".join(s.split())

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
    uris = _pool(sp, mood, size, ratio_tr, targets_override=targets_override, extra_genres=extra_genres)
    desc = f"Auto-generated • mood={mood} • TR={ratio_tr}%"
    pid  = _ensure_playlist(sp, name, public, desc)
    _replace(sp, pid, uris)
    pl = sp.playlist(pid)
    return {"ok":True,"name":pl.get("name"),
            "link":pl["external_urls"]["spotify"],
            "size":len(uris),"mood":mood,"ratio_tr":ratio_tr,"public":public}

# Zekalı mapping (opsiyonel dosya)
try:
    with open("mood_mapping.json","r",encoding="utf-8") as f:
        MOODMAP = json.load(f)
except Exception:
    MOODMAP = {}

app = Flask(__name__)

@app.route("/")
def home():
    base=request.host_url.rstrip("/")
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
    if not _check_secret(): return jsonify({"error":"Forbidden"}),403
    user=_pick_user(); sp=_get_sp(user)
    if not sp: return jsonify({"error":"Not authorized. Open /authorize?user=<name> first."}),401

    q_raw = request.args.get("q") or ""
    q = q_raw.lower()
    qn = _norm(q_raw)

    # defaults
    mood="gym"; size=40; ratio=30; public=True

    # SMART mapping hits
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

    # legacy (kelime bazlı)
    if "focus" in q: mood="focus"
    elif "night" in q or "nd" in q or "gece" in q: mood="night_drive"
    elif "happy" in q or "pop" in q: mood="happy_pop"
    elif "mel" in q or "huzun" in q or "mood" in q: mood="melancholy"
    elif "gym" in q or "spor" in q: mood="gym"

    # smart override
    if smart_used and smart_mood:
        mood = smart_mood

    m=re.search(r'(\d{2,3})', q)
    if m: size=max(1,min(300,int(m.group(1))))
    m=re.search(r'tr\s*([0-9]{1,2}|100)', q)
    if m: ratio=max(0,min(100,int(m.group(1))))
    elif smart_used and smart_tr is not None:
        ratio = int(round(smart_tr))

    if "private" in q or "gizli" in q or "prv" in q: public=False
    if "public" in q or "acik" in q or "pub" in q: public=True

    name=_title(user, mood)
    try: 
        return jsonify(_make(sp,name,mood,size,ratio,public,
                             targets_override=(smart_targets if smart_used else None),
                             extra_genres=(extra_genres if smart_used else None)))
    except Exception as e: 
        return jsonify({"error":str(e)}),500

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

if __name__ == "__main__":
    port=int(os.environ.get("PORT","5000"))
    app.run(host="0.0.0.0", port=port)
