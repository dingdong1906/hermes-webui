"""
Hermes Web UI -- Profile state management.
Wraps hermes_cli.profiles to provide profile switching for the web UI.

The web UI maintains a process-level "active profile" that determines which
HERMES_HOME directory is used for config, skills, memory, cron, and API keys.
Profile switches update os.environ['HERMES_HOME'] and monkey-patch module-level
cached paths in hermes-agent modules (skills_tool, cron/jobs) that snapshot
HERMES_HOME at import time.
"""
import json
import os
import threading
from pathlib import Path

# ── Module state ────────────────────────────────────────────────────────────
_active_profile = 'default'
_profile_lock = threading.Lock()
# Read from env var so test isolation (HERMES_HOME=TEST_STATE_DIR) is respected.
# Evaluated at import time; server restart picks up any env change.
_DEFAULT_HERMES_HOME = Path(os.getenv('HERMES_HOME', str(Path.home() / '.hermes')))


def _read_active_profile_file() -> str:
    """Read the sticky active profile from ~/.hermes/active_profile."""
    ap_file = _DEFAULT_HERMES_HOME / 'active_profile'
    if ap_file.exists():
        try:
            name = ap_file.read_text().strip()
            if name:
                return name
        except Exception:
            pass
    return 'default'


# ── Public API ──────────────────────────────────────────────────────────────

def get_active_profile_name() -> str:
    """Return the currently active profile name."""
    return _active_profile


def get_active_hermes_home() -> Path:
    """Return the HERMES_HOME path for the currently active profile."""
    if _active_profile == 'default':
        return _DEFAULT_HERMES_HOME
    profile_dir = _DEFAULT_HERMES_HOME / 'profiles' / _active_profile
    if profile_dir.is_dir():
        return profile_dir
    return _DEFAULT_HERMES_HOME


def _set_hermes_home(home: Path):
    """Set HERMES_HOME env var and monkey-patch cached module-level paths."""
    os.environ['HERMES_HOME'] = str(home)

    # Patch skills_tool module-level cache (snapshots HERMES_HOME at import)
    try:
        import tools.skills_tool as _sk
        _sk.HERMES_HOME = home
        _sk.SKILLS_DIR = home / 'skills'
    except (ImportError, AttributeError):
        pass

    # Patch cron/jobs module-level cache
    try:
        import cron.jobs as _cj
        _cj.HERMES_DIR = home
        _cj.CRON_DIR = home / 'cron'
        _cj.JOBS_FILE = _cj.CRON_DIR / 'jobs.json'
        _cj.OUTPUT_DIR = _cj.CRON_DIR / 'output'
    except (ImportError, AttributeError):
        pass


def _reload_dotenv(home: Path):
    """Load .env from the profile dir into os.environ (additive)."""
    env_path = home / '.env'
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v:
                    os.environ[k] = v
    except Exception:
        pass


def init_profile_state():
    """Initialize profile state at server startup.

    Reads ~/.hermes/active_profile, sets HERMES_HOME env var, patches
    module-level cached paths.  Called once from config.py after imports.
    """
    global _active_profile
    _active_profile = _read_active_profile_file()
    home = get_active_hermes_home()
    _set_hermes_home(home)
    _reload_dotenv(home)


def switch_profile(name: str) -> dict:
    """Switch the active profile.

    Validates the profile exists, updates process state, patches module caches,
    reloads .env, and reloads config.yaml.

    Returns: {'profiles': [...], 'active': name}
    Raises ValueError if profile doesn't exist or agent is busy.
    """
    global _active_profile

    # Import here to avoid circular import at module load
    from api.config import STREAMS, STREAMS_LOCK, reload_config

    # Block if agent is running
    with STREAMS_LOCK:
        if len(STREAMS) > 0:
            raise RuntimeError(
                'Cannot switch profiles while an agent is running. '
                'Cancel or wait for it to finish.'
            )

    # Resolve profile directory
    if name == 'default':
        home = _DEFAULT_HERMES_HOME
    else:
        home = _DEFAULT_HERMES_HOME / 'profiles' / name
        if not home.is_dir():
            raise ValueError(f"Profile '{name}' does not exist.")

    with _profile_lock:
        _active_profile = name
        _set_hermes_home(home)
        _reload_dotenv(home)

    # Write sticky default for CLI consistency
    try:
        ap_file = _DEFAULT_HERMES_HOME / 'active_profile'
        ap_file.write_text(name if name != 'default' else '')
    except Exception:
        pass

    # Reload config.yaml from the new profile
    reload_config()

    # Return profile-specific defaults so frontend can apply them
    from api.workspace import get_last_workspace
    from api.config import get_config
    cfg = get_config()
    model_cfg = cfg.get('model', {})
    default_model = None
    if isinstance(model_cfg, str):
        default_model = model_cfg
    elif isinstance(model_cfg, dict):
        default_model = model_cfg.get('default')

    return {
        'profiles': list_profiles_api(),
        'active': name,
        'default_model': default_model,
        'default_workspace': get_last_workspace(),
    }


def list_profiles_api() -> list:
    """List all profiles with metadata, serialized for JSON response."""
    try:
        from hermes_cli.profiles import list_profiles
        infos = list_profiles()
    except ImportError:
        # hermes_cli not available -- return just the default
        return [_default_profile_dict()]

    active = _active_profile
    result = []
    for p in infos:
        result.append({
            'name': p.name,
            'path': str(p.path),
            'is_default': p.is_default,
            'is_active': p.name == active,
            'gateway_running': p.gateway_running,
            'model': p.model,
            'provider': p.provider,
            'has_env': p.has_env,
            'skill_count': p.skill_count,
        })
    return result


def _default_profile_dict() -> dict:
    """Fallback profile dict when hermes_cli is not importable."""
    return {
        'name': 'default',
        'path': str(_DEFAULT_HERMES_HOME),
        'is_default': True,
        'is_active': True,
        'gateway_running': False,
        'model': None,
        'provider': None,
        'has_env': (_DEFAULT_HERMES_HOME / '.env').exists(),
        'skill_count': 0,
    }


def create_profile_api(name: str, clone_from: str = None,
                       clone_config: bool = False) -> dict:
    """Create a new profile. Returns the new profile info dict."""
    try:
        from hermes_cli.profiles import create_profile, validate_profile_name
    except ImportError:
        raise RuntimeError('Profile management requires hermes-agent to be installed.')

    validate_profile_name(name)
    create_profile(
        name,
        clone_from=clone_from,
        clone_config=clone_config,
        clone_all=False,
        no_alias=True,
    )

    # Find and return the newly created profile info
    for p in list_profiles_api():
        if p['name'] == name:
            return p
    return {'name': name, 'path': str(_DEFAULT_HERMES_HOME / 'profiles' / name)}


def delete_profile_api(name: str) -> dict:
    """Delete a profile. Switches to default first if it's the active one."""
    if name == 'default':
        raise ValueError("Cannot delete the default profile.")

    # If deleting the active profile, switch to default first
    if _active_profile == name:
        try:
            switch_profile('default')
        except RuntimeError:
            raise RuntimeError(
                f"Cannot delete active profile '{name}' while an agent is running. "
                "Cancel or wait for it to finish."
            )

    try:
        from hermes_cli.profiles import delete_profile
        delete_profile(name, yes=True)
    except ImportError:
        # Manual fallback: just remove the directory
        import shutil
        profile_dir = _DEFAULT_HERMES_HOME / 'profiles' / name
        if profile_dir.is_dir():
            shutil.rmtree(str(profile_dir))
        else:
            raise ValueError(f"Profile '{name}' does not exist.")

    return {'ok': True, 'name': name}
