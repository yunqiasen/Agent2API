import assert from 'node:assert/strict';
import test from 'node:test';
import { startLoginPoll, type LoginResult } from '../src/lib/login-poll.ts';

const drain = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
const noop = () => {};

function delayed() {
  let resolve!: (result: LoginResult) => void;
  const promise = new Promise<LoginResult>(r => { resolve = r; });
  return { promise, resolve };
}

test('slow polling never overlaps', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 });
  const pending = delayed();
  let calls = 0;
  const cancel = startLoginPoll({ request: () => { calls++; return pending.promise; },
    onResult: noop, onError: noop, onTimeout: noop });
  t.mock.timers.tick(0); await drain();
  assert.equal(calls, 1);
  t.mock.timers.tick(9000); await drain();
  assert.equal(calls, 1);
  pending.resolve({ ok: false, pending: true }); await drain();
  t.mock.timers.tick(2000); await drain();
  assert.equal(calls, 2);
  cancel();
});

test('closing ignores a late success and aborts in-flight request', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 });
  const pending = delayed();
  let signal: AbortSignal | undefined;
  const results: LoginResult[] = [];
  const cancel = startLoginPoll({ request: s => { signal=s; return pending.promise; },
    onResult: r => results.push(r), onError: noop, onTimeout: noop });
  t.mock.timers.tick(0); await drain();
  cancel();
  assert.equal(signal?.aborted, true);
  pending.resolve({ ok: true }); await drain();
  assert.deepEqual(results, []);
});

for (const result of [{ ok: true }, { ok: false, done: true }, { ok: false, pending: false }]) {
  test(`terminal ${JSON.stringify(result)} stops polling`, async t => {
    t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 });
    let calls = 0; let delivered = 0;
    const cancel = startLoginPoll({ request: async () => { calls++; return result; },
      onResult: () => { delivered++; }, onError: noop, onTimeout: noop });
    t.mock.timers.tick(0); await drain();
    t.mock.timers.tick(10000); await drain();
    assert.equal(calls, 1); assert.equal(delivered, 1);
    cancel();
  });
}

test('wall-clock expiry aborts a stuck request exactly once', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 });
  const pending = delayed(); let signal: AbortSignal | undefined; let expired = 0;
  const cancel = startLoginPoll({ request: s => { signal=s; return pending.promise; },
    onResult: noop, onError: noop, onTimeout: () => { expired++; }, timeoutMs: 5000 });
  t.mock.timers.tick(0); await drain();
  t.mock.timers.tick(5000); await drain();
  assert.equal(expired, 1); assert.equal(signal?.aborted, true);
  t.mock.timers.tick(10000); assert.equal(expired, 1);
  cancel();
});

test('transport error callback can stop a lost admin session', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 });
  let calls=0; let errors=0;
  const cancel = startLoginPoll({ request: async () => { calls++; throw new Error('401'); },
    onResult: noop, onError: () => { errors++; return false; }, onTimeout: noop });
  t.mock.timers.tick(0); await drain(); t.mock.timers.tick(10000); await drain();
  assert.equal(calls, 1); assert.equal(errors, 1); cancel();
});
