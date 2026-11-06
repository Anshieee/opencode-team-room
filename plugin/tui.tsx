/** @jsxImportSource @opentui/solid */
import { homedir } from "node:os"
import { lstat, readFile } from "node:fs/promises"
import path from "node:path"
import { Plugin } from "@opencode/plugin/tui"
import { For, Show, createSignal, onCleanup, onMount } from "solid-js"

const PLUGIN_ID = "app-workflow.team"
const REMOTE_ROUTE = "team-remote-session"
const EVENT_PREFIX = "APP_WORKFLOW_EVENT "
const EVENT_PATH = "/v1/peers"
const MAX_MESSAGE_CHARS = 8000

type RoomRuntime = { version: 1; url: string; token: string; pid?: number }
type Peer = { id: string; name: string; url: string; token: string }
type Session = { id: string; title?: string; parentID?: string; time?: { created?: number } }
type PublicMessage = {
  id: string
  type: string
  text?: string
  description?: string
  content?: Array<{ type: string; text?: string; name?: string; state?: { status?: string } }>
}

function stateFile() {
  return path.join(process.env.XDG_STATE_HOME || path.join(homedir(), ".local/state"), "app-workflow", "room.json")
}

async function noSymlinkComponents(target: string) {
  const resolved = path.resolve(target)
  const parsed = path.parse(resolved)
  let current = parsed.root
  for (const segment of resolved.slice(parsed.root.length).split(path.sep).filter(Boolean)) {
    current = path.join(current, segment)
    let info
    try {
      info = await lstat(current)
    } catch (error) {
      if (typeof error === "object" && error !== null && "code" in error && error.code === "ENOENT") continue
      throw new Error("Cannot inspect the private team room state path.")
    }
    if (info.isSymbolicLink()) throw new Error("Team room state path contains a symbolic link.")
  }
}

async function readRoom(): Promise<RoomRuntime | undefined> {
  const file = stateFile()
  await noSymlinkComponents(file)
  let info
  try {
    info = await lstat(file)
  } catch (error) {
    if (typeof error === "object" && error !== null && "code" in error && error.code === "ENOENT") return
    throw new Error("Cannot read the private team room state.")
  }
  if (
    !info.isFile() ||
    (info.mode & 0o077) !== 0 ||
    (typeof process.getuid === "function" && info.uid !== process.getuid())
  ) throw new Error("Team room state must be a private regular file owned by this user.")
  let value: unknown
  try {
    value = JSON.parse(await readFile(file, "utf8"))
  } catch {
    throw new Error("Team room state is invalid.")
  }
  if (!value || typeof value !== "object") throw new Error("Team room state is invalid.")
  const room = value as Partial<RoomRuntime>
  let url: URL
  try {
    url = new URL(room.url || "")
  } catch {
    throw new Error("Team room address is invalid.")
  }
  if (
    room.version !== 1 ||
    typeof room.token !== "string" ||
    room.token.length < 32 ||
    url.protocol !== "http:" ||
    url.hostname !== "127.0.0.1" ||
    !url.port ||
    url.pathname !== "/" ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) throw new Error("Team room state is invalid.")
  return { version: 1, url: url.origin, token: room.token, pid: room.pid }
}

async function localRequest<T>(room: RoomRuntime, route: string, body?: unknown): Promise<T> {
  const response = await fetch(room.url + route, {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${room.token}`, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    redirect: "error",
    cache: "no-store",
  })
  if (!response.ok) throw new Error(`Team room request failed (HTTP ${response.status}).`)
  return response.json() as Promise<T>
}

function peerBase(room: RoomRuntime, peer: Peer) {
  let url: URL
  try {
    url = new URL(peer.url)
  } catch {
    throw new Error("The teammate address is invalid.")
  }
  const expectedPath = `/peer/${encodeURIComponent(peer.id)}`
  if (
    typeof peer.token !== "string" ||
    url.origin !== room.url ||
    url.pathname !== expectedPath ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) throw new Error("The teammate address is invalid.")
  return { base: url.origin + expectedPath, token: peer.token }
}

async function peerRequest<T>(room: RoomRuntime, peer: Peer, route: string, body?: unknown): Promise<T> {
  if (!route.startsWith("/api/") || route.includes("..") || route.includes("%")) throw new Error("Invalid teammate request.")
  const target = peerBase(room, peer)
  const response = await fetch(target.base + route, {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${target.token}`, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    redirect: "error",
    cache: "no-store",
  })
  if (!response.ok) throw new Error(`Teammate OpenCode request failed (HTTP ${response.status}).`)
  return response.json() as Promise<T>
}

function displayMessage(message: PublicMessage) {
  if (message.type === "user") return (message.text || "").slice(0, MAX_MESSAGE_CHARS)
  if (message.type === "assistant") {
    return (message.content || []).map((part) => {
      if (part.type === "text") return part.text || ""
      if (part.type === "tool") return `[${part.name || "tool"}: ${part.state?.status || "running"}]`
      return ""
    }).filter(Boolean).join("\n").slice(0, MAX_MESSAGE_CHARS)
  }
  if (message.type === "compaction") return "[OpenCode compacted earlier context]"
  if (message.type === "system" || message.type === "synthetic") return (message.description || "[session update]").slice(0, MAX_MESSAGE_CHARS)
  if (message.type === "shell") return "[terminal activity]"
  return `[${message.type}]`
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : "Team action failed."
}

function addOption<Value>(title: string, value: Value, description?: string) {
  return { title, value, description }
}

async function chooseTransport(context: Plugin.Context) {
  return context.ui.dialog.select({
    title: "Team network",
    options: [
      addOption("Automatic — Wi-Fi, then available Tailscale", "auto"),
      addOption("Wi-Fi only", "lan"),
      addOption("Already connected Tailscale only", "tailscale"),
    ],
  })
}

async function spawnRoom(
  args: string[],
  code: string | undefined,
  onEvent: (event: Record<string, unknown>) => void,
  owned: Set<Bun.Subprocess>,
) {
  const executable = path.join(homedir(), ".local/bin/opencode-team-room")
  const env: Record<string, string> = { HOME: homedir() }
  for (const key of [
    "PATH",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "TMPDIR",
    "OPENCODE_PASSWORD",
  ]) {
    const value = process.env[key]
    if (value) env[key] = value
  }
  const child = Bun.spawn([executable, ...args, "--ui-events"], {
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
    env,
  })
  owned.add(child)
  if (code !== undefined) {
    child.stdin.write(code + "\n")
    child.stdin.end()
  } else {
    child.stdin.end()
  }

  const stderrLines: string[] = []
  const stderrTask = (async () => {
    const reader = child.stderr.getReader()
    const decoder = new TextDecoder()
    let buffer = ""
    while (true) {
      const item = await reader.read()
      if (item.done) break
      buffer += decoder.decode(item.value, { stream: true })
      const lines = buffer.split("\n")
      buffer = lines.pop() || ""
      for (const line of lines) if (line.trim()) stderrLines.push(line.trim().slice(0, 500))
      if (stderrLines.length > 8) stderrLines.shift()
    }
    buffer += decoder.decode()
    if (buffer.trim()) stderrLines.push(buffer.trim().slice(0, 500))
  })()

  const stdoutTask = (async () => {
    const reader = child.stdout.getReader()
    const decoder = new TextDecoder()
    let buffer = ""
    while (true) {
      const item = await reader.read()
      if (item.done) break
      buffer += decoder.decode(item.value, { stream: true })
      const lines = buffer.split("\n")
      buffer = lines.pop() || ""
      for (const line of lines) {
        if (!line.startsWith(EVENT_PREFIX)) continue
        try {
          const event = JSON.parse(line.slice(EVENT_PREFIX.length)) as Record<string, unknown>
          onEvent(event)
        } catch {
          // Ignore malformed child events; never echo child output into the TUI.
        }
      }
    }
  })()

  const exited = child.exited.then(async (status) => {
    owned.delete(child)
    await Promise.allSettled([stderrTask, stdoutTask])
    return status
  })

  return {
    child,
    exited,
    failure: async () => {
      await Promise.allSettled([stderrTask, stdoutTask])
      return stderrLines.at(-1) || `Room process exited (${await child.exited}).`
    },
  }
}

async function waitForRoomFile(timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const room = await readRoom().catch(() => undefined)
    if (room) return room
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error("The room started but its private control file did not appear.")
}

async function stopChild(child: Bun.Subprocess) {
  try {
    child.kill("SIGINT")
  } catch {
    return
  }
  const exited = await Promise.race([
    child.exited.then(() => true, () => true),
    new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 3000)),
  ])
  if (!exited) {
    try {
      child.kill("SIGKILL")
    } catch {}
  }
  await child.exited.catch(() => undefined)
}

async function showMembers(context: Plugin.Context) {
  const room = await readRoom()
  if (!room) throw new Error("No active team room.")
  const result = await localRequest<{ members: Array<{ id: string; name: string }>; pending?: Array<{ id: string; name: string }> }>(room, "/v1/members")
  const pending = result.pending || []
  const members = result.members || []
  const choices = [
    ...pending.map((member) => addOption(`Approve ${member.name}`, { kind: "approve", id: member.id })),
    ...pending.map((member) => addOption(`Reject ${member.name}`, { kind: "reject", id: member.id })),
    ...members.filter((member) => member.id !== "self").map((member) => addOption(`Revoke ${member.name}`, { kind: "revoke", id: member.id })),
  ]
  const roster = members.map((member) => member.name).join(", ") || "No active members"
  if (!choices.length) {
    await context.ui.dialog.alert({ title: "Team room", message: `Members: ${roster}\nNo pending requests.` })
    return
  }
  const selected = await context.ui.dialog.select({
    title: "Manage team room",
    placeholder: `Members: ${roster}`,
    options: choices,
  })
  if (!selected) return
  await localRequest(room, `/v1/${selected.kind}`, { id: selected.id })
  const past = selected.kind === "approve" ? "approved" : selected.kind === "reject" ? "rejected" : "revoked"
  context.ui.toast.show({ message: `Team member ${past}.` })
}

async function sendRemotePrompt(context: Plugin.Context, route: { peerId: string; sessionID: string }) {
  const text = await context.ui.dialog.prompt({ title: "Message teammate session", placeholder: "Write a prompt for the remote OpenCode session" })
  if (!text?.trim()) return
  if (text.length > 100000) throw new Error("Keep remote prompts under 100,000 characters.")
  const room = await readRoom()
  if (!room) throw new Error("The team room has ended.")
  const peers = (await localRequest<{ peers: Peer[] }>(room, EVENT_PATH)).peers
  const peer = peers.find((item) => item.id === route.peerId)
  if (!peer) throw new Error("That teammate is no longer sharing an OpenCode instance.")
  await peerRequest(room, peer, `/api/session/${encodeURIComponent(route.sessionID)}/prompt`, { text: text.trim() })
  context.ui.toast.show({ message: "Prompt sent to the teammate session." })
}

async function interruptRemote(context: Plugin.Context, route: { peerId: string; sessionID: string }) {
  const room = await readRoom()
  if (!room) throw new Error("The team room has ended.")
  const peers = (await localRequest<{ peers: Peer[] }>(room, EVENT_PATH)).peers
  const peer = peers.find((item) => item.id === route.peerId)
  if (!peer) throw new Error("That teammate is no longer sharing an OpenCode instance.")
  const response = await fetch(peerBase(room, peer).base + `/api/session/${encodeURIComponent(route.sessionID)}/interrupt`, {
    method: "POST",
    headers: { Authorization: `Bearer ${peerBase(room, peer).token}`, "Content-Type": "application/json" },
    redirect: "error",
  })
  if (!response.ok) throw new Error(`Interrupt request failed (HTTP ${response.status}).`)
  context.ui.toast.show({ message: "Interrupt sent." })
}

async function chooseLocalSession(context: Plugin.Context) {
  const sessions = context.data.session.list().filter((item) => !item.parentID)
  const selected = await context.ui.dialog.select({
    title: "This laptop",
    options: sessions.map((item) => addOption(item.title || "Untitled session", item.id)),
  })
  if (selected) context.ui.router.navigate({ type: "session", sessionID: selected })
  else if (!sessions.length) context.ui.router.navigate({ type: "home" })
}

async function chooseRemoteSession(context: Plugin.Context) {
  const room = await readRoom()
  if (!room) throw new Error("Start or join a team room first.")
  const peers = (await localRequest<{ peers: Peer[] }>(room, EVENT_PATH)).peers
  if (!peers.length) throw new Error("No teammates are sharing an OpenCode instance yet.")
  const peer = await context.ui.dialog.select({
    title: "Choose teammate",
    options: peers.map((item) => addOption(item.name, item, "Open shared sessions")),
  })
  if (!peer) return
  const result = await peerRequest<{ data?: Session[] }>(room, peer, "/api/session?limit=100&order=desc")
  const sessions = (result.data || []).filter((item) => !item.parentID)
  if (!sessions.length) throw new Error(`${peer.name} has no sessions to show.`)
  const session = await context.ui.dialog.select({
    title: `${peer.name}'s sessions`,
    options: sessions.map((item) => addOption(item.title || "Untitled session", item, new Date(item.time?.created || 0).toLocaleString())),
  })
  if (!session) return
  context.ui.router.navigate({
    type: "plugin",
    name: REMOTE_ROUTE,
    data: { peerId: peer.id, peerName: peer.name, sessionID: session.id, sessionTitle: session.title || "Untitled session" },
  })
}

async function startRoom(context: Plugin.Context, action: "host" | "join", transport: string, share: boolean, code: string | undefined, owned: Set<Bun.Subprocess>, state: { child?: Bun.Subprocess; inviteCode?: string; pending?: boolean }) {
  const existing = await readRoom()
  if (existing) throw new Error("A team room is already active on this laptop.")
  const args = [action, "--transport", transport]
  if (share) args.push("--share")
  if (action === "join") args.push("--code-stdin")
  const launched = await spawnRoom(args, code, (event) => {
    if (event.event === "join-pending") {
      state.pending = true
      context.ui.toast.show({ message: "Join request sent. Waiting for the host to approve it." })
    }
    if (event.event === "room-ready" && typeof event.code === "string") {
      state.inviteCode = event.code
      state.pending = false
      context.ui.toast.show({ message: "Team room is ready." })
    }
    if (event.event === "room-joined") {
      state.pending = false
      context.ui.toast.show({ message: "Joined the team room." })
    }
    if (event.event === "join-request" && typeof event.name === "string") {
      context.ui.toast.show({ message: `${event.name} is waiting for room approval.` })
    }
  }, owned)
  state.child = launched.child
  void launched.exited.then(() => {
    if (state.child === launched.child) state.child = undefined
    state.pending = false
    if (state.inviteCode) state.inviteCode = undefined
  })

  const expected = action === "host" ? "room-ready" : "join-pending"
  let event: Record<string, unknown>
  try {
    event = await Promise.race([
    new Promise<Record<string, unknown>>((resolve, reject) => {
      const start = Date.now()
      const timer = setInterval(async () => {
        if (state.child !== launched.child) {
          clearInterval(timer)
          reject(new Error(await launched.failure()))
          return
        }
        if (action === "join" && state.pending) {
          clearInterval(timer)
          resolve({ event: "join-pending" })
          return
        }
        if (action === "host" && state.inviteCode) {
          clearInterval(timer)
          resolve({ event: "room-ready", code: state.inviteCode })
          return
        }
        if (Date.now() - start > 45000) {
          clearInterval(timer)
          reject(new Error("Room startup took too long. Check Tailscale, Wi-Fi discovery, and the visible OpenCode service."))
        }
      }, 100)
    }),
    launched.exited.then(async (status) => {
      throw new Error(status === 0 ? "Room process ended before it was ready." : await launched.failure())
    }),
  ]).finally(() => {
    void waitForRoomFile(5000).catch(() => undefined)
  })
  } catch (error) {
    await stopChild(launched.child)
    throw error
  }

  try {
    if (event.event === expected) {
      if (event.event === "room-ready" && typeof event.code === "string") {
        await waitForRoomFile()
        await context.ui.dialog.alert({ title: "Team room ready", message: `Share this one-time code with your teammate:\n\n${event.code}\n\nKeep this OpenCode window open while the room is active.` })
      } else {
        context.ui.toast.show({ message: "Waiting for the host to approve your join request." })
      }
    }
  } catch (error) {
    await stopChild(launched.child)
    throw error
  }
}

async function mainMenu(context: Plugin.Context, owned: Set<Bun.Subprocess>, state: { child?: Bun.Subprocess; inviteCode?: string; pending?: boolean }) {
  const route = context.ui.router.current()
  const remote = route.type === "plugin" && route.name === REMOTE_ROUTE ? route.data : undefined
  let room: RoomRuntime | undefined
  try {
    room = await readRoom()
  } catch (error) {
    context.ui.toast.show({ title: "Team state error", message: errorText(error), variant: "error" })
    return
  }
  const actions = [
    ...(remote ? [
      addOption("Message this remote session", "prompt"),
      addOption("Interrupt this remote session", "interrupt"),
      addOption("Choose another shared session", "remote"),
      addOption("Return to a local session", "local"),
    ] : [
      addOption("Switch to a shared session", "remote"),
      addOption("Switch to a local session", "local"),
    ]),
    ...(room ? [addOption("Manage members", "members"), addOption("Stop room", "stop")] : []),
    ...(state.inviteCode && room ? [addOption("Show room code again", "code")] : []),
    ...(!room && state.pending ? [addOption("Cancel pending join", "cancel")] : []),
    ...(!room && !state.pending ? [addOption("Host a room", "host"), addOption("Join a room by code", "join")] : []),
  ]
  const action = await context.ui.dialog.select({ title: "Team workspace", options: actions })
  if (!action) return
  try {
    if (action === "host") {
      const share = await context.ui.dialog.confirm({ title: "Share this OpenCode instance?", message: "Approved teammates can view and prompt sessions on this laptop. Provider settings and credentials are not shared." })
      if (share === undefined) return
      const transport = await chooseTransport(context)
      if (!transport) return
      await startRoom(context, "host", transport, share, undefined, owned, state)
    } else if (action === "join") {
      const code = await context.ui.dialog.prompt({ title: "Join room", placeholder: "Paste the private room code" })
      if (!code?.trim()) return
      const share = await context.ui.dialog.confirm({ title: "Share this OpenCode instance?", message: "Only choose yes if the host should be able to open and prompt your sessions." })
      if (share === undefined) return
      const transport = await chooseTransport(context)
      if (!transport) return
      await startRoom(context, "join", transport, share, code.trim(), owned, state)
    } else if (action === "members") {
      await showMembers(context)
    } else if (action === "stop") {
      const confirmed = await context.ui.dialog.confirm({ title: "Stop team room?", message: "All teammate connections will be closed." })
      if (!confirmed) return
      const current = await readRoom()
      if (current) await localRequest(current, "/v1/stop", {})
      state.inviteCode = undefined
      context.ui.toast.show({ message: "Team room stopped." })
    } else if (action === "code" && state.inviteCode) {
      await context.ui.dialog.alert({ title: "Room code", message: `Share this one-time code:\n\n${state.inviteCode}` })
    } else if (action === "cancel") {
      const current = await readRoom().catch(() => undefined)
      if (current) await localRequest(current, "/v1/stop", {})
      else if (state.child) await stopChild(state.child)
      state.pending = false
      context.ui.toast.show({ message: "Pending room join cancelled." })
    } else if (action === "remote") {
      await chooseRemoteSession(context)
    } else if (action === "local") {
      await chooseLocalSession(context)
    } else if (action === "prompt" && remote && typeof remote.peerId === "string" && typeof remote.sessionID === "string") {
      await sendRemotePrompt(context, { peerId: remote.peerId, sessionID: remote.sessionID })
    } else if (action === "interrupt" && remote && typeof remote.peerId === "string" && typeof remote.sessionID === "string") {
      await interruptRemote(context, { peerId: remote.peerId, sessionID: remote.sessionID })
    }
  } catch (error) {
    context.ui.toast.show({ title: "Team action failed", message: errorText(error), variant: "error" })
  }
}

function RemoteSessionPage(props: { context: Plugin.Context; data?: Record<string, unknown> }) {
  const [messages, setMessages] = createSignal<PublicMessage[]>([])
  const [status, setStatus] = createSignal("Connecting to teammate…")
  const [failure, setFailure] = createSignal("")
  const peerId = String(props.data?.peerId || "")
  const peerName = String(props.data?.peerName || "teammate")
  const sessionID = String(props.data?.sessionID || "")
  const sessionTitle = String(props.data?.sessionTitle || "OpenCode session")

  onMount(() => {
    let active = true
    let busy = false
    const refresh = async () => {
      if (!active || busy) return
      busy = true
      try {
        const room = await readRoom()
        if (!room) throw new Error("The team room has ended.")
        const peers = (await localRequest<{ peers: Peer[] }>(room, EVENT_PATH)).peers
        const peer = peers.find((item) => item.id === peerId)
        if (!peer) throw new Error("This teammate is no longer sharing an OpenCode instance.")
        const result = await peerRequest<{ data?: PublicMessage[] }>(room, peer, `/api/session/${encodeURIComponent(sessionID)}/message?limit=40&order=desc`)
        if (!active) return
        setMessages((result.data || []).slice(0, 40).reverse())
        setFailure("")
        setStatus("Live view refreshes every 2 seconds. Use /team to message or interrupt.")
      } catch (error) {
        if (active) {
          setFailure(errorText(error))
          setStatus("Remote session unavailable.")
        }
      } finally {
        busy = false
      }
    }
    void refresh()
    const timer = setInterval(() => void refresh(), 2000)
    onCleanup(() => {
      active = false
      clearInterval(timer)
    })
  })

  return (
    <box flexDirection="column" flexGrow={1} paddingLeft={2} paddingRight={2} paddingTop={1}>
      <text fg={props.context.theme.text.base}><b>{peerName} · {sessionTitle}</b></text>
      <text fg={props.context.theme.text.muted}>{status()}</text>
      <Show when={failure()}><text fg={props.context.theme.text.feedback.error.base}>{failure()}</text></Show>
      <scrollbox flexGrow={1} flexShrink={1} stickyScroll={true}>
        <For each={messages()}>{(message) => (
          <box flexDirection="column" marginBottom={1}>
            <text fg={props.context.theme.text.base}>
              <b>{message.type === "user" ? "Teammate" : message.type === "assistant" ? "OpenCode" : "Session"}</b>
            </text>
            <text fg={props.context.theme.text.base}>{displayMessage(message)}</text>
          </box>
        )}</For>
      </scrollbox>
      <text fg={props.context.theme.text.muted}>/team → Message this remote session · Return to a local session from the same menu</text>
    </box>
  )
}

export default Plugin.define({
  id: PLUGIN_ID,
  setup(context) {
    const owned = new Set<Bun.Subprocess>()
    const state: { child?: Bun.Subprocess; inviteCode?: string; pending?: boolean } = {}
    const unregister = context.ui.router.register({
      name: REMOTE_ROUTE,
      render: ({ data }) => <RemoteSessionPage context={context} data={data} />,
    })
    context.keymap.layer(() => ({
      mode: "global",
      commands: [{
        id: "app-workflow.team.open",
        title: "Team workspace",
        description: "Host or join a trusted room and switch shared OpenCode sessions",
        group: "Team",
        palette: true,
        slash: { name: "team-room" },
        run: () => void mainMenu(context, owned, state),
      }],
    }))
    return async () => {
      unregister()
      const room = await readRoom().catch(() => undefined)
      if (room && state.child && room.pid === state.child.pid) {
        await localRequest(room, "/v1/stop", {}).catch(() => undefined)
      }
      await Promise.all([...owned].map((child) => stopChild(child)))
    }
  },
})
