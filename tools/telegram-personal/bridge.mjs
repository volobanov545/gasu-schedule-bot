import { createInterface } from 'node:readline';
import { createHandler, makeClient, parseCredentials, safeError, SenderError } from './client.mjs';

let handle;
let startupError;
try {
  const credentials = parseCredentials(process.env.TELEGRAM_USER_CREDENTIALS);
  delete process.env.TELEGRAM_USER_CREDENTIALS;
  handle = createHandler({ credentials, ...await makeClient(credentials) });
} catch (error) { startupError = safeError(error); }

try {
  for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
    let id = null;
    let close = false;
    let response;
    try {
      if (Buffer.byteLength(line) > 262_144) throw new SenderError('REQUEST_TOO_LARGE');
      const request = JSON.parse(line);
      if (!Number.isSafeInteger(request.id) || request.id <= 0) throw new SenderError('INVALID_REQUEST');
      id = request.id;
      close = request.method === 'close';
      if (startupError) throw new SenderError(startupError);
      const result = await handle(request.method, request.params ?? {});
      response = { id, ok: true, result };
    } catch (error) { response = { id, ok: false, error: safeError(error) }; }
    process.stdout.write(`${JSON.stringify(response)}\n`);
    if (close) break;
  }
} finally {
  if (handle) await handle('close').catch(() => {});
}
