/* Jarvis browser client. No credentials are persisted by this application. */
'use strict';
(function () {
  const wakePrefix = /^(?:(?:hey|hi|hello)[\s,]+)?(?:jarvis|jarves|jervis|जार्विस|जारविस|జార్విస్|ஜார்விஸ்)(?=[\s,.!?;:।—-]|$)/i;
  const languages = [
    ['en-IN', 'English', 'english|अंग्रेजी|अंग्रेज़ी|ఇంగ్లీష్'], ['hi-IN', 'Hindi · हिंदी', 'hindi|हिंदी|हिन्दी|హిందీ'],
    ['te-IN', 'Telugu · తెలుగు', 'telugu|తెలుగు|तेलुगु'], ['ta-IN', 'Tamil · தமிழ்', 'tamil|தமிழ்|तमिल'],
    ['kn-IN', 'Kannada · ಕನ್ನಡ', 'kannada|ಕನ್ನಡ'], ['ml-IN', 'Malayalam · മലയാളം', 'malayalam|മലയാളം'],
    ['bn-IN', 'Bengali · বাংলা', 'bengali|bangla|বাংলা'], ['mr-IN', 'Marathi · मराठी', 'marathi|मराठी'],
    ['gu-IN', 'Gujarati · ગુજરાતી', 'gujarati|ગુજરાતી'], ['ur-PK', 'Urdu · اردو', 'urdu|اردو'],
    ['es-ES', 'Spanish · Español', 'spanish|español'], ['fr-FR', 'French · Français', 'french|français'],
    ['de-DE', 'German · Deutsch', 'german|deutsch'], ['ar-SA', 'Arabic · العربية', 'arabic|العربية'],
    ['ja-JP', 'Japanese · 日本語', 'japanese|日本語'], ['zh-CN', 'Chinese · 中文', 'chinese|中文']
  ];
  function normalizedVoice(text) { return String(text || '').normalize('NFC').trim().toLowerCase().replace(/[.!?,;:।]+$/g, '').trim(); }
  function localVoiceControl(transcript, { awake = false, explicit = false } = {}) {
    let text = normalizedVoice(transcript);
    if (/^stop\s+(?:jarvis|जार्विस|जारविस|జార్విస్|ஜார்விஸ்)$/.test(text)) return { type: 'end' };
    const prefixed = wakePrefix.test(text);
    if (prefixed) text = text.replace(wakePrefix, '').replace(/^[\s,;:]+/, '');
    if (prefixed && /^(stop listening|सुनना बंद करो|వినడం ఆపు)$/.test(text)) return { type: 'end' };
    if (/^(stop|stop speaking|stop talking|quiet|रुको|बोलना बंद करो|ఆపు|మాట్లాడటం ఆపు)$/.test(text)) return { type: 'stop' };
    if (!prefixed && !awake && !explicit) return null;
    const language = /^(?:change language to|switch language to|language|भाषा|భాష) (.+)$/.exec(text);
    if (language) {
      const match = languages.find(row => row[2].split('|').includes(language[1]));
      if (match) return { type: 'language', value: match[0] };
    }
    if (/^(increase|decrease) volume$/.test(text)) return { type: 'volume', delta: text.startsWith('increase') ? 10 : -10 };
    const volume = /^set volume to (-?\d+(?:\.\d+)?) percent$/.exec(text);
    if (volume) return { type: 'volume', value: Number(volume[1]) };
    if (/^(mute|unmute)$/.test(text)) return { type: 'mute', value: text === 'mute' };
    if (/^(talk|text) mode$/.test(text)) return { type: 'mode', value: text === 'talk mode' };
    return null;
  }
  function wakeDecision(transcript, conversationActive = false) {
    const text = String(transcript || '').trim();
    if (!text) return { type: 'ignore', conversationActive };
    const wake = wakePrefix.exec(text);
    if (wake) {
      const command = (text.slice(0, wake.index) + ' ' + text.slice(wake.index + wake[0].length))
        .replace(/^[\s,.!?;:—-]+|[\s,.!?;:—-]+$/g, '').trim();
      return command && !/^(hi|hello|hey)$/i.test(command) ? { type: 'command', text: command, conversationActive: true }
        : { type: 'wake', conversationActive: true };
    }
    return conversationActive ? { type: 'command', text, conversationActive: true }
      : { type: 'ignore', conversationActive: false };
  }

  // General reply speech only. Never sanitize exact transaction summaries.
  function spokenText(value) {
    return String(value ?? '')
      .replace(/^[ \t]*(?:`{3,}|~{3,})[^\n]*$/gm, '')
      .replace(/!?\[([^\]]+)\]\(https?:\/\/[^\s)]+(?:\s+"[^"]*")?\)/gi, '$1')
      .replace(/https?:\/\/\S+|www\.\S+/gi, '')
      .replace(/[0-9#*]\uFE0F?\u20E3/gu, '')
      .replace(/\p{Extended_Pictographic}[\uFE0E\uFE0F\p{Emoji_Modifier}]*(?:\u200D\p{Extended_Pictographic}[\uFE0E\uFE0F\p{Emoji_Modifier}]*)*(?:[\u{E0020}-\u{E007F}]+)?/gu, '')
      .replace(/[\p{Regional_Indicator}\p{Emoji_Modifier}\uFE0E\uFE0F]/gu, '')
      .replace(/^[ \t]*(?:#{1,6}\s+|[-*+•]\s+|>\s*)/gm, '')
      .replace(/(\*\*|__|~~)(.*?)\1/g, '$2')
      .replace(/([*`])([^\n]*?)\1/g, '$2')
      .replace(/\b_([^_\n]+)_\b/g, '$1')
      .replace(/[*`]/g, '')
      .replace(/\s+/g, ' ').trim();
  }

  function safeExternalUrl(value) {
    try {
      if (typeof value !== 'string' || !/^https:\/\//i.test(value)) return null;
      const url = new URL(value);
      return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
    } catch { return null; }
  }

  function normalizeReminders(data) {
    const rows = Array.isArray(data) ? data : data && data.reminders;
    if (!Array.isArray(rows)) return [];
    return rows.filter(row => row && row.id != null && row.due_at != null)
      .map(row => ({ id: String(row.id), label: String(row.label || row.text || 'Reminder'), dueAt: typeof row.due_at === 'number' ? (row.due_at < 1e12 ? row.due_at * 1000 : row.due_at) : Date.parse(row.due_at) }))
      .filter(row => Number.isFinite(row.dueAt)).sort((a, b) => a.dueAt - b.dueAt);
  }

  function dueReminders(rows, seen, now = Date.now()) {
    return rows.filter(row => {
      const key = row.id + ':' + row.dueAt;
      if (row.dueAt > now || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function mountBrowser(doc, win) {
    const el = id => doc.getElementById(id);
    let authenticated = false, busy = false, destroyed = false, sessionId = '';
    const controllers = new Set();
    const Recognition = win.SpeechRecognition || win.webkitSpeechRecognition;
    let micEnabled = false, recognition = null, recognizing = false, recognitionStarting = false, speaking = false;
    let conversationActive = false, restartTimer = null, speechTimer = null, speechVersion = 0;
    let lastVoiceText = '', lastVoiceAt = 0;
    let suppressAutoplay = false;
    let voiceVolume = 80, lastAudibleVolume = 80;
    let activeSpeech = null;
    let pendingConfirmation = null;
    let greetOnStart = false;
    const usedVoicePhrases = new Set();
    function confirmationStatus() {
      if (!pendingConfirmation) return;
      status(pendingConfirmation.ready ? 'Awaiting exact phrase: “' + pendingConfirmation.phrase + '”. Or say “cancel action”; buttons remain available.' : 'Review the action below. Use its buttons, or finish listening to the spoken summary before approving by voice.');
    }
    function handleVoiceConfirmation(transcript, explicit = false) {
      const text = normalizedVoice(transcript);
      if (speaking || busy) return true;
      const pending = pendingConfirmation;
      if (pending && (conversationActive || explicit)) {
        if (/^(cancel action|no|नहीं|नही|రద్దు చేయి|వద్దు|కాదు)$/.test(text)) return pending.decide(false, true).then(() => true);
        if (pending.ready && text === normalizedVoice(pending.phrase)) return pending.decide(true, true).then(() => true);
        // A new question can replace a legacy research prompt, never a mutation.
        // Approval-looking speech must remain here; it is not a fresh question.
        if (pending.tool === 'public_research' && !/(?:\bapprove\b|\byes\b|हाँ|हां|అవును)/i.test(text) && wakeDecision(text, conversationActive || explicit).type === 'command') {
          pending.retire();
          return false;
        }
        confirmationStatus();
        return true;
      }
      // Approval-like speech never becomes a new chat/tool request.
      return usedVoicePhrases.has(text) || /^(yes|हाँ|हां|అవును|approve(?:\s.*)?)$/.test(text);
    }
    let voiceLanguage = 'en-IN', availableVoices = [];
    function refreshVoices() { availableVoices = win.speechSynthesis?.getVoices?.() || []; }
    function setLanguage(value) {
      try {
        if (typeof value !== 'string' || !/^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$/.test(value)) throw new Error('Invalid language');
        value = Intl.getCanonicalLocales(value)[0];
      } catch { status('Enter a valid BCP 47 language tag, such as pt-BR.', true); return; }
      voiceLanguage = value;
      const known = languages.some(row => row[0] === value);
      el('voice-language').value = known ? value : 'custom';
      el('voice-language-custom').hidden = known;
      if (!known) el('voice-language-custom').value = value;
      if (recognition) {
        recognition.lang = value;
        // abort() ends asynchronously in real browsers; onend schedules restart.
        if (recognizing) { try { recognition.abort(); } catch { /* Wait for the existing recognition to end. */ } }
        else startRecognition();
      }
      refreshVoices();
      status('Voice language: ' + value + '. Browser recognition and installed voices determine support.');
    }
    let sttAvailable = false, recorder = null, mediaStream = null, recordTimer = null, capturePending = false, discardRecording = false;
    const canRecord = !!(win.MediaRecorder && win.navigator.mediaDevices && win.navigator.mediaDevices.getUserMedia);
    function releaseStream() {
      if (mediaStream) { mediaStream.getTracks().forEach(track => track.stop()); mediaStream = null; }
    }
    function stopRecording(discard = false) {
      discardRecording = discard;
      win.clearTimeout(recordTimer);
      if (recorder && recorder.state !== 'inactive') recorder.stop();
      releaseStream();
      el('record-toggle').textContent = 'Record command';
    }
    async function toggleRecording() {
      if (recorder && recorder.state === 'recording') { stopRecording(); return; }
      if (!micEnabled || busy || capturePending || destroyed || !sttAvailable) return;
      capturePending = true; discardRecording = false; cancelSpeech(false); updateControls();
      try {
        const stream = await win.navigator.mediaDevices.getUserMedia({ audio: true });
        if (!micEnabled || doc.hidden || destroyed) { stream.getTracks().forEach(track => track.stop()); return; }
        mediaStream = stream;
        const mime = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus'].find(type => win.MediaRecorder.isTypeSupported && win.MediaRecorder.isTypeSupported(type));
        recorder = new win.MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
        const chunks = []; let bytes = 0;
        recorder.ondataavailable = event => {
          if (event.data.size) { chunks.push(event.data); bytes += event.data.size; }
          if (bytes > 15 * 1024 * 1024) { stopRecording(true); status('Recording is too large. Try a shorter command.', true); }
        };
        recorder.onerror = () => { stopRecording(true); disableMic('Recording failed. Please use typed chat or try again.'); };
        recorder.onstop = async () => {
          releaseStream(); win.clearTimeout(recordTimer);
          const mimeType = recorder.mimeType || mime || 'audio/webm'; recorder = null;
          el('record-toggle').textContent = 'Record command';
          if (discardRecording || !micEnabled || destroyed || doc.hidden || !bytes) { updateControls(); return; }
          busy = true; updateControls(); status('Uploading recording for transcription…');
          micState('Transcribing', 'Your recording is being sent to your server.');
          try {
            const form = new win.FormData();
            form.append('audio', new win.Blob(chunks, { type: mimeType }), 'command.' + (mimeType.includes('mp4') ? 'mp4' : mimeType.includes('ogg') ? 'ogg' : 'webm'));
            const result = await api('/api/transcribe', form);
            busy = false;
            if (!micEnabled || destroyed || doc.hidden) return;
            if (typeof result.text !== 'string' || !result.text.trim()) throw new Error('No speech was recognized. Try again or type your command.');
            if (!applyLocalControl(localVoiceControl(result.text, { explicit: true })) && !await handleVoiceConfirmation(result.text, true)) await sendText(result.text);
          } catch (error) { status(error.message, true); }
          finally { busy = false; updateControls(); if (micEnabled && !speaking) micState('Push-to-talk ready', 'Record a command, then select Stop & send. Audio is uploaded to your server.', true); }
        };
        recorder.start(1000);
        el('record-toggle').textContent = 'Stop & send';
        micState('Recording command', 'Select Stop & send to upload. Maximum 30 seconds. Stop mic discards this recording.', true);
        recordTimer = win.setTimeout(() => stopRecording(), 30000);
      } catch (error) {
        releaseStream();
        const reason = error.name === 'NotAllowedError' ? 'Microphone permission denied. Allow access in browser settings.' : error.name === 'NotFoundError' ? 'No microphone was found. Typed chat is available.' : 'Cannot access your microphone: ' + error.message;
        disableMic(reason);
      } finally { capturePending = false; updateControls(); }
    }
    function micState(title, description, active = false) {
      el('mic-state').textContent = title; el('mic-description').textContent = description;
      el('mic-indicator').classList.toggle('active', active);
      el('mic-toggle').textContent = micEnabled ? 'Stop mic' : 'Enable mic';
      el('mic-toggle').setAttribute('aria-pressed', String(micEnabled));
      el('start-conversation').disabled = !authenticated || busy || speaking || !!pendingConfirmation || conversationActive;
    }
    function pauseRecognition() {
      win.clearTimeout(restartTimer);
      if (recognition) { try { recognition.abort(); } catch { /* Already ended. */ } }
      recognizing = false; recognitionStarting = false;
    }
    function startRecognition() {
      if (!micEnabled || destroyed || doc.hidden || !authenticated || !recognition || recognizing || recognitionStarting) return;
      try { recognitionStarting = true; recognition.start(); }
      catch (error) { recognitionStarting = false; disableMic('Unable to start microphone: ' + error.message); }
    }
    function recognitionState() {
      if (!micEnabled || !Recognition) return;
      if (speaking || busy) micState(speaking ? 'Speaking' : 'Thinking', recognizing ? 'Mic on · local controls only. Use headphones to reduce echo.' : 'Mic starting or reconnecting. Use the buttons until it is ready.', recognizing);
      else if (!recognizing) micState('Starting microphone…', 'Allow microphone access if asked. Waiting for the browser speech service.');
      else micState(conversationActive ? 'Active · listening to you' : 'Waiting for “Jarvis”', conversationActive ? 'Mic on. Speak naturally after each answer. Say “stop Jarvis” to end.' : 'Mic on · wake only. Say “Hey Jarvis”, or select Start conversation.', true);
    }
    function cancelSpeech(resume = true) {
      speechVersion++; win.clearTimeout(speechTimer); speaking = false; activeSpeech = null;
      if (win.speechSynthesis) win.speechSynthesis.cancel();
      el('stop-speaking').hidden = true;
      if (resume) {
        if (!micEnabled) micState('Microphone off', 'Type below, or enable your microphone.');
        else if (!Recognition) micState('Push-to-talk ready', 'Record a command, then select Stop & send. Audio is uploaded to your server.', true);
        else recognitionState();
        startRecognition();
      }
    }
    function stopSpeech() {
      suppressAutoplay = true;

      cancelSpeech();
      status('Voice stopped. Pending replies stay as text; server actions are not undone.');
      confirmationStatus();
    }
    function setVoiceVolume(value) {
      const number = Number(value);
      if (!Number.isFinite(number)) return;
      const previousVolume = voiceVolume;
      voiceVolume = Math.max(0, Math.min(100, Math.round(number)));
      if (voiceVolume > 0) lastAudibleVolume = voiceVolume;
      el('voice-volume').value = String(voiceVolume);
      el('volume-value').textContent = voiceVolume + '%';
      if (speaking && previousVolume !== voiceVolume) {
        const remaining = activeSpeech?.boundary == null ? null : activeSpeech.text.slice(activeSpeech.boundary);
        const preview = activeSpeech?.preview;
        const confirmation = activeSpeech?.confirmation;
        cancelSpeech();
        if (voiceVolume === 0) status('Voice muted. Replies remain as text.');
        else if (remaining) say(remaining, preview, confirmation);
        else status('Volume updated. Playback stopped because this browser did not report word position; the reply remains as text.');
      }
    }
    function voiceAvailable() {
      return !!(win.speechSynthesis && win.SpeechSynthesisUtterance);
    }
    function voiceUnavailable() {
      el('voice-output-status').textContent = 'Voice playback is unavailable. Read the reply below or try Test voice in a supported browser.';
    }
    function setTalkMode(enabled) {
      if (enabled && !voiceAvailable()) { el('speak-replies').checked = false; voiceUnavailable(); return; }
      el('speak-replies').checked = enabled;
      if (!enabled) stopSpeech();
      status(enabled ? 'Talk mode enabled for future replies.' : 'Text mode enabled. Microphone controls remain available; server actions are not undone.');
    }
    function applyLocalControl(control) {
      if (!control) return false;
      if (control.type === 'stop') stopSpeech();
      else if (control.type === 'end') { conversationActive = false; stopSpeech(); }
      else if (control.type === 'volume') setVoiceVolume(control.value ?? voiceVolume + control.delta);
      else if (control.type === 'mute') setVoiceVolume(control.value ? 0 : lastAudibleVolume);
      else if (control.type === 'mode') setTalkMode(control.value);
      else if (control.type === 'language') setLanguage(control.value);
      else return false;
      return true;
    }
    function say(text, preview = false, confirmation = null) {
      text = confirmation ? String(text || '') : spokenText(text);
      if (!voiceAvailable()) { voiceUnavailable(); return false; }
      if (!text || !voiceVolume || (!preview && (suppressAutoplay || !el('speak-replies').checked)) || doc.hidden || destroyed) return false;
      cancelSpeech(false); speaking = true; startRecognition();
      el('voice-output-status').textContent = 'Voice playback requested. If you hear nothing, check device sound or try Test voice.';
      micState('Speaking', micEnabled ? Recognition ? recognizing ? 'Mic on · local controls only. Use headphones to reduce echo.' : 'Mic starting or reconnecting. Tap Stop speaking to interrupt.' : 'Push-to-talk cannot hear a live stop. Tap Stop speaking.' : 'Microphone is off.');
      const version = speechVersion;
      const utterance = new win.SpeechSynthesisUtterance(String(text));
      utterance.lang = voiceLanguage;
      const voice = availableVoices.find(v => v.lang.toLowerCase() === voiceLanguage.toLowerCase()) || availableVoices.find(v => v.lang.split('-')[0].toLowerCase() === voiceLanguage.split('-')[0].toLowerCase());
      if (voice) utterance.voice = voice;
      activeSpeech = { text: String(text), boundary: null, preview, confirmation };
      utterance.onboundary = event => {
        if (version === speechVersion && activeSpeech && Number.isInteger(event.charIndex) && event.charIndex >= 0 && event.charIndex < activeSpeech.text.length) activeSpeech.boundary = event.charIndex;
      };
      utterance.volume = voiceVolume / 100;
      el('stop-speaking').hidden = false;
      const finish = (completed = false) => {
        if (version !== speechVersion) return;
        speechVersion++;
        if (completed && confirmation && confirmation === pendingConfirmation) { confirmation.ready = true; confirmationStatus(); }
        speaking = false; activeSpeech = null; win.clearTimeout(speechTimer); el('stop-speaking').hidden = true;
        if (micEnabled && !Recognition) micState('Push-to-talk ready', 'Record a command, then select Stop & send. Audio is uploaded to your server.', true);
        else if (micEnabled) { recognitionState(); startRecognition(); }
        else micState('Microphone off', 'Type below, or enable your microphone.');
      };
      utterance.onend = () => finish(true);
      utterance.onerror = () => { if (version !== speechVersion) return; finish(); voiceUnavailable(); };
      speechTimer = win.setTimeout(() => { if (version === speechVersion) { finish(); win.speechSynthesis.cancel(); } }, 120000);
      try { win.speechSynthesis.speak(utterance); } catch { finish(); voiceUnavailable(); return false; }
      return true;
    }
    function disableMic(reason = 'Type below, or enable your microphone.') {
      greetOnStart = false;
      suppressAutoplay = true;
      if (pendingConfirmation) pendingConfirmation.ready = false;
      micEnabled = false; conversationActive = false; lastVoiceText = ''; pauseRecognition(); stopRecording(true);
      el('record-toggle').hidden = true;
      micState('Microphone off', reason);
    }
    async function enableMic() {
      if (!authenticated || destroyed) return;
      if (micEnabled) { disableMic(); cancelSpeech(false); updateControls(); return; }
      if (!win.isSecureContext) { disableMic('Microphone access requires HTTPS or localhost. Typed chat is available.'); return; }
      if (!Recognition) {
        if (!canRecord || !sttAvailable) { disableMic('Voice input is unavailable: this browser needs server transcription and recording support. Typed chat is available.'); return; }
        micEnabled = true; el('record-toggle').hidden = false;
        micState('Push-to-talk ready', 'Record a command, then select Stop & send. Audio is uploaded to your server.', true);
        return;
      }
      cancelSpeech(false);
      micEnabled = true;
      if (!recognition) {
        recognition = new Recognition(); recognition.continuous = true; recognition.interimResults = true; recognition.lang = voiceLanguage;
        recognition.onstart = () => { recognitionStarting = false; recognizing = true; recognitionState(); if (greetOnStart) greet(); };
        recognition.onresult = async event => {
          if (!micEnabled || destroyed || doc.hidden) return;
          for (let i = event.resultIndex; i < event.results.length; i++) {
            const transcript = event.results[i][0].transcript;
            el('heard-transcript').textContent = transcript;
            el('heard-transcript').dataset.final = String(event.results[i].isFinal);
            if (!event.results[i].isFinal) continue;
            const now = Date.now();
            const control = localVoiceControl(transcript, { awake: conversationActive });
            if (applyLocalControl(control)) break;
            if (speaking || busy) { status('Heard, but only local controls work while speaking or thinking. Wait for the reply, or say “stop”.'); continue; }
            const confirmationHandled = handleVoiceConfirmation(transcript);
            if (confirmationHandled) { await confirmationHandled; break; }
            if (transcript === lastVoiceText && now - lastVoiceAt < 1800) continue;
            lastVoiceText = transcript; lastVoiceAt = now;
            const decision = wakeDecision(transcript, conversationActive); conversationActive = decision.conversationActive;
            if (decision.type === 'wake') {
              recognitionState();
              greet();
              break;
            }
            if (decision.type === 'command') { await sendText(decision.text); break; }
            if (decision.type === 'ignore') status('Not sent: waiting for a wake name. Say “Hey Jarvis”, or select Start conversation.');
          }
        };
        recognition.onerror = event => {
          if (event.error === 'aborted' || event.error === 'no-speech') return;
          const reasons = { 'not-allowed': 'Microphone permission denied. Allow it in browser settings, then try again.', 'service-not-allowed': 'Speech recognition permission denied by the browser.', 'audio-capture': 'No working microphone was found. Connect a microphone or use typed chat.', network: 'Browser speech service is unavailable. Check your connection or use typed chat.', 'language-not-supported': 'This browser does not support the selected speech language.' };
          disableMic(reasons[event.error] || 'Speech recognition stopped: ' + event.error);
        };
        recognition.onend = () => { recognizing = false; recognitionStarting = false; recognitionState(); if (micEnabled && !doc.hidden && !destroyed) restartTimer = win.setTimeout(startRecognition, 500); };
      }
      micState('Starting microphone…', 'Allow microphone access if your browser asks.');
      startRecognition();
    }
    function greet() {
      greetOnStart = false;
      suppressAutoplay = false;
      say(voiceLanguage.startsWith('hi') ? 'नमस्ते' : voiceLanguage.startsWith('te') ? 'నమస్కారం' : 'Hi');
    }
    async function startConversation() {
      if (!authenticated || destroyed || busy || speaking || pendingConfirmation || conversationActive) return;
      conversationActive = true;
      if (!micEnabled) {
        greetOnStart = !!Recognition;
        await enableMic();
      } else { recognitionState(); greet(); }
      updateControls();
    }
    try { sessionId = win.sessionStorage.getItem('jarvis.session') || ''; } catch { /* Storage may be disabled. */ }
    function node(tag, text, className) {
      const item = doc.createElement(tag);
      if (text !== undefined) item.textContent = String(text);
      if (className) item.className = className;
      return item;
    }
    function status(text, error = false) {
      el('request-status').textContent = text;
      el('request-status').classList.toggle('error', error);
    }
    function updateControls() {
      el('send').disabled = busy || capturePending || !!recorder || !authenticated;
      el('command').disabled = !authenticated;
      el('record-toggle').disabled = busy || capturePending || !authenticated;
      el('mic-toggle').disabled = !authenticated;
      el('start-conversation').disabled = !authenticated || busy || speaking || !!pendingConfirmation || conversationActive;
      el('stop-speaking').hidden = !speaking && !(busy && !suppressAutoplay && el('speak-replies').checked && voiceVolume > 0);
      recognitionState();
    }
    function requireLogin() {
      authenticated = false;
      pendingConfirmation = null;
      disableMic('Reconnect before enabling your microphone.'); cancelSpeech(false);
      el('login-panel').hidden = false;
      el('connection-status').textContent = 'Authentication required';
      el('connection-dot').classList.remove('connected');
      updateControls();
    }
    async function api(url, body) {
      const controller = new win.AbortController();
      controllers.add(controller);
      const timer = win.setTimeout(() => controller.abort(), url === '/api/chat' ? 240000 : 90000);
      const form = body && win.FormData && body instanceof win.FormData;
      try {
        const response = await win.fetch(url, {
          method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
          headers: body === undefined || form ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
          ...(body === undefined ? {} : { body: form ? body : JSON.stringify(body) }), signal: controller.signal
        });
        let data;
        try { data = await response.json(); } catch { throw new Error('Server returned an unreadable response.'); }
        if (response.status === 401) { requireLogin(); throw new Error('Session expired or access token required. Reconnect to continue.'); }
        if (!response.ok) {
          const detail = data && (data.error || data.detail);
          throw new Error(typeof detail === 'string' ? detail : 'Request failed (' + response.status + ').');
        }
        if (!data || typeof data !== 'object') throw new Error('Server returned an invalid response.');
        return data;
      } catch (error) {
        if (error.name === 'AbortError') throw new Error('Request timed out or was interrupted. An action may have completed; check before retrying.');
        throw error;
      } finally { win.clearTimeout(timer); controllers.delete(controller); }
    }
    function addMessage(role, text) {
      el('welcome').hidden = true;
      const article = node('article', undefined, 'message ' + role);
      article.append(node('div', role === 'user' ? 'You' : role === 'error' ? 'Connection notice' : 'Jarvis', 'message-label'), node('div', text, 'message-body'));
      el('messages').append(article);
      el('messages').scrollTop = el('messages').scrollHeight;
      return article;
    }
    function renderResponse(data) {
      pendingConfirmation = null;
      let responseSpeech = typeof data.text === 'string' ? data.text : '';
      let speechConfirmation = null;
      if (typeof data.session_id === 'string' && data.session_id) {
        sessionId = data.session_id;
        try { win.sessionStorage.setItem('jarvis.session', sessionId); } catch { /* Memory-only session remains usable. */ }
      }
      const article = addMessage('assistant', typeof data.text === 'string' ? data.text : 'The server returned no reply text.');
      if (data.confirmation && data.confirmation.id != null) {
        const confirmation = data.confirmation;
        const confirmationSession = sessionId;
        const card = node('section', undefined, 'confirmation');
        const phrase = typeof confirmation.voice_phrase === 'string' ? confirmation.voice_phrase.trim() : '';
        const summary = typeof confirmation.spoken_summary === 'string' ? confirmation.spoken_summary.trim() : '';
        const voiceAllowed = !!(phrase && summary && !usedVoicePhrases.has(normalizedVoice(phrase)));
        const instructions = voiceAllowed ? 'After the summary finishes, say exactly “' + phrase + '” to approve this action, or “cancel action” to deny. You can also use the buttons.' : 'Review the exact tool and arguments. Approval is button-only for this request.';
        card.append(node('h3', 'Permission required'), node('p', instructions), node('pre', String(confirmation.tool || 'Action')), node('pre', JSON.stringify(confirmation.arguments === undefined ? null : confirmation.arguments, null, 2)));
        if (summary) card.append(node('p', summary));
        const actions = node('div', undefined, 'confirmation-actions');
        const approve = node('button', 'Approve action', 'primary');
        const cancel = node('button', 'Cancel');
        approve.type = cancel.type = 'button';
        let settled = false;
        const pending = { tool: confirmation.tool, phrase: voiceAllowed ? phrase : '', ready: false, decide: null, retire: () => {
          settled = true; approve.disabled = cancel.disabled = true;
          if (phrase) usedVoicePhrases.add(normalizedVoice(phrase));
          if (pendingConfirmation === pending) pendingConfirmation = null;
          card.append(node('p', 'Replaced by your new question. This old request cannot be approved here.'));
        } };
        const decide = async (approval, byVoice = false) => {
          if (settled || busy || !authenticated || destroyed || pendingConfirmation !== pending) return;
          if (byVoice && (speaking || approval && !pending.ready)) return;
          if (phrase) usedVoicePhrases.add(normalizedVoice(phrase));
          pendingConfirmation = null; cancelSpeech(false);
          settled = true; busy = true; suppressAutoplay = false; approve.disabled = cancel.disabled = true; updateControls();
          status(approval ? 'Submitting your approval…' : 'Cancelling request…');
          try {
            const result = await api('/api/confirm', { id: confirmation.id, approve: approval, session_id: confirmationSession, language: voiceLanguage, ...(byVoice && approval ? { voice_phrase: phrase } : {}) });
            card.append(node('p', approval ? 'Approval submitted.' : 'Request cancelled.'));
            renderResponse(result); status('Ready for your next command.');
          } catch (error) {
            card.append(node('p', 'Outcome not confirmed. Do not repeat an action until you have checked its state.'));
            addMessage('error', error.message); status(error.message, true);
          } finally { busy = false; updateControls(); startRecognition(); }
        };
        pending.decide = decide; pendingConfirmation = pending;
        responseSpeech = summary ? summary + ' ' + instructions : responseSpeech + ' ' + instructions;
        speechConfirmation = pending;
        approve.addEventListener('click', () => decide(true));
        cancel.addEventListener('click', () => decide(false));
        actions.append(approve, cancel); card.append(actions); article.append(card);
      }
      if (Array.isArray(data.results) && data.results.length) {
        const details = node('details', undefined, 'result-details');
        details.append(node('summary', 'Inspect tool results'), node('pre', JSON.stringify(data.results, null, 2)));
        article.append(details);
        const offered = new Set();
        function offerLink(result, depth = 0) {
          if (!result || typeof result !== 'object' || depth > 4) return;
          const url = safeExternalUrl(result.url);
          if (url && !offered.has(url)) {
            offered.add(url);
            const link = node('a', 'Open link · ' + new URL(url).hostname, 'result-link');
            link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
            article.append(link);
          }
          for (const value of Object.values(result)) if (value && typeof value === 'object') offerLink(value, depth + 1);
        }
        offerLink(data.results);
      }
      if (responseSpeech) say(responseSpeech, false, speechConfirmation);
      return article;
    }
    async function sendText(raw) {
      const text = String(raw || '').trim();
      if (!text || busy || capturePending || recorder || !authenticated || destroyed) return;
      if (pendingConfirmation?.phrase) usedVoicePhrases.add(normalizedVoice(pendingConfirmation.phrase));
      pendingConfirmation = null;
      busy = true; suppressAutoplay = false; cancelSpeech(false); updateControls(); status('Jarvis is working…');
      addMessage('user', text);
      el('command').value = '';
      try {
        renderResponse(await api('/api/chat', { text, language: voiceLanguage, ...(sessionId ? { session_id: sessionId } : {}) }));
        status('Ready for your next command.');
      } catch (error) { addMessage('error', error.message); status(error.message, true); }
      finally { busy = false; updateControls(); startRecognition(); if (pendingConfirmation && !speaking) confirmationStatus(); }
    }
    const seenReminders = new Set();
    let remindersLoading = false;
    async function refreshReminders() {
      if (!authenticated || destroyed || remindersLoading) return;
      remindersLoading = true;
      try {
        const rows = normalizeReminders(await api('/api/reminders'));
        if (destroyed) return;
        el('reminders').replaceChildren();
        for (const row of rows) {
          const item = node('li', row.label);
          const time = node('time', new Date(row.dueAt).toLocaleString());
          time.setAttribute('datetime', new Date(row.dueAt).toISOString());
          item.append(time); el('reminders').append(item);
        }
        el('reminder-status').textContent = rows.length ? 'Times shown in your device timezone.' : 'No reminders scheduled.';
        if (!doc.hidden && !busy && !speaking && !recorder) {
          const due = dueReminders(rows, seenReminders);
          for (const row of due) addMessage('assistant', 'Reminder: ' + row.label);
          if (due.length) say(due.map(row => 'Reminder: ' + row.label).join('. '));
        }
      } catch (error) { el('reminder-status').textContent = 'Unable to refresh: ' + error.message; }
      finally { remindersLoading = false; }
    }
    async function refreshStatus() {
      el('refresh-status').disabled = true;
      try {
        const data = await api('/api/status');
        if (destroyed) return;
        authenticated = true;
        sttAvailable = data.stt === true || typeof data.stt === 'string' && !['', 'none', 'disabled', 'unavailable'].includes(data.stt.toLowerCase()) || !!(data.stt && typeof data.stt === 'object' && (data.stt.configured || data.stt.available));
        el('voice-capability').textContent = Recognition ? 'Browser wake word' : canRecord && sttAvailable ? 'Push-to-talk · upload' : 'Typed commands only';
        el('login-panel').hidden = true;
        el('connection-status').textContent = 'Connected';
        el('connection-dot').classList.add('connected');
        el('model-status').textContent = data.llm_configured ? String(data.model || 'Configured') : 'Not configured';
        const tools = Array.isArray(data.tools) ? data.tools : data.tools && typeof data.tools === 'object' ? Object.keys(data.tools) : [];
        el('capabilities').replaceChildren();
        for (const tool of tools) el('capabilities').append(node('li', typeof tool === 'string' ? tool : String(tool.name || tool.tool || 'Unnamed tool')));
        if (!tools.length) el('capabilities').append(node('li', 'No tools reported.'));
        status(data.llm_configured ? 'Connected. Ask a question or give a command.' : 'Connected. Language model is not configured; available tools may still work.');
        await refreshReminders();
      } catch (error) {
        if (authenticated) { el('connection-status').textContent = 'Connection unavailable'; el('connection-dot').classList.remove('connected'); }
        else if (el('login-panel').hidden) el('connection-status').textContent = 'Connection unavailable';
        status(error.message, true);
      } finally { el('refresh-status').disabled = false; updateControls(); }
    }
    el('composer').addEventListener('submit', async event => { event.preventDefault(); await sendText(el('command').value); });
    el('command').addEventListener('keydown', async event => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); await sendText(el('command').value); }
    });
    el('login-form').addEventListener('submit', async event => {
      event.preventDefault();
      if (el('login-submit').disabled) return;
      const token = el('token').value;
      if (!token) return;
      el('login-submit').disabled = true; el('login-error').textContent = '';
      try { await api('/api/login', { token }); el('token').value = ''; await refreshStatus(); if (authenticated) el('command').focus(); }
      catch (error) { el('login-error').textContent = error.message; }
      finally { el('token').value = ''; el('login-submit').disabled = false; }
    });
    el('refresh-status').addEventListener('click', refreshStatus);
    el('refresh-reminders').addEventListener('click', refreshReminders);
    for (const [id, text] of [['suggest-time', 'What time is it?'], ['suggest-help', 'What can you help me with?']]) {
      el(id).addEventListener('click', () => { el('command').value = text; el('command').focus(); });
    }
    el('mic-toggle').addEventListener('click', enableMic);
    el('start-conversation').addEventListener('click', startConversation);
    el('record-toggle').addEventListener('click', toggleRecording);
    el('stop-speaking').addEventListener('click', stopSpeech);
    el('test-voice').addEventListener('click', () => say('Hello. Your voice preview is ready.', true));
    el('voice-volume').addEventListener('input', () => setVoiceVolume(el('voice-volume').value));
    for (const [tag, label] of languages) { const option = node('option', label); option.value = tag; el('voice-language').append(option); }
    const customOption = node('option', 'Other · BCP 47 tag'); customOption.value = 'custom'; el('voice-language').append(customOption);
    el('voice-language').value = voiceLanguage;
    el('voice-language').addEventListener('change', () => {
      if (el('voice-language').value === 'custom') { el('voice-language-custom').hidden = false; el('voice-language-custom').focus(); }
      else setLanguage(el('voice-language').value);
    });
    el('voice-language-custom').addEventListener('change', () => setLanguage(el('voice-language-custom').value));
    refreshVoices();
    if (win.speechSynthesis) win.speechSynthesis.onvoiceschanged = refreshVoices;
    setVoiceVolume(voiceVolume);
    el('speak-replies').addEventListener('change', () => setTalkMode(el('speak-replies').checked));
    el('voice-capability').textContent = Recognition ? 'Browser wake word' : 'Wake word unavailable';
    if (!voiceAvailable()) { el('speak-replies').checked = false; el('speak-replies').disabled = true; voiceUnavailable(); }
    updateControls();
    const polling = win.setInterval(() => { if (!doc.hidden) refreshReminders(); }, 15000);
    function visibilityChanged() {
      if (doc.hidden) { disableMic('Listening suspended while this tab is hidden. Enable mic when you return.'); cancelSpeech(false); }
      else refreshReminders();
    }
    doc.addEventListener('visibilitychange', visibilityChanged);
    function stop() { destroyed = true; disableMic(); cancelSpeech(false); win.clearInterval(polling); for (const controller of controllers) controller.abort(); }
    win.addEventListener('pagehide', stop);
    return { ready: refreshStatus(), stop, sendText };
  }

  const core = { spokenText, localVoiceControl, wakeDecision, safeExternalUrl, normalizeReminders, dueReminders, mountBrowser };
  if (typeof module !== 'undefined' && module.exports) module.exports = core;
  if (typeof document !== 'undefined' && typeof window !== 'undefined') mountBrowser(document, window);
})();
