# OpenCode Team Room

An OpenCode TUI plugin for joining a teammate's OpenCode session from your own window. It adds selectable menus to host or join a room, approve members, open shared sessions, send prompts, and interrupt a run.

## Install

Requirements: OpenCode **2.1.3**, Python **3.11+**, Bun, Git, and OpenSSL. Each teammate installs it on their own laptop. The installer creates a private Python environment, adds the plugin to OpenCode's local plugin directory, and leaves provider credentials and OpenCode settings alone.

```sh
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/Anshieee/opencode-team-room/v0.1.0/install.sh | bash'
```

Restart OpenCode, then run `/team-room` or choose **Team workspace** from the command palette.

To update an installation managed by this project:

```sh
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/Anshieee/opencode-team-room/v0.1.0/install.sh | bash -s -- --update'
```

Updates are refused if the installed plugin or helper files changed. The old plugin is copied to `~/.local/share/opencode-team-room/backups/` before replacement. Each published version has its own runtime directory.

## Use

One teammate chooses **Host a room** and shares the short-lived code privately. Each joining teammate enters it in **Join a room by code**. The host approves each request. Choose Wi-Fi for nearby devices or an already-connected Tailscale network. The installer and room do not configure a firewall, sign into Tailscale, enable Serve/Funnel, or expose a public relay.

Room actions are also available in a terminal through `opencode-team-room host`, `join`, `members`, `approve ID`, `reject ID`, `revoke ID`, and `stop`. Codes are read through a private prompt or stdin; do not put them in shell history or chat logs.

The remote session appears in a custom OpenCode page with the latest messages refreshed every two seconds. It displays user and assistant text plus tool names and status. It hides reasoning, tool input/output, and compaction payloads. Prompts and interrupt requests go to the teammate's existing OpenCode server and follow that server's approval settings. The plugin does not replace OpenCode's built-in session renderer. A separate experimental native TUI patch is not included here.

An approved teammate can prompt any session on the instance the owner chose to share. This is whole-instance access, not per-session access. Share only with people you trust, coordinate who is driving, and revoke a member or stop the room when finished. The room ends when its host process exits.

## Files and rollback

- Plugin: `${XDG_CONFIG_HOME:-~/.config}/opencode/plugins/app-workflow-team`
- Private runtime and update backups: `${XDG_DATA_HOME:-~/.local/share}/opencode-team-room`
- Helper command: `~/.local/bin/opencode-team-room`
- Ephemeral room state: `${XDG_STATE_HOME:-~/.local/state}/app-workflow/room.json`

The installer does not overwrite an existing plugin, helper or runtime. `--update` only replaces an unchanged installation with its install receipt. To roll back an update, stop OpenCode and restore the backed-up plugin directory from `backups/plugin-*`; each previous runtime release remains under `releases/`. Removing the plugin directory disables the TUI integration but leaves the helper and room data available for manual cleanup.

## Development

The room protocol and gateway are in `app_workflow/room.py` and `app_workflow/room_transport.py`. The OpenCode TUI plugin is in `plugin/tui.tsx`. The plugin currently targets the 2.1.3 TUI extension API; review compatibility before changing that version.

## License

MIT. See [LICENSE](LICENSE).
