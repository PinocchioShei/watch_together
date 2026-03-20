// 网络请求客户端：统一附加鉴权头与 401 处理。

export function createApiClient(ctx) {
  const {
    state,
    safeCloseWs,
    stopTabLock,
    setAuthMode,
    setMessage,
  } = ctx;

  return async function api(path, options = {}) {
    const hasBody = Object.prototype.hasOwnProperty.call(options, "body");
    const isFormData = hasBody && options.body instanceof FormData;
    const authHeader = state.token ? { Authorization: `Bearer ${state.token}` } : {};
    const res = await fetch(path, {
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
        setMessage("Session expired. Please login again.", true);
      }

      throw new Error(detail);
    }
    return res.json();
  };
}
