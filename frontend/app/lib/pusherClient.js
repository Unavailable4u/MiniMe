// frontend/app/lib/pusherClient.js
//
// §2.5 (shared Pusher client refactor). Before this: SessionContext's
// two Pusher effects (the per-chat "session-*" channel, and the
// per-user "user-*" notification channel) each called `new Pusher(...)`
// AND `.disconnect()` in their own cleanup. That meant:
//   - every switchChat()/createNewChat() tore down and rebuilt the
//     ENTIRE websocket connection just to move to a different channel
//     on it, not just re-subscribing;
//   - the moment both effects were mounted (normal steady state), the
//     app carried two fully separate live Pusher connections.
// Neither of those was a channel-scoping problem — it was a connection
// problem. Channels are cheap and plentiful on virtually every Pusher
// plan; connections are the constrained/billed resource. This module
// gives the whole app exactly one Pusher connection, created lazily on
// first use, that every consumer subscribes/unsubscribes channels on
// without ever disconnecting it themselves. This is also what makes
// per-workspace-dock channel subscriptions (§2.4/§2.5, upcoming) cheap:
// N docks means N `pusher.subscribe()` calls on this one connection,
// not N connections.
import Pusher from "pusher-js";

let sharedClient = null;

/**
 * Returns the app-wide Pusher client, creating it on first call.
 * Returns null (rather than throwing) when the required env vars
 * aren't set, so callers can keep their existing "not configured"
 * fallback behavior.
 */
export function getPusherClient() {
  const key = process.env.NEXT_PUBLIC_PUSHER_KEY;
  const cluster = process.env.NEXT_PUBLIC_PUSHER_CLUSTER;
  if (!key || !cluster) return null;
  if (!sharedClient) {
    sharedClient = new Pusher(key, { cluster });
  }
  return sharedClient;
}

/**
 * Subscribes to the shared client's real connection state (connected /
 * connecting / disconnected / unavailable) rather than a caller
 * optimistically flipping a boolean the moment it asks for a
 * subscription. Returns an unsubscribe function; calling it only
 * removes this listener, it never tears down the shared connection.
 */
export function onPusherConnectionChange(callback) {
  const client = getPusherClient();
  if (!client) return () => {};
  const handler = (states) => callback(states.current);
  client.connection.bind("state_change", handler);
  callback(client.connection.state); // report current state immediately
  return () => client.connection.unbind("state_change", handler);
}
