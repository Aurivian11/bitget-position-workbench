/* Login page: password only, no browser persistence. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const svg = p => '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">' + p + "</svg>";
  const EYE = svg('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>');
  const EYE_OFF = svg('<path d="m3 3 18 18"/><path d="M10.6 5.1A10.8 10.8 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-2.6 3.5M6.6 6.6C3.9 8.4 2 12 2 12s3.5 7 10 7a9.8 9.8 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>');
  const reason = new URLSearchParams(location.search).get("reason");
  if (reason === "expired" || reason === "signed-out") {
    const note = $("login-note");
    note.textContent = reason === "signed-out" ? "你已安全退出。" : "登录已过期，请重新登录。";
    note.className = "login-note " + (reason === "signed-out" ? "ok" : "warn");
    note.hidden = false;
  }
  $("pw-toggle").addEventListener("click", () => {
    const input = $("pw"), show = input.type === "password";
    input.type = show ? "text" : "password";
    $("pw-toggle").innerHTML = show ? EYE_OFF : EYE;
    $("pw-toggle").setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
    input.focus();
  });
  $("login-form").addEventListener("submit", async event => {
    event.preventDefault();
    const button = $("login-btn"), error = $("login-error");
    error.hidden = true; button.disabled = true; button.textContent = "正在验证…";
    try {
      const response = await fetch("api/admin/login", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: {"Content-Type": "application/json", "Accept": "application/json"},
        body: JSON.stringify({password: $("pw").value})
      });
      let data = {};
      try { data = await response.json(); } catch (_) { /* empty */ }
      if (!response.ok || !data.authenticated) throw new Error(data.error || "登录失败，请稍后重试。");
      $("pw").value = "";
      button.textContent = "登录成功";
      location.replace(location.pathname + location.hash);
    } catch (err) {
      error.textContent = err instanceof TypeError ? "网络连接异常，请稍后重试。" : err.message;
      error.hidden = false;
      button.disabled = false; button.textContent = "登录";
      $("pw").select();
    }
  });
})();
