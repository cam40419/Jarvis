# Voice conversations

Simon serves voice through the authenticated chat interface. For remote devices,
configure the single HTTPS origin described in [remote access](remote-access.md).
The previous portfolio proxy deployment has been retired from these instructions.

## Try locally first

```powershell
Start-ScheduledTask -TaskName Simon-Local
```

Open `http://localhost:8000/login`, sign in with your account, and open chat. Choose **Talk to Simon → Start conversation**, allow the microphone, and say hello. Interrupt while Simon is speaking. Try a current-facts question and inspect **View conversation & approvals** for its task result. **Mute** disables microphone input; **Stop task** cancels pending backend work; **End call** ends the voice session. An already dispatched device change cannot be undone by cancelling it.

Voice uses `SIMON_OPENAI_API_KEY` with access to `gpt-live-1`. Configuration:

```dotenv
SIMON_VOICE_ENABLED=true
SIMON_VOICE_MODEL=gpt-live-1
SIMON_VOICE_NAME=cedar
SIMON_VOICE_MAX_SECONDS=900
```

The live model handles speech and delegates tasks to Simon's existing Auto model routing and tools. Home commands execute immediately under existing device permissions. Email and calendar writes still require the review card. Backend work has its own model usage, in addition to live call duration. At most one call per user can be active, with three starts per minute, a 15-minute default duration, and a browser heartbeat timeout. Usage snapshots are recorded as cumulative seconds. Unconfirmed final usage is explicitly marked.

Keep the page in the foreground and the phone unlocked. Switching tabs/apps or locking the phone ends the call in this version. It is a browser conversation, without a background wake word. Bluetooth/speaker behavior and perceived interruption latency need testing on your phone. Use HTTPS remotely: an HTTP LAN IP cannot request the microphone. Raw audio is not stored by Simon; provider session storage is disabled. Text fragments and delegated task results are persisted. Captions are fragments, not guaranteed complete turns or proof that audio was heard.

## Recovery

Use the combined database and file backup procedure in [storage recovery](storage-recovery.md). Database backups include voice transcripts and task history. Store them privately. Run one Simon process; multiple workers require shared voice-session coordination that is not implemented in this phase. On a process crash, the browser disconnects on its next status poll; stale session admission expires at the stored call deadline. Do not automatically reconnect or replay an interrupted command.

## Personal voice settings

Use Personality & voice in chat to choose Jarvis mode, Vesper, your preferred address, and
additional personality guidance. Settings are private to your account within the workspace.
The server voice remains a fallback for accounts without a saved selection. Start a new call
after changing its voice.
