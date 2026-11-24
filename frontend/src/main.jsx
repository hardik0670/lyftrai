import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import Markdown from "react-markdown";
import SearchSources from "./SearchSources";
import "./styles.css";

const apiUrl = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");
const historyKey = "lyftr-chat-history";
const historyLimit = 7;

const welcomePrompts = [
  "What would you like to ask?",
  "Explain a concept simply.",
  "What’s the weather in Mumbai?",
  "What are today’s world headlines?",
];
const messageLabels = { user: "You", assistant: "Assistant", error: "Message failed" };

function readHistory() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(historyKey));
    if (Array.isArray(saved)) {
      return saved.filter((chat) => typeof chat?.id === "string" && typeof chat.title === "string")
        .slice(0, historyLimit);
    }
  } catch {
    // Chat is still usable when browser storage is blocked.
  }
  return [];
}

function WelcomeHeading({ prompts }) {
  const [visibleText, setVisibleText] = useState(prompts[0]);

  useEffect(() => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const holdMs = 1250;
    let timeout;

    function startTyping() {
      clearTimeout(timeout);
      let promptIndex = 0;
      let letters = prompts[0].length;
      let erasing = true;
      setVisibleText(prompts[0]);
      if (reducedMotion.matches) return;

      function tick() {
        const prompt = prompts[promptIndex];
        letters += erasing ? -1 : 1;
        setVisibleText(prompt.slice(0, letters));

        if (letters === 0) {
          promptIndex = (promptIndex + 1) % prompts.length;
          erasing = false;
          timeout = setTimeout(tick, 130);
        } else if (letters === prompt.length) {
          erasing = true;
          timeout = setTimeout(tick, holdMs);
        } else {
          // Slow down at the ends of a line and move faster through the middle.
          const edge = Math.cos(Math.PI * letters / prompt.length) ** 2;
          const delay = erasing ? 14 + 18 * edge : 22 + 22 * edge;
          timeout = setTimeout(tick, delay);
        }
      }

      timeout = setTimeout(tick, holdMs);
    }

    startTyping();
    reducedMotion.addEventListener("change", startTyping);
    return () => {
      clearTimeout(timeout);
      reducedMotion.removeEventListener("change", startTyping);
    };
  }, [prompts]);

  return (
    <h2 className="welcome-heading" aria-label={prompts[0]}>
      {prompts.map((prompt) => (
        <span className="welcome-heading-size" aria-hidden="true" key={prompt}>
          {prompt}<span className="typewriter-caret" />
        </span>
      ))}
      <span className="welcome-heading-text" aria-hidden="true">
        {visibleText}<span className="typewriter-caret" />
      </span>
    </h2>
  );
}

function errorMessage(status, detail) {
  if (status === 503 && detail === "Live search is temporarily unavailable.") return detail;
  if (status === 503) return "The chat service is temporarily unavailable. Try again in a moment.";
  if (status === 422) return "Check that your message is between 1 and 4,000 characters.";
  return typeof detail === "string" ? detail : "Something went wrong. Try sending your message again.";
}

function CopyReply({ reply }) {
  const [label, setLabel] = useState("Copy reply");

  async function copy() {
    try {
      await navigator.clipboard.writeText(reply);
      setLabel("Copied");
    } catch {
      setLabel("Copy unavailable");
    }
  }

  return (
    <button className="copy-reply" type="button" onClick={copy} aria-label={label} title={label}>
      <span className="copy-icon" data-copied={label === "Copied"} aria-hidden="true" />
    </button>
  );
}

function ChatHistory({ chats, activeId, busy, error, onOpen, onDelete }) {
  const [expanded, setExpanded] = useState(false);
  const [menuId, setMenuId] = useState(null);

  return (
    <aside className="history" data-expanded={expanded} aria-label="Chat history">
      <h2 className="history-heading">History</h2>
      <button
        className="history-toggle"
        type="button"
        aria-expanded={expanded}
        aria-controls="history-content"
        onClick={() => setExpanded(!expanded)}
      >
        History
      </button>
      <div className="history-content" id="history-content">
        {chats.length === 0 && <p className="history-status">No chats yet.</p>}
        {error && <p className="history-status" role="status">{error}</p>}
        <ul className="history-list">
          {chats.map((chat) => (
            <li className="history-row" key={chat.id}>
              <button
                className="history-entry"
                type="button"
                title={chat.title}
                aria-current={activeId === chat.id ? "true" : undefined}
                disabled={busy}
                onClick={async () => {
                  setMenuId(null);
                  if (await onOpen(chat.id)) setExpanded(false);
                }}
              >
                {chat.title}
              </button>
              <button
                className="history-menu-toggle"
                type="button"
                aria-label={`Options for ${chat.title}`}
                aria-expanded={menuId === chat.id}
                disabled={busy}
                onClick={() => setMenuId(menuId === chat.id ? null : chat.id)}
              >
                <span aria-hidden="true">⋯</span>
              </button>
              {menuId === chat.id && (
                <div className="history-menu">
                  <button
                    className="history-delete"
                    type="button"
                    disabled={busy}
                    onClick={() => onDelete(chat.id)}
                  >
                    Delete chat
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </div>
    </aside>
  );
}

function App() {
  const [sessionId, setSessionId] = useState(() => crypto.randomUUID());
  const [temporary, setTemporary] = useState(false);
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [history, setHistory] = useState(readHistory);
  const [historyAction, setHistoryAction] = useState(null);
  const [historyError, setHistoryError] = useState("");
  const input = useRef(null);
  const thread = useRef(null);
  const loadingHistory = historyAction === "opening";
  const busy = sending || historyAction !== null;

  useEffect(() => {
    thread.current.scrollTop = thread.current.scrollHeight;
  }, [messages, sending]);

  useEffect(() => {
    try {
      window.localStorage.setItem(historyKey, JSON.stringify(history));
    } catch {
      // The current history remains available until this page closes.
    }
  }, [history]);

  function resetChat(isTemporary = false) {
    setSessionId(crypto.randomUUID());
    setTemporary(isTemporary);
    setMessages([]);
    setDraft("");
    setHistoryError("");
    input.current.focus();
  }

  async function openHistory(id) {
    if (id === sessionId) return true;
    if (busy) return false;
    setHistoryError("");
    setHistoryAction("opening");
    try {
      const response = await fetch(`${apiUrl}/sessions/${encodeURIComponent(id)}/messages`);
      if (response.status === 404) {
        setHistory((previous) => previous.filter((chat) => chat.id !== id));
        setHistoryError("This conversation is no longer available.");
        return false;
      }
      if (!response.ok) throw new Error("Could not load this conversation.");
      const body = await response.json();
      if (!Array.isArray(body.messages)) throw new Error("Invalid conversation.");
      setSessionId(id);
      setTemporary(false);
      setMessages(body.messages);
      setDraft("");
      return true;
    } catch {
      setHistoryError("Could not load this conversation. Try again.");
      return false;
    } finally {
      setHistoryAction(null);
    }
  }

  async function deleteHistory(id) {
    if (busy || !window.confirm("Delete this chat and its messages?")) return;
    setHistoryError("");
    setHistoryAction("deleting");
    try {
      const response = await fetch(`${apiUrl}/sessions/${encodeURIComponent(id)}/messages`, {
        method: "DELETE",
      });
      if (!response.ok && response.status !== 404) throw new Error("Could not delete conversation.");
      setHistory((previous) => previous.filter((chat) => chat.id !== id));
      if (id === sessionId) resetChat();
    } catch {
      setHistoryError("Could not delete this conversation. Try again.");
    } finally {
      setHistoryAction(null);
    }
  }

  async function sendMessage(event) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || busy) return;

    const requestBody = { session_id: sessionId, message };
    const previousLookup = messages.findLast((item) => item.role === "assistant")?.search?.query;
    if (previousLookup) requestBody.search_context = previousLookup;
    if (temporary) {
      requestBody.temporary_history = messages
        .filter((item, index) => {
          if (item.role === "assistant") return !item.search;
          const reply = messages[index + 1];
          return item.role === "user" && reply?.role === "assistant" && !reply.search;
        })
        .map(({ role, content }) => ({ role, content }));
    }

    setDraft("");
    setSending(true);
    setMessages((previous) => [...previous, { role: "user", content: message }]);

    try {
      const response = await fetch(`${apiUrl}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorMessage(response.status, body.detail));
      if (!body.reply) throw new Error("The server returned an incomplete reply.");

      setMessages((previous) => [
        ...previous,
        { role: "assistant", content: body.reply, search: body.search },
      ]);
      if (!temporary && !body.search) {
        setHistory((previous) => {
          const existing = previous.find((chat) => chat.id === sessionId);
          const title = message.replace(/\s+/g, " ").slice(0, 80);
          return [existing ?? { id: sessionId, title }, ...previous.filter((chat) => chat.id !== sessionId)]
            .slice(0, historyLimit);
        });
      }
    } catch (error) {
      const content = error instanceof TypeError
        ? "Could not reach the chat server. Try again in a moment."
        : error.message;
      setMessages((previous) => [...previous, { role: "error", content }]);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="layout">
      <header className="header">
        <h1>Lyftr Chat</h1>
        <div className="author">
          <span>Made by <strong>Hardik Khatri</strong></span>
          <a href="mailto:khatrihardik148@gmail.com">khatrihardik148@gmail.com</a>
        </div>
        <div className="header-actions">
          <button className="new-chat" type="button" onClick={() => resetChat()} disabled={busy}>
            <span aria-hidden="true">+</span>
            New chat
          </button>
          <button
            className="temporary-chat"
            type="button"
            aria-pressed={temporary}
            onClick={() => resetChat(!temporary)}
            disabled={busy}
          >
            Temporary chat
          </button>
        </div>
      </header>

      <div className="workspace">
        <ChatHistory
          chats={history}
          activeId={sessionId}
          busy={busy}
          error={historyError}
          onOpen={openHistory}
          onDelete={deleteHistory}
        />
        <main className="chat" aria-label="Chat">
          {temporary && <p className="temporary-note">This chat won’t appear in your history.</p>}
          <div className="thread" ref={thread} role="log" aria-label="Conversation" aria-busy={loadingHistory}>
            {messages.length === 0 && !loadingHistory && (
              <WelcomeHeading prompts={welcomePrompts} />
            )}
            {loadingHistory && <p className="pending" role="status">Opening conversation…</p>}
            {messages.map((message, index) => (
              <article
                className={`message ${message.role}`}
                key={`${sessionId}-${index}`}
                data-testid={message.role === "assistant" ? "assistant-message" : undefined}
              >
                <div className="message-header">
                  <h2>{messageLabels[message.role]}</h2>
                  {message.role === "assistant" && <CopyReply reply={message.content} />}
                </div>
                {message.role === "assistant" ? (
                  <div className="reply-text">
                    <Markdown skipHtml disallowedElements={["img"]}>{message.content}</Markdown>
                  </div>
                ) : (
                  <p className="message-text">{message.content}</p>
                )}
                {message.search && <SearchSources result={message.search} />}
              </article>
            ))}
            {sending && (
              <div className="typing-indicator" role="status" aria-label="Waiting for a reply">
                <span aria-hidden="true" />
                <span aria-hidden="true" />
                <span aria-hidden="true" />
              </div>
            )}
          </div>

          <form className="composer" onSubmit={sendMessage}>
            <div className="composer-row">
              <textarea
                ref={input}
                aria-label="Your message"
                aria-describedby="composer-hint"
                data-testid="chat-input"
                placeholder="Type a message"
                rows="2"
                maxLength={4000}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    event.currentTarget.form.requestSubmit();
                  }
                }}
              />
              <button
                className="send-button"
                data-testid="send-button"
                type="submit"
                aria-label="Send message"
                disabled={busy || !draft.trim()}
              >
                <span className="send-icon" aria-hidden="true" />
              </button>
            </div>
            <p id="composer-hint">Enter to send · Shift + Enter for a new line</p>
          </form>
        </main>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
