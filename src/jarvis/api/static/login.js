"use strict";
let session = null;
const $ = (id) => document.getElementById(id);
function status(message, error = false) {
  $("status").textContent = message;
  $("status").className = error ? "error" : "";
}
async function api(path, body) {
  const headers = { "Content-Type": "application/json" };
  if (session) headers["X-CSRF-Token"] = session.csrf_token;
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST", headers, credentials: "same-origin",
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (response.status === 204) return null;
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message || "Request failed. Please try again.");
  return value;
}
function decode(value) {
  return Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));
}
function encode(buffer) {
  return btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, "-")
    .replace(/\//g, "_").replace(/=/g, "");
}
function credentialJSON(credential) {
  const response = { clientDataJSON: encode(credential.response.clientDataJSON) };
  for (const field of ["attestationObject", "authenticatorData", "signature", "userHandle"]) {
    if (credential.response[field]) response[field] = encode(credential.response[field]);
  }
  if (credential.response.getTransports) response.transports = credential.response.getTransports();
  return { id: credential.id, rawId: encode(credential.rawId), type: credential.type,
    response, clientExtensionResults: credential.getClientExtensionResults() };
}
function showSession(value) {
  session = value;
  $("signed-in").hidden = !value;
  $("signed-out").hidden = !!value;
  if (!value) return;
  const member = value.memberships.find(m => m.household_id === value.household_id);
  $("welcome").textContent = `Hello, ${member.display_name}.`;
  $("account").textContent = `Signed in with ${value.method === "passkey" ? "a passkey" : "development access"}.`;
  $("permissions").textContent = `Permissions: ${value.scopes.join(", ")}`;
  $("households").replaceChildren(...value.memberships.map(m => {
    const option = document.createElement("option");
    option.value = m.household_id; option.textContent = m.household_name;
    option.selected = m.household_id === value.household_id; return option;
  }));
}
async function action(callback) {
  document.querySelectorAll("button").forEach(button => button.disabled = true);
  status("");
  try { await callback(); }
  catch (error) { status(error.name === "NotAllowedError" ? "Passkey request cancelled or unavailable. Try again." : error.message, true); }
  finally { document.querySelectorAll("button").forEach(button => button.disabled = false); }
}
async function passkey(register) {
  if (!window.PublicKeyCredential || !navigator.credentials) throw new Error("Passkeys require a supported browser and a secure connection.");
  const base = `/auth/passkeys/${register ? "register" : "login"}`;
  const flow = await api(`${base}/options`, register ? { token: $("enrollment").value } : {});
  const options = flow.options;
  options.challenge = decode(options.challenge);
  if (register) options.user.id = decode(options.user.id);
  for (const field of ["allowCredentials", "excludeCredentials"]) {
    if (options[field]) options[field] = options[field].map(item => ({ ...item, id: decode(item.id) }));
  }
  const credential = register ? await navigator.credentials.create({ publicKey: options })
    : await navigator.credentials.get({ publicKey: options });
  if (!credential) throw new Error("No passkey was returned. Please try again.");
  showSession(await api(`${base}/verify`, { ceremony_id: flow.ceremony_id, credential: credentialJSON(credential) }));
  $("enrollment").value = "";
  status(register ? "Passkey created. You're signed in." : "You're signed in.");
}
$("sign-in").onclick = () => action(() => passkey(false));
$("enroll-form").onsubmit = event => { event.preventDefault(); action(() => passkey(true)); };
$("dev-form").onsubmit = event => {
  event.preventDefault(); action(async () => {
    showSession(await api("/auth/dev-login", { token: $("dev-token").value }));
    $("dev-token").value = ""; status("Development session started.");
  });
};
$("logout").onclick = () => action(async () => { await api("/auth/logout", {}); showSession(null); status("Signed out."); });
$("households").onchange = () => action(async () => {
  try { showSession(await api("/auth/household", { household_id: $("households").value })); }
  catch (error) { showSession(session); throw error; }
});
$("echo").onclick = () => action(async () => {
  const result = await api("/v1/capabilities/invoke", { capability: "system.echo",
    arguments: { message: "Jarvis is connected." }, idempotency_key: crypto.randomUUID() });
  status(result.output.message);
});
(async () => {
  try { const config = await api("/auth/config"); $("dev-panel").hidden = !config.dev_login_enabled; }
  catch (error) { status(error.message, true); }
  try { showSession(await api("/auth/session")); } catch { showSession(null); }
})();
