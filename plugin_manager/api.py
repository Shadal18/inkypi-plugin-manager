"""API routes for pluginmanager plugin - handles install/uninstall/update of third-party plugins."""

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse
import concurrent.futures

from flask import Blueprint, current_app, jsonify, request, send_file
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

plugin_manage_bp = Blueprint("pluginmanager_api", __name__)

_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 300

_AUTO_UPDATE_THREAD = None
_AUTO_UPDATE_THREAD_LOCK = threading.Lock()
_AUTO_UPDATE_POLL_SECONDS = 300
_CHECK_ALL_MAX_WORKERS = 4


def _create_job():
    job_id = str(uuid.uuid4())
    job = {
        "lines": [],
        "done": False,
        "success": None,
        "error": None,
        "created_at": time.time(),
        "lock": threading.Lock(),
        "progress": {
            "current": 0,
            "total": 0,
            "active_plugin_ids": [],
            "results": [],
        },
    }

    with _JOBS_LOCK:
        _JOBS[job_id] = job

    return job_id, job


def _get_job(job_id):
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def _purge_old_jobs():
    cutoff = time.time() - _JOB_TTL
    with _JOBS_LOCK:
        expired = [jid for jid, j in _JOBS.items() if j["created_at"] < cutoff]
        for jid in expired:
            del _JOBS[jid]


def _append_job_line(job, line):
    with job["lock"]:
        job["lines"].append(line)


def _update_job_progress(
    job,
    current=None,
    total=None,
    current_plugin_id=None,
    result=None,
):
    """Safely update state reported by the check-all progress endpoint."""
    with job["lock"]:
        progress = job.setdefault(
            "progress",
            {
                "current": 0,
                "total": 0,
                "current_plugin_id": None,
                "results": [],
            },
        )

        if current is not None:
            progress["current"] = current

        if total is not None:
            progress["total"] = total

        if current_plugin_id is not None:
            progress["current_plugin_id"] = current_plugin_id

        if result is not None:
            progress["results"].append(result)


def _add_active_plugin(job, plugin_id):
    with job["lock"]:
        active = job["progress"].setdefault("active_plugin_ids", [])
        if plugin_id not in active:
            active.append(plugin_id)


def _remove_active_plugin(job, plugin_id):
    with job["lock"]:
        active = job["progress"].setdefault("active_plugin_ids", [])
        if plugin_id in active:
            active.remove(plugin_id)
            

def _mark_job_done(job, success, error=None):
    with job["lock"]:
        job["done"] = True
        job["success"] = success
        job["error"] = error


def _sanitize_restart_lines(line: str) -> str | None:
    if not line:
        return None

    lower = line.lower().strip()

    blocked_patterns = [
        "restarting inkypi service",
        "inkypi is restarting after this operation",
        "newly installed or updated plugins will not be available until inkypi has restarted",
        "you can reload now, or keep installing more plugins and restart later",
        "sleeping 3 seconds before restart",
    ]

    if any(pat in lower for pat in blocked_patterns):
        return None

    return line


def _append_manual_restart_notice(job):
    _append_job_line(job, "[INFO] Done")
    _append_job_line(
        job,
        "[INFO] Newly installed or updated plugins will not be available until InkyPi has been restarted manually.",
    )
    _append_job_line(
        job,
        "[INFO] Use the restart button when you're ready, or install more plugins first.",
    )


def _run_subprocess_job(job_id, cmd, env, cwd, success_markers, append_restart_notice=False):
    job = _get_job(job_id)
    if not job:
        return

    try:
        _append_job_line(job, f"[DEBUG] Running command: {' '.join(cmd)}")

        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        if proc.stdout is not None:
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\n")
                filtered = _sanitize_restart_lines(line)
                if filtered:
                    _append_job_line(job, filtered)

        proc.wait()

        with job["lock"]:
            all_output = "\n".join(job["lines"])

        succeeded = any(marker in all_output for marker in success_markers) or proc.returncode == 0

        if succeeded and append_restart_notice:
            _append_manual_restart_notice(job)

        _mark_job_done(job, succeeded, None if succeeded else "Operation failed — see output above")

    except Exception as e:
        logger.exception("Background job %s raised an exception", job_id)
        _append_job_line(job, f"[ERROR] Unexpected error: {e}")
        _mark_job_done(job, False, str(e))


def _run_local_install_job(job_id, cmd, env, cwd, temp_path):
    try:
        _run_subprocess_job(
            job_id,
            cmd,
            env,
            cwd,
            ["[INFO] Done", "Plugin successfully installed"],
            True,
        )
    finally:
        try:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            logger.exception("Failed to remove temporary uploaded ZIP: %s", temp_path)


def _project_dir():
    try:
        from config import Config

        return os.path.dirname(Config.BASE_DIR)
    except ImportError:
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def _plugins_dir():
    try:
        from config import Config

        return os.path.join(Config.BASE_DIR, "plugins")
    except ImportError:
        return os.path.join(_project_dir(), "src", "plugins")


def _cli_script():
    plugin_dir = os.path.dirname(__file__)
    return os.path.join(plugin_dir, "inkypi-plugin")


def _third_party_plugins():
    device_config = current_app.config["DEVICE_CONFIG"]
    plugins = [dict(p) for p in device_config.get_plugins() if p.get("repository")]
    for plugin in plugins:
        plugin["auto_update_enabled"] = _plugin_auto_update_enabled(plugin["id"])
    return plugins


def _unmanaged_plugins():
    device_config = current_app.config["DEVICE_CONFIG"]
    return [p for p in device_config.get_plugins() if not p.get("repository")]


def _normalize_branch(branch):
    branch = (branch or "").strip()
    if branch.lower() in ("", "default", "default branch", "repo default", "default repo"):
        return None
    return branch


def _validate_install_url(url):
    if not url or not isinstance(url, str):
        return False, "URL is required"
    url = url.strip()
    if not url:
        return False, "URL is required"
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL"
    if parsed.scheme != "https":
        return False, "Only HTTPS URLs are allowed"
    if not parsed.netloc:
        return False, "Invalid URL host"
    host = parsed.netloc.lower().split(":")[0]
    if host not in ("github.com", "www.github.com"):
        return False, "Only GitHub.com repository URLs are accepted"
    return True, None


def _resolve_github_url(url):
    ok, err = _validate_install_url(url)
    if not ok:
        return False, None, err

    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/")
    parts = [p for p in path.split("/") if p]

    if len(parts) < 2:
        return False, None, "Invalid GitHub URL format (expected user/repo)"

    canonical = f"https://github.com/{parts[0]}/{parts[1]}"
    return True, canonical, None


def _get_github_branches(repo_url):
    try:
        parsed = urlparse(repo_url)
        repo_path = parsed.path.strip("/")
        api_url = f"https://api.github.com/repos/{repo_path}/branches"

        result = subprocess.run(
            ["curl", "-s", api_url],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return False, None, "Failed to query GitHub API"

        branches_data = json.loads(result.stdout)
        if not isinstance(branches_data, list):
            return False, None, "Unexpected response from GitHub API"

        branches = [b["name"] for b in branches_data if isinstance(b, dict) and "name" in b]

        if not branches:
            return False, None, "No branches found"

        return True, branches, None
    except Exception as e:
        return False, None, str(e)


def _get_plugin_git_branch(plugin_dir):
    try:
        result = subprocess.run(
            ["git", "-C", plugin_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        logger.warning(
            "Could not get git branch for %s (returncode=%s stdout=%r stderr=%r)",
            plugin_dir,
            result.returncode,
            result.stdout,
            result.stderr,
        )
        return None
    except Exception:
        logger.exception("Exception while getting git branch for %s", plugin_dir)
        return None


def _get_plugin_local_commit(plugin_dir):
    try:
        logger.warning("LOCAL-COMMIT git -C %s rev-parse HEAD", plugin_dir)
        result = subprocess.run(
            ["git", "-C", plugin_dir, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()

        logger.warning(
            "Could not get local commit for %s (returncode=%s stdout=%r stderr=%r)",
            plugin_dir,
            result.returncode,
            result.stdout,
            result.stderr,
        )
        return None
    except Exception:
        logger.exception("Exception while getting local commit for %s", plugin_dir)
        return None


def _get_plugin_remote_url(plugin_dir):
    try:
        result = subprocess.run(
            ["git", "-C", plugin_dir, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()

        logger.warning(
            "Could not get remote URL for %s (returncode=%s stdout=%r stderr=%r)",
            plugin_dir,
            result.returncode,
            result.stdout,
            result.stderr,
        )
        return None
    except Exception:
        logger.exception("Exception while getting remote URL for %s", plugin_dir)
        return None


def _find_preferred_remote_commit(remote_refs):
    remote_commit = None
    default_branch = None

    for branch_name in ["main", "master", "develop"]:
        for ref_line in remote_refs:
            if f"refs/heads/{branch_name}" in ref_line:
                parts = ref_line.split()
                if len(parts) >= 1:
                    remote_commit = parts[0]
                    default_branch = branch_name
                    return remote_commit, default_branch

    if remote_refs:
        first_ref = remote_refs[0]
        parts = first_ref.split()
        if len(parts) >= 2:
            remote_commit = parts[0]
            ref_path = parts[1]
            if "refs/heads/" in ref_path:
                default_branch = ref_path.replace("refs/heads/", "")
    return remote_commit, default_branch


def _load_auto_update_config():
    path = os.path.join(os.path.dirname(__file__), "auto_update_config.json")
    default = {"plugins": {}}

    if not os.path.isfile(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        if "plugins" not in data or not isinstance(data["plugins"], dict):
            data["plugins"] = {}
        return data
    except Exception:
        logger.exception("Failed to load auto update config")
        return default


def _save_auto_update_config(data):
    path = os.path.join(os.path.dirname(__file__), "auto_update_config.json")
    if not isinstance(data, dict):
        data = {"plugins": {}}
    if "plugins" not in data or not isinstance(data["plugins"], dict):
        data["plugins"] = {}

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def _plugin_auto_update_enabled(plugin_id):
    config = _load_auto_update_config()
    plugin_entry = config.get("plugins", {}).get(plugin_id, {})
    return bool(plugin_entry.get("enabled", False))


def _set_plugin_auto_update(plugin_id, enabled):
    config = _load_auto_update_config()
    if "plugins" not in config or not isinstance(config["plugins"], dict):
        config["plugins"] = {}
    config["plugins"][plugin_id] = {"enabled": bool(enabled)}
    _save_auto_update_config(config)


def _restart_service_now():
    appname = os.environ.get("APPNAME", "inkypi").strip() or "inkypi"
    subprocess.Popen(
        ["sudo", "systemctl", "restart", f"{appname}.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _auto_update_loop():
    logger.info("PluginManager auto-update thread started")
    while True:
        try:
            config = _load_auto_update_config()
            enabled_plugins = [
                plugin_id
                for plugin_id, settings in config.get("plugins", {}).items()
                if isinstance(settings, dict) and settings.get("enabled")
            ]

            if enabled_plugins:
                logger.info("Auto-update pass starting for plugins: %s", ", ".join(enabled_plugins))
                updated_any = False

                for plugin_id in enabled_plugins:
                    try:
                        plugin_dir = os.path.join(_plugins_dir(), plugin_id)
                        git_dir = os.path.join(plugin_dir, ".git")
                        info_path = os.path.join(plugin_dir, "plugin-info.json")

                        if not os.path.isdir(plugin_dir):
                            logger.warning("Auto-update skipped %s: plugin directory missing", plugin_id)
                            continue

                        if not os.path.isdir(git_dir):
                            logger.warning("Auto-update skipped %s: not a git repo", plugin_id)
                            continue

                        if not os.path.isfile(info_path):
                            logger.warning("Auto-update skipped %s: plugin-info.json missing", plugin_id)
                            continue

                        try:
                            with open(info_path, "r", encoding="utf-8") as f:
                                info = json.load(f)
                        except Exception:
                            logger.exception("Auto-update skipped %s: failed to parse plugin-info.json", plugin_id)
                            continue

                        repo_url = (info.get("repository") or "").strip()
                        if not repo_url:
                            logger.warning("Auto-update skipped %s: repository missing", plugin_id)
                            continue

                        branch = _get_plugin_git_branch(plugin_dir)
                        before_commit = _get_plugin_local_commit(plugin_dir)

                        cli = _cli_script()
                        if not os.path.isfile(cli):
                            logger.error("Auto-update skipped %s: CLI missing", plugin_id)
                            continue

                        project_dir = _project_dir()
                        env = {**os.environ, "PROJECT_DIR": project_dir, "PM_AUTO_MODE": "1"}

                        cmd = ["bash", cli, "install", plugin_id, repo_url]
                        if branch and branch != "HEAD":
                            cmd.append(branch)

                        logger.info("Auto-updating plugin %s", plugin_id)
                        result = subprocess.run(
                            cmd,
                            env=env,
                            cwd=project_dir,
                            capture_output=True,
                            text=True,
                            timeout=600,
                        )

                        if result.returncode != 0:
                            logger.error(
                                "Auto-update failed for %s: %s",
                                plugin_id,
                                (result.stdout or "") + "\n" + (result.stderr or ""),
                            )
                            continue

                        after_commit = _get_plugin_local_commit(plugin_dir)
                        if before_commit and after_commit and before_commit != after_commit:
                            updated_any = True
                            logger.info("Plugin %s updated from %s to %s", plugin_id, before_commit, after_commit)
                        else:
                            logger.info("Plugin %s already up to date", plugin_id)

                    except subprocess.TimeoutExpired:
                        logger.exception("Auto-update timed out for %s", plugin_id)
                    except Exception:
                        logger.exception("Unexpected auto-update error for %s", plugin_id)

                if updated_any:
                    logger.info("At least one auto-update changed plugin code; restarting InkyPi service")
                    try:
                        _restart_service_now()
                    except Exception:
                        logger.exception("Failed to restart service after auto-update pass")
                else:
                    logger.info("Auto-update pass completed with no changes")

        except Exception:
            logger.exception("PluginManager auto-update loop failure")

        time.sleep(_AUTO_UPDATE_POLL_SECONDS)


def _ensure_auto_update_worker_started():
    global _AUTO_UPDATE_THREAD

    with _AUTO_UPDATE_THREAD_LOCK:
        if _AUTO_UPDATE_THREAD and _AUTO_UPDATE_THREAD.is_alive():
            return

        _AUTO_UPDATE_THREAD = threading.Thread(target=_auto_update_loop, daemon=True)
        _AUTO_UPDATE_THREAD.start()


@plugin_manage_bp.before_app_request
def _bootstrap_auto_update_worker():
    _ensure_auto_update_worker_started()


@plugin_manage_bp.route("/pluginmanager-api/install", methods=["POST"])
def install_plugin():
    data = request.get_json() or {}
    url = data.get("url", "")
    branch = _normalize_branch(data.get("branch", None))

    ok, err = _validate_install_url(url)
    if not ok:
        return jsonify({"success": False, "error": err}), 400

    ok, canonical_url, err = _resolve_github_url(url)
    if not ok:
        return jsonify({"success": False, "error": err}), 400

    cli = _cli_script()
    if not os.path.isfile(cli):
        return jsonify({"success": False, "error": "Plugin CLI not found"}), 500

    project_dir = _project_dir()
    env = {**os.environ, "PROJECT_DIR": project_dir}

    _purge_old_jobs()
    job_id, _ = _create_job()

    cmd = ["bash", cli, "install-from-url", canonical_url]
    if branch:
        cmd.append(branch)

    thread = threading.Thread(
        target=_run_subprocess_job,
        args=(job_id, cmd, env, project_dir, ["[INFO] Done", "Plugin successfully installed"], True),
        daemon=True,
    )
    thread.start()
    return jsonify({"success": True, "job_id": job_id, "reload_on_success": False})


@plugin_manage_bp.route("/pluginmanager-api/install-local", methods=["POST"])
def install_local_plugin():
    uploaded = request.files.get("file")

    if not uploaded or not uploaded.filename:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    filename = secure_filename(uploaded.filename)
    if not filename.lower().endswith(".zip"):
        return jsonify({"success": False, "error": "Only .zip plugin packages are supported"}), 400

    cli = _cli_script()
    if not os.path.isfile(cli):
        return jsonify({"success": False, "error": "Plugin CLI not found"}), 500

    project_dir = _project_dir()
    env = {**os.environ, "PROJECT_DIR": project_dir}

    upload_dir = os.path.join(project_dir, ".pluginmanager_uploads")
    os.makedirs(upload_dir, exist_ok=True)

    temp_name = f"{uuid.uuid4()}_{filename}"
    temp_path = os.path.join(upload_dir, temp_name)

    uploaded.save(temp_path)

    _purge_old_jobs()
    job_id, _ = _create_job()

    cmd = ["bash", cli, "install-local", temp_path]

    thread = threading.Thread(
        target=_run_local_install_job,
        args=(job_id, cmd, env, project_dir, temp_path),
        daemon=True,
    )
    thread.start()
    return jsonify({"success": True, "job_id": job_id, "reload_on_success": False})


@plugin_manage_bp.route("/pluginmanager-api/uninstall", methods=["POST"])
def uninstall_plugin():
    data = request.get_json() or {}
    plugin_id = (data.get("plugin_id") or "").strip()

    if not plugin_id:
        return jsonify({"success": False, "error": "plugin_id is required"}), 400

    third_party = _third_party_plugins()
    allowed_ids = {p["id"] for p in third_party}
    if plugin_id not in allowed_ids:
        return jsonify({"success": False, "error": "Plugin not found or cannot be uninstalled"}), 400

    cli = _cli_script()
    if not os.path.isfile(cli):
        return jsonify({"success": False, "error": "Plugin CLI not found"}), 500

    project_dir = _project_dir()
    env = {**os.environ, "PROJECT_DIR": project_dir}

    _purge_old_jobs()
    job_id, _ = _create_job()

    thread = threading.Thread(
        target=_run_subprocess_job,
        args=(
            job_id,
            ["bash", cli, "uninstall", plugin_id],
            env,
            project_dir,
            ["Plugin successfully uninstalled", "[INFO] Done"],
            True,
        ),
        daemon=True,
    )
    thread.start()
    return jsonify({"success": True, "job_id": job_id, "reload_on_success": False})


@plugin_manage_bp.route("/pluginmanager-api/check-updates", methods=["POST"])
def check_updates():
    data = request.get_json() or {}
    plugin_id = (data.get("plugin_id") or "").strip()

    if not plugin_id:
        return jsonify({"success": False, "error": "plugin_id is required"}), 400

    third_party = _third_party_plugins()
    plugin_info = next((p for p in third_party if p["id"] == plugin_id), None)
    if not plugin_info:
        return jsonify({"success": False, "error": "Plugin not found"}), 400

    try:
        plugin_dir = os.path.join(_plugins_dir(), plugin_id)
        git_dir = os.path.join(plugin_dir, ".git")

        logger.warning(
            "CHECK-UPDATES plugin_id=%s plugin_dir=%s git_dir_exists=%s",
            plugin_id,
            plugin_dir,
            os.path.isdir(git_dir),
        )

        if not os.path.isdir(git_dir):
            return jsonify({"success": False, "error": "Plugin is not a git repository"}), 400

        local_commit = _get_plugin_local_commit(plugin_dir)
        if not local_commit:
            logger.warning("Could not get local commit for %s from %s", plugin_id, plugin_dir)
            return jsonify(
                {
                    "success": False,
                    "error": f"Could not determine current version for {plugin_id} at {plugin_dir}",
                }
            ), 500

        remote_url = _get_plugin_remote_url(plugin_dir)
        if not remote_url:
            logger.warning("Could not get remote URL for %s", plugin_id)
            return jsonify({"success": False, "error": "Could not determine remote repository"}), 500

        ls_remote_result = subprocess.run(
            ["git", "ls-remote", "--heads", remote_url],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if ls_remote_result.returncode != 0:
            logger.warning("Could not query remote for %s: %s", plugin_id, ls_remote_result.stderr)
            return jsonify({"success": False, "error": "Failed to check remote repository"}), 500

        remote_refs = [line for line in ls_remote_result.stdout.strip().split("\n") if line.strip()]
        remote_commit, default_branch = _find_preferred_remote_commit(remote_refs)

        if not remote_commit:
            logger.warning("Could not determine remote commit for %s", plugin_id)
            return jsonify(
                {
                    "success": True,
                    "has_updates": False,
                    "commits_behind": 0,
                    "current_branch": None,
                    "remote_branch": None,
                }
            )

        current_branch = _get_plugin_git_branch(plugin_dir)

        if local_commit == remote_commit:
            return jsonify(
                {
                    "success": True,
                    "has_updates": False,
                    "commits_behind": 0,
                    "current_branch": current_branch,
                    "remote_branch": default_branch,
                }
            )

        return jsonify(
            {
                "success": True,
                "has_updates": True,
                "commits_behind": 1,
                "current_branch": current_branch,
                "remote_branch": default_branch,
            }
        )

    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Check updates timed out"}), 500
    except Exception as e:
        logger.exception("Failed to check for plugin updates")
        return jsonify({"success": False, "error": str(e)}), 500


def _check_single_plugin_updates(job, plugin_id, repo_url):
    """Check one plugin's repository for updates. Runs inside a worker thread
    from the check-all ThreadPoolExecutor. Marks itself active only when this
    worker actually begins running (not when merely submitted/queued), so the
    active count never exceeds the pool's max_workers."""
    _add_active_plugin(job, plugin_id)

    result = {
        "plugin_id": plugin_id,
        "has_updates": False,
        "error": None,
        "current_branch": None,
        "remote_branch": None,
    }

    try:
        if not repo_url:
            result["error"] = "No repository URL"
            return result

        plugin_dir = os.path.join(_plugins_dir(), plugin_id)
        git_dir = os.path.join(plugin_dir, ".git")

        if not os.path.isdir(git_dir):
            result["error"] = "Not a git repository"
            return result

        local_commit = _get_plugin_local_commit(plugin_dir)
        if not local_commit:
            result["error"] = "Could not get local commit"
            return result

        remote_url = _get_plugin_remote_url(plugin_dir)
        if not remote_url:
            result["error"] = "Could not get remote URL"
            return result

        ls_remote_result = subprocess.run(
            ["git", "ls-remote", "--heads", remote_url],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if ls_remote_result.returncode != 0:
            result["error"] = "Could not query remote"
            return result

        remote_refs = [
            line for line in ls_remote_result.stdout.splitlines() if line.strip()
        ]
        remote_commit, remote_branch = _find_preferred_remote_commit(remote_refs)

        if not remote_commit:
            result["error"] = "Could not determine remote commit"
            return result

        current_branch = _get_plugin_git_branch(plugin_dir)
        result.update(
            {
                "has_updates": local_commit != remote_commit,
                "current_branch": current_branch,
                "remote_branch": remote_branch,
            }
        )
        return result

    except subprocess.TimeoutExpired:
        result["error"] = "Check timed out"
        return result
    except Exception as e:
        logger.exception("Failed to check updates for %s", plugin_id)
        result["error"] = str(e)
        return result
    finally:
        _remove_active_plugin(job, plugin_id)
        

def _run_check_all_updates_job(app, job_id):
    """Check all managed plugin repositories in the background, several at a time.

    Runs in a plain thread with no Flask request context. Only the initial
    _third_party_plugins() call needs current_app, so that's the only part
    wrapped in app.app_context() — the concurrent per-plugin checks touch
    only git/subprocess and are safe across worker threads.
    """
    job = _get_job(job_id)
    if not job:
        return

    with app.app_context():
        try:
            third_party = _third_party_plugins()
            total = len(third_party)

            _update_job_progress(job, current=0, total=total)
            _append_job_line(
                job,
                f"[INFO] Checking {total} managed plugin "
                f"{'repository' if total == 1 else 'repositories'} "
                f"(up to {_CHECK_ALL_MAX_WORKERS} at a time)",
            )

            if total == 0:
                _append_job_line(job, "[INFO] No managed plugin repositories found")
                _mark_job_done(job, True)
                return

            completed = 0

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=_CHECK_ALL_MAX_WORKERS
            ) as executor:
                future_to_plugin = {}

                for plugin in third_party:
                    plugin_id = plugin["id"]
                    repo_url = (plugin.get("repository") or "").strip()
                    future = executor.submit(
                        _check_single_plugin_updates, job, plugin_id, repo_url
                    )
                    future_to_plugin[future] = plugin_id
                    _append_job_line(job, f"[INFO] Queued check: {plugin_id}")

                for future in concurrent.futures.as_completed(future_to_plugin):
                    plugin_id = future_to_plugin[future]

                    try:
                        result = future.result()
                    except Exception as e:
                        logger.exception("Worker crashed checking %s", plugin_id)
                        result = {
                            "plugin_id": plugin_id,
                            "has_updates": False,
                            "error": str(e),
                        }
                        _remove_active_plugin(job, plugin_id)

                    completed += 1
                    _update_job_progress(
                        job, current=completed, total=total, result=result
                    )

                    if result.get("error"):
                        _append_job_line(
                            job,
                            f"[WARN] [{completed}/{total}] {plugin_id}: {result['error']}",
                        )
                    elif result.get("has_updates"):
                        _append_job_line(
                            job,
                            f"[INFO] [{completed}/{total}] {plugin_id}: update available",
                        )
                    else:
                        _append_job_line(
                            job,
                            f"[INFO] [{completed}/{total}] {plugin_id}: up to date",
                        )

            with job["lock"]:
                results = list(job["progress"]["results"])

            update_count = sum(1 for r in results if r.get("has_updates"))
            failure_count = sum(1 for r in results if r.get("error"))

            _append_job_line(
                job,
                f"[INFO] Finished checking {total} plugin repositories: "
                f"{update_count} update(s) available, {failure_count} error(s)",
            )
            _mark_job_done(job, True)

        except Exception as e:
            logger.exception("Failed to run check-all-updates job %s", job_id)
            _append_job_line(
                job,
                f"[ERROR] Failed to check managed repositories: {e}",
            )
            _mark_job_done(job, False, str(e))


@plugin_manage_bp.route("/pluginmanager-api/update", methods=["POST"])
def update_plugin():
    data = request.get_json() or {}
    plugin_id = (data.get("plugin_id") or "").strip()
    branch = _normalize_branch(data.get("branch", None))

    if not plugin_id:
        return jsonify({"success": False, "error": "plugin_id is required"}), 400

    third_party = _third_party_plugins()
    plugin_info = next((p for p in third_party if p["id"] == plugin_id), None)
    if not plugin_info:
        return jsonify({"success": False, "error": "Plugin not found or cannot be updated"}), 400

    repo_url = plugin_info.get("repository", "").strip()
    if not repo_url:
        return jsonify({"success": False, "error": "Plugin repository URL not found"}), 400

    cli = _cli_script()
    if not os.path.isfile(cli):
        return jsonify({"success": False, "error": "Plugin CLI not found"}), 500

    project_dir = _project_dir()
    env = {**os.environ, "PROJECT_DIR": project_dir}

    _purge_old_jobs()
    job_id, _ = _create_job()

    cmd = ["bash", cli, "install", plugin_id, repo_url]
    if branch:
        cmd.append(branch)

    thread = threading.Thread(
        target=_run_subprocess_job,
        args=(job_id, cmd, env, project_dir, ["[INFO] Done", "Plugin successfully installed"], True),
        daemon=True,
    )
    thread.start()
    return jsonify({"success": True, "job_id": job_id, "reload_on_success": False})


@plugin_manage_bp.route("/pluginmanager-api/update-all", methods=["POST"])
def update_all_plugins():
    data = request.get_json() or {}
    branch = _normalize_branch(data.get("branch", None))

    cli = _cli_script()
    if not os.path.isfile(cli):
        return jsonify({"success": False, "error": "Plugin CLI not found"}), 500

    project_dir = _project_dir()
    env = {**os.environ, "PROJECT_DIR": project_dir}

    _purge_old_jobs()
    job_id, _ = _create_job()

    cmd = ["bash", cli, "update-all"]
    if branch:
        cmd.append(branch)

    thread = threading.Thread(
        target=_run_subprocess_job,
        args=(job_id, cmd, env, project_dir, ["[INFO] Done", "All plugins updated"], True),
        daemon=True,
    )
    thread.start()
    return jsonify({"success": True, "job_id": job_id, "reload_on_success": False})


@plugin_manage_bp.route("/pluginmanager-api/add-branch", methods=["POST"])
def add_branch():
    data = request.get_json() or {}
    url = data.get("url", "")

    ok, err = _validate_install_url(url)
    if not ok:
        return jsonify({"success": False, "error": err}), 400

    ok, branches, err = _get_github_branches(url)
    if not ok:
        return jsonify({"success": False, "error": err}), 500

    return jsonify({"success": True, "branches": branches})


@plugin_manage_bp.route("/pluginmanager-api/convert-unmanaged", methods=["POST"])
def convert_unmanaged():
    data = request.get_json() or {}
    plugin_id = (data.get("plugin_id") or "").strip()
    repo_url = (data.get("repository") or "").strip()
    branch = _normalize_branch(data.get("branch", None))

    if not plugin_id:
        return jsonify({"success": False, "error": "plugin_id is required"}), 400

    if not repo_url:
        return jsonify({"success": False, "error": "repository URL is required"}), 400

    ok, err = _validate_install_url(repo_url)
    if not ok:
        return jsonify({"success": False, "error": err}), 400

    ok, canonical_url, err = _resolve_github_url(repo_url)
    if not ok:
        return jsonify({"success": False, "error": err}), 400

    unmanaged = _unmanaged_plugins()
    plugin_info = next((p for p in unmanaged if p["id"] == plugin_id), None)
    if not plugin_info:
        return jsonify({"success": False, "error": "Plugin not found or is already managed"}), 400

    cli = _cli_script()
    if not os.path.isfile(cli):
        return jsonify({"success": False, "error": "Plugin CLI not found"}), 500

    project_dir = _project_dir()
    env = {**os.environ, "PROJECT_DIR": project_dir}

    _purge_old_jobs()
    job_id, _ = _create_job()

    cmd = ["bash", cli, "convert-unmanaged", plugin_id, canonical_url]
    if branch:
        cmd.append(branch)

    thread = threading.Thread(
        target=_run_subprocess_job,
        args=(job_id, cmd, env, project_dir, ["[INFO] Done", "Plugin successfully converted"], True),
        daemon=True,
    )
    thread.start()
    return jsonify({"success": True, "job_id": job_id, "reload_on_success": False})


@plugin_manage_bp.route("/pluginmanager-api/auto-update-config", methods=["GET"])
def get_auto_update_config():
    try:
        third_party = _third_party_plugins()
        config = _load_auto_update_config()

        plugins = []
        for plugin in third_party:
            plugin_id = plugin["id"]
            plugins.append(
                {
                    "plugin_id": plugin_id,
                    "enabled": bool(config.get("plugins", {}).get(plugin_id, {}).get("enabled", False)),
                }
            )

        return jsonify({"success": True, "plugins": plugins})
    except Exception as e:
        logger.exception("Failed to get auto update config")
        return jsonify({"success": False, "error": str(e)}), 500


@plugin_manage_bp.route("/pluginmanager-api/auto-update-config", methods=["POST"])
def set_auto_update_config():
    try:
        data = request.get_json() or {}
        plugin_id = (data.get("plugin_id") or "").strip()
        enabled = bool(data.get("enabled", False))

        if not plugin_id:
            return jsonify({"success": False, "error": "plugin_id is required"}), 400

        third_party = _third_party_plugins()
        allowed_ids = {p["id"] for p in third_party}
        if plugin_id not in allowed_ids:
            return jsonify({"success": False, "error": "Plugin not found or not managed"}), 400

        _set_plugin_auto_update(plugin_id, enabled)
        return jsonify({"success": True, "plugin_id": plugin_id, "enabled": enabled})
    except Exception as e:
        logger.exception("Failed to set auto update config")
        return jsonify({"success": False, "error": str(e)}), 500


@plugin_manage_bp.route("/pluginmanager-api/check-all-updates", methods=["POST"])
def check_all_updates():
    """Start a background all-managed-repositories update check."""
    _purge_old_jobs()
    job_id, _ = _create_job()

    app = current_app._get_current_object()

    thread = threading.Thread(
        target=_run_check_all_updates_job,
        args=(app, job_id),
        daemon=True,
    )
    thread.start()

    return jsonify(
        {
            "success": True,
            "job_id": job_id,
        }
    )

@plugin_manage_bp.route("/pluginmanager-api/check-all-updates/<job_id>", methods=["GET"])
def check_all_updates_status(job_id):
    """Poll progress for a running check-all-updates job."""
    job = _get_job(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404

    with job["lock"]:
        progress = job.get("progress", {})
        return jsonify(
            {
                "success": True,
                "done": job["done"],
                "job_success": job["success"],
                "error": job["error"],
                "current": progress.get("current", 0),
                "total": progress.get("total", 0),
                "active_plugin_ids": list(progress.get("active_plugin_ids", [])),
                "plugins": list(progress.get("results", [])),
            }
        )


@plugin_manage_bp.route("/pluginmanager-api/core-changes", methods=["GET"])
def serve_core_changes():
    try:
        md_path = os.path.join(os.path.dirname(__file__), "CORE_CHANGES.md")
        if os.path.exists(md_path):
            return send_file(md_path, mimetype="text/markdown", as_attachment=False)
        return jsonify({"error": "CORE_CHANGES.md not found"}), 404
    except Exception as e:
        logger.exception("Failed to serve CORE_CHANGES.md")
        return jsonify({"error": str(e)}), 500


@plugin_manage_bp.route("/pluginmanager-api/restart-service", methods=["POST"])
def restart_service():
    """Restart InkyPi on demand from the UI."""
    appname = os.environ.get("APPNAME", "inkypi").strip() or "inkypi"

    try:
        subprocess.Popen(
            ["sudo", "systemctl", "restart", f"{appname}.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({"success": True, "service": f"{appname}.service"})
    except Exception as e:
        logger.exception("Failed to restart service")
        return jsonify({"success": False, "error": str(e)}), 500