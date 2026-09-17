"use client";
import DesktopChatSidebar from "../ChatSidebar";
import NotificationBell from "../NotificationBell";
import AccountMenu from "../auth/AccountMenu";
import MobileDrawer from "./MobileDrawer";

// Mobile counterpart of ../ChatSidebar.jsx. Per this folder's README:
// all the actual chat-list/search/batch/project logic stays in that
// file, completely unforked — this wrapper only changes HOW it's
// positioned (a slide-in overlay instead of a persistent flex column)
// and adds the one thing the mobile header deliberately has no room for
// (see mobile/AppShell.jsx's header comment): account/notification
// access. Those two live here, at the top of this drawer, instead of
// next to the hamburger.
//
// `collapsed={false}` is passed to the desktop component always: mobile
// has no equivalent of desktop's "collapsed rail" state (there's no
// permanently-visible sliver to collapse TO on a phone-width screen) —
// closed here just means the whole drawer is off-screen, handled
// entirely by MobileDrawer's `open` prop instead. The desktop
// component's own internal "Hide chats" chevron still works as-is,
// wired to the same onClose as everything else in this drawer, since
// with collapsed permanently false, its onToggle callback only ever
// means "please close me now."
export default function ChatSidebar({ open, onClose, onOpenChat }) {
  return (
    <MobileDrawer side="left" open={open} onClose={onClose}>
      <div className="flex flex-col h-full w-64">
        <div className="h-11 shrink-0 flex items-center justify-between px-3 border-b border-[var(--neutral-800)]">
          <div className="flex items-center gap-1.5">
            <img src="/minime-logo.svg" alt="" className="w-4 h-4 object-contain" />
            <span className="text-xs font-bold text-[var(--neutral-200)]">
              Mini<span className="text-[#ff5168]">Me</span>
            </span>
          </div>
          <div className="flex items-center gap-3">
            <NotificationBell
              onOpenChat={(chatId) => {
                onClose();
                onOpenChat(chatId);
              }}
            />
            <AccountMenu />
          </div>
        </div>
        <div className="flex-1 min-h-0">
          <DesktopChatSidebar collapsed={false} onToggle={onClose} />
        </div>
      </div>
    </MobileDrawer>
  );
}
