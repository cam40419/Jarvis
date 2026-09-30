'use strict';
(() => {
  let saved = null;
  const panel = el('personality-panel'), note = el('personality-status');
  const preset = el('persona-preset'), address = el('persona-address');
  const voice = el('persona-voice'), instructions = el('persona-instructions');
  el('personality-open').onclick = async () => {
    if (!ready) return;
    panel.showModal(); note.textContent = 'Loading your preferences…';
    el('persona-fields').disabled = true;
    el('persona-save').disabled = true;
    try {
      saved = await api('/v1/preferences');
      const p = saved.persona;
      preset.value = p.preset; address.value = p.address_as;
      voice.value = p.voice || ''; instructions.value = p.instructions;
      note.textContent = ''; el('persona-save').disabled = false; el('persona-fields').disabled = false;
    } catch (error) { note.textContent = error.message; }
  };
  preset.onchange = () => {
    if (preset.value === 'jarvis') { address.value = 'sir'; voice.value = 'vesper'; }
    if (preset.value === 'simon') { address.value = ''; voice.value = ''; }
  };
  el('personality-form').onsubmit = async event => {
    event.preventDefault(); if (!saved) return;
    el('persona-save').disabled = true;
    el('persona-fields').disabled = true;
    try {
      const result = await api('/v1/preferences', {
        profile: saved.profile, answer_length: saved.answer_length,
        auto_deep_enabled: saved.auto_deep_enabled, expected_version: saved.version,
        idempotency_key: crypto.randomUUID(), persona: {preset: preset.value,
          address_as: address.value.trim(), voice: voice.value || null,
          instructions: instructions.value.trim()},
      });
      saved = result; preferences = result;
      note.textContent = 'Saved for you in this workspace. Applies to new replies and your next voice call.';
    } catch (error) { note.textContent = error.message; }
    finally { el('persona-save').disabled = false; el('persona-fields').disabled = false; }
  };
})();
