export interface LoginResult {
  ok: boolean;
  pending?: boolean;
  done?: boolean;
}

interface PollOptions<T extends LoginResult> {
  request: (signal: AbortSignal) => Promise<T>;
  onResult: (result: T, elapsedSeconds: number) => void;
  onError: (error: unknown) => boolean | void;
  onTimeout: () => void;
  intervalMs?: number;
  timeoutMs?: number;
}

/** 串行请求 + 真实时间截止；取消后丢弃旧回包，释放网络请求。 */
export function startLoginPoll<T extends LoginResult>(options: PollOptions<T>): () => void {
  const started = Date.now();
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout>;
  let stopped = false;

  function cancel() {
    stopped = true;
    clearTimeout(timer);
    clearTimeout(deadline);
    controller.abort();
  }

  const deadline = setTimeout(() => {
    if (stopped) return;
    cancel();
    options.onTimeout();
  }, options.timeoutMs ?? 300_000);

  async function tick() {
    if (stopped) return;
    try {
      const result = await options.request(controller.signal);
      if (stopped) return;
      options.onResult(result, Math.floor((Date.now() - started) / 1000));
      if (result.ok || result.done || result.pending === false) {
        cancel();
        return;
      }
    } catch (error) {
      if (stopped) return;
      if (options.onError(error) === false) {
        cancel();
        return;
      }
    }
    if (!stopped) timer = setTimeout(tick, options.intervalMs ?? 2000);
  }

  timer = setTimeout(tick, 0);
  return cancel;
}
