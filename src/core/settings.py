import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import yaml
from .errors import ConfigError

def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default

def _deep_get(d: Dict[str, Any], path: List[str]) -> Any:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur

def _deep_set(d: Dict[str, Any], path: List[str], value: Any) -> None:
    cur = d
    for p in path[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[path[-1]] = value

def _apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    mapping = {
        "KAFKA_BOOTSTRAP_SERVERS": ["kafka", "bootstrap_servers"],
        "POSTGRES_HOST": ["postgres", "host"],
        "POSTGRES_PORT": ["postgres", "port"],
        "POSTGRES_DB": ["postgres", "db"],
        "POSTGRES_USER": ["postgres", "user"],
        "POSTGRES_PASSWORD": ["postgres", "password"],
        "REDIS_HOST": ["redis", "host"],
        "REDIS_PORT": ["redis", "port"],
        "NEO4J_URI": ["neo4j", "uri"],
        "NEO4J_USER": ["neo4j", "user"],
        "NEO4J_PASSWORD": ["neo4j", "password"],
        "EASTWOOD_ENV": ["app", "env"],
        "EASTWOOD_BOOKING_API_BASE_URL": ["booking", "api_base_url"],
        "EASTWOOD_BOOKING_PUBLIC_BASE_URL": ["booking", "public_base_url"],
        "EASTWOOD_SMS_PROVIDER": ["sms", "provider"],
        "EASTWOOD_TWILIO_ACCOUNT_SID": ["sms", "twilio", "account_sid"],
        "EASTWOOD_TWILIO_AUTH_TOKEN": ["sms", "twilio", "auth_token"],
        "EASTWOOD_TWILIO_FROM_NUMBER": ["sms", "twilio", "from_number"],
        "EASTWOOD_AGENT_NOTIFY_NUMBER": ["sms", "agent_notify_number"],
    }
    for env_key, path in mapping.items():
        v = _get_env(env_key)
        if v is None:
            continue
        if path[-1] == "port":
            try:
                _deep_set(cfg, path, int(v))
            except Exception:
                _deep_set(cfg, path, v)
        else:
            _deep_set(cfg, path, v)
    return cfg

@dataclass(frozen=True)
class Settings:
    cfg: Dict[str, Any]

    @property
    def env(self) -> str:
        return str(_deep_get(self.cfg, ["app", "env"]) or "dev")

    @property
    def api_host(self) -> str:
        return str(_deep_get(self.cfg, ["api", "host"]) or "0.0.0.0")

    @property
    def api_port(self) -> int:
        return int(_deep_get(self.cfg, ["api", "port"]) or 8001)

    @property
    def metrics_port(self) -> int:
        return int(_deep_get(self.cfg, ["metrics", "port"]) or 8000)

    @property
    def kafka_bootstrap(self) -> str:
        v = _deep_get(self.cfg, ["kafka", "bootstrap_servers"])
        if not v:
            raise ConfigError("kafka.bootstrap_servers missing")
        return str(v)

    @property
    def kafka_client_id(self) -> str:
        return str(_deep_get(self.cfg, ["kafka", "client_id"]) or "eastwood")

    @property
    def topics(self) -> Dict[str, str]:
        t = _deep_get(self.cfg, ["topics"])
        if not isinstance(t, dict) or not t:
            raise ConfigError("topics missing")
        return {k: str(v) for k, v in t.items()}

    @property
    def postgres(self) -> Dict[str, Any]:
        p = _deep_get(self.cfg, ["postgres"])
        if not isinstance(p, dict) or not p:
            raise ConfigError("postgres config missing")
        return p

    @property
    def redis(self) -> Dict[str, Any]:
        r = _deep_get(self.cfg, ["redis"])
        if not isinstance(r, dict) or not r:
            raise ConfigError("redis config missing")
        return r

    @property
    def policy(self) -> Dict[str, Any]:
        p = _deep_get(self.cfg, ["policy"])
        if not isinstance(p, dict) or not p:
            raise ConfigError("policy config missing")
        return p

    @property
    def recency_days(self) -> int:
        return int(_deep_get(self.cfg, ["policy", "recency_days"]) or 30)

    @property
    def allowed_products(self) -> List[str]:
        arr = _deep_get(self.cfg, ["policy", "allowed_products"]) or []
        return [str(x) for x in arr]

    @property
    def consent_required(self) -> bool:
        v = _deep_get(self.cfg, ["policy", "consent", "required"])
        return bool(v if v is not None else True)

    @property
    def consent_required_fields(self) -> List[str]:
        arr = _deep_get(self.cfg, ["policy", "consent", "required_fields"]) or []
        return [str(x) for x in arr]

    @property
    def dedupe_window_days(self) -> int:
        return int(_deep_get(self.cfg, ["policy", "dedupe", "window_days"]) or self.recency_days)

    @property
    def booking(self) -> Dict[str, Any]:
        b = _deep_get(self.cfg, ["booking"])
        if not isinstance(b, dict) or not b:
            raise ConfigError("booking config missing")
        return b

    @property
    def booking_api_base_url(self) -> str:
        return str(_deep_get(self.cfg, ["booking", "api_base_url"]) or f"http://{self.api_host}:{self.api_port}")

    @property
    def booking_public_base_url(self) -> str:
        return str(_deep_get(self.cfg, ["booking", "public_base_url"]) or self.booking_api_base_url)

    @property
    def booking_token_ttl_hours(self) -> int:
        return int(_deep_get(self.cfg, ["booking", "token_ttl_hours"]) or 72)

    @property
    def work_hours(self) -> Dict[str, Any]:
        wh = _deep_get(self.cfg, ["booking", "work_hours"])
        if not isinstance(wh, dict) or not wh:
            raise ConfigError("booking.work_hours missing")
        return wh

    @property
    def sms(self) -> Dict[str, Any]:
        s = _deep_get(self.cfg, ["sms"])
        if not isinstance(s, dict) or not s:
            raise ConfigError("sms config missing")
        return s

    @property
    def sms_provider(self) -> str:
        return str(_deep_get(self.cfg, ["sms", "provider"]) or "twilio")

    @property
    def twilio(self) -> Dict[str, Any]:
        t = _deep_get(self.cfg, ["sms", "twilio"]) or {}
        return dict(t) if isinstance(t, dict) else {}

    @property
    def agent_notify_number(self) -> str:
        return str(_deep_get(self.cfg, ["sms", "agent_notify_number"]) or "")

    @property
    def carriers(self) -> Dict[str, Any]:
        c = _deep_get(self.cfg, ["carriers"]) or {}
        return dict(c) if isinstance(c, dict) else {}

def load_settings(path: str = "config/config.yaml") -> Settings:
    p = _get_env("EASTWOOD_CONFIG", path) or path
    if not os.path.exists(p):
        raise ConfigError(f"config not found: {p}")
    with open(p, "r") as f:
        cfg = yaml.safe_load(f) or {}
    cfg = _apply_env_overrides(cfg)
    return Settings(cfg=cfg)