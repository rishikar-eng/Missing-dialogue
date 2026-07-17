import { useState } from "react";
import { api, isHosted, type LoginEnvelope } from "../api";
import { sessionFromData, useAuth } from "../auth";

// Map Rian status codes to a friendly message. (See AUTH_USER_API doc.)
function errorFor(env: LoginEnvelope): string {
  const ra = (env.data?.ra as number | undefined);
  switch (env.status) {
    case 1024: return `Wrong password${ra != null ? ` — ${ra} attempt${ra === 1 ? "" : "s"} left before lockout` : ""}.`;
    case 1025: return "Account locked. Contact your Rian admin.";
    case 1021:
    case 1022: return "Email not verified. Verify your Rian account first.";
    case 1030: return "Invalid code. Check the OTP and try again.";
    case 410: return "That code expired. Request a new one.";
    // Rian /v1 codes (LAUNCH-PLAN.md + observed): 50010/50004/50169 invalid creds
    // (50004 seen for bad credentials with the {em,pw} request shape; 50169 with
    // legacy extra params), 50020 locked, 50030 not activated, 50370 other.
    case 50004:
    case 50010:
    case 50169: return "Incorrect email or password.";
    case 50020: return "Account locked. Contact your Rian admin.";
    case 50030: return "Account not activated. Activate your Rian account first.";
    default:
      if (env._http === 401) return "Incorrect email or password.";
      return (env.data?.msg as string) || env.message || `Login failed (status ${env.status}).`;
  }
}

export default function LoginScreen() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [phase, setPhase] = useState<"creds" | "otp">("creds");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  const succeed = (env: LoginEnvelope): boolean => {
    // Success is signalled by a token in the envelope. The live /v1 API returns
    // status:1 on success; the older /api docs used 200 — accept either, but the
    // token's presence is the real signal.
    const at = env.data?.at as string | undefined;
    if (at) {
      signIn(sessionFromData(email.trim(), env.data!));
      return true;
    }
    return false;
  };

  const submitCreds = async () => {
    setBusy(true); setError(null); setInfo(null);
    try {
      const env = await api.authLogin({ em: email.trim(), pw: password });
      if (succeed(env)) return;
      if (env.status === 3001) {
        // 2FA — trigger the code to be sent, then ask for it.
        setPhase("otp");
        setInfo("A one-time code was sent to your email.");
        api.authLogin({ em: email.trim(), pw: password, gotp: 1 }).catch(() => {});
        return;
      }
      setError(errorFor(env));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const submitOtp = async () => {
    setBusy(true); setError(null);
    try {
      const env = await api.authLogin({ em: email.trim(), pw: password, gotp: 1, otp: otp.trim() });
      if (succeed(env)) return;
      setError(errorFor(env));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    phase === "creds" ? submitCreds() : submitOtp();
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-6">
      <form onSubmit={onSubmit} className="card w-full max-w-sm space-y-4">
        <div>
          <h1 className="font-display text-xl font-semibold tracking-tight">Dialogue QC</h1>
          <p className="text-xs text-ink-400 mt-0.5">Sign in with your Rian account to continue.</p>
        </div>

        {phase === "creds" ? (
          <>
            <label className="block">
              <span className="text-xs text-ink-400">Email</span>
              <input
                type="email" autoFocus required value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="mt-1 w-full bg-ink-800 border border-ink-700 rounded px-3 py-2 text-sm outline-none focus:border-amber/60"
                placeholder="you@rian.io"
              />
            </label>
            <label className="block">
              <span className="text-xs text-ink-400">Password</span>
              <input
                type="password" required value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="mt-1 w-full bg-ink-800 border border-ink-700 rounded px-3 py-2 text-sm outline-none focus:border-amber/60"
                placeholder="••••••••"
              />
            </label>
          </>
        ) : (
          <label className="block">
            <span className="text-xs text-ink-400">One-time code (2FA)</span>
            <input
              type="text" inputMode="numeric" autoFocus required value={otp}
              onChange={(e) => setOtp(e.target.value)}
              className="mt-1 w-full bg-ink-800 border border-ink-700 rounded px-3 py-2 text-sm font-mono tracking-widest outline-none focus:border-amber/60"
              placeholder="123456"
            />
            <button
              type="button"
              className="text-[11px] text-amber hover:text-amber/80 mt-1"
              onClick={() => { setPhase("creds"); setOtp(""); setError(null); setInfo(null); }}
            >
              ← use a different account
            </button>
          </label>
        )}

        {info && <div className="text-xs text-sky-300 bg-sky-400/5 border border-sky-400/20 rounded px-2 py-1.5">{info}</div>}
        {error && <div className="text-xs text-err bg-err/5 border border-err/20 rounded px-2 py-1.5">{error}</div>}

        <button type="submit" className="btn-primary w-full" disabled={busy}>
          {busy ? "Signing in…" : phase === "creds" ? "Sign in" : "Verify code"}
        </button>

        {/* Testing escape hatch — lets you use the app without a Rian login on the local
            desktop build. NEVER shown in the hosted build: that login sits on a public
            URL and a "skip sign-in" button there would let anyone with the link straight in. */}
        {!isHosted && (
          <button
            type="button"
            className="w-full text-[11px] text-ink-400 hover:text-ink-200 pt-1"
            onClick={() => signIn({ email: "", name: "Guest (testing)", at: "" })}
          >
            Skip sign-in (testing) →
          </button>
        )}
      </form>
    </div>
  );
}
