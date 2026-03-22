// 网络请求客户端：统一附加鉴权头与 401 处理。

export function createApiClient(ctx) {
  const {
    state,
    safeCloseWs,
    stopTabLock,
    setAuthMode,
    setMessage,
    showTab,
  } = ctx;

  async function doFetchWithRetry(path, requestOptions) {
    const maxAttempts = 3;
    let lastError = null;

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 12000);
      try {
        const res = await fetch(path, { ...requestOptions, signal: controller.signal });
        clearTimeout(timeoutId);
        return res;
      } catch (err) {
        clearTimeout(timeoutId);
        lastError = err;
        // 仅对网络错误做重试，HTTP 错误由上层处理。
        if (attempt < maxAttempts) {
          await new Promise((resolve) => setTimeout(resolve, 250 * attempt));
          continue;
        }
      }
    }

    throw lastError || new Error("Network request failed");
  }

  return async function api(path, options = {}) {
    const hasBody = Object.prototype.hasOwnProperty.call(options, "body");
    const isFormData = hasBody && options.body instanceof FormData;
    const authHeader = state.token ? { Authorization: `Bearer ${state.token}` } : {};
    let res;
    try {
      res = await doFetchWithRetry(path, {
        ...options,
        headers: isFormData
          ? { ...authHeader, ...(options.headers || {}) }
          : {
              "Content-Type": "application/json",
              ...authHeader,
              ...(options.headers || {}),
            },
        credentials: "include",
      });
    } catch (err) {
      const msg = String(err?.message || "");
      if (/database is locked|server busy/i.test(msg)) {
        throw new Error("Server busy, retry in a moment.");
      }
      throw new Error("Network unstable, please retry.");
    }

    // HTTP 错误在这里统一处理。
    if (!res.ok) {
      let detail = "Request failed";
      try {
        const data = await res.json();
        detail = data.detail || detail;
      } catch {}

      if (res.status === 401) {
        safeCloseWs();
        stopTabLock();
        state.token = "";
        sessionStorage.removeItem("wt_token");
        state.me = null;
        state.roomId = null;
        state.roomDisplayNo = null;
        state.controllerUserId = null;
        state.forceTakeover = false;
        state.localOverrideUntil = 0;
        setAuthMode(false);
        if (typeof showTab === "function") {
          showTab("login");
        }
        alert("You were signed out because this account was used elsewhere. Please login again.");
        setMessage("Session expired. Please login again.", true);
      }

      throw new Error(detail);
    }

    return res.json();
  };
}
