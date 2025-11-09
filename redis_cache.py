import os, json, tempfile
try:
    import redis
except Exception:
    redis = None

class TokenCache:
    def __init__(self, user: str):
        self.user = (user or "default").lower()
        self.redis_url = os.getenv("REDIS_URL")
        self.key = f"spotitoken:{self.user}"
        self.tmp = os.path.join(tempfile.gettempdir(), f"token_cache_{self.user}.json")
        self.r = None
        if self.redis_url and redis:
            self.r = redis.from_url(self.redis_url, decode_responses=True)

    def get(self):
        if self.r:
            js = self.r.get(self.key)
            return json.loads(js) if js else None
        if os.path.exists(self.tmp):
            with open(self.tmp, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def set(self, token_dict):
        if self.r:
            self.r.set(self.key, json.dumps(token_dict))
        else:
            with open(self.tmp, "w", encoding="utf-8") as f:
                json.dump(token_dict, f)

    def clear(self):
        if self.r:
            self.r.delete(self.key)
        try:
            if os.path.exists(self.tmp): os.remove(self.tmp)
        except Exception:
            pass
