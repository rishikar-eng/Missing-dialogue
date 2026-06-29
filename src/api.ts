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
  voice_id: string | null;
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

export type AlignmentReport = {
  tol_s: number;
  summary: {
    n_characters_checked: number;
    n_missing: number;
    n_misaligned: number;
    n_extra: number;
    n_unmapped: number;
  };
  errors: AlignError[];
  unmapped_characters: string[];
};

export type AnalyzeResult = {
  characters: Character[];
  source_format: string | null;
  fps: number | null;
  n_segments: number;
  channels: string[];
  alignment: AlignmentReport;
};

export type AnalyzeRequest = {
  script_path: string;
  audio_dir: string;
  fps?: number | null;
  strip_prefix?: string;
  tol_s?: number;
};

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export const api = {
  analyze: (req: AnalyzeRequest) => post<AnalyzeResult>("/api/analyze", req),
  realign: (tolS: number) => post<AlignmentReport>("/api/realign", { tol_s: tolS }),
  progress: () => get<Progress>("/api/progress"),
  audioSliceUrl: (channel: string, startS: number, endS: number) =>
    `${API}/api/audio-slice?channel=${encodeURIComponent(channel)}&start_s=${startS}&end_s=${endS}`,
};
