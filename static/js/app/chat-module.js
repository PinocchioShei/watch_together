// 聊天业务模块。

import { escapeHtml } from "../shared/format.js";

export function createChatModule({ state, chatBox, chatForm, chatInput }) {
  function appendChat(by, message, sentAt) {
    const row = document.createElement("div");
    row.className = "chat-row";
    const t = new Date(sentAt || Date.now()).toLocaleTimeString();
    row.innerHTML = `<b>${by}</b> [${t}]: ${escapeHtml(message)}`;
    chatBox.appendChild(row);
    chatBox.scrollTop = chatBox.scrollHeight;
  }

  function bindChatEvents() {
    chatForm.onsubmit = (e) => {
      e.preventDefault();
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
      const message = chatInput.value.trim();
      if (!message) return;
      state.ws.send(JSON.stringify({ type: "chat", message }));
      chatInput.value = "";
    };
  }

  return {
    appendChat,
    bindChatEvents,
  };
}
