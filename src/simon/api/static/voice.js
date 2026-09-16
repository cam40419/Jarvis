'use strict';
(() => {
  const panel = el('voice-panel'), start = el('voice-start'), end = el('voice-end');
  const mute = el('voice-mute'), audio = el('voice-audio'), info = el('voice-status');
  let current = null, config = null, historyLoad = 0;
  const say = text => { info.textContent = text; };
  const endpoint = call => '/v1/voice/sessions/' + call.id;
  const alive = call => current === call && !call.ended;
  function controls(active) {
    start.disabled = active || !config?.enabled;
    end.disabled = !active;
    mute.disabled = !active;
    el('voice-stop-task').disabled = !active;
    el('voice-history').disabled = active;
  }
  function release(call) {
    call.ended = true;
    clearInterval(call.heartbeat); clearInterval(call.poll); clearTimeout(call.deadline);
    call.stream?.getTracks().forEach(track => track.stop());
    if (call.channel?.readyState === 'open') {
      try { call.channel.send(JSON.stringify({type: 'session.close'})); } catch { /* connection ended */ }
    }
    call.peer?.close();
    if (current === call) { audio.srcObject = null; current = null; }
  }
  async function stop(message = 'Call ended.', unloading = false) {
    const call = current;
    if (!call) return;
    release(call); controls(false); say(message);
    if (call.id) {
      try {
        if (unloading) {
          await fetch(appPath(endpoint(call) + '/close'), {method: 'POST', keepalive: true,
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token}, body: '{}'});
        } else await api(endpoint(call) + '/close', {});
      } catch { say('Disconnected. Simon will end the server session when its heartbeat expires.'); }
    }
  }
  function caption(call, event) {
    if (!alive(call) || !event.event_id || call.seen.has(event.event_id)) return;
    call.seen.add(event.event_id);
    const speaker = event.type === 'session.input_transcript.delta' ? 'You' : 'Simon';
    appendCaption(call, speaker, event.delta || '');
  }
  function appendCaption(call, speaker, text) {
    if (call.speaker !== speaker) {
      const row = make('div', undefined, 'voice-caption');
      row.append(make('strong', speaker)); call.caption = make('p', ''); row.append(call.caption);
      el('voice-captions').append(row); call.speaker = speaker;
    }
    call.caption.append(document.createTextNode(text));
    const captions = el('voice-captions'); captions.scrollTop = captions.scrollHeight;
  }
  async function gather(peer) {
    if (peer.iceGatheringState === 'complete') return;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { cleanup(); reject(Error('Microphone connection timed out. Please retry.')); }, 10000);
      const changed = () => { if (peer.iceGatheringState === 'complete') { cleanup(); resolve(); } };
      const cleanup = () => { clearTimeout(timer); peer.removeEventListener('icegatheringstatechange', changed); };
      peer.addEventListener('icegatheringstatechange', changed); changed();
    });
  }
  async function begin() {
    if (current || !config?.enabled) return;
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection) {
      say('Voice needs a supported browser over HTTPS, or localhost on this computer.'); return;
    }
    const call = {seen: new Set(), ended: false}; current = call; controls(true);
    historyLoad++;
    el('voice-captions').replaceChildren(); el('voice-results').hidden = true;
    el('voice-task-status').textContent = 'Ready'; el('voice-duration').textContent = '0:00';
    el('voice-hear').hidden = true;
    mute.textContent = 'Mute'; mute.setAttribute('aria-pressed', 'false');
    say('Connecting your microphone…');
    try {
      call.stream = await navigator.mediaDevices.getUserMedia({audio: {
        echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      }});
      if (!alive(call)) { release(call); return; }
      call.peer = new RTCPeerConnection();
      call.peer.ontrack = event => {
        if (!alive(call)) return;
        audio.srcObject = event.streams[0] || new MediaStream([event.track]);
        audio.play().catch(() => { el('voice-hear').hidden = false; });
      };
      call.peer.onconnectionstatechange = () => {
        if (!alive(call)) return;
        if (call.peer.connectionState === 'connected') say('Listening. You can interrupt Simon at any time.');
        if (['failed', 'disconnected', 'closed'].includes(call.peer.connectionState)) stop('Voice connection ended. Tap Start to reconnect.');
      };
      call.stream.getTracks().forEach(track => {
        call.peer.addTrack(track, call.stream);
        track.onended = () => { if (alive(call)) stop('Microphone disconnected.'); };
      });
      call.channel = call.peer.createDataChannel('oai-events');
      call.channel.onmessage = event => {
        if (!alive(call)) return;
        let value; try { value = JSON.parse(event.data); } catch { return; }
        if (value.type?.endsWith('_transcript.delta')) caption(call, value);
        if (value.type === 'session.closed') stop('Call ended.');
        if (value.type === 'error') say('Voice reported a problem. Please repeat or reconnect.');
      };
      await call.peer.setLocalDescription(await call.peer.createOffer());
      await gather(call.peer);
      if (!alive(call)) return;
      const result = await api('/v1/voice/sessions', {sdp: call.peer.localDescription.sdp,
        idempotency_key: crypto.randomUUID(), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'});
      call.id = result.id;
      if (!alive(call)) { await api(endpoint(call) + '/close', {}); return; }
      el('voice-results').href = appPath('/chat#' + result.thread_id);
      el('voice-results').hidden = false;
      await call.peer.setRemoteDescription({type: 'answer', sdp: result.sdp});
      call.heartbeat = setInterval(async () => {
        try { await api(endpoint(call) + '/heartbeat', {}); }
        catch { if (alive(call)) stop('Connection to Simon was lost.'); }
      }, 10000);
      call.poll = setInterval(async () => {
        if (!alive(call) || call.polling) return;
        call.polling = true;
        try {
          const state = await api(endpoint(call));
          if (!alive(call)) return;
          el('voice-task-status').textContent = state.backend_status;
          el('voice-duration').textContent = `${Math.floor(state.seconds / 60)}:${String(Math.floor(state.seconds % 60)).padStart(2, '0')}`;
          if (['closed', 'failed'].includes(state.state)) stop(state.error || 'Call ended.');
        } catch { if (alive(call)) stop('Connection to Simon was lost.'); }
        finally { call.polling = false; }
      }, 2000);
      call.deadline = setTimeout(() => { if (alive(call)) stop('Time limit reached. Start a new conversation to continue.'); }, result.max_seconds * 1000);
      say('Connected. Say hello to Simon.');
    } catch (error) {
      if (!alive(call)) return;
      await stop(error.name === 'NotAllowedError' ? 'Microphone permission was denied. Allow it in your browser and retry.' : error.message);
    }
  }
  el('voice-open').onclick = async () => {
    if (!ready) return;
    panel.showModal(); say('Loading voice…');
    try {
      config = await api('/v1/voice'); controls(!!current);
      const history = el('voice-history'); history.replaceChildren(make('option', 'Recent voice conversations'));
      history.firstChild.value = '';
      for (const record of config.sessions) {
        const option = make('option', new Date(record.created_at).toLocaleString());
        option.value = record.id; history.append(option);
      }
      el('voice-history-label').hidden = !config.sessions.length;
      if (!current) say(config.enabled ? `Ready. Calls last up to ${Math.floor(config.max_seconds / 60)} minutes.` : 'Voice needs OpenAI mode and an API key on the server.');
    } catch (error) { say(error.message); }
  };
  start.onclick = begin;
  el('voice-history').onchange = async event => {
    const id = event.target.value; if (!id || current) return;
    const load = ++historyLoad;
    try {
      const record = await api('/v1/voice/sessions/' + encodeURIComponent(id));
      if (load !== historyLoad || current) return;
      const context = {}; el('voice-captions').replaceChildren();
      for (const fragment of record.fragments) appendCaption(context, fragment.speaker === 'user' ? 'You' : 'Simon', fragment.text);
      el('voice-results').href = appPath('/chat#' + record.thread_id); el('voice-results').hidden = false;
      el('voice-task-status').textContent = record.backend_status;
      say(record.usage_final ? `${Math.round(record.seconds)} seconds recorded.` : 'Final call usage was not confirmed.');
    } catch (error) { say(error.message); }
  };
  end.onclick = () => stop();
  mute.onclick = () => {
    if (!current?.stream) return;
    const muted = mute.getAttribute('aria-pressed') !== 'true';
    current.stream.getAudioTracks().forEach(track => { track.enabled = !muted; });
    mute.setAttribute('aria-pressed', String(muted)); mute.textContent = muted ? 'Unmute' : 'Mute';
  };
  el('voice-stop-task').onclick = async () => {
    const call = current; if (!call?.id) return;
    try { await api(endpoint(call) + '/stop-task', {}); el('voice-task-status').textContent = 'Task stopped. Already sent actions may have completed.'; }
    catch (error) { say(error.message); }
  };
  el('voice-hear').onclick = () => audio.play().then(() => { el('voice-hear').hidden = true; }).catch(() => say('Audio could not play. Check your speaker permissions.'));
  el('voice-close').onclick = async () => { await stop(); panel.close(); };
  panel.addEventListener('cancel', () => stop());
  window.addEventListener('pagehide', () => stop('Call ended.', true));
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && current) stop('Call ended while Simon was in the background.', true);
  });
})();
