"""Plugin Manager plugin - manages installation and uninstallation of third-party plugins."""


from plugins.base_plugin.base_plugin import BasePlugin
from PIL import Image
from flask import send_from_directory
from pathlib import Path
import logging
import os
import subprocess
from datetime import datetime


logger = logging.getLogger(__name__)



class PluginManager(BasePlugin):
    """Plugin for managing third-party plugins installation/uninstallation."""
   
    @classmethod
    def get_blueprint(cls):
        """Return the Flask blueprint for this plugin's API routes."""
        from . import api
        return api.plugin_manage_bp
   
    @staticmethod
    def _get_plugin_last_commit_date(plugin_id):
        """Get the last commit date for a plugin from its local git repository."""
        try:
            from flask import current_app
            from config import Config
           
            plugins_dir = os.path.join(Config.BASE_DIR, "plugins")
            plugin_dir = os.path.join(plugins_dir, plugin_id)
            git_dir = os.path.join(plugin_dir, ".git")
           
            if not os.path.isdir(git_dir):
                return None
           
            result = subprocess.run(
                ["git", "-C", plugin_dir, "log", "-1", "--format=%ci", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
           
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
           
            return None
        except Exception as e:
            logger.debug(f"Could not get commit date for plugin {plugin_id}: {e}")
            return None
   
    @staticmethod
    def _get_unmanaged_plugins():
        """Get plugins without a repository URL (unmanaged)."""
        try:
            from flask import current_app
            device_config = current_app.config.get('DEVICE_CONFIG')
            if device_config:
                return [p for p in device_config.get_plugins() if not p.get("repository")]
        except (RuntimeError, ImportError):
            pass
        return []
   
    def generate_settings_template(self):
        """Add third-party plugins list to template parameters."""
        template_params = super().generate_settings_template()

        try:
            from flask import current_app

            core_needs_patch = False
            core_patch_missing = []

            try:
                from .patch_core import check_core_patched

                is_patched, missing = check_core_patched()
                core_needs_patch = not is_patched
                core_patch_missing = missing
            except Exception as e:
                logger.warning(f"Could not check patch status: {e}")

            template_params["core_needs_patch"] = core_needs_patch
            template_params["core_patch_missing"] = core_patch_missing

            if core_needs_patch:
                patch_script = os.path.join(os.path.dirname(__file__), "patch-core.sh")

                if os.path.isfile(patch_script):
                    try:
                        subprocess.Popen(
                            ["bash", patch_script],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        template_params["auto_patch_started"] = True
                    except Exception as e:
                        logger.warning(f"Could not start auto core patch: {e}")
                        template_params["auto_patch_started"] = False
                else:
                    logger.warning("patch-core.sh not found for pluginmanager")
                    template_params["auto_patch_started"] = False

                template_params["third_party_plugins"] = []
                template_params["unmanaged_plugins"] = []

            else:
                device_config = current_app.config.get("DEVICE_CONFIG")

                if device_config:
                    third_party = [
                        dict(plugin)
                        for plugin in device_config.get_plugins()
                        if plugin.get("repository")
                    ]

                    try:
                        from .api import _plugin_auto_update_enabled
                    except ImportError:
                        from api import _plugin_auto_update_enabled

                    for plugin in third_party:
                        plugin_id = plugin.get("id")

                        if plugin_id:
                            version_date = self._get_plugin_last_commit_date(plugin_id)
                            plugin["version_date"] = version_date or "Unknown"
                            plugin["auto_update_enabled"] = (
                                _plugin_auto_update_enabled(plugin_id)
                            )
                        else:
                            plugin["auto_update_enabled"] = False

                    template_params["third_party_plugins"] = third_party
                    template_params["unmanaged_plugins"] = self._get_unmanaged_plugins()

                else:
                    template_params["third_party_plugins"] = []
                    template_params["unmanaged_plugins"] = []

        except (RuntimeError, ImportError):
            template_params["third_party_plugins"] = []
            template_params["unmanaged_plugins"] = []
            template_params["core_needs_patch"] = False
            template_params["core_patch_missing"] = []
            template_params["auto_patch_started"] = False

        return template_params
        
    def generate_image(self, settings, device_config):
        """Return a placeholder image - this plugin is UI-only."""
        width, height = device_config.get_resolution()
        img = Image.new('RGB', (width, height), color='white')
        return img