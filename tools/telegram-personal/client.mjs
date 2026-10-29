// Outbound-only, single-group adapter. Never log credentials or RPC payloads.
export class SenderError extends Error {
  constructor(code) { super(code); this.code = code; }
}

const knownErrors = new Set([
  'AUTH_KEY_UNREGISTERED', 'AUTH_KEY_DUPLICATED', 'SESSION_REVOKED',
  'SESSION_EXPIRED', 'USER_DEACTIVATED', 'USER_DEACTIVATED_BAN',
  'CHAT_ADMIN_REQUIRED', 'CHAT_WRITE_FORBIDDEN', 'CHANNEL_PRIVATE',
  'MESSAGE_AUTHOR_REQUIRED', 'MESSAGE_EDIT_TIME_EXPIRED', 'MESSAGE_NOT_MODIFIED',
  'TOPIC_CLOSED', 'TOPIC_DELETED', 'USER_BANNED_IN_CHANNEL', 'PEER_ID_INVALID',
  'RPC_CALL_FAIL', 'MESSAGE_TOO_LONG', 'MESSAGE_EMPTY', 'RICH_MESSAGE_INVALID',
]);

export function safeError(error) {
  if (error instanceof SenderError) return error.code;
  const code = error?.text;
  if (knownErrors.has(code)) return code;
  if (typeof code === 'string' && /^(FLOOD|SLOWMODE)_WAIT_\d+$/.test(code)) return code;
  return 'TELEGRAM_REQUEST_FAILED';
}

function positive(value) { return Number.isSafeInteger(value) && value > 0; }

export function parseCredentials(raw) {
  let value;
  try { value = JSON.parse(raw); } catch { throw new SenderError('INVALID_CREDENTIALS'); }
  if (!value || typeof value !== 'object' || !positive(value.api_id)
      || !positive(value.user_id) || !positive(value.topic_id)
      || !Number.isSafeInteger(value.chat_id) || !String(value.chat_id).startsWith('-100')
      || !/^[a-f0-9]{32}$/i.test(value.api_hash ?? '')
      || typeof value.session !== 'string' || value.session.length < 100
      || typeof value.access_hash !== 'string' || !/^-?\d{1,20}$/.test(value.access_hash)) {
    throw new SenderError('INVALID_CREDENTIALS');
  }
  return value;
}

export async function makeClient(credentials, { authorize = true } = {}) {
  const { TelegramClient, MemoryStorage, html } = await import('@mtcute/node');
  const { default: Long } = await import('long');
  const client = new TelegramClient({
    apiId: credentials.api_id, apiHash: credentials.api_hash,
    storage: new MemoryStorage(), disableUpdates: true, logLevel: 0,
    initConnectionOptions: {
      deviceModel: 'GASU schedule CI', appVersion: '1.0', systemVersion: 'Node.js',
    },
  });
  // Bound every high-level RPC, including ones issued internally. No automatic
  // flood retries, login attempts or hidden multi-minute sleeps in CI.
  const call = client.call.bind(client);
  client.call = (request, options = {}) => call(request, {
    ...options, floodSleepThreshold: 0, maxRetryCount: 0, timeout: 25_000,
  });
  if (authorize) {
    try { await client.importSession(credentials.session); }
    catch (error) { await client.destroy().catch(() => {}); throw error; }
  }
  const channelId = Number(String(credentials.chat_id).slice(4));
  const peer = {
    _: 'inputPeerChannel', channelId, accessHash: Long.fromString(credentials.access_hash ?? '0'),
  };
  return { client, peer, parseHtml: html };
}

export function createHandler({ client, peer, credentials, parseHtml = (value) => value }) {
  let ready = false;
  let closed = false;
  const rich = (content) => {
    if (typeof content !== 'string' || !content || Buffer.byteLength(content) > 131_072) {
      throw new SenderError('INVALID_CONTENT');
    }
    return { type: 'html', content, skipEntityDetection: true };
  };
  const target = (params) => {
    if ((params.chat_id !== undefined && params.chat_id !== credentials.chat_id)
        || (params.topic_id !== undefined && params.topic_id !== credentials.topic_id)) {
      throw new SenderError('TARGET_MISMATCH');
    }
  };
  async function preflight() {
    if (ready) return;
    const self = await client.getMe();
    if (self.isBot || self.id !== credentials.user_id) throw new SenderError('ACCOUNT_MISMATCH');
    const member = await client.getChatMember({ chatId: peer, userId: 'me' });
    if (member?.status !== 'creator'
        && !(member?.status === 'admin' && member.raw.adminRights?.pinMessages)) {
      throw new SenderError('PIN_PERMISSION_REQUIRED');
    }
    if (member.raw.adminRights?.anonymous) throw new SenderError('ANONYMOUS_ADMIN_UNSUPPORTED');
    const [topic] = await client.getForumTopicsById(peer, [credentials.topic_id]);
    if (!topic) throw new SenderError('TOPIC_DELETED');
    if (topic.raw?.closed) throw new SenderError('TOPIC_CLOSED');
    ready = true;
  }
  async function ownMessage(id) {
    if (!positive(id)) throw new SenderError('INVALID_MESSAGE_ID');
    const [message] = await client.getMessages(peer, [id]);
    if (!message) throw new SenderError('MESSAGE_NOT_FOUND');
    if (message.chat?.id !== credentials.chat_id
        || !message.replyToMessage?.isForumTopic
        || message.replyToMessage.threadId !== credentials.topic_id) {
      throw new SenderError('TARGET_MISMATCH');
    }
    if (message.sender?.id !== credentials.user_id) throw new SenderError('MESSAGE_AUTHOR_REQUIRED');
    return message;
  }
  function sentId(message) {
    if (!positive(message?.id)) throw new SenderError('INVALID_RESPONSE');
    return message.id;
  }
  return async function handle(method, params = {}) {
    if (method === 'close') {
      if (!closed) await client.destroy();
      closed = true;
      return null;
    }
    if (closed) throw new SenderError('CLIENT_CLOSED');
    if (!['preflight', 'send', 'send_rich', 'edit_rich', 'pin_message'].includes(method)) {
      throw new SenderError('UNKNOWN_METHOD');
    }
    target(params);
    await preflight();
    if (method === 'preflight') return `user:${credentials.user_id}`;
    if (method === 'send') {
      rich(params.text);
      return sentId(await client.sendText(peer, parseHtml(params.text), {
        threadId: credentials.topic_id, disableWebPreview: true,
        silent: params.silent === true, sendAs: 'me',
      }));
    }
    if (method === 'send_rich') {
      return sentId(await client.sendRichMessage(peer, {
        content: rich(params.rich_html), threadId: credentials.topic_id,
        silent: params.silent === true, sendAs: 'me',
      }));
    }
    await ownMessage(params.message_id);
    if (method === 'edit_rich') {
      try {
        return sentId(await client.editMessage({
          chatId: peer, message: params.message_id, richMessage: rich(params.rich_html),
        }));
      } catch (error) {
        if (safeError(error) === 'MESSAGE_NOT_MODIFIED') return params.message_id;
        throw error;
      }
    }
    await client.pinMessage({ chatId: peer, message: params.message_id, notify: false });
    return null;
  };
}
