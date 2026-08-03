"use client";
import MessageBubble from "./MessageBubble";

// Perf audit #3 (message-list virtualization), step 3: extracted verbatim
// out of WorkspaceChatPanel.jsx's messages.map(). Behavior is unchanged —
// same ref-into-messageRefs wiring (still used by the cross-panel scroll
// sync, see step2's checklist comment in WorkspaceChatPanel.jsx), same
// onClick, same props forwarded to MessageBubble. Still rendered from a
// plain .map() for now, NOT yet from react-window — that's a later step,
// once this extraction alone is confirmed to change nothing.
export default function MessageRow({
  message,
  index,
  messageRefs,
  onSelect,
  onNavigateSubTab,
  onSendCommand,
}) {
  return (
    <div
      ref={(el) => (messageRefs.current[index] = el)}
      onClick={() => onSelect(index)}
    >
      <MessageBubble
        message={message}
        onNavigateSubTab={onNavigateSubTab}
        onSendCommand={onSendCommand}
      />
    </div>
  );
}
