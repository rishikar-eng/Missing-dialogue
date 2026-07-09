// The Electron shell starts the Python backend on this fixed local port.
const API = "http://127.0.0.1:8765";

export type Character = {
  id: string;
  name: string;
  aliases: string[];
  line_count: number;
  total_speech_s: number;
  first_start_s: number;
  channel: string | null;
  mapped_by: "name" | "content" | "manual" | null;
  level_dbfs: number | null;
  level_min_dbfs: number | null;
  level_max_dbfs: number | null;
  voice_id: string | null;
  voices: VoiceEntry[] | null;
};

// One ElevenLabs voice for a character in one dub language (from the voice bank).
export type VoiceEntry = {
  lang: string; // "hi" | "ta" | "te" | "ml" | "mr" | "bn" | "kn" | "?"
  name: string;
  id: string | null;
  form: "normal" | "granute";
};

// A per-line loudness problem on a dub track.
export type LoudnessFlag = {
  type: "QUIET" | "LOUD";
  character: string;
  channel: string;
  script_index: number;
  script_start_s: number;
  script_end_s: number;
  text: string;
  level_dbfs: number;
  peak_dbfs: number;
  message: string;
};

// A short, playable evidence clip attached to a naming check.
export type IssueSample = {
  label: string;
  channel: string;
  start_s: number;
  end_s: number;
};

// Content-based (voice-timeline) diagnostics for the name→track mapping.
export type NamingIssue = {
  kind: "rescued" | "possible_match" | "name_mismatch" | "verified_absent";
  message: string;
  character?: string;
  character_name?: string;
  channel?: string;
  labelled_character_name?: string;
  voice_character_name?: string;
  recall?: number;
  precision?: number;
  best_recall?: number;
  samples?: IssueSample[];
};

export type AlignError = {
  type: "MISSING" | "MISALIGNED" | "EXTRA";
  subtype: string | null;
  severity: "error" | "warn" | "info";
  character: string | null;
  channel: string | null;
  script_index: number | null;
  script_start_s: number | null;
  script_end_s: number | null;
  audio_start_s: number | null;
  audio_end_s: number | null;
  drift_s: number | null;
  coverage: number | null;
  text: string | null;
  message: string;
};

// A whole track that only lines up with the script after a large time shift.
export type SyncWarning = {
  character: string;
  channel: string;
  offset_s: number;
  message: string;
};

export type AlignmentReport = {
  tol_s: number;
  summary: {
    n_characters_checked: number;
    n_missing: number;
    n_misaligned: number;
    n_extra: number;
    n_unmapped: number;
    n_sync_warnings?: number;
  };
  errors: AlignError[];
  unmapped_characters: string[];
  sync_warnings: SyncWarning[];
};

export type AnalyzeResult = {
  characters: Character[];
  source_format: string | null;
  fps: number | null;
  n_segments: number;
  parse_stats: { candidates: number; parsed: number; dropped: number } | null;
  channels: string[];
  original_audio: boolean; // an original-language reference file was provided
  naming_issues: NamingIssue[];
  loudness_flags: LoudnessFlag[];
  alignment: AlignmentReport;
};

export type AnalyzeRequest = {
  script_path: string;
  audio_dir: string;
  fps?: number | null;
  strip_prefix?: string;
  tol_s?: number;
  original_audio_path?: string | null;
};

// The Rian access token for the logged-in session (in memory only — MVP).
let authToken: string | null = null;
export const setAuthToken = (t: string | null) => { authToken = t; };
const authHeaders = (): Record<string, string> => (authToken ? { Authorization: authToken } : {});

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export type Progress = { running: boolean; done: number; total: number; stage: string };

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

// ---- Auth (proxied through the local backend to Rian) ----
export type LoginBody = { em: string; pw: string; gotp?: number; otp?: string };
// Rian's envelope: { status, data: { at, rt, userId, firstName, roles, ra, i2wae, ... }, _http }
export type LoginEnvelope = { status: number; data: Record<string, unknown> | null; message?: string; _http?: number };

export const api = {
  analyze: (req: AnalyzeRequest) => post<AnalyzeResult>("/api/analyze", req),
  realign: (tolS: number) => post<AlignmentReport>("/api/realign", { tol_s: tolS }),
  remap: (characterId: string, channel: string | null, tolS: number) =>
    post<{
      characters: Character[];
      loudness_flags: LoudnessFlag[];
      naming_issues: NamingIssue[];
      alignment: AlignmentReport;
    }>("/api/remap", { character_id: characterId, channel, tol_s: tolS }),
  progress: () => get<Progress>("/api/progress"),
  authLogin: (body: LoginBody) => post<LoginEnvelope>("/api/auth/login", body),
  authLogout: (rt: string, at?: string) => post<LoginEnvelope>("/api/auth/logout", { rt, at }),
  // channel=null + source:"original" slices the original-language reference file.
  audioSliceUrl: (channel: string | null, startS: number, endS: number, padS?: number,
                  opts?: { source?: "dub" | "original" }) =>
    `${API}/api/audio-slice?start_s=${startS}&end_s=${endS}` +
    (channel != null ? `&channel=${encodeURIComponent(channel)}` : "") +
    (padS != null ? `&pad_s=${padS}` : "") +
    (opts?.source ? `&source=${opts.source}` : ""),
};
