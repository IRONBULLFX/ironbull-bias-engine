"""HFM Affiliates API (https://api.hfaffiliates.com, OpenAPI 'HFM Affiliates API' v1.0.0).
Checks that a trading account belongs to the partner (your IB) and that the email matches the client record.
Needs HFM_API_KEY (sent as 'Authorization: Bearer <key>'). The key and client details are never logged or returned."""
import os
from data.http import get_json

BASE = "https://api.hfaffiliates.com"
PERF = "/api/performance/client-performance"
CLIENTS = "/api/clients/"


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _norm_email(e):
    return str(e or "").strip().casefold()


def _truthy(x):
    return x is True or str(x).strip().lower() in ("true", "1", "yes")


class HFMClient:
    def __init__(self, key=None, base=None, getter=get_json):
        self.key = key if key is not None else os.environ.get("HFM_API_KEY", "")
        self.base = (base or os.environ.get("HFM_API_BASE") or BASE).rstrip("/")
        self.getter = getter

    @property
    def configured(self):
        return bool(self.key)

    def _get(self, path, params=None):
        payload, err = self.getter(self.base + path, params, headers={"Authorization": f"Bearer {self.key}"})
        return payload, err

    def find_account(self, account: str):
        """Returns (record | None, error | None). Looks the account up with the client-performance filter first;
        falls back to the full client list (matching trading-account id or wallet id)."""
        payload, err = self._get(PERF, {"accounts": account})
        if payload is not None:
            for r in payload.get("clients") or []:
                if str(r.get("account_id")) == account:
                    return {"account": account, "email": r.get("email"), "deposits": _num(r.get("deposits")),
                            "archived": _truthy(r.get("archived")), "activity_status": r.get("activity_status")}, None
        elif err and err not in ("HTTP 404", "HTTP 422"):
            return None, err
        payload2, err2 = self._get(CLIENTS)
        if payload2 is None:
            return None, err or err2
        for r in payload2.get("data") or []:
            if account in (str(r.get("id")), str(r.get("wallet"))):
                return {"account": account, "email": r.get("email"), "deposits": _num(r.get("deposits")),
                        "archived": False, "activity_status": None}, None
        return None, None

    def verify(self, account: str, email: str = None, email_hash_check=None, min_deposit: float = 1.0) -> dict:
        """status: ok | no_match | unfunded | archived | error.
        Pass `email` (first verification) or `email_hash_check` (callable(email)->bool, for re-checks)."""
        if not self.configured:
            return {"status": "error", "reason": "HFM_API_KEY not set"}
        account = str(account).strip()
        if not account.isdigit() or not 4 <= len(account) <= 12:
            return {"status": "no_match"}
        rec, err = self.find_account(account)
        if err:
            return {"status": "error", "reason": "HFM is busy, try again in a minute" if err == "HTTP 429" else f"HFM API: {err}"}
        if rec is None or not rec.get("email"):
            return {"status": "no_match"}
        matches = (_norm_email(rec["email"]) == _norm_email(email)) if email is not None else bool(email_hash_check and email_hash_check(rec["email"]))
        if not matches:
            return {"status": "no_match"}
        if rec["archived"]:
            return {"status": "archived"}
        if (rec["deposits"] or 0) < min_deposit:
            return {"status": "unfunded"}
        return {"status": "ok", "activity_status": rec.get("activity_status")}

    def diagnostics(self) -> dict:
        """Admin-only connectivity check. Returns counts and field names, never client details."""
        if not self.configured:
            return {"ok": False, "error": "HFM_API_KEY not set"}
        payload, err = self._get(PERF)
        if payload is None:
            return {"ok": False, "error": err}
        clients = payload.get("clients") or []
        funded = sum(1 for r in clients if (_num(r.get("deposits")) or 0) > 0)
        return {"ok": True, "clients_returned": len(clients), "funded": funded,
                "fields": sorted(clients[0].keys()) if clients else [], "totals": payload.get("totals")}
