import json, subprocess, sys, time, urllib.request
from pathlib import Path
import pytest, yaml

SRV = Path(__file__).resolve().parents[1] / "src" / "rollout_controller.py"
BOOT = "/tmp/boot-example.yaml"

@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import urllib.request as u
    u.urlretrieve("https://raw.githubusercontent.com/Agentic-Operation/agent-bootstrap-management/main/examples/loyalty-agent/bootstrap.yaml", BOOT)
    proc = subprocess.Popen([sys.executable, str(SRV), "--port", "8497", "--workdir", str(tmp_path_factory.mktemp("w"))],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={"PATH": "/usr/bin:/bin", "AGENT_CONFORMANCE": "/nonexistent", "SPEC444_SCHEMAS": "/tmp"})
    time.sleep(1)
    yield "http://localhost:8497"
    proc.terminate()

def post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req)

def test_validate_and_inventory(server):
    bp = yaml.safe_load(open(BOOT))["bootstrap_profile"]
    r = post(f"{server}/v1/bootstraps", bp)
    assert r.status == 201
    r = post(f"{server}/v1/bootstraps/validate", {"path": BOOT})
    assert json.loads(r.read())["ok"] is True
    with urllib.request.urlopen(f"{server}/v1/bootstraps/{bp['id']}/inventory") as r:
        assert isinstance(json.loads(r.read()), list)
