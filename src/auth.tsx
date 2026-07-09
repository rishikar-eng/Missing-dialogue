import { createContext, useContext, useState, type ReactNode } from "react";
import { api, setAuthToken } from "./api";

// In-memory session (MVP): the user logs in each time the app starts.
export type SessionUser = {
  email: string;
  name: string;
  userId?: number;
  roles?: string[];
  at: string; // Bearer access token
  rt?: string; // refresh token
};

type AuthCtx = {
  user: SessionUser | null;
  signIn: (u: SessionUser) => void;
  signOut: () => void;
};

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<SessionUser | null>(null);

  const signIn = (u: SessionUser) => {
    setAuthToken(u.at);
    setUser(u);
  };
  const signOut = () => {
    if (user?.rt) api.authLogout(user.rt, user.at).catch(() => {}); // best-effort
    setAuthToken(null);
    setUser(null);
  };

  return <Ctx.Provider value={{ user, signIn, signOut }}>{children}</Ctx.Provider>;
}

export const useAuth = (): AuthCtx => {
  const c = useContext(Ctx);
  if (!c) throw new Error("useAuth must be used within AuthProvider");
  return c;
};

// Build a SessionUser from Rian's login envelope `data`.
// The live /v1 API uses short field codes: nm=name, ui=userId, em=email, an=account,
// tui=teamUserId. (The older /api docs used firstName/lastName/userId — supported as
// a fallback.)
export function sessionFromData(email: string, data: Record<string, unknown>): SessionUser {
  const first = (data.firstName as string) || "";
  const last = (data.lastName as string) || "";
  const name =
    (data.nm as string) ||
    `${first} ${last}`.trim() ||
    (data.userName as string) ||
    (data.em as string) ||
    email;
  return {
    email: (data.em as string) || email,
    name,
    userId: (data.ui as number | undefined) ?? (data.userId as number | undefined),
    roles: (data.roles as string[]) || [],
    at: data.at as string,
    rt: data.rt as string | undefined,
  };
}
