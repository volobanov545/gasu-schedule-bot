import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createHandler, parseCredentials, safeError } from './client.mjs';

const credentials = {
  api_id: 42, api_hash: 'a'.repeat(32), session: 'x'.repeat(400), user_id: 123,
  chat_id: -1002251286962, topic_id: 15389, access_hash: '123456789123456789',
};
function fixture(overrides = {}) {
  const calls = [];
  const peer = { _: 'inputPeerChannel', channelId: 2251286962, accessHash: 'hash' };
  const client = {
    getMe: async () => ({ id: 123, isBot: false }),
    getChatMember: async () => ({ status: 'admin', raw: { adminRights: { pinMessages: true } } }),
    getForumTopicsById: async () => [{ raw: { closed: false } }],
    getMessages: async () => [{
      id: 60, chat: { id: credentials.chat_id }, sender: { id: 123 },
      replyToMessage: { isForumTopic: true, threadId: 15389 },
    }],
    sendRichMessage: async (...args) => { calls.push(['rich', ...args]); return { id: 60 }; },
    sendText: async (...args) => { calls.push(['text', ...args]); return { id: 61 }; },
    editMessage: async (args) => { calls.push(['edit', args]); return { id: args.message }; },
    pinMessage: async (args) => { calls.push(['pin', args]); },
    destroy: async () => { calls.push(['destroy']); },
    ...overrides,
  };
  return { handle: createHandler({ client, peer, credentials }), calls, peer };
}

test('credentials require complete explicit target and session, never expose values', () => {
  assert.deepEqual(parseCredentials(JSON.stringify(credentials)), credentials);
  for (const value of [null, '{}', JSON.stringify({ ...credentials, user_id: 0 }),
    JSON.stringify({ ...credentials, access_hash: 'not-a-hash' }),
    JSON.stringify({ ...credentials, chat_id: 123 })]) {
    assert.throws(() => parseCredentials(value), { message: 'INVALID_CREDENTIALS' });
  }
});
test('preflight confirms account, pin rights and configured topic without sending', async () => {
  const { handle, calls } = fixture();
  assert.equal(await handle('preflight'), 'user:123');
  assert.deepEqual(calls, []);
});
test('wrong account and missing pin rights stop before any write', async () => {
  for (const overrides of [
    { getMe: async () => ({ id: 999 }) },
    { getMe: async () => ({ id: 123, isBot: true }) },
    { getChatMember: async () => ({ status: 'member', raw: {} }) },
    { getForumTopicsById: async () => [null] },
  ]) {
    const { handle, calls } = fixture(overrides);
    await assert.rejects(handle('send_rich', { rich_html: '<p>test</p>' }));
    assert.deepEqual(calls, []);
  }
});
test('rich tables and disclosures go unchanged to personal sender with exact topic', async () => {
  const { handle, calls, peer } = fixture();
  const rich_html = '<details><summary>Вс</summary><table><tr><td>09:00</td></tr></table></details>';
  assert.equal(await handle('send_rich', { rich_html, silent: true }), 60);
  assert.deepEqual(calls, [['rich', peer, {
    content: { type: 'html', content: rich_html, skipEntityDetection: true },
    threadId: 15389, silent: true, sendAs: 'me',
  }]]);
});
test('target override and unknown methods cannot trigger an RPC', async () => {
  const { handle, calls } = fixture({ getMe: () => { throw Error('should not reach'); } });
  await assert.rejects(handle('send', { chat_id: -10099, text: 'test' }), /TARGET_MISMATCH/);
  await assert.rejects(handle('send', { topic_id: 1, text: 'test' }), /TARGET_MISMATCH/);
  await assert.rejects(handle('read_history'), /UNKNOWN_METHOD/);
  assert.deepEqual(calls, []);
});
test('can edit only own card in configured topic; missing does not become another edit', async () => {
  for (const message of [null,
    { chat: { id: credentials.chat_id }, replyToMessage: { isForumTopic: true, threadId: 15389 }, sender: { id: 999 } },
    { chat: { id: credentials.chat_id }, replyToMessage: { isForumTopic: true, threadId: 99 }, sender: { id: 123 } },
  ]) {
    const { handle, calls } = fixture({ getMessages: async () => [message] });
    await assert.rejects(handle('edit_rich', { message_id: 60, rich_html: '<p>x</p>' }));
    assert.deepEqual(calls, []);
  }
});
test('unchanged edit is success; pin uses no notification; close destroys once', async () => {
  const { handle, calls } = fixture({ editMessage: async () => { throw { text: 'MESSAGE_NOT_MODIFIED' }; } });
  assert.equal(await handle('edit_rich', { message_id: 60, rich_html: '<p>x</p>' }), 60);
  assert.equal(await handle('pin_message', { message_id: 60 }), null);
  assert.equal(calls[0][1].notify, false);
  await handle('close'); await handle('close');
  assert.equal(calls.filter(([method]) => method === 'destroy').length, 1);
});
test('rich failure is never silently replaced by classic or repeated', async () => {
  const { handle, calls } = fixture({ sendRichMessage: async () => { throw { text: 'FLOOD_WAIT_19' }; } });
  await assert.rejects(handle('send_rich', { rich_html: '<p>x</p>' }), (error) => safeError(error) === 'FLOOD_WAIT_19');
  assert.deepEqual(calls, []);
  assert.equal(safeError(Error(`secret:${credentials.session}`)), 'TELEGRAM_REQUEST_FAILED');
  assert.equal(safeError({ text: `SECRET_${credentials.api_hash}` }), 'TELEGRAM_REQUEST_FAILED');
});
test('JSONL startup failures preserve request IDs and never print supplied secrets', () => {
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('bridge.mjs', import.meta.url))], {
    input: '{"id":1,"method":"preflight"}\n{"id":2,"method":"close"}\n',
    encoding: 'utf8', timeout: 5000,
    env: { ...process.env, TELEGRAM_USER_CREDENTIALS: '{"secret":"not-for-output"}' },
  });
  assert.equal(result.status, 0);
  assert.equal(result.stderr, '');
  assert.deepEqual(result.stdout.trim().split('\n').map((line) => JSON.parse(line)), [
    { id: 1, ok: false, error: 'INVALID_CREDENTIALS' },
    { id: 2, ok: false, error: 'INVALID_CREDENTIALS' },
  ]);
  assert.ok(!result.stdout.includes('not-for-output'));
});
test('installed mtcute objects expose the exact forum, sender and permission shapes used', async () => {
  const { Chat, ChatMember, ForumTopic, Message, PeersIndex } = await import('@mtcute/node');
  const { default: Long } = await import('long');
  const rawChat = {
    _: 'channel', id: 2251286962, megagroup: true, forum: true, title: 'Schedule test',
    accessHash: Long.fromString(credentials.access_hash),
  };
  const peers = PeersIndex.from({
    users: [{ _: 'user', id: 123, firstName: 'Test' }], chats: [rawChat],
  });
  const chat = new Chat(rawChat);
  assert.equal(chat.id, credentials.chat_id);
  assert.equal(chat.inputPeer._, 'inputPeerChannel');
  assert.equal(chat.inputPeer.accessHash.toString(), credentials.access_hash);
  const member = new ChatMember({ _: 'channelParticipantCreator', userId: 123 }, peers);
  assert.equal(member.status, 'creator');
  const topic = new ForumTopic({ _: 'forumTopic', id: 15389, closed: true }, peers);
  assert.equal(topic.raw.closed, true);
  const message = new Message({
    _: 'message', id: 60, date: 0, message: '',
    peerId: { _: 'peerChannel', channelId: 2251286962 },
    fromId: { _: 'peerUser', userId: 123 },
    replyTo: { _: 'messageReplyHeader', forumTopic: true, replyToMsgId: 15389 },
  }, peers);
  const { handle, calls } = fixture({ getMessages: async () => [message] });
  assert.equal(await handle('edit_rich', { message_id: 60, rich_html: '<p>test</p>' }), 60);
  assert.equal(calls[0][0], 'edit');
});
