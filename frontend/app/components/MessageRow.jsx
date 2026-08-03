"use client";
import MessageBubble from "./MessageBubble";

// Perf audit #3 (message-list virtualization), step 3: originally
// extracted verbatim out of WorkspaceChatPanel.jsx's messages.map(),
// including a ref-into-messageRefs wiring the cross-panel scroll sync
// used to read DOM node positions from. Step 7 moved that sync onto
// react-window's onRowsRendered instead (offsets by index, not live DOM
// refs), which made messageRefs dead here; step 9 removed the prop and
// the ref callback along with it. Same onClick, same props forwarded to
// MessageBubble.
export default function MessageRow({
  message,
  index,
  onSelect,
  onNavigateSubTab,
  onSendCommand,
}) {
  return (
    <div onClick={() => onSelect(index)}>
      <MessageBubble
        message={message}
        onNavigateSubTab={onNavigateSubTab}
        onSendCommand={onSendCommand}
      />
    </div>
  );
}
