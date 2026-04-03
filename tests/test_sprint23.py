"""Sprint 23 tests: profile/workspace/model coherence."""
import json, pathlib, re, urllib.request, urllib.error

BASE = "http://127.0.0.1:8788"

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status

def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


# ── Workspace profile-locality ──────────────────────────────────────────────

def test_workspace_list_returns_data():
    """Workspace list endpoint works after profile-local refactor."""
    data, status = get("/api/workspaces")
    assert status == 200
    assert "workspaces" in data
    assert isinstance(data["workspaces"], list)
    assert "last" in data


def test_workspace_add_remove_roundtrip():
    """Workspace add/remove still works with profile-local storage."""
    import os
    # Use a path that won't resolve differently (macOS /tmp -> /private/tmp)
    resolved_tmp = str(pathlib.Path("/tmp").resolve())
    # Clean slate
    post("/api/workspaces/remove", {"path": resolved_tmp})
    # Add
    data, status = post("/api/workspaces/add", {"path": "/tmp", "name": "Temp"})
    assert status == 200
    assert any(w["path"] == resolved_tmp for w in data.get("workspaces", []))
    # Remove
    data, status = post("/api/workspaces/remove", {"path": resolved_tmp})
    assert status == 200
    assert not any(w["path"] == resolved_tmp for w in data.get("workspaces", []))


# ── Profile switch response fields ─────────────────────────────────────────

def test_profile_switch_returns_default_model_and_workspace():
    """switch_profile() response includes default_model and default_workspace."""
    # Prior tests (test_chat_stream_opens_successfully) may leave a live LLM stream in
    # STREAMS. The server-side thread keeps running until the LLM response completes.
    # Wait up to 30 seconds for it to drain before attempting the profile switch.
    import time
    for _ in range(60):
        health, _ = get("/health")
        if health.get("active_streams", 0) == 0:
            break
        time.sleep(0.5)
    data, status = post("/api/profile/switch", {"name": "default"})
    assert status == 200, f"Profile switch returned {status}: {data}"
    assert "active" in data
    assert data["active"] == "default"
    # default_workspace should always be present (may be null for model)
    assert "default_workspace" in data
    assert isinstance(data["default_workspace"], str)
    assert "default_model" in data  # can be None


def test_profile_active_endpoint():
    """GET /api/profile/active returns name and path."""
    data, status = get("/api/profile/active")
    assert status == 200
    assert "name" in data, "Response missing 'name' field"
    assert isinstance(data["name"], str) and data["name"], "Profile name should be a non-empty string"
    assert "path" in data


# ── Session profile tagging ────────────────────────────────────────────────

def test_new_session_has_profile_field():
    """Sessions created after Sprint 22 should have a profile field."""
    data, status = post("/api/session/new", {})
    assert status == 200
    session = data["session"]
    assert "profile" in session
    # Clean up
    post("/api/session/delete", {"session_id": session["session_id"]})


def test_sessions_list_includes_profile():
    """Sessions created after Sprint 22 expose a profile field."""
    # Create a session and check via the direct session endpoint
    # (/api/sessions filters out empty Untitled sessions; use /api/session instead)
    create_data, _ = post("/api/session/new", {})
    sid = create_data["session"]["session_id"]
    try:
        data, status = get(f"/api/session?session_id={sid}")
        assert status == 200
        session = data.get("session", data)
        assert "profile" in session, f"'profile' field missing from session: {list(session.keys())}"
    finally:
        post("/api/session/delete", {"session_id": sid})


# ── Static JS analysis ─────────────────────────────────────────────────────

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()

def test_sessions_js_has_profile_filter():
    """sessions.js should filter sessions by active profile."""
    content = (REPO_ROOT / "static" / "sessions.js").read_text()
    assert "_showAllProfiles" in content
    assert "profileFiltered" in content
    assert "S.activeProfile" in content


def test_panels_js_clears_model_on_switch():
    """switchToProfile() must clear localStorage model key."""
    content = (REPO_ROOT / "static" / "panels.js").read_text()
    assert "localStorage.removeItem('hermes-webui-model')" in content
    assert "loadWorkspaceList" in content
    assert "renderSessionList" in content
