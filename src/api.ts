// Where the backend lives:
//  - Electron (file://) and Vite dev (port 5173): the local backend on its fixed port.
//  - Served by the backend itself (hosted via ngrok / LAN / cloud): same origin — "".
const isLocalShell =
  typeof window !== "undefined" &&
  (window.location.protocol === "file:" || window.location.port === "5173");
const API = isLocalShell ? "http://127.0.0.1:8765" : "";
// Hosted mode = the backend serves us: no Electron file dialogs, jobs instead of
// long-blocking requests, server-side file browsing.
export const isHosted = !isLocalShell;

// ---- API key (hosted only) ----
// The host shares a link like https://x.ngrok-free.app/?key=SECRET. We capture the key
// once into (a) an in-memory var — always works this tab, even in private mode; (b) a
// `dqc_key` cookie — so <audio>/<a download> requests, which can't set headers,
// authenticate WITHOUT the key ever landing in a URL/log; (c) localStorage — survives a
// reload. fetch() uses the X-API-Key header.
let apiKey: string | null = null;

const readCookieKey = (): string | null => {
  try {
    const m = document.cookie.match(/(?:^|;\s*)dqc_key=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  } catch { return null; }
};

// Persist the key to a cookie + localStorage. Returns true if at least one stuck, so the
// caller knows whether it's safe to strip ?key= from the URL (else a reload would lose it).
const persistKey = (k: string): boolean => {
  let ok = false;
  try { document.cookie = `dqc_key=${encodeURIComponent(k)}; path=/; max-age=31536000; SameSite=Strict`; ok = readCookieKey() === k; } catch { /* */ }
  try { localStorage.setItem("dqc_api_key", k); ok = true; } catch { /* */ }
  return ok;
};

if (typeof window !== "undefined") {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get("key");
    if (fromUrl) {
      apiKey = fromUrl; // in-memory first — this tab is authenticated regardless of storage
      const persisted = persistKey(fromUrl);
      // Strip ?key= from the address bar ONLY if we managed to persist it; otherwise keep
      // it so a reload still authenticates (private mode / storage blocked).
      if (persisted) {
        params.delete("key");
        const rest = params.toString();
        window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : ""));
      }
    } else {
      apiKey = readCookieKey() ?? (() => { try { return localStorage.getItem("dqc_api_key"); } catch { return null; } })();
    }
  } catch {
    /* location/URL parsing failed — header auth still works if a key is set later */
  }
}

export const setApiKey = (k: string | null) => {
  apiKey = k;
  if (k) persistKey(k);
  else {
    try { localStorage.removeItem("dqc_api_key"); } catch { /* */ }
    try { document.cookie = "dqc_key=; path=/; max-age=0; SameSite=Strict"; } catch { /* */ }
  }
};

// Sent on every fetch. ngrok-skip-browser-warning stops ngrok's free interstitial HTML
// from replacing our JSON responses; ignored by every non-ngrok host.
const baseHeaders = (): Record<string, string> => ({
  "ngrok-skip-browser-warning": "true",
  ...(apiKey ? { "X-API-Key": apiKey } : {}),
});
const keyHeaders = baseHeaders; // (kept name for the auth headers used by media fetch())

export type Character = {
  id: string;
  name: string;
  aliases: string[];
  line_count: number;
  total_speech_s: number;
  first_start_s: number;
  channel: string | null;
  mapped_by: "name" | "content" | "manual" | null;
  grouped_in: string | null; // bit-part delivered inside this group stem (walla/crowd); not "No audio"
  level_dbfs: number | null;
  level_min_dbfs: number | null;
  level_max_dbfs: number | null;
  voice_id: string | null;
  voices: VoiceEntry[] | null;
  // Studio character-list (roster) match — a mapping aid; null when no confident match.
  roster_name: string | null;
  roster_voice_name: string | null;
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
  kind: "rescued" | "possible_match" | "name_mismatch" | "verified_absent" | "grouped";
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
  mode?: "compare"; // set for scriptless original-vs-dub runs; absent for script-based
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

// Scriptless QC: compare the ORIGINAL episode audio against the dub (a folder of
// speaker tracks, combined by speech-union, OR one full-episode dub file).
export type CompareRequest = {
  original_audio_path: string;
  audio_dir?: string | null;
  dub_audio_path?: string | null;
  strip_prefix?: string;
  tol_s?: number;
};

// The Rian access token for the logged-in session (in memory only — MVP).
let authToken: string | null = null;
export const setAuthToken = (t: string | null) => { authToken = t; };
const authHeaders = (): Record<string, string> => (authToken ? { Authorization: authToken } : {});

const KEY_HINT =
  "This server needs an access key — open the app through the link that includes ?key=… (ask whoever shared it).";

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(), ...keyHeaders() },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    if (res.status === 401 && String(detail?.detail || "").includes("API key")) throw new Error(KEY_HINT);
    throw new Error(detail?.detail || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export type Progress = { running: boolean; done: number; total: number; stage: string };

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`, { headers: { ...authHeaders(), ...keyHeaders() } });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    if (res.status === 401 && String(detail?.detail || "").includes("API key")) throw new Error(KEY_HINT);
    throw new Error(detail?.detail || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

// ---- Auth (proxied through the local backend to Rian) ----
export type LoginBody = { em: string; pw: string; gotp?: number; otp?: string };
// Rian's envelope: { status, data: { at, rt, userId, firstName, roles, ra, i2wae, ... }, _http }
export type LoginEnvelope = { status: number; data: Record<string, unknown> | null; message?: string; _http?: number };

// A background analysis on the hosted server (submit -> poll until done/error).
export type JobInfo = {
  job_id: string;
  kind: string;
  status: "queued" | "running" | "done" | "error";
  progress: { stage: string; done: number; total: number };
  result: AnalyzeResult | null;
  error: string | null;
};

// One directory level of the server-side shared folder (hosted mode's file picker).
export type BrowseResult = {
  path: string; // relative to the shared root ("" at the root)
  abs: string;  // absolute server path — what analyze endpoints expect
  dirs: string[];
  files: { name: string; size: number }[];
};

export type Healthz = { status: string; auth_required?: boolean; browse_enabled?: boolean };

// One episode x N dub languages -> one .xlsx (a sheet per language).
export type EpisodeRequest = {
  script_path: string;
  languages: Record<string, string>;   // sheet name -> that language's tracks folder
  original_audio_path?: string | null;
  episode?: string;
  strip_prefix?: string;
  tol_s?: number;
};

export type EpisodeResult = {
  episode: string;
  languages: string[];
  failed: Record<string, string>;      // language -> why it was skipped
  report_ready: boolean;
  summary: Record<string, { n_missing: number; n_misaligned: number; n_extra: number }>;
};

export const api = {
  analyze: (req: AnalyzeRequest) => post<AnalyzeResult>("/api/analyze", req),
  // Hosted flavour: returns 202 + a job id immediately (tunnels cut long requests).
  analyzeJob: (req: AnalyzeRequest) => post<JobInfo>("/api/jobs/analyze", req),
  // One episode, every language, one workbook. Always a job (6 languages ~= 10-20 min).
  episodeJob: (req: EpisodeRequest) => post<JobInfo>("/api/jobs/episode", req),
  // The workbook from the last episode run (cookie/desktop auth; see media note below).
  reportXlsxUrl: () => `${API}/api/report.xlsx`,
  jobStatus: (jobId: string) => get<JobInfo>(`/api/jobs/${encodeURIComponent(jobId)}`),
  browse: (path: string) => get<BrowseResult>(`/api/browse?path=${encodeURIComponent(path)}`),
  healthz: () => get<Healthz>("/api/healthz"),
  compare: (req: CompareRequest) => post<AnalyzeResult>("/api/compare", req),
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
  // Media URLs are used by <audio src> / <a download>, which can't send headers — in
  // hosted mode the `dqc_key` cookie authenticates them (so the key never appears in a
  // URL or a log). Desktop mode has no key and points at 127.0.0.1 — bare URLs work there.
  // MISSING lines cut from the ORIGINAL audio, as one WAV. mode "stitch" = clips
  // back-to-back; "timeline" = full episode-length track, silent except at the gaps.
  // padS = context each side of every missing gap (2.5s => the cuts land in room tone
  // instead of chopping a word; overlapping windows merge into one passage).
  missingCompilationUrl: (mode: "stitch" | "timeline" = "stitch", padS = 2.5, tolS = 1.0) =>
    `${API}/api/missing-compilation?mode=${mode}&pad_s=${padS}&tol_s=${tolS}`,
  // All dub tracks summed into ONE full-length WAV — for A/B-ing against the original in an editor.
  dubMixdownUrl: () => `${API}/api/dub-mixdown`,
  // channel=null + source:"original" slices the original-language reference file.
  audioSliceUrl: (channel: string | null, startS: number, endS: number, padS?: number,
                  opts?: { source?: "dub" | "original" }) =>
    `${API}/api/audio-slice?start_s=${startS}&end_s=${endS}` +
    (channel != null ? `&channel=${encodeURIComponent(channel)}` : "") +
    (padS != null ? `&pad_s=${padS}` : "") +
    (opts?.source ? `&source=${opts.source}` : ""),
};
