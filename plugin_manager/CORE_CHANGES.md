# Core Changes Required for Plugin Manager

## Quick Start - How to Apply the Patch

**For users who just want to use the Plugin Manager:**

1. Install the pluginmanager plugin via the CLI.
2. Open the InkyPi WebUI and click on PluginManager. 
3. Patch will be applied automatically.
4. Wait 30 seconds and press reload button

**That's it!** After patching, the Plugin Manager will work fully from the web interface.

### Undoing the Patch

If you need to undo the patch, you can restore the original core files:

1. Open a terminal/SSH session on your Raspberry Pi
2. Navigate to your InkyPi directory:
   ```bash
   cd /home/pi/InkyPi
   ```
3. Restore the original files using git:
   ```bash
   git checkout src/plugins/plugin_registry.py src/inkypi.py
   ```
   Or if you want to pull the latest changes from the repository:
   ```bash
   git pull
   ```
4. Run the update script to ensure all changes are properly applied:
   ```bash
   sudo bash install/update.sh
   ```
5. Restart the InkyPi service (if the update script didn't already do this):
   ```bash
   sudo systemctl restart inkypi.service
   ```
   (Replace `inkypi` with your service name if different)

**What happens after undoing:**
- The Plugin Manager will stop auto patch your system next time it is opened, so better uninstall the PluginManager before undoing the patch.
- **All installed third-party plugins will continue to work normally** - they don't depend on the Plugin Manager to function or be installed
- You can still manage plugins manually using the CLI script (`install/cli/inkypi-plugin`)
- To use the Plugin Manager UI again, simply let it auto patch again.

**Note**: The patch changes are minimal and safe. Undoing them only affects the Plugin Manager's ability to register its API routes. All other InkyPi functionality remains unchanged.

---

## What This Patch Does (Simple Explanation)

The Plugin Manager needs to add some API routes to work properly. However, due to how Flask (the web framework) works, these routes must be registered before the web server starts.

**The patch does two things:**

1. **Adds a generic function** (`register_plugin_blueprints`) that allows any plugin to register its own API routes
2. **Calls this function** during InkyPi startup so plugin routes are available

**Why manual patching is needed:**
- The patch button in the UI can't work because it needs the routes to be registered first
- This creates a "chicken and egg" problem: we need routes to patch, but we need to patch to get routes
- The solution is a one-time manual patch via command line

**After patching:**
- The Plugin Manager works completely from the web interface
- No more manual steps needed
- Other plugins can also use this mechanism to add their own API routes

---

## Technical Details

### Why Core Changes Are Necessary

Flask's architecture requires that blueprints be registered **before** the application starts serving requests. The execution order in InkyPi is:

1. Flask app is created (`app = Flask(__name__)`)
2. Plugins are loaded (`load_plugins(...)`)
3. Core blueprints are registered (`app.register_blueprint(...)`)
4. Application starts serving (`serve(app, ...)`)

**Key Constraint**: Blueprints cannot be registered after `serve()` is called. Flask builds its routing table at registration time, and this must happen before the server starts.

**Why Plugins Can't Self-Register**:
- Plugins are loaded via `importlib` before the Flask app is fully configured
- Plugins don't have direct access to the Flask app instance during import
- Even if they did, Flask doesn't support registering blueprints after serving starts

### Required Core Changes

#### 1. Plugin Registry Enhancement (`src/plugins/plugin_registry.py`)

Added a generic function to register blueprints from any plugin:

```python
def register_plugin_blueprints(app):
    """Register blueprints from plugins that expose them via get_blueprint() method.
    
    This is a generic mechanism that allows any plugin to register Flask blueprints
    by implementing a get_blueprint() class method that returns a Blueprint instance.
    
    Args:
        app: Flask application instance to register blueprints with
    """
    for plugin_id, plugin_instance in PLUGIN_CLASSES.items():
        try:
            if hasattr(plugin_instance, 'get_blueprint'):
                bp = plugin_instance.get_blueprint()
                if bp:
                    app.register_blueprint(bp)
                    logger.info(f"Registered blueprint for plugin '{plugin_id}'")
        except Exception as e:
            logger.warning(f"Failed to register blueprint for plugin '{plugin_id}': {e}")
```

**What it does**: Iterates through all loaded plugins, checks if they expose a `get_blueprint()` method, and registers any returned blueprints with the Flask app.

**Why it's generic**: This mechanism works for **any** plugin, not just pluginmanager. Any plugin can implement `get_blueprint()` to register its own routes.

#### 2. Application Initialization (`src/inkypi.py`)

Added a single function call to register plugin blueprints:

```python
from plugins.plugin_registry import load_plugins, get_plugin_instance, register_plugin_blueprints

# ... existing code ...

# Register Blueprints
app.register_blueprint(main_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(plugin_bp)
app.register_blueprint(playlist_bp)
app.register_blueprint(apikeys_bp)

# Register blueprints from plugins (generic mechanism - any plugin can expose blueprints)
register_plugin_blueprints(app)
```

**What it does**: Calls the generic registration function after core blueprints are registered but before the app starts serving.

**Why it's minimal**: This is a single function call that enables blueprint support for all plugins. No plugin-specific code.

### How Plugins Use This Mechanism

Any plugin can register blueprints by:

1. **Creating a blueprint module** (e.g., `api.py`):
   ```python
   from flask import Blueprint
   plugin_bp = Blueprint("pluginname_api", __name__)
   
   @plugin_bp.route("/pluginname-api/endpoint")
   def endpoint():
       return {"success": True}
   ```

2. **Exposing the blueprint** in the plugin class:
   ```python
   class PluginName(BasePlugin):
       @classmethod
       def get_blueprint(cls):
           from . import api
           return api.plugin_bp
   ```

3. **The core automatically registers it** when the app starts.

### Files Modified

- `src/plugins/plugin_registry.py` - Added `register_plugin_blueprints()` function (~15 lines)
- `src/inkypi.py` - Added import and call to `register_plugin_blueprints(app)` (~2 lines)

