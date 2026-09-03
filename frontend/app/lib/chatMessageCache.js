// frontend/app/lib/chatMessageCache.js
//
// Perf audit item #6 (client-side cache, paired with item #5's
// after_seq): holds the most recent window of each chat's messages in
// IndexedDB, keyed by chat_id, so reopening a chat can paint instantly
// from cache and then top up with only what's changed server-side
// (a GET ?after_seq=<cached latestSeq>, wired in WorkspaceDockContext's
// switchChat()) instead of re-fetching the same page every time.
//
// SAFETY ASSUMPTION THIS RELIES ON: messages are append-only. Checked
// against backend/eo/chat_store.py directly -- there is no
// edit_message()/delete_message() path, and the only place a row in
// chat_messages is ever removed is scripts/backfill_chat_messages.py's
// reprocessing delete, which is an offline maintenance script, not
// something a running server does to a chat a client might have
// cached. That means a cached message is never stale, only ever "not
// yet the newest" -- there's no version/updated_at check needed here.
// If message editing or redaction is ever added, this assumption
// breaks and this cache needs a version check (or a TTL) added before
// it can keep being trusted as-is -- see the "if this assumption ever
// changes" note at the bottom of this file.
//
// SELF-HEALING BY DESIGN: this module is read/written only from
// switchChat()'s reopen path, not from every place a message gets
// appended to live state (sent messages, streamed assistant replies,
// Pusher events). That's deliberate, not an oversight: those in-session
// updates don't need to touch the cache, because the very next
// switchChat() call always does an after_seq fetch from whatever
// latestSeq was last cached -- server-side truth always wins, and any
// messages that arrived live but never got written to cache are simply
// picked up as part of that delta, exactly like messages from another
// device/tab would be. Skipping cache writes on every live append
// avoids adding an IndexedDB write to the hot streaming path for a
// consistency property we get for free anyway.
//
// Every operation here fails soft: this cache is a latency
// optimization, never a correctness dependency. If IndexedDB is
// unavailable (SSR, private browsing, a disabled-storage policy) or
// any call throws, callers get null/no-op and fall back to the normal
// network fetch, exactly as if this file didn't exist.

const DB_NAME = "minime_chat_cache";
const DB_VERSION = 1;
const STORE = "chats";

// Two independent caps, both enforced on every write:
//  - MAX_MESSAGES_PER_CHAT: keeps a single very long chat from growing
//    its cache entry without bound. When a chat's message list exceeds
//    this, only the most recent MAX_MESSAGES_PER_CHAT are kept and
//    hasMoreOlder is forced true (see putCachedChat below) -- correct
//    either way, since "true" just means loadOlderMessages's existing
//    before_seq path may re-fetch a few messages it fetched before,
//    never that anything is skipped or duplicated.
//  - MAX_CACHED_CHATS: keeps the total number of cached chats bounded
//    (unbounded IndexedDB growth from a long chat list is the mobile
//    complaint this exists to avoid). Enforced as plain LRU by
//    updatedAt -- oldest-touched chat evicted first.
const MAX_MESSAGES_PER_CHAT = 300;
const MAX_CACHED_CHATS = 40;

let dbPromise = null;

function openDb() {
  if (typeof indexedDB === "undefined") return Promise.resolve(null);
  if (!dbPromise) {
    dbPromise = new Promise((resolve) => {
      try {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains(STORE)) {
            const store = db.createObjectStore(STORE, { keyPath: "chatId" });
            store.createIndex("updatedAt", "updatedAt");
          }
        };
        req.onsuccess = () => resolve(req.result);
        // Never reject -- a failed open just means "no cache available."
        req.onerror = () => resolve(null);
      } catch {
        resolve(null);
      }
    });
  }
  return dbPromise;
}

async function withStore(mode, fn) {
  try {
    const db = await openDb();
    if (!db) return null;
    return await new Promise((resolve) => {
      const tx = db.transaction(STORE, mode);
      const store = tx.objectStore(STORE);
      let result;
      Promise.resolve(fn(store))
        .then((r) => { result = r; })
        .catch(() => { result = null; });
      tx.oncomplete = () => resolve(result);
      tx.onerror = () => resolve(null);
      tx.onabort = () => resolve(null);
    });
  } catch {
    return null;
  }
}

function reqToPromise(req) {
  return new Promise((resolve) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => resolve(null);
  });
}

// Returns { messages, latestSeq, hasMoreOlder } for chatId, or null if
// nothing is cached (first time this chat's been opened on this
// device, cache was evicted, or IndexedDB isn't available at all).
export async function getCachedChat(chatId) {
  if (!chatId) return null;
  const record = await withStore("readonly", (store) => reqToPromise(store.get(chatId)));
  if (!record || !record.messages?.length) return null;
  return {
    messages: record.messages,
    latestSeq: record.latestSeq,
    hasMoreOlder: !!record.hasMoreOlder,
  };
}

// Upserts the cache entry for chatId and enforces both caps. Callers
// pass the FULL merged message list they're currently showing (not
// just a delta) -- this function trims and stores, it doesn't merge.
// Fire-and-forget from the caller's perspective: never awaited on the
// UI-update path, since a slow or failed cache write must never delay
// or block showing messages the user already has.
export async function putCachedChat(chatId, messages, hasMoreOlder) {
  if (!chatId || !messages?.length) return;
  let trimmed = messages;
  let effectiveHasMoreOlder = !!hasMoreOlder;
  if (messages.length > MAX_MESSAGES_PER_CHAT) {
    trimmed = messages.slice(messages.length - MAX_MESSAGES_PER_CHAT);
    effectiveHasMoreOlder = true; // see MAX_MESSAGES_PER_CHAT comment above
  }
  const latestSeq = trimmed[trimmed.length - 1]?.seq;
  if (latestSeq == null) return; // no seq to key a future delta fetch off of -- skip caching rather than cache something we can't safely resume from
  const record = { chatId, messages: trimmed, latestSeq, hasMoreOlder: effectiveHasMoreOlder, updatedAt: Date.now() };

  await withStore("readwrite", async (store) => {
    store.put(record);
    // Enforce MAX_CACHED_CHATS inline, same transaction: cheap (an
    // index-ordered cursor over at most MAX_CACHED_CHATS+1 keys, not a
    // full scan) and keeps eviction from racing a concurrent put.
    const countReq = store.count();
    const count = await reqToPromise(countReq);
    if (count != null && count > MAX_CACHED_CHATS) {
      const index = store.index("updatedAt");
      let toDelete = count - MAX_CACHED_CHATS;
      await new Promise((resolve) => {
        const cursorReq = index.openCursor();
        cursorReq.onsuccess = () => {
          const cursor = cursorReq.result;
          if (!cursor || toDelete <= 0) { resolve(); return; }
          if (cursor.value.chatId !== chatId) { // never evict the entry we just wrote
            cursor.delete();
            toDelete -= 1;
          }
          cursor.continue();
        };
        cursorReq.onerror = () => resolve();
      });
    }
  });
}

// Drops one chat's cache entry. Not currently wired to any call site --
// added so deleteChat()/a future "clear cache" setting has something
// to call instead of leaving a deleted chat's messages orphaned in
// IndexedDB until MAX_CACHED_CHATS eviction eventually clears it.
export async function evictCachedChat(chatId) {
  if (!chatId) return;
  await withStore("readwrite", (store) => { store.delete(chatId); });
}

// If this assumption ever changes (message editing/redaction lands):
// the cheapest fix is a `chats.updated_at` (already exists on the
// `chats` row) round-trip on reopen -- fetch it alongside the delta
// request, compare against a cachedAt timestamp stored here, and
// invalidate (call evictCachedChat, then fall back to a full
// limit=60 fetch) on mismatch, rather than trusting the cached prefix
// blindly the way this file does today.
