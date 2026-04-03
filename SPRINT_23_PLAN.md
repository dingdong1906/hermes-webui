# Sprint 23 — Profile/Workspace/Model Coherence

**Goal:** Make the three systems (Profiles, Workspaces, Model picker) behave as a coherent
hierarchy. Profile is the identity layer. Workspace and model are per-profile defaults that
flow into per-session overrides. Switching profiles updates defaults immediately; it never
retroactively changes existing sessions.

**Repo:** `nesquena/hermes-webui`  
**Branch to create:** `feat/profile-workspace-model-coherence`  
**Base:** current `master` (f21b088, v0.24)  
**Test baseline:** 415 passing tests  

---

## The Invariant (Do Not Violate)

```
Profile switch  →  sets new DEFAULTS for future sessions
                   refreshes dependent UI (models list, workspace list, session list)
                   NEVER mutates existing sessions

Session create  →  inherits active profile's default model + default workspace
                   tagged with active profile name

Session override (mid-convo model change, workspace chip change)
                →  affects ONLY that session
                   does not touch profile defaults
```

---

## What Is Broken Today (Root Cause Analysis)

### Problem 1 — Model picker ignores profile default on switch

`switchToProfile()` in `static/panels.js` calls `populateModelDropdown()`, which rebuilds
the dropdown and restores the model from `localStorage.getItem('hermes-webui-model')` — a
single global browser key. So switching from Profile A (GPT-4) to Profile B (Claude) leaves
the picker still showing GPT-4 because localStorage trumps the server default.

**Root cause:** `populateModelDropdown()` has this guard:
```js
if (data.default_model && !localStorage.getItem('hermes-webui-model')) {
    sel.value = data.default_model;
}
```
The localStorage key is never cleared on profile switch, so the profile's default model
never applies after the first session.

### Problem 2 — Workspace list is global, not per-profile

`WORKSPACES_FILE = STATE_DIR / 'workspaces.json'` — a single file in the global state dir.
`api/workspace.py:load_workspaces()` reads this file unconditionally. Switching profiles
does NOT reload the workspace list. Profile A's workspaces remain visible under Profile B.

`LAST_WORKSPACE_FILE = STATE_DIR / 'last_workspace.txt'` — also global. New sessions on
Profile B inherit Profile A's last-used workspace.

### Problem 3 — `DEFAULT_WORKSPACE` is a process-level singleton

`api/config.py` line 193: `DEFAULT_WORKSPACE = _discover_default_workspace()` — evaluated
at server startup, frozen forever. `new_session()` in `api/models.py` line 71 calls
`get_last_workspace()` which reads `LAST_WORKSPACE_FILE` — also global. So new sessions
never get the active profile's configured default workspace.

### Problem 4 — Session list is not filtered by active profile

`_allSessions` in `sessions.js` contains sessions from all profiles. Session objects have
a `profile` field (added in Sprint 22) but the sidebar never filters on it. Users see
other profiles' sessions mixed in.

### Problem 5 — `switchToProfile()` doesn't refresh the workspace list or session list

After a switch, the workspace dropdown shows stale data. The session list still shows all
profiles' sessions. Neither is refreshed.

---

## Changes Required

### 1. `api/workspace.py` — Make workspace storage profile-aware

The workspace list and last-workspace pointer need to live inside the active profile's
state, not in a global `STATE_DIR` file.

**New helper — `_profile_workspaces_file()` and `_profile_last_workspace_file()`:**
```python
def _profile_state_dir() -> Path:
    """Return the state dir for the active profile.
    Falls back to global STATE_DIR when profiles module is unavailable."""
    try:
        from api.profiles import get_active_hermes_home
        home = get_active_hermes_home()
        # Per-profile state lives inside the profile's HERMES_HOME
        d = home / 'webui_state'
        d.mkdir(parents=True, exist_ok=True)
        return d
    except ImportError:
        from api.config import STATE_DIR
        return STATE_DIR

def _workspaces_file() -> Path:
    return _profile_state_dir() / 'workspaces.json'

def _last_workspace_file() -> Path:
    return _profile_state_dir() / 'last_workspace.txt'
```

**Update `load_workspaces()`** to call `_workspaces_file()` instead of `WORKSPACES_FILE`.
The fallback default when the file doesn't exist should be the profile's configured
default workspace, not the global `DEFAULT_WORKSPACE`:
```python
def load_workspaces() -> list:
    f = _workspaces_file()
    if f.exists():
        try:
            return json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            pass
    # Fallback: build a single-entry list from the profile's default workspace
    default = _get_profile_default_workspace()
    return [{'path': str(default), 'name': 'default'}]
```

**New helper — `_get_profile_default_workspace()`:**
```python
def _get_profile_default_workspace() -> Path:
    """Return the default workspace for the active profile.
    Priority: profile config.yaml 'workspace' key > env var > STATE_DIR/workspace."""
    from api.config import get_config, DEFAULT_WORKSPACE
    cfg_ws = get_config().get('workspace') or get_config().get('default_workspace')
    if cfg_ws:
        p = Path(cfg_ws).expanduser()
        if p.is_dir():
            return p
    return DEFAULT_WORKSPACE
```

**Update `save_workspaces()`, `get_last_workspace()`, `set_last_workspace()`** to use
`_workspaces_file()` and `_last_workspace_file()` respectively. These are already
small single-liners — just swap the path source.

**Important:** The global `WORKSPACES_FILE` in `api/config.py` can stay as-is for
backward compatibility. `api/workspace.py` simply stops importing and using it directly.

**Migration:** On first call to `load_workspaces()` for a profile, if the profile-local
file doesn't exist but the global `WORKSPACES_FILE` does, copy the global file's contents
as the starting point for the default profile only. Non-default profiles start fresh.

### 2. `api/routes.py` — New endpoint: `GET /api/profile/default-workspace`

```python
if parsed.path == '/api/profile/default-workspace':
    from api.workspace import _get_profile_default_workspace
    return j(handler, {'workspace': str(_get_profile_default_workspace())})
```

This lets the frontend ask "what workspace should I start a new session with?" using the
currently-active profile's config, not a stale server-startup value.

### 3. `api/profiles.py` — Return `default_model` and `default_workspace` on switch

`switch_profile()` currently returns `{'profiles': [...], 'active': name}`.

Extend the return value:
```python
return {
    'profiles': list_profiles_api(),
    'active': name,
    'default_model': _cfg.DEFAULT_MODEL,          # freshly read after reload_config()
    'default_workspace': str(_get_profile_default_workspace()),
}
```

This gives the frontend everything it needs to update the picker and workspace chip
atomically in a single round-trip — no second fetch required.

`_get_profile_default_workspace()` should be imported from `api.workspace` (or duplicated
as a small helper here to avoid circular imports — check carefully).

### 4. `static/panels.js` — `switchToProfile()` uses returned defaults

```js
async function switchToProfile(name) {
  if (S.busy) { showToast('Cannot switch profiles while agent is running'); return; }
  try {
    const data = await api('/api/profile/switch', { method: 'POST', body: JSON.stringify({ name }) });
    S.activeProfile = data.active || name;

    // ── Model: apply profile default, bypassing localStorage ──────────────
    // Profile switch is an explicit user intent to adopt this profile's model.
    // We clear the localStorage preference so the profile default wins.
    if (data.default_model) {
      localStorage.removeItem('hermes-webui-model');
    }
    await populateModelDropdown(); // now respects data.default_model via server
    // If the session has a model set, keep it. If no active session, update the picker.
    const sel = $('modelSelect');
    if (sel && data.default_model && !S.session) {
      sel.value = data.default_model;
    }

    // ── Workspace: update active workspace to profile default ─────────────
    if (data.default_workspace) {
      S._profileDefaultWorkspace = data.default_workspace;
    }

    syncTopbar();

    // ── Refresh all dependent panels ──────────────────────────────────────
    _skillsData = null;
    await Promise.all([
      loadWorkspaceList(),      // refresh workspace list from new profile's storage
    ]);
    renderSessionListFromCache(); // re-render to show/hide by profile filter
    await loadSessions();         // fetch sessions tagged to the new profile

    if (_currentPanel === 'skills')    await loadSkills();
    if (_currentPanel === 'memory')    await loadMemory();
    if (_currentPanel === 'tasks')     await loadCrons();
    if (_currentPanel === 'profiles')  await loadProfilesPanel();

    showToast('Switched to profile: ' + name);
  } catch (e) { showToast('Switch failed: ' + e.message); }
}
```

### 5. `static/sessions.js` — Filter session list by active profile

The session list should default to showing only the current profile's sessions.
Add a toggle to show all profiles.

**State variable** (add near `_activeProject`):
```js
let _profileFilter = true;  // true = show only active profile's sessions
```

**Filter application** (inside `renderSessionListFromCache()`, after project filter):
```js
// Profile filter — show only sessions tagged to the active profile
// (sessions with profile=null are legacy pre-Sprint22, always shown)
const profileFiltered = _profileFilter
  ? projectFiltered.filter(s => !s.profile || s.profile === S.activeProfile)
  : projectFiltered;
```

**Toggle button** — add a small "All profiles" / "This profile" toggle in the session
list header, similar to the existing archived toggle. When clicked, flips `_profileFilter`
and calls `renderSessionListFromCache()`. No server round-trip needed.

**On profile switch** — reset `_profileFilter = true` and call `renderSessionListFromCache()`
so you immediately see only the new profile's sessions.

### 6. `static/panels.js` — `renderWorkspaceDropdown()` refresh on profile switch

`loadWorkspaceList()` already calls `GET /api/workspaces`. Since `api/workspace.py` will
now read from the profile-local file, calling `loadWorkspaceList()` after a profile switch
is sufficient — no other changes needed in the dropdown renderer.

However, the workspace chip in the topbar shows the current session's workspace, which
should not change on profile switch. Only the *dropdown list* (available options) should
update. This is already the correct behavior — the chip reads `S.session.workspace`, not
the list.

### 7. `api/models.py` — `new_session()` uses profile default workspace

```python
def new_session(workspace=None, model=None):
    try:
        from api.profiles import get_active_profile_name
        _profile = get_active_profile_name()
    except ImportError:
        _profile = None

    # Use profile's default workspace, not the global last_workspace
    if workspace is None:
        try:
            from api.workspace import _get_profile_default_workspace, get_last_workspace
            # last_workspace is now profile-local too, so this is correct
            workspace = get_last_workspace()
        except Exception:
            from api.config import DEFAULT_WORKSPACE
            workspace = str(DEFAULT_WORKSPACE)

    s = Session(
        workspace=workspace,
        model=model or _cfg.DEFAULT_MODEL,
        profile=_profile,
    )
    ...
```

Note: since `get_last_workspace()` will be profile-local after change #1, this
effectively already does the right thing. The explicit comment is just for clarity.

---

## What NOT to Change

- **The workspace chip on the topbar** — it shows the current session's workspace, not
  a profile default. This is correct. Don't change it.
- **Per-session model overrides** — the session's `model` field should not be touched on
  profile switch. Already correct.
- **The `WORKSPACES_FILE` import in `api/config.py`** — leave it. It's used by the
  settings serialization and possibly conftest.py. `api/workspace.py` simply stops
  importing it directly.
- **Auth, streaming, profiles.py lock logic** — untouched.
- **The `profile` field on Session** — already exists from Sprint 22. Just start using it
  in the filter.

---

## New Tests Required

File: `tests/test_sprint23.py`

```
test_workspace_file_is_profile_local
  — GET /api/workspaces before and after profile switch return different lists
    (after saving different workspaces to each profile's state dir)

test_new_session_inherits_profile_default_workspace
  — Create session with no workspace arg; verify session.workspace matches
    the active profile's configured workspace

test_switch_profile_response_includes_default_model
  — POST /api/profile/switch returns default_model field

test_switch_profile_response_includes_default_workspace
  — POST /api/profile/switch returns default_workspace field

test_session_list_profile_field
  — Sessions created under different profiles have correct profile field

test_profile_filter_excludes_other_profiles
  — Static analysis: sessions.js renderSessionListFromCache contains
    '_profileFilter' and 's.profile === S.activeProfile'

test_workspace_list_reload_on_switch
  — Static analysis: switchToProfile() in panels.js calls loadWorkspaceList()

test_model_localstorage_cleared_on_switch
  — Static analysis: switchToProfile() in panels.js calls
    localStorage.removeItem('hermes-webui-model')
```

---

## File Change Summary

| File | Change |
|------|--------|
| `api/workspace.py` | Make `load_workspaces`, `save_workspaces`, `get_last_workspace`, `set_last_workspace` read/write from profile-local paths. Add `_get_profile_default_workspace()`. Add migration for default profile. |
| `api/profiles.py` | `switch_profile()` returns `default_model` and `default_workspace` in response. |
| `api/routes.py` | Add `GET /api/profile/default-workspace` endpoint. |
| `api/models.py` | `new_session()` comment clarification only (behavior already correct after workspace.py fix). |
| `static/panels.js` | `switchToProfile()`: clear localStorage model key, call `loadWorkspaceList()`, call `loadSessions()`, reset profile filter. |
| `static/sessions.js` | Add `_profileFilter` state, filter `renderSessionListFromCache()` by active profile, add "All profiles" toggle button, reset filter on profile switch. |
| `tests/test_sprint23.py` | New test file with 8 tests. |

---

## Explicit Non-Goals (Out of Scope for Sprint 23)

- Migrating existing sessions from "no profile tag" to "default profile" — they stay
  untagged and are shown under all profiles (the `!s.profile` guard handles this).
- Per-profile session storage directories — sessions stay in the global `SESSION_DIR`.
  The profile tag on the session object is sufficient for filtering.
- UI for setting a profile's default workspace (that's a settings panel feature, Sprint 24).
- Disabling workspaces by default — rejected. The workspace is the agent's `cwd`;
  hiding it doesn't simplify things, it makes the default silently wrong.

---

## Circular Import Warning

`api/workspace.py` will import from `api.profiles`. `api/profiles.py` imports from
`api.config`. `api/config.py` imports from `api.profiles` (already, for `get_config()`).

To avoid a new circular: `api/workspace.py` should use a **deferred import** inside the
helper functions, not a top-level import. The pattern already exists in `api/profiles.py`
and `api/models.py`. Example:

```python
def _profile_state_dir() -> Path:
    try:
        from api.profiles import get_active_hermes_home   # deferred — avoid circular
        ...
```

Do NOT add `from api.profiles import ...` at the top level of `workspace.py`.

---

## How to Verify End-to-End (Manual Checklist)

After implementation, verify these flows in the browser:

1. **Profile switch updates model picker**
   - Set Profile A's config.yaml: `model: anthropic/claude-opus-4-5`
   - Set Profile B's config.yaml: `model: openai/gpt-5.4-mini`
   - Load the UI. Switch to Profile A. Picker should show Claude.
   - Switch to Profile B. Picker should show GPT-4o-mini.
   - Verify: changing the picker manually doesn't affect the other profile.

2. **Profile switch updates workspace list**
   - Add workspace `/tmp/work-a` to Profile A via the workspace dropdown.
   - Switch to Profile B. Open workspace dropdown. `/tmp/work-a` should NOT appear.
   - Add `/tmp/work-b` to Profile B. Switch back to Profile A. Only A's workspaces appear.

3. **New session inherits profile's default workspace**
   - While on Profile B (default workspace: `/tmp/work-b`), create a new session.
   - Session workspace chip should show `/tmp/work-b`, not a stale Profile A path.

4. **Session list filters by profile**
   - Create 2 sessions on Profile A, 2 sessions on Profile B.
   - While on Profile B, sidebar shows only Profile B's 2 sessions.
   - Toggle "All profiles" — all 4 appear.

5. **Existing sessions survive profile switch unmodified**
   - Open Session X on Profile A (model: Claude, workspace: `/tmp/work-a`).
   - Switch to Profile B.
   - Switch back to Profile A and reopen Session X.
   - Model and workspace should be unchanged.

---

## Commit Message Template

```
feat: Sprint 23 — profile/workspace/model coherence

- Workspaces are now profile-local: each profile's workspace list and
  last-workspace pointer live in {profile_home}/webui_state/ instead of
  the global STATE_DIR. Switching profiles reloads the correct workspace list.

- Profile switch response now includes default_model and default_workspace,
  so the frontend can update the model picker and session defaults in one
  round-trip.

- Model picker: switching profiles clears the localStorage preference and
  applies the new profile's default model. Per-session overrides are unaffected.

- Session list: filtered to active profile by default with an "All profiles"
  toggle. Sessions from before Sprint 22 (no profile tag) always shown.

- new_session() inherits the active profile's default workspace via the
  now-profile-local get_last_workspace().

Tests: N passed, 0 failed (+8 new tests in test_sprint23.py).
Co-Authored-By: <agent>
```

---

## Key Existing Code Locations (for the implementing agent)

```
api/workspace.py          — load_workspaces, save_workspaces, get/set_last_workspace
api/config.py:38          — WORKSPACES_FILE = STATE_DIR / 'workspaces.json'
api/config.py:41          — LAST_WORKSPACE_FILE = STATE_DIR / 'last_workspace.txt'
api/config.py:194         — DEFAULT_MODEL = os.getenv(...)
api/config.py:193         — DEFAULT_WORKSPACE = _discover_default_workspace()
api/config.py:677-690     — save_settings() updates DEFAULT_MODEL / DEFAULT_WORKSPACE globals
api/models.py:64-71       — new_session() — uses get_last_workspace() and _cfg.DEFAULT_MODEL
api/models.py:37          — Session.__init__ — has profile=None field
api/profiles.py:100-135   — switch_profile() — returns {'profiles':[], 'active': name}
api/profiles.py:155-180   — list_profiles_api() — has p.model per profile
api/routes.py:169-170     — GET /api/workspaces
api/routes.py:360-367     — workspace add/remove/rename endpoints
api/routes.py:385-421     — profile switch/create/delete endpoints
static/panels.js:659-680  — switchToProfile() — currently missing ws/session refresh
static/panels.js:474-492  — toggleWsDropdown() / closeWsDropdown()
static/sessions.js:67     — _allSessions cache
static/sessions.js:89-120 — filterSessions() / renderSessionListFromCache()
static/sessions.js:71     — _activeProject filter (model for _profileFilter)
static/ui.js:10-44        — populateModelDropdown() — has the localStorage guard
```
