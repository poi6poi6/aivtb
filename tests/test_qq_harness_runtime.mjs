// Actual pinned DSH runtime, real QQ modules imported, localhost fake bridge only.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {basename, dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const directory = await mkdtemp(join(tmpdir(), 'vtuber-qq-runtime-'));
const token = 'offline-qq-runtime-verification';
const requests = [], responses = new Set();
const server = createServer((req, res) => {
  assert.equal(req.headers.authorization, `Bearer ${token}`);
  assert.equal(req.method, 'GET');
  assert.equal(req.url, '/commands');
  requests.push(req.url);
  responses.add(res);
  const timer = setTimeout(() => { res.writeHead(200, {'Content-Type': 'application/json'}); res.end(JSON.stringify({ok: true, command: null})); }, 100);
  res.on('close', () => { clearTimeout(timer); responses.delete(res); });
});
let child;
try {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  child = spawn(process.execPath, [join(root, 'tools', 'qq-harness', 'runner.mjs')], {stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true});
  const outputs = [], errors = [];
  const exited = new Promise(resolve => child.on('exit', (code, signal) => resolve({code, signal})));
  child.stdout.on('data', chunk => outputs.push(chunk.toString()));
  child.stderr.on('data', chunk => errors.push(chunk.toString()));
  child.stdin.write(JSON.stringify({type: 'start', endpoint: `http://127.0.0.1:${server.address().port}`, token,
    home: join(directory, 'home'), workspace: join(directory, 'workspace')}) + '\n');
  const deadline = Date.now() + 40000;
  while (!outputs.join('').includes('\n') && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 50));
  assert.deepEqual(outputs.join('').trim().split('\n').map(JSON.parse), [{type: 'ready'}]);
  assert(requests.length > 0, 'the real DSH plugin must be polling our authenticated host');
  const diagnostics = JSON.parse(await readFile(join(directory, 'transport-status.json'), 'utf8'));
  assert.equal(diagnostics.phase, 'starting');
  assert.equal(diagnostics.counts.inbound, 0, 'diagnostics belong to the isolated runtime workspace');
  // EOF closes the child runtime and cancels its long poll.
  child.stdin.end();
  const result = await Promise.race([exited, new Promise((_, reject) => setTimeout(() => reject(new Error('Runner shutdown timed out')), 12000).unref())]);
  assert.deepEqual(result, {code: 0, signal: null});
  assert.equal(errors.join(''), '');
  assert.deepEqual(outputs.join('').trim().split('\n').map(JSON.parse), [{type: 'ready'}]);
  console.log('Actual DSH QQ runtime startup, authenticated command polling, EOF shutdown, no QQ login or model calls: PASS');
} finally {
  child?.kill();
  for (const response of responses) response.destroy();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  assert.equal(dirname(resolve(directory)), resolve(tmpdir()));
  assert(basename(directory).startsWith('vtuber-qq-runtime-'));
  await rm(directory, {recursive: true, force: true});
}
