import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { Api } from "@/api/client";
import type { Me } from "@/api/types.gen";

interface Auth {
  /** Who is signed in; null when nobody is, undefined while checking. */
  me: Me | null | undefined;
  api: Api;
  /** Asks the server again who is signed in (after signing in). */
  refresh: () => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<Auth | null>(null);

/**
 * The browser holds no token: an HttpOnly session cookie, set by the server when
 * signing in, signs every request . Being signed in means /me answers.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const api = useMemo(
    () =>
      new Api(
        () => null,
        () => setMe(null),
      ),
    [],
  );

  const refresh = useCallback(async () => {
    try {
      setMe(await api.me());
    } catch {
      setMe(null);
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // signed out locally either way
    }
    setMe(null);
  }, [api]);

  const value = useMemo(
    () => ({ me, api, refresh, signOut }),
    [me, api, refresh, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth outside AuthProvider");
  return auth;
}
