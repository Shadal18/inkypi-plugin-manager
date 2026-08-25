# InkyPi Plugin Manager

An InkyPi plugin that provides a web-based interface for installing, updating, converting, and removing third-party InkyPi plugins.

_InkyPi Plugin Manager_ is a plugin for [InkyPi](https://github.com/fatihak/InkyPi) that lets you manage compatible GitHub-hosted plugins from the InkyPi web UI instead of relying on command-line workflows.

## Fork Notice

This project is a heavily modified fork of [InkyPi-Plugin-PluginManager](https://github.com/RobinWts/InkyPi-Plugin-PluginManager) by RobinWts.

It retains the original goal of managing InkyPi plugins through the web UI, but this fork adds substantial functionality and changes to the installation, update, management, and restart workflows.

Notable additions in this fork include:

- Managed and unmanaged plugin detection.
- Conversion of unmanaged plugins into managed GitHub-linked plugins.
- Git branch selection during install and conversion.
- Per-plugin and bulk update checks.
- Update-all support.
- Per-plugin auto-update configuration.
- Terminal-style job output in the web UI.
- Deferred restart handling for manual operations.
- Automatic restart only when background auto-update actually changes a plugin.
- Plugin validation and automatic core-patch bootstrap.

## Install

Use the InkyPi plugin installer with the plugin ID and this repository URL.

```bash
inkypi plugin install plugin_manager https://github.com/Shadal18/inkypi-plugin-manager
```

Depending on your InkyPi installation and service user, Git may report a “detected dubious ownership in repository” error after installation.

If that happens, add the Plugin Manager directory to Git’s system-wide safe directory list:

```bash
sudo git config --system --add safe.directory ~/InkyPi/src/plugins/plugin_manager
```

Restart InkyPi after installation:

```bash
sudo systemctl restart inkypi.service
```

The first time you open Plugin Manager, it automatically applies the required InkyPi core patch.

For details about that one-time patch, see [CORE_CHANGES.md](./pluginmanager/CORE_CHANGES.md).

## Update

To update Plugin Manager on your InkyPi device:

1. SSH into your InkyPi host.
2. Change into the Plugin Manager directory:
   ```bash
   cd ~/InkyPi/src/plugins/plugin_manager
   ```
3. Run this update command:
   ```bash
   git pull origin main && \
   if [ -d plugin_manager ]; then \
     rsync -a plugin_manager/ ./ && \
     rm -rf plugin_manager; \
   fi && \
   sudo systemctl restart inkypi.service
   ```

If you do not see your changes after updating:

- Confirm that you are in the correct plugin directory.
- Clear your browser cache or hard refresh the InkyPi web UI.
- Check the InkyPi service logs for plugin errors.
- Reopen Plugin Manager if an InkyPi update overwrote the required core patch.

## Requirements

- A working InkyPi installation with plugin support.
- Network access from the InkyPi device to GitHub.com.
- Plugins hosted on GitHub.com.
- A valid `plugin-info.json` file in each plugin.
- A plugin `id` in `plugin-info.json` that matches its plugin folder name.

## Features

This plugin extends InkyPi with a browser-based workflow for managing third-party plugins.

- Install compatible plugins directly from GitHub repository URLs.
- Select a Git branch when installing a plugin.
- View installed plugins as managed or unmanaged.
- Check one managed plugin for updates.
- Check all managed plugins for updates at once.
- Update individual managed plugins.
- Update all managed plugins in one job.
- Remove installed plugins from the web UI.
- Display plugin version timestamps.
- Display update status using local and remote commit hashes.
- Convert unmanaged plugins into managed plugins.
- Attach a GitHub repository URL and optional branch to unmanaged plugins.
- Enable auto-update individually for managed plugins.
- Show terminal-style output for install, update, conversion, and removal jobs.
- Queue multiple changes before restarting InkyPi.
- Validate plugin structure and matching plugin IDs.
- Apply the required InkyPi core patch automatically when Plugin Manager is first opened.

## Managed Plugins

The **Managed plugins** section shows plugins that already have a linked GitHub repository URL.

Managed plugins can participate in normal update workflows.

Each managed plugin card includes:

- Plugin name.
- Plugin ID.
- Version timestamp.
- Update status.
- Check button.
- Update button.
- Remove button.
- Auto-update checkbox.

## Unmanaged Plugins

The **Unmanaged plugins** section shows installed plugins that do not yet have a stored repository URL.

These plugins cannot be checked or updated through the managed workflow until they are converted.

To convert an unmanaged plugin:

1. Find the plugin under **Unmanaged plugins**.
2. Enter the GitHub repository URL.
3. Optionally select a branch.
4. Click **Convert**.
5. Wait for the terminal job to finish.
6. Restart InkyPi when ready.

After conversion, the plugin appears under **Managed plugins**.

## Installing Plugins

To install a third-party plugin:

1. Open Plugin Manager from the main InkyPi page.
2. Open the **Install from GitHub** section.
3. Paste a GitHub repository URL.
4. Optionally choose a branch.
5. Click **Install**.
6. Wait for the terminal output to finish.
7. Restart InkyPi when ready.

Example repository URL:

```text
https://github.com/fatihak/InkyPi-Plugin-Template
```

## Updating Plugins

To update one managed plugin:

1. Find the plugin under **Managed plugins**.
2. Click **Check**.
3. Confirm that an update is available.
4. Click **Update**.
5. Wait for the terminal job to complete.
6. Restart InkyPi when ready.

To update all managed plugins:

1. Click **Check all updates**.
2. Review the update status for each plugin.
3. Click **Update all** if updates are available.
4. Wait for the bulk update job to complete.
5. Restart InkyPi when ready.

**Up to date** means the installed plugin matches the linked remote repository.

**Update available** means the local and remote Git commits differ.

## Auto-Update

Managed plugins can be marked for auto-update with the checkbox on each managed plugin card.

Auto-update behavior:

- Auto-update is configured separately for each managed plugin.
- The background worker periodically checks enabled plugins.
- InkyPi restarts automatically only when at least one plugin was updated.
- No restart is performed when all checked plugins are already current.

## Restart Behavior

For install, update, removal, and conversion jobs started from the web UI:

- Plugin Manager finishes the job first.
- The web UI then offers a restart action.
- You can complete multiple plugin changes before restarting InkyPi.

For automatic background updates:

- InkyPi restarts automatically only if a plugin was successfully updated.

## Troubleshooting

### Core files need to be patched

If Plugin Manager shows **Core files need to be patched**, the required core patch has not been applied or was overwritten by an InkyPi update.

Open Plugin Manager and let the automatic patch flow finish.

### No valid InkyPi plugin found

Confirm that:

- The repository URL is correct and reachable.
- The repository is hosted on GitHub.com.
- The repository contains a valid InkyPi plugin folder.
- The plugin contains `plugin-info.json`.
- The `id` inside `plugin-info.json` matches the plugin folder name.

### Plugin appears as unmanaged

A plugin appears under **Unmanaged plugins** when it exists locally but has no stored repository URL.

Enter its GitHub repository URL and use **Convert** to add it to the managed update workflow.

### Update checks fail

If update checks do not work as expected:

1. Confirm that the plugin has a `.git` directory.
2. Confirm that the linked GitHub repository URL is valid.
3. Verify network connectivity from InkyPi to GitHub.com.
4. Confirm the plugin appears under **Managed plugins**.
5. Confirm that the configured branch still exists.

### Auto-update does not run

If a plugin does not auto-update:

1. Confirm that auto-update is enabled on its managed plugin card.
2. Confirm that it is still a valid Git repository.
3. Confirm that `plugin-info.json` still exists.
4. Confirm that the stored repository URL is valid.
5. Check the InkyPi logs for background update errors.

```bash
sudo journalctl -u inkypi.service -n 200 --no-pager
```

### Restart InkyPi manually

Manual Plugin Manager actions do not restart InkyPi immediately by default.

Use the restart button after a job finishes, or restart it manually:

```bash
sudo systemctl restart inkypi.service
```

### Recover from a broken plugin

If a third-party plugin prevents InkyPi from starting, remove it over SSH:

```bash
ls ~/InkyPi/src/plugins
```

```bash
sudo rm -rf ~/InkyPi/src/plugins/PLUGIN_FOLDER
```

```bash
sudo systemctl restart inkypi.service
```

Replace `PLUGIN_FOLDER` with the actual plugin directory name.

## Repository

GitHub repository:

[https://github.com/Shadal18/inkypi-plugin-manager](https://github.com/Shadal18/inkypi-plugin-manager)

## Screenshots

- Plugin Manager dashboard.
- Plugin installation and management interface.

<p align="center">
  <img src="screenshots/example-1.png" width="45%" />
  <img src="screenshots/example-2.png" width="45%" />
</p>

## License

This project is licensed under the GNU General Public License v3.0.
