"""
Hermes Web UI -- Workspace and file system helpers.

Workspace lists and last-used workspace are stored per-profile so each
profile has its own workspace configuration.  State files live at
``{profile_home}/webui_state/workspaces.json`` and
``{profile_home}/webui_state/last_workspace.txt``.  The global STATE_DIR
paths are used as fallback when no profile module is available.
"""
import json
import os
from pathlib import Path

from api.config import (
    WORKSPACES_FILE as _GLOBAL_WS_FILE,
    LAST_WORKSPACE_FILE as _GLOBAL_LW_FILE,
    DEFAULT_WORKSPACE as _BOOT_DEFAULT_WORKSPACE,
    MAX_FILE_BYTES, IMAGE_EXTS, MD_EXTS
)


# ── Profile-aware path resolution ───────────────────────────────────────────

def _profile_state_dir() -> Path:
    """Return the webui_state directory for the active profile.

    For the default profile, returns the global STATE_DIR (respects
    HERMES_WEBUI_STATE_DIR env var for test isolation).
    For named profiles, returns {profile_home}/webui_state/.
    """
    try:
        from api.profiles import get_active_profile_name, get_active_hermes_home
        name = get_active_profile_name()
        if name and name != 'default':
            d = get_active_hermes_home() / 'webui_state'
            d.mkdir(parents=True, exist_ok=True)
            return d
    except ImportError:
        pass
    return _GLOBAL_WS_FILE.parent


def _workspaces_file() -> Path:
    """Return the workspaces.json path for the active profile."""
    return _profile_state_dir() / 'workspaces.json'


def _last_workspace_file() -> Path:
    """Return the last_workspace.txt path for the active profile."""
    return _profile_state_dir() / 'last_workspace.txt'


def _profile_default_workspace() -> str:
    """Read the profile's default workspace from its config.yaml.

    Falls back to the boot-time DEFAULT_WORKSPACE constant.
    """
    try:
        from api.profiles import get_active_hermes_home
        from api.config import get_config
        cfg = get_config()
        ws = cfg.get('default_workspace')
        if ws:
            p = Path(ws).expanduser().resolve()
            if p.is_dir():
                return str(p)
    except (ImportError, Exception):
        pass
    return str(_BOOT_DEFAULT_WORKSPACE)


# ── Public API ──────────────────────────────────────────────────────────────

def load_workspaces() -> list:
    ws_file = _workspaces_file()
    if ws_file.exists():
        try:
            return json.loads(ws_file.read_text(encoding='utf-8'))
        except Exception:
            pass
    # Fallback: for the DEFAULT profile only, migrate from the legacy global file.
    # Named profiles should start with a clean list, not inherit another profile's workspaces.
    try:
        from api.profiles import get_active_profile_name
        is_default = get_active_profile_name() in ('default', None)
    except ImportError:
        is_default = True
    if is_default and _GLOBAL_WS_FILE.exists():
        try:
            return json.loads(_GLOBAL_WS_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return [{'path': _profile_default_workspace(), 'name': 'default'}]


def save_workspaces(workspaces: list):
    ws_file = _workspaces_file()
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(json.dumps(workspaces, ensure_ascii=False, indent=2), encoding='utf-8')


def get_last_workspace() -> str:
    lw_file = _last_workspace_file()
    if lw_file.exists():
        try:
            p = lw_file.read_text(encoding='utf-8').strip()
            if p and Path(p).is_dir():
                return p
        except Exception:
            pass
    # Fallback: try global file
    if _GLOBAL_LW_FILE.exists():
        try:
            p = _GLOBAL_LW_FILE.read_text(encoding='utf-8').strip()
            if p and Path(p).is_dir():
                return p
        except Exception:
            pass
    return _profile_default_workspace()


def set_last_workspace(path: str):
    try:
        lw_file = _last_workspace_file()
        lw_file.parent.mkdir(parents=True, exist_ok=True)
        lw_file.write_text(str(path), encoding='utf-8')
    except Exception:
        pass


def safe_resolve_ws(root: Path, requested: str) -> Path:
    """Resolve a relative path inside a workspace root, raising ValueError on traversal."""
    resolved = (root / requested).resolve()
    resolved.relative_to(root.resolve())
    return resolved


def list_dir(workspace: Path, rel='.'):
    target = safe_resolve_ws(workspace, rel)
    if not target.is_dir():
        raise FileNotFoundError(f"Not a directory: {rel}")
    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        entries.append({
            'name': item.name,
            'path': str(item.relative_to(workspace)),
            'type': 'dir' if item.is_dir() else 'file',
            'size': item.stat().st_size if item.is_file() else None,
        })
        if len(entries) >= 200:
            break
    return entries


def read_file_content(workspace: Path, rel: str):
    target = safe_resolve_ws(workspace, rel)
    if not target.is_file():
        raise FileNotFoundError(f"Not a file: {rel}")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"File too large ({size} bytes, max {MAX_FILE_BYTES})")
    content = target.read_text(encoding='utf-8', errors='replace')
    return {'path': rel, 'content': content, 'size': size, 'lines': content.count('\n') + 1}
