// Interactive only: the human types codes/password locally, never into chat/CI.
import { createInterface } from 'node:readline/promises';
import { Writable } from 'node:stream';
import { access, mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { createHandler, makeClient, safeError, SenderError } from './client.mjs';

let hidden = false;
const output = new Writable({ write(chunk, _encoding, done) { if (!hidden) process.stdout.write(chunk); done(); } });
output.isTTY = process.stdout.isTTY;
const input = createInterface({ input: process.stdin, output, terminal: true });
async function ask(question, secret = false) {
  const answer = input.question(question);
  hidden = secret;
  try { return (await answer).trim(); }
  finally { hidden = false; if (secret) process.stdout.write('\n'); }
}

let client;
const directory = new URL('../../.telegram-personal/', import.meta.url);
const destination = new URL('credentials.json', directory);
try {
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new SenderError('INTERACTIVE_TERMINAL_REQUIRED');
  try {
    await access(destination);
    throw new SenderError('LOGIN_ALREADY_CONFIGURED');
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  console.log('Подключение личного аккаунта к расписанию. Ничего в группу пока не отправляется.');
  console.log('Открой https://my.telegram.org/apps и создай API-приложение (любое имя).');
  console.log('Коды и пароль вводи только здесь. Они не сохраняются и не печатаются.');
  const api_id = Number(await ask('API ID: '));
  const api_hash = await ask('API hash (ввод скрыт): ', true);
  if (!Number.isSafeInteger(api_id) || api_id <= 0 || !/^[a-f0-9]{32}$/i.test(api_hash)) {
    throw new SenderError('INVALID_API_CREDENTIALS');
  }
  const chat_id = Number(process.env.TARGET_CHAT_ID || -1002251286962);
  const topic_id = Number(process.env.SCHEDULE_TOPIC_ID || 15389);
  const runtime = await makeClient({ api_id, api_hash, chat_id }, { authorize: false });
  client = runtime.client;
  const self = await client.start({
    phone: () => ask('Номер Telegram с кодом страны (ввод скрыт): ', true),
    code: () => ask('Код входа из Telegram (ввод скрыт): ', true),
    password: () => ask('Облачный пароль Telegram (ввод скрыт): ', true),
    codeSentCallback: () => console.log('Telegram отправил код входа. Посмотри его в приложении.'),
    invalidCodeCallback: () => { throw new SenderError('LOGIN_REJECTED'); },
  });
  if (self.isBot) throw new SenderError('ACCOUNT_MISMATCH');
  // A numeric private-group ID is not enough to reconstruct its access hash.
  // The human can supply a public username for a targeted lookup; otherwise
  // inspect bounded in-memory dialog metadata and retain only the target peer.
  const reference = await ask('Публичный username группы, если есть (Enter — найти среди моих чатов): ');
  let chat;
  if (reference) chat = await client.getChat(reference);
  else {
    for await (const dialog of client.iterDialogs({ limit: 200 })) {
      if (dialog.chat.id === chat_id) { chat = dialog.chat; break; }
    }
  }
  if (!chat || chat.id !== chat_id || chat.raw._ !== 'channel' || !chat.raw.accessHash) {
    throw new SenderError('TARGET_CHAT_NOT_FOUND');
  }
  const credentials = {
    api_id, api_hash, user_id: self.id, chat_id, topic_id,
    access_hash: chat.raw.accessHash.toString(), session: '',
  };
  await createHandler({ ...runtime, credentials, peer: chat.inputPeer })('preflight');
  credentials.session = await client.exportSession();
  // Stop this client before making a session available to CI on another IP.
  await client.destroy();
  client = null;
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await writeFile(destination, JSON.stringify(credentials), { flag: 'wx', mode: 0o600 });
  console.log(`Готово. Доступ сохранён локально: ${fileURLToPath(destination)}`);
  console.log('Сам файл и его содержимое никуда не пересылай. Можно вернуться в чат.');
} catch (error) {
  console.error(`Подключение не завершено: ${safeError(error)}. Действующий бот не изменён.`);
  process.exitCode = 1;
} finally {
  if (client) await client.destroy().catch(() => {});
  if (process.stdin.isTTY && process.stdout.isTTY) {
    await ask('Нажми Enter, чтобы закрыть окно.').catch(() => {});
  }
  input.close();
}
