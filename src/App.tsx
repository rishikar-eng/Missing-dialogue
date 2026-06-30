import { useEffect, useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, type AlignError, type AnalyzeResult, type Progress } from "./api";

const extractPath = (file: File): string | null =>
  window.electronAPI?.getPathForFile?.(file) ??
  ((file as unknown as { path?: string }).path ?? null);

const fmtTime = (s: number | null | undefined): string => {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return `${m}:${sec.toFixed(1).padStart(4, "0")}`;
};

const TYPE_STYLE: Record<string, { dot: string; label: string }> = {
  MISSING: { dot: "bg-err", label: "Missing" },
  MISALIGNED: { dot: "bg-amber", label: "Misaligned" },
  EXTRA: { dot: "bg-sky-400", label: "Extra" },
};

const pad = (n: number) => String(n).padStart(2, "0");
// seconds -> HH:MM:SS:FF, matching the script's timecode format
const toTimecode = (s: number, fps: number): string => {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  const f = Math.round((s - Math.floor(s)) * fps);
  return `${pad(h)}:${pad(m)}:${pad(sec)}:${pad(f)}`;
};

const hasElectron = () => typeof window !== "undefined" && !!window.electronAPI;

export default function App() {
  const [scriptPath, setScriptPath] = useState("");
  const [audioDir, setAudioDir] = useState("");
  const [stripPrefix, setStripPrefix] = useState("");
  const [tolS, setTolS] = useState(1.0);
  const [filter, setFilter] = useState<"ALL" | "MISSING" | "MISALIGNED" | "EXTRA">("ALL");
  const [result, setResult] = useState<AnalyzeResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const analyze = useMutation({
    mutationFn: () =>
      api.analyze({ script_path: scriptPath, audio_dir: audioDir, strip_prefix: stripPrefix, tol_s: tolS }),
    onSuccess: (r) => {
      setError(null);
      setResult(r);
    },
    onError: (e: Error) => setError(e.message),
  });

  const realign = useMutation({
    mutationFn: () => api.realign(tolS),
    onSuccess: (rep) => {
      setError(null);
      setResult((prev) => (prev ? { ...prev, alignment: rep } : prev));
    },
    onError: (e: Error) => setError(e.message),
  });

  const pickScript = async () => {
    const p = await window.electronAPI?.pickFile([
      { name: "Scripts", extensions: ["docx", "srt", "csv", "tsv"] },
    ]);
    if (p) setScriptPath(p);
  };
  const pickAudio = async () => {
    const p = await window.electronAPI?.pickFolder();
    if (p) setAudioDir(p);
  };

  const NO_PATH_MSG = "Drag-and-drop needs the desktop app — in the browser, paste the full path instead.";
  const onDropScript = (file: File) => {
    const p = extractPath(file);
    if (p) { setError(null); setScriptPath(p); } else setError(NO_PATH_MSG);
  };
  const onDropAudio = (file: File) => {
    const p = extractPath(file);
    if (!p) { setError(NO_PATH_MSG); return; }
    setError(null);
    // If a file was dropped rather than a folder, use its parent directory.
    setAudioDir(file.type !== "" ? p.replace(/[\\/][^\\/]*$/, "") : p);
  };

  // Poll real per-track progress while Analyse runs.
  const [progress, setProgress] = useState<Progress | null>(null);
  useEffect(() => {
    if (!analyze.isPending) {
      setProgress(null);
      return;
    }
    const id = setInterval(() => {
      api.progress().then(setProgress).catch(() => {});
    }, 700);
    return () => clearInterval(id);
  }, [analyze.isPending]);

  const reset = () => {
    setScriptPath("");
    setAudioDir("");
    setStripPrefix("");
    setResult(null);
    setError(null);
    setFilter("ALL");
    setProgress(null);
  };

  const downloadReport = () => {
    if (!result) return;
    const fps = result.fps ?? 25;
    const esc = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const detailOf = (e: AlignError) => {
      if (e.type === "MISSING") return `No speech in track (coverage ${Math.round((e.coverage ?? 0) * 100)}%)`;
      if (e.type === "MISALIGNED")
        return `${(e.subtype ?? "drift").replace("_", " ")} ${e.drift_s != null && e.drift_s > 0 ? "+" : ""}${e.drift_s?.toFixed(2)}s`;
      return "Extra speech (no scripted line)";
    };
    const lines: string[] = [];
    lines.push(["#", "Type", "Character", "Timecode", "Start_s", "End_s", "Script line", "Detail", "Track"].map(esc).join(","));
    let n = 1;
    // whole characters with no track first
    for (const c of result.characters.filter((c) => !c.channel && c.line_count > 0)) {
      lines.push(
        [n++, "NO AUDIO", c.name, "", "", "", "", `No track delivered — ${c.line_count} lines / ${Math.round(c.total_speech_s)}s of dialogue`, ""]
          .map(esc)
          .join(","),
      );
    }
    // then every error, in episode order
    const errs = [...result.alignment.errors].sort(
      (a, b) => (a.script_start_s ?? a.audio_start_s ?? 0) - (b.script_start_s ?? b.audio_start_s ?? 0),
    );
    for (const e of errs) {
      const t = e.script_start_s ?? e.audio_start_s;
      lines.push(
        [
          n++,
          TYPE_STYLE[e.type].label,
          e.character ?? "",
          t != null ? toTimecode(t, fps) : "",
          e.script_start_s ?? e.audio_start_s ?? "",
          e.script_end_s ?? e.audio_end_s ?? "",
          e.text ?? "",
          detailOf(e),
          e.channel ?? "",
        ]
          .map(esc)
          .join(","),
      );
    }
    const csv = "﻿" + lines.join("\r\n"); // BOM so Excel reads UTF-8
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const base = (scriptPath.split(/[\\/]/).pop() || "report").replace(/\.[^.]+$/, "");
    a.href = url;
    a.download = `dialogue-qc_${base}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const chars = result?.characters ?? [];
  const report = result?.alignment ?? null;
  const errors = report?.errors ?? [];
  const filtered = useMemo(
    () => (filter === "ALL" ? errors : errors.filter((e) => e.type === filter)),
    [errors, filter],
  );
  const s = report?.summary;
  const canAnalyze = scriptPath.trim() && audioDir.trim() && !analyze.isPending;

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-ink-800 bg-ink-950/75 backdrop-blur-xl sticky top-0 z-20">
        <div className="max-w-5xl mx-auto px-8 h-[58px] flex items-center justify-between">
          <h1 className="font-display text-[18px] font-semibold tracking-tight">
            Dialogue QC <span className="text-ink-500 font-normal text-sm">· missing-dialogue detection</span>
          </h1>
          <div className="flex items-center gap-3">
            {(scriptPath || audioDir || result) && (
              <button className="btn-ghost" onClick={reset} disabled={analyze.isPending}>
                New analysis
              </button>
            )}
            <span className="font-mono text-[10.5px] text-ink-500">offline · local</span>
          </div>
        </div>
      </header>

      <main className="flex-1 px-6 py-6 max-w-5xl w-full mx-auto space-y-5">
        {error && <div className="card text-sm text-err">{error}</div>}

        {/* 1 — inputs */}
        <section className="card space-y-3">
          <div>
            <div className="section-title">1 · Choose script + audio</div>
            <div className="section-sub">
              Point at the dub script (DOCX / SRT / CSV) and the folder of per-speaker audio tracks. Nothing is
              uploaded — it all stays on this machine.
            </div>
          </div>

          {!hasElectron() && (
            <div className="text-[11px] text-amber/90 bg-amber/5 border border-amber/20 rounded px-2 py-1">
              Browser preview — choosing files needs the desktop app. Run <b>npm run dev</b> and
              use the app window (drag-and-drop / click-to-browse).
            </div>
          )}
          <PathRow
            label="Script file"
            value={scriptPath}
            kind="file"
            onPick={hasElectron() ? pickScript : undefined}
            onDropFile={onDropScript}
            onError={setError}
          />
          <PathRow
            label="Audio folder"
            value={audioDir}
            kind="folder"
            onPick={hasElectron() ? pickAudio : undefined}
            onDropFile={onDropAudio}
            onError={setError}
          />
          <div className="flex items-center gap-3 flex-wrap">
            <label className="flex items-center gap-2 text-xs text-ink-400">
              <span title="Optional: a common filename prefix to strip from track names (e.g. 'GAVV EPI 16 MAL - ').">
                Strip prefix
              </span>
              <input
                className="bg-ink-800 border border-ink-700 rounded px-2 py-1 text-xs font-mono w-64"
                value={stripPrefix}
                onChange={(e) => setStripPrefix(e.target.value)}
                placeholder="(optional)"
              />
            </label>
            <button className="btn-primary ml-auto" disabled={!canAnalyze} onClick={() => analyze.mutate()}>
              {analyze.isPending ? "Analysing…" : "Analyse"}
            </button>
          </div>

          {analyze.isPending && (
            <div className="space-y-1 pt-1">
              <div className="h-1.5 bg-ink-800 rounded-full overflow-hidden">
                <div
                  className="h-full bg-amber transition-all duration-500"
                  style={{
                    width:
                      progress && progress.total
                        ? `${Math.round((progress.done / progress.total) * 100)}%`
                        : "8%",
                  }}
                />
              </div>
              <div className="text-[11px] text-ink-500 font-mono">
                {progress && progress.total
                  ? `VAD track ${progress.done}/${progress.total} — ${progress.stage}`
                  : "Parsing script…"}
              </div>
            </div>
          )}
        </section>

        {/* 2 — characters */}
        {chars.length > 0 && (
          <section className="card">
            <div className="flex items-start justify-between gap-3 flex-wrap mb-2">
              <div>
                <div className="section-title">2 · Characters</div>
                <div className="section-sub">Built from the script automatically.</div>
              </div>
              <span className="font-mono text-[11px] text-ink-400 bg-ink-800 border border-ink-700 px-2 py-1 rounded-full">
                {result?.source_format ?? "?"} · {result?.n_segments} lines · fps {result?.fps ?? "—"}
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[11px] uppercase tracking-wide text-ink-500 border-b border-ink-800">
                    <th className="py-1.5 pr-3">Character</th>
                    <th className="py-1.5 pr-3">Lines</th>
                    <th className="py-1.5 pr-3">Dialogue</th>
                    <th className="py-1.5 pr-3">Aliases</th>
                    <th className="py-1.5 pr-3">Mapped track</th>
                  </tr>
                </thead>
                <tbody>
                  {chars.map((c) => (
                    <tr key={c.id} className="border-b border-ink-900/60">
                      <td className="py-1.5 pr-3 font-medium">{c.name}</td>
                      <td className="py-1.5 pr-3 tabular-nums">{c.line_count}</td>
                      <td className="py-1.5 pr-3 tabular-nums text-ink-400">{c.total_speech_s.toFixed(0)}s</td>
                      <td className="py-1.5 pr-3 text-ink-400 text-xs max-w-[280px]">
                        {c.aliases.length > 1 ? <span title={c.aliases.join(", ")}>{c.aliases.join(", ")}</span> : "—"}
                      </td>
                      <td className="py-1.5 pr-3">
                        {c.channel ? (
                          <span className="text-emerald-400 font-mono text-xs">{c.channel}</span>
                        ) : (
                          <span className="text-err font-mono text-xs">no audio ✗</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* 3 — errors */}
        {report && (
          <section className="card">
            <div className="flex items-start justify-between gap-3 flex-wrap">
              <div>
                <div className="section-title">3 · Detected errors</div>
                <div className="section-sub">Missing / misaligned / extra dialogue, with timestamps.</div>
              </div>
              <div className="flex items-center gap-3 flex-wrap">
                <label className="flex items-center gap-2 text-xs text-ink-400">
                  <span title="A line is only flagged misaligned when its start/end drifts more than this. Higher = fewer false drifts.">
                    Tolerance
                  </span>
                  <input
                    type="range"
                    min={0.2}
                    max={3}
                    step={0.1}
                    value={tolS}
                    onChange={(e) => setTolS(parseFloat(e.target.value))}
                    className="w-32 accent-amber"
                  />
                  <span className="font-mono text-ink-200 w-9 tabular-nums">{tolS.toFixed(1)}s</span>
                  <button className="btn-ghost" disabled={realign.isPending} onClick={() => realign.mutate()}>
                    {realign.isPending ? "…" : "Re-run"}
                  </button>
                </label>
                <button className="btn-primary" onClick={downloadReport} title="Download all issues as a CSV (opens in Excel)">
                  ↓ Download report
                </button>
              </div>
            </div>

            {s && (
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4">
                <Stat label="Missing" value={s.n_missing} tone="err" />
                <Stat label="Misaligned" value={s.n_misaligned} tone="amber" />
                <Stat label="Extra" value={s.n_extra} tone="sky" />
                <Stat label="No audio" value={s.n_unmapped} tone="muted" />
              </div>
            )}
            {report.unmapped_characters.length > 0 && (
              <div className="mt-3 text-xs text-err">
                No audio track for: {report.unmapped_characters.join(", ")}
              </div>
            )}

            {errors.length > 0 ? (
              <>
                <div className="flex items-center gap-2 mt-4 mb-2">
                  {(["ALL", "MISSING", "MISALIGNED", "EXTRA"] as const).map((f) => (
                    <button
                      key={f}
                      onClick={() => setFilter(f)}
                      className={`text-xs px-2.5 py-1 rounded-full border ${
                        filter === f
                          ? "border-amber text-amber bg-amber/10"
                          : "border-ink-700 text-ink-400 hover:text-ink-200"
                      }`}
                    >
                      {f === "ALL" ? `All (${errors.length})` : TYPE_STYLE[f].label}
                    </button>
                  ))}
                </div>
                <div className="max-h-[440px] overflow-y-auto divide-y divide-ink-900/60 border border-ink-800 rounded-lg">
                  {filtered.map((e, i) => (
                    <ErrorRow key={i} e={e} />
                  ))}
                </div>
              </>
            ) : (
              <div className="mt-4 text-sm text-emerald-400">No issues detected — everything lines up. ✓</div>
            )}
          </section>
        )}
      </main>
    </div>
  );
}

function PathRow({
  label,
  value,
  kind,
  onPick,
  onDropFile,
  onError,
}: {
  label: string;
  value: string;
  kind: "file" | "folder";
  onPick?: () => void;
  onDropFile?: (file: File) => void;
  onError?: (msg: string) => void;
}) {
  const [over, setOver] = useState(false);
  const click = () => {
    if (onPick) onPick();
    else onError?.("Choosing files needs the desktop app — run `npm run dev` and use the app window.");
  };
  return (
    <div className="flex items-start gap-2">
      <span className="text-xs text-ink-400 w-24 shrink-0 pt-3">{label}</span>
      <div
        onClick={click}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          const file = e.dataTransfer.files?.[0];
          if (file) onDropFile?.(file);
        }}
        className={`flex-1 cursor-pointer select-none rounded-lg border-2 border-dashed px-3 py-3 text-sm transition ${
          over ? "border-amber bg-amber/10" : "border-ink-700 hover:border-amber/50 hover:bg-ink-800/40"
        }`}
      >
        {value ? (
          <span className="font-mono text-ink-200 break-all">{value}</span>
        ) : (
          <span className="text-ink-500">
            Drag a {kind} here, or <span className="text-amber">click to browse</span>
          </span>
        )}
      </div>
    </div>
  );
}

function ErrorRow({ e }: { e: AlignError }) {
  const [open, setOpen] = useState(false);
  const st = TYPE_STYLE[e.type];
  const t = e.script_start_s ?? e.audio_start_s;
  const winStart = e.script_start_s ?? e.audio_start_s;
  const winEnd = e.script_end_s ?? e.audio_end_s;
  const audioUrl =
    e.channel != null && winStart != null && winEnd != null
      ? api.audioSliceUrl(e.channel, winStart, winEnd)
      : null;

  return (
    <div className="px-3 py-2 hover:bg-ink-800/40">
      <button className="flex items-start gap-3 w-full text-left" onClick={() => setOpen((o) => !o)}>
        <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${st.dot}`} />
        <span className="font-mono text-xs text-ink-300 tabular-nums w-14 shrink-0 pt-0.5">{fmtTime(t)}</span>
        <span className="min-w-0 flex-1">
          <span className="text-sm block">
            <span className="font-medium">{e.character ?? "?"}</span>
            <span className="text-ink-500">
              {" · "}
              {st.label}
              {e.subtype ? ` (${e.subtype.replace("_", " ")})` : ""}
            </span>
            {e.drift_s != null && (
              <span className="text-amber">
                {" "}
                {e.drift_s > 0 ? "+" : ""}
                {e.drift_s.toFixed(2)}s
              </span>
            )}
          </span>
          {!open && e.text && <span className="text-xs text-ink-500 block truncate">“{e.text}”</span>}
        </span>
        <span className="text-ink-500 text-xs pt-0.5 shrink-0">{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <div className="pl-[4.4rem] pr-2 pt-2 pb-1 space-y-2">
          <div className="text-xs text-ink-500">{e.message}</div>
          <div>
            <div className="text-[10px] uppercase tracking-wide text-ink-600 mb-0.5">Script line</div>
            <div className="text-sm text-ink-200 bg-ink-800/60 border border-ink-700 rounded px-2 py-1">
              {e.text ? (
                `“${e.text}”`
              ) : e.type === "EXTRA" ? (
                <span className="text-ink-500">— no scripted line (extra speech in the track) —</span>
              ) : (
                <span className="text-ink-500">— line text unavailable —</span>
              )}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wide text-ink-600 mb-0.5">
              {e.channel ? `Audio · ${e.channel}` : "Audio"}
              {e.type === "MISSING" && <span className="text-err"> (should contain this line — listen for the gap)</span>}
            </div>
            {audioUrl ? (
              <audio key={audioUrl} controls preload="metadata" className="w-full h-8">
                <source src={audioUrl} type="audio/wav" />
              </audio>
            ) : (
              <div className="text-xs text-ink-500">No audio track mapped for this line.</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone: "err" | "amber" | "sky" | "muted" }) {
  const color = { err: "text-err", amber: "text-amber", sky: "text-sky-400", muted: "text-ink-300" }[tone];
  return (
    <div className="bg-ink-800/60 border border-ink-700 rounded-lg px-3 py-2">
      <div className={`text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
      <div className="text-[11px] uppercase tracking-wide text-ink-500">{label}</div>
    </div>
  );
}
