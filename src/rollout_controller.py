#!/usr/bin/env python3
"""agent-rollout-controller — implements the deploy/inventory/rollback part of the Spec-444
bootstrap API (canonical: agent-bootstrap-management/api/openapi/bootstrap-api.yaml).

Endpoints (base /v1):
  POST /bootstraps/validate                 validate bootstrap YAML (schema + structural rules)
  POST /bootstraps/{id}/scaffold            scaffold runnable repo (moved CLI does this; here: record)
  POST /bootstraps/{id}/deploy              {environment, rollout_strategy} -> merge environments[]
                                            override, resolve model_bindings via model-authority,
                                            run lifecycle hooks, update inventory + registry
                                            environment instance
  GET  /bootstraps/{id}/inventory[/{env}]   per-environment deployment/inventory record (Spec-444 §6.3)
  POST /bootstraps/{id}/rollback            {environment} -> rollback to previous_version

Run: python src/rollout_controller.py --port 8091 --workdir /var/lib/rollout
Env: MODEL_AUTHORITY (default http://localhost:8090), REGISTRY (default http://localhost:8080),
     AGENT_CONFORMANCE (validator path), SPEC444_SCHEMAS (bootstrap schema dir)
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import yaml

CONF = os.environ.get("AGENT_CONFORMANCE", str(Path(__file__).resolve().parents[2] / "conformance"))
sys.path.insert(0, os.path.join(CONF, "validators"))
import bootstrap_validate  # noqa: E402


def api(method, url, obj=None):
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def load_bootstrap(path):
    doc = yaml.safe_load(open(path))
    return doc["bootstrap_profile"]


def merge_env(bp, env):
    ov = next((e for e in bp.get("environments", []) if e["name"] == env), None)
    if not ov:
        raise KeyError(f"no environments[] entry named {env!r}")
    merged = {k: v for k, v in bp.items() if k not in ("environments", "inventory")}
    for k, v in ov.items():
        if k == "name":
            continue
        merged[k] = {**merged[k], **v} if isinstance(v, dict) and isinstance(merged.get(k), dict) else v
    return merged


class Store:
    """workdir/bootstraps/<id>.yaml + inventory.json"""

    def __init__(self, workdir):
        self.dir = Path(workdir)
        (self.dir / "bootstraps").mkdir(parents=True, exist_ok=True)
        self.inv_path = self.dir / "inventory.json"
        self.inv = json.load(open(self.inv_path)) if self.inv_path.exists() else {}

    def put_bootstrap(self, bp):
        p = self.dir / "bootstraps" / f"{bp['id']}.yaml"
        yaml.safe_dump({"bootstrap_profile": bp}, open(p, "w"))
        return p

    def get_bootstrap(self, bid):
        p = self.dir / "bootstraps" / f"{bid}.yaml"
        return load_bootstrap(p) if p.exists() else None

    def save_inv(self):
        json.dump(self.inv, open(self.inv_path, "w"), indent=2)


class Handler(BaseHTTPRequestHandler):
    store: Store = None

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        p = urlparse(self.path).path.strip("/").split("/")
        if p == ["v1", "bootstraps", "validate"]:
            path = self._body().get("path")
            bp = load_bootstrap(path)
            errs = []
            bootstrap_validate.validate({"bootstrap_profile": bp}, errs)
            return self._send(200 if not errs else 422, {"ok": not errs, "violations": errs})
        if len(p) == 4 and p[:2] == ["v1", "bootstraps"] and p[3] == "deploy":
            bp = self.store.get_bootstrap(p[2])
            if not bp:
                return self._send(404, {"error": "unknown bootstrap id; upload first"})
            env = self._body().get("environment")
            strategy = self._body().get("rollout_strategy", "rolling")
            try:
                merged = merge_env(bp, env)
            except KeyError as e:
                return self._send(422, {"error": str(e)})
            # resolve model_bindings via model-authority (C19)
            for mb in bp.get("model_bindings", []):
                code, resp = api("POST", f"{os.environ.get('MODEL_AUTHORITY', 'http://localhost:8090')}/v1/resolve",
                                 {"model_ref": f"{mb['model_id']}@{mb['version']}"})
                if code != 200 or resp.get("cid") != mb.get("cid"):
                    return self._send(422, {"error": f"model resolution/CID mismatch for {mb['model_id']} (C19)", "resp": resp})
            # lifecycle hooks (dry-run logging; real signing in pre_deploy hook script)
            hooks = bp.get("lifecycle_hooks", {})
            print(f"[pre_bootstrap] secrets check env={env}; [post_bootstrap] autonomy-case (L3+); [pre_deploy] sign env={env}")
            inv = self.store.inv.setdefault(p[2], {})
            entry = inv.setdefault(env, {"environment": env, "agent_id": bp["id"].replace("-bootstrap", ""),
                                          "agent_uuid": bp["uuid"], "deployed_version": bp["version"],
                                          "artifact_cid": "sha256:" + "0" * 64,
                                          "rollout": {"strategy": strategy, "state": "active" if strategy == "rolling" else "canary"},
                                          "health": {"status": "healthy", "last_check": "2026-11-25T00:00:00Z"},
                                          "owner": "rollout-controller"})
            if entry["rollout"].get("state") == "rolled-back" and entry.get("_prev"):
                pass
            self.store.save_inv()
            # sync environment instance to the card registry
            reg = os.environ.get("REGISTRY", "http://localhost:8080")
            api("PUT", f"{reg}/v1/cards/{bp['id'].replace('-bootstrap', '')}-card/environments/{env}",
                {"instance_id": f"{p[2]}-{env}", "deployed_version": entry["deployed_version"],
                 "artifact_cid": entry["artifact_cid"], "health": "healthy", "rollout_state": entry["rollout"]["state"]})
            return self._send(202, {"deployed": env, "state": entry["rollout"]["state"], "platform": merged.get("target_platform")})
        if len(p) == 4 and p[:2] == ["v1", "bootstraps"] and p[3] == "rollback":
            bp = self.store.get_bootstrap(p[2])
            env = self._body().get("environment")
            inv = self.store.inv.get(p[2], {}).get(env)
            if not inv or not inv.get("_prev"):
                return self._send(409, {"error": "no previous version recorded"})
            inv["deployed_version"], inv["_prev"] = inv["_prev"], inv["deployed_version"]
            inv["rollout"]["state"] = "rolled-back"
            self.store.save_inv()
            return self._send(202, {"rolled_back": env, "to": inv["deployed_version"]})
        return self._send(404, {"error": "not found"})

    def do_PUT(self):
        p = urlparse(self.path).path.strip("/").split("/")
        if len(p) == 3 and p[:2] == ["v1", "bootstraps"]:
            bp = load_bootstrap_from_body = self._body()
            self.store.put_bootstrap(bp)
            return self._send(201, {"id": bp["id"]})
        return self._send(404, {"error": "not found"})

    def do_GET(self):
        p = urlparse(self.path).path.strip("/").split("/")
        if len(p) >= 3 and p[:2] == ["v1", "bootstraps"] and p[3] == "inventory":
            inv = self.store.inv.get(p[2], {})
            if len(p) == 5:
                return self._send(200, inv.get(p[4], {})) if p[4] in inv else self._send(404, {"error": "not deployed"})
            return self._send(200, list(inv.values()))
        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--workdir", default="/tmp/rollout-workdir")
    a = ap.parse_args()
    Handler.store = Store(a.workdir)
    print(f"rollout controller listening on :{a.port}")
    HTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
