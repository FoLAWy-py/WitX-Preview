import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncState<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  reload: () => Promise<void>;
}

/** Loads data when `deps` change and exposes a `reload`. Late responses are ignored. */
export function useAsync<T>(
  load: () => Promise<T>,
  deps: unknown[],
): AsyncState<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const [loading, setLoading] = useState(true);
  const generation = useRef(0);
  const run = useCallback(load, deps);

  const reload = useCallback(async () => {
    const mine = ++generation.current;
    try {
      const value = await run();
      if (mine === generation.current) {
        setData(value);
        setError(undefined);
      }
    } catch (err) {
      if (mine === generation.current) setError(err as Error);
    } finally {
      if (mine === generation.current) setLoading(false);
    }
  }, [run]);

  useEffect(() => {
    setLoading(true);
    void reload();
  }, [reload]);

  return { data, error, loading, reload };
}
