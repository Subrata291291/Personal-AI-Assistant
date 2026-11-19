/** Compact animated typing indicator shown while the assistant has no text yet. */
export default function AssistantTypingIndicator() {
  return (
    <div
      className="flex items-center gap-3"
      data-testid="assistant-typing-indicator"
      role="status"
      aria-label="Assistant is preparing a response"
    >
      <span className="flex items-center gap-1.5" aria-hidden="true">
        <span className="assistant-typing-dot h-1.5 w-1.5 rounded-full bg-violet-400" />
        <span className="assistant-typing-dot h-1.5 w-1.5 rounded-full bg-violet-400" />
        <span className="assistant-typing-dot h-1.5 w-1.5 rounded-full bg-violet-400" />
      </span>
      <span className="truncate text-[11px] leading-4 text-slate-400">
        Thinking
      </span>
    </div>
  )
}
