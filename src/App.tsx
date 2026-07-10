import { useEffect, useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, type AlignError, type AnalyzeResult, type Character, type LoudnessFlag, type NamingIssue, type Progress, type VoiceEntry } from "./api";
import { useAuth } from "./auth";

const extractPath = (file: File): string | null =>
  window.electronAPI?.getPathForFile?.(file) ??
  ((file as unknown as { path?: string }).path ?? null);

const fmtTime = (s: number | null | undefined): string => {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return `${m}:${sec.toFixed(1).padStart(4, "0")}`;
};

// NB: every class here is a literal string so Tailwind's JIT generates it
// (dynamically-built class names like `"bg-err/" + n` are NOT picked up).
const TYPE_STYLE: Record<string, { dot: string; label: string; fill: string; soft: string; text: string }> = {
  MISSING: { dot: "bg-err", label: "Missing", fill: "bg-err/30 border-err", soft: "bg-err/10 border-err/30", text: "text-err" },
  MISALIGNED: { dot: "bg-amber", label: "Misaligned", fill: "bg-amber/30 border-amber", soft: "bg-amber/10 border-amber/30", text: "text-amber" },
  EXTRA: { dot: "bg-sky-400", label: "Extra", fill: "bg-sky-400/30 border-sky-400", soft: "bg-sky-400/10 border-sky-400/30", text: "text-sky-400" },
};

// How much audio to play on each side of the flagged region, for context.
const CONTEXT_PAD_S = 2.5;

// One clear sentence telling the reviewer what to listen for, per issue type.
const listenHint = (e: AlignError): string => {
  if (e.type === "MISSING")
    return "This track should contain the line during the highlighted slot, but it's silent. Listen for the gap where the voice should be.";
  if (e.type === "MISALIGNED") {
    const dir = e.subtype === "late" ? "late" : e.subtype === "early" ? "early" : "off";
    const by = e.drift_s != null ? ` by ${Math.abs(e.drift_s).toFixed(2)}s` : "";
    return `The line is present but ${dir}${by}. Listen for the voice landing outside the highlighted slot.`;
  }
  return "Unscripted speech here — the track talks during the highlighted slot but no script line covers it. Listen to what was said.";
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
  const { user, signOut } = useAuth();
  const [scriptPath, setScriptPath] = useState("");
  const [audioDir, setAudioDir] = useState("");
  const [originalAudioPath, setOriginalAudioPath] = useState("");
  const [stripPrefix, setStripPrefix] = useState("");
  const [tolS, setTolS] = useState(1.0);
  const [filter, setFilter] = useState<"ALL" | "MISSING" | "MISALIGNED" | "EXTRA">("ALL");
  const [result, setResult] = useState<AnalyzeResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dlOpen, setDlOpen] = useState(false);

  const analyze = useMutation({
    mutationFn: () =>
      api.analyze({
        script_path: scriptPath,
        audio_dir: audioDir,
        strip_prefix: stripPrefix,
        tol_s: tolS,
        original_audio_path: originalAudioPath.trim() || null,
      }),
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

  // Manual character↔track reassignment (fixes mappings the automatics got wrong).
  const remap = useMutation({
    mutationFn: ({ characterId, channel }: { characterId: string; channel: string | null }) =>
      api.remap(characterId, channel, tolS),
    onSuccess: (r) => {
      setError(null);
      setResult((prev) =>
        prev
          ? {
              ...prev,
              characters: r.characters,
              loudness_flags: r.loudness_flags,
              naming_issues: r.naming_issues,
              alignment: r.alignment,
            }
          : prev,
      );
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
  const pickOriginalAudio = async () => {
    const p = await window.electronAPI?.pickFile([
      { name: "Audio", extensions: ["wav", "flac", "ogg", "aiff", "aif", "mp3", "m4a"] },
    ]);
    if (p) setOriginalAudioPath(p);
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
  const onDropOriginalAudio = (file: File) => {
    const p = extractPath(file);
    if (p) { setError(null); setOriginalAudioPath(p); } else setError(NO_PATH_MSG);
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
    setOriginalAudioPath("");
    setStripPrefix("");
    setResult(null);
    setError(null);
    setFilter("ALL");
    setProgress(null);
  };

  // ---- report helpers (shared by CSV + TXT) ----
  const reportBase = () => (scriptPath.split(/[\\/]/).pop() || "report").replace(/\.[^.]+$/, "");
  const triggerDownload = (content: string, filename: string, mime: string) => {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };
  const hhmmss = (s: number | null | undefined) => {
    if (s == null) return "";
    const p = (n: number) => String(Math.floor(n)).padStart(2, "0");
    return `${p(s / 3600)}:${p((s % 3600) / 60)}:${p(s % 60)}`;
  };
  const EXTRA_MIN_S = 1.5;

  // Split the data the way both reports present it.
  const reportData = () => {
    const r = result!;
    const nameById = (id?: string | null) => (id ? r.characters.find((c) => c.id === id)?.name ?? id : "");
    const errs = r.alignment.errors;
    const byStart = (a: AlignError, b: AlignError) =>
      (a.script_start_s ?? a.audio_start_s ?? 0) - (b.script_start_s ?? b.audio_start_s ?? 0);
    return {
      r,
      nameById,
      noAudio: r.characters.filter((c) => !c.channel && c.line_count > 0).sort((a, b) => b.total_speech_s - a.total_speech_s),
      missing: errs.filter((e) => e.type === "MISSING").sort(byStart),
      misaligned: errs.filter((e) => e.type === "MISALIGNED").sort(byStart),
      loud: [...r.loudness_flags].sort((a, b) => a.script_start_s - b.script_start_s),
      extra: errs
        .filter((e) => e.type === "EXTRA" && e.audio_end_s != null && e.audio_start_s != null && e.audio_end_s - e.audio_start_s >= EXTRA_MIN_S)
        .sort(byStart),
      issues: r.naming_issues,
      sync: r.alignment.sync_warnings ?? [],
    };
  };

  const downloadCsv = () => {
    if (!result) return;
    const fps = result.fps ?? 25;
    const d = reportData();
    const esc = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const L: string[] = [];
    const HEAD = ["#", "Type", "Character", "Timecode", "Start_s", "End_s", "Script line", "Detail", "Track"];
    L.push(HEAD.map(esc).join(","));
    const row = (cells: unknown[]) => L.push(cells.map(esc).join(","));
    const sec = (title: string) => { L.push(""); row([`== ${title} ==`]); };

    if (d.issues.length) {
      sec(`TRACK CHECKS — verify by listening (${d.issues.length})`);
      for (const it of d.issues) {
        const tag =
          it.kind === "name_mismatch" ? "NAME != VOICE"
          : it.kind === "possible_match" ? "POSSIBLE MATCH"
          : it.kind === "verified_absent" ? "NO AUDIO (verified)"
          : "RECOVERED";
        row(["", tag, it.character_name ?? it.labelled_character_name ?? "", "", "", "", "", it.message, it.channel ?? ""]);
      }
    }
    if (d.sync.length) {
      sec(`WHOLE-TRACK SYNC — tracks that only align after a large shift (${d.sync.length})`);
      for (const w of d.sync)
        row(["", "Out of sync", d.nameById(w.character), "", "", "", "", w.message, w.channel]);
    }
    if (d.loud.length) {
      sec(`LOUDNESS — quiet / hot lines (${d.loud.length})`);
      for (const x of d.loud)
        row(["", x.type === "LOUD" ? "Too hot" : "Too quiet", d.nameById(x.character), toTimecode(x.script_start_s, fps),
          x.script_start_s, x.script_end_s, x.text, x.message, x.channel]);
    }
    sec("ACTION LIST — fix these (undelivered tracks, missing & misaligned lines)");
    let n = 1;
    for (const c of d.noAudio)
      row([n++, "NO AUDIO", c.name, "", "", "", "", `No track delivered — ${c.line_count} lines / ${Math.round(c.total_speech_s)}s of dialogue`, ""]);
    for (const e of [...d.missing, ...d.misaligned].sort((a, b) => (a.script_start_s ?? 0) - (b.script_start_s ?? 0))) {
      const detail = e.type === "MISSING"
        ? `No speech in track (coverage ${Math.round((e.coverage ?? 0) * 100)}%)`
        : `${(e.subtype ?? "drift").replace("_", " ")} ${e.drift_s != null && e.drift_s > 0 ? "+" : ""}${e.drift_s?.toFixed(2)}s`;
      row([n++, TYPE_STYLE[e.type].label, d.nameById(e.character), toTimecode(e.script_start_s ?? 0, fps),
        e.script_start_s ?? "", e.script_end_s ?? "", e.text ?? "", detail, e.channel ?? ""]);
    }
    sec(`REVIEW — extra speech >= ${EXTRA_MIN_S}s (${d.extra.length} shown, ${result.alignment.summary.n_extra - d.extra.length} shorter blips hidden)`);
    for (const e of d.extra)
      row([n++, "Extra", d.nameById(e.character), toTimecode(e.audio_start_s ?? 0, fps), e.audio_start_s ?? "", e.audio_end_s ?? "", "", "Extra speech (no scripted line)", e.channel ?? ""]);

    triggerDownload("﻿" + L.join("\r\n"), `dialogue-qc_${reportBase()}.csv`, "text/csv;charset=utf-8");
  };

  const downloadTxt = () => {
    if (!result) return;
    const d = reportData();
    const r = d.r;
    const S = r.alignment.summary;
    const L: string[] = [];
    const bar = "=".repeat(80);
    const wrap = (text: string, indent = 14, width = 78) => {
      const padStr = " ".repeat(indent);
      const out: string[] = [];
      let cur = padStr;
      for (const w of text.split(/\s+/)) {
        if (cur.length > indent && cur.length + 1 + w.length > width) { out.push(cur); cur = padStr + w; }
        else cur = cur.length > indent ? `${cur} ${w}` : cur + w;
      }
      if (cur.trim()) out.push(cur);
      return out.join("\n");
    };
    const sec = (title: string, note = "") => L.push("", title + (note ? `   — ${note}` : ""), "-".repeat(80));
    const block = (tag: string, tc: string, who: string, track: string, extra: string, text?: string | null) => {
      let head = `[${tag}]`.padEnd(14) + `${tc}  ${who}`;
      if (track) head += `  ->  ${track}`;
      if (extra) head += `   (${extra})`;
      L.push(head);
      if (text) L.push(wrap(`"${text}"`));
    };
    const pad3 = (n: number) => String(n).padStart(3);

    L.push(bar, "DIALOGUE QC REPORT", bar);
    L.push(`Script  : ${reportBase()}`);
    L.push(`Format  : ${r.source_format ?? "?"} · ${r.n_segments} lines · ${r.fps ?? 25} fps`);
    if (r.parse_stats) {
      L.push(`Coverage: ${r.parse_stats.parsed} of ${r.parse_stats.candidates} dialogue rows parsed` +
        (r.parse_stats.dropped > 0
          ? `  — WARNING: ${r.parse_stats.dropped} rows could NOT be parsed and were NOT checked`
          : ""));
    }
    L.push(`Audio   : ${r.channels.length} tracks`);
    // the tolerance the RESULTS were scored at — the live slider may have moved since
    L.push(`Tolerance: ${r.alignment.tol_s.toFixed(1)}s  (the only adjustable setting; higher = more lenient on line timing — fewer 'misaligned', more counted OK; affects Missing/Misaligned only, not Extra/Loudness/mapping)`);
    L.push("-".repeat(80), "SUMMARY");
    L.push(`  Missing lines ............. ${pad3(S.n_missing)}   (scripted, but the track is silent)`);
    L.push(`  Misaligned lines .......... ${pad3(S.n_misaligned)}   (present but early / late)`);
    L.push(`  Whole-track sync .......... ${pad3(d.sync.length)}   (track only aligns after a large shift)`);
    L.push(`  Loudness issues ........... ${pad3(d.loud.length)}   (too quiet / near clipping)`);
    L.push(`  Undelivered characters .... ${pad3(d.noAudio.length)}   (no track for them at all)`);
    L.push(`  Track labelling checks .... ${pad3(d.issues.length)}   (verify by listening)`);
    L.push(`  Extra speech .............. ${pad3(S.n_extra)}   (unscripted reactions/noise — see the CSV for the list)`);
    L.push(bar);

    if (d.sync.length) {
      sec("WHOLE-TRACK SYNC WARNINGS", "the whole track is shifted vs the script");
      for (const w of d.sync) {
        L.push(`[OUT OF SYNC] ${d.nameById(w.character)}  ->  ${w.channel}   (${w.offset_s > 0 ? "+" : ""}${w.offset_s.toFixed(2)}s)`);
        L.push(wrap(w.message));
      }
    }

    if (d.issues.length) {
      sec("TRACK LABELLING CHECKS", "confirm by listening in the app");
      for (const it of d.issues) {
        if (it.kind === "rescued") {
          block("RECOVERED", "", it.character_name ?? "", it.channel ?? "", `covers ${Math.round((it.recall ?? 0) * 100)}% of their lines`);
          L.push(wrap(`No track was named for ${it.character_name}, but this track's voice matches. Verify it's them.`));
        } else if (it.kind === "possible_match") {
          block("POSSIBLE MATCH", "", it.character_name ?? "", it.channel ?? "", `covers ${Math.round((it.recall ?? 0) * 100)}%, confidence ${Math.round((it.precision ?? 0) * 100)}%`);
          L.push(wrap(`Not confident enough to auto-assign — the character is still counted as no-audio. Listen to confirm whether this track contains them.`));
        } else if (it.kind === "name_mismatch") {
          L.push(`[NAME != VOICE] '${it.channel}' (labelled ${it.labelled_character_name})`);
          L.push(wrap(`Voice matches ${it.voice_character_name}, not ${it.labelled_character_name}. Check the labelling.`));
        } else {
          block("NO AUDIO*", "", it.character_name ?? "", "", "verified absent");
          L.push(wrap("No track's voice covers their lines anywhere — audio genuinely not delivered."));
        }
      }
    }
    if (d.noAudio.length) {
      sec("UNDELIVERED CHARACTERS", "no audio track supplied");
      for (const c of d.noAudio) L.push(`[NO AUDIO]    ${c.name}`.padEnd(40) + `(${c.line_count} lines, ${Math.round(c.total_speech_s)}s of dialogue)`);
    }
    if (d.missing.length) {
      sec(`MISSING LINES (${d.missing.length})`, "the character's track is silent where the script has a line");
      for (const e of d.missing) block("MISSING", hhmmss(e.script_start_s), d.nameById(e.character), e.channel ?? "", "", e.text);
    }
    if (d.misaligned.length) {
      sec(`MISALIGNED LINES (${d.misaligned.length})`, "line is present but off-timing");
      for (const e of d.misaligned) {
        const dr = e.drift_s;
        const dt = dr != null ? `${dr > 0 ? "late" : "early"} ${Math.abs(dr).toFixed(2)}s` : e.subtype ?? "";
        block("MISALIGNED", hhmmss(e.script_start_s), d.nameById(e.character), e.channel ?? "", dt, e.text);
      }
    }
    if (d.loud.length) {
      sec(`LOUDNESS ISSUES (${d.loud.length})`, "delivered lines that are too quiet or too hot");
      for (const x of d.loud) {
        const tag = x.type === "LOUD" ? "TOO HOT" : "TOO QUIET";
        const det = `${x.level_dbfs.toFixed(0)} dBFS${x.type === "LOUD" ? `, peak ${x.peak_dbfs.toFixed(0)}` : ""}`;
        block(tag, hhmmss(x.script_start_s), d.nameById(x.character), x.channel, det, x.text);
      }
    }
    L.push("", bar, "End of report.", bar);
    triggerDownload(L.join("\r\n"), `dialogue-qc_${reportBase()}.txt`, "text/plain;charset=utf-8");
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
            {user && (
              <span className="text-[11px] text-ink-400" title={user.email}>
                {user.name}
              </span>
            )}
            <button className="btn-ghost" onClick={signOut} title="Sign out of your Rian session">
              Sign out
            </button>
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
            onChange={setScriptPath}
            onDropFile={onDropScript}
          />
          <PathRow
            label="Audio folder"
            value={audioDir}
            kind="folder"
            onPick={hasElectron() ? pickAudio : undefined}
            onChange={setAudioDir}
            onDropFile={onDropAudio}
          />
          <PathRow
            label="Original audio (optional)"
            value={originalAudioPath}
            kind="file"
            onPick={hasElectron() ? pickOriginalAudio : undefined}
            onChange={setOriginalAudioPath}
            onDropFile={onDropOriginalAudio}
          />
          <div className="text-[11px] text-ink-500 -mt-1">
            Original audio: one file with the source-language episode (e.g. the original mix).
            Flagged issues will show it side-by-side with the dub so you can hear what the
            original had at that moment.
          </div>
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
                {progress?.stage || "Parsing script…"}
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
            {result?.parse_stats && result.parse_stats.dropped > 0 && (
              <div className="mb-2 text-xs text-err bg-err/5 border border-err/30 rounded px-2 py-1.5">
                ⚠ {result.parse_stats.dropped} of {result.parse_stats.candidates} dialogue rows in the
                script could not be parsed — <b>those lines were NOT checked</b>. The results below
                cover only the {result.parse_stats.parsed} parsed lines. (Odd timecodes or table
                formatting in the script are the usual cause.)
              </div>
            )}
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[11px] uppercase tracking-wide text-ink-500 border-b border-ink-800">
                    <th className="py-1.5 pr-3">Character</th>
                    <th className="py-1.5 pr-3">Lines</th>
                    <th className="py-1.5 pr-3">Dialogue</th>
                    <th className="py-1.5 pr-3">Aliases</th>
                    <th className="py-1.5 pr-3">Mapped track</th>
                    <th className="py-1.5 pr-3" title="Loudness range across the character's lines: quietest…loudest (dBFS; 0 = max, lower = quieter). A wide range can mean inconsistent delivery across chunks.">Level (min…max)</th>
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
                        <span className="inline-flex items-center gap-1.5">
                          {/* Editable mapping: fix a wrong/missing track assignment by hand.
                              Reassigning re-scores errors + loudness instantly (cached VAD). */}
                          <select
                            value={c.channel ?? ""}
                            disabled={remap.isPending}
                            onChange={(e) =>
                              remap.mutate({ characterId: c.id, channel: e.target.value || null })
                            }
                            title="Assign a different audio track to this character (fixes naming mismatches). Errors and loudness re-score instantly."
                            className={`bg-ink-800 border border-ink-700 rounded px-1.5 py-0.5 text-xs font-mono max-w-[220px] ${
                              c.channel ? "text-emerald-400" : "text-err"
                            }`}
                          >
                            <option value="">no audio ✗</option>
                            {(result?.channels ?? []).map((ch) => (
                              <option key={ch} value={ch}>{ch}</option>
                            ))}
                          </select>
                          {c.mapped_by === "content" && (
                            <span
                              className="text-[10px] text-sky-300 bg-sky-400/10 border border-sky-400/30 rounded px-1"
                              title="Matched by voice timeline, not by filename — the track name didn't match. Please verify."
                            >
                              via voice ⓘ
                            </span>
                          )}
                          {c.mapped_by === "manual" && (
                            <span
                              className="text-[10px] text-amber bg-amber/10 border border-amber/30 rounded px-1"
                              title="You assigned this track manually."
                            >
                              manual
                            </span>
                          )}
                        </span>
                      </td>
                      <td className="py-1.5 pr-3 font-mono text-xs tabular-nums text-ink-300">
                        {c.level_min_dbfs != null && c.level_max_dbfs != null
                          ? `${c.level_min_dbfs.toFixed(0)}…${c.level_max_dbfs.toFixed(0)} dB`
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {chars.some((c) => c.voices && c.voices.length > 0) && <VoicesSection chars={chars} />}

            {result && result.naming_issues.length > 0 && (
              <div className="mt-3 space-y-1.5">
                <div className="text-[11px] uppercase tracking-wide text-ink-400">
                  Track ↔ character checks ({result.naming_issues.length}) — click to listen &amp; verify
                </div>
                {result.naming_issues.map((iss, i) => (
                  <NamingIssueRow
                    key={i}
                    iss={iss}
                    onAssign={
                      iss.kind === "possible_match" && iss.character && iss.channel
                        ? () => remap.mutate({ characterId: iss.character!, channel: iss.channel! })
                        : undefined
                    }
                  />
                ))}
              </div>
            )}
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
                  <span title="How strict 'misaligned' is: a line passes when enough of its slot contains speech. Lower = stricter (more flagged), higher = more lenient.">
                    Tolerance
                  </span>
                  {/* The backend maps tol -> coverage as max(0.2, min(0.8, 1 - tol/2)), which
                      saturates below 0.4 and above 1.6 — a wider slider would silently do
                      nothing across the extra travel, so the range mirrors the real span. */}
                  <input
                    type="range"
                    min={0.4}
                    max={1.6}
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
                <div className="relative">
                  <button
                    className="btn-primary"
                    onClick={() => setDlOpen((o) => !o)}
                    title="Download the QC report"
                  >
                    ↓ Download report ▾
                  </button>
                  {dlOpen && (
                    <div className="absolute right-0 mt-1 z-30 min-w-[220px] bg-ink-800 border border-ink-700 rounded-lg shadow-xl overflow-hidden">
                      <button
                        className="block w-full text-left px-3 py-2 text-sm hover:bg-ink-700"
                        onClick={() => { downloadTxt(); setDlOpen(false); }}
                      >
                        Report (.txt)
                        <span className="block text-[11px] text-ink-400">Readable summary for reviewers</span>
                      </button>
                      <button
                        className="block w-full text-left px-3 py-2 text-sm hover:bg-ink-700 border-t border-ink-700"
                        onClick={() => { downloadCsv(); setDlOpen(false); }}
                      >
                        Data (.csv)
                        <span className="block text-[11px] text-ink-400">Full issue list for Excel / tracking</span>
                      </button>
                    </div>
                  )}
                </div>
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
            {report.sync_warnings?.length > 0 && (
              <div className="mt-3 space-y-1.5">
                {report.sync_warnings.map((w, i) => (
                  <div key={i} className="text-xs text-amber bg-amber/5 border border-amber/30 rounded px-2 py-1.5">
                    <span className="font-mono text-[10px] opacity-80 mr-2">WHOLE-TRACK SYNC</span>
                    {w.message}
                  </div>
                ))}
              </div>
            )}

            {result?.original_audio && (s?.n_missing ?? 0) > 0 && (
              <MissingCompilation nMissing={s!.n_missing} tolS={report.tol_s} />
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
                    <ErrorRow key={i} e={e} hasOriginal={!!result?.original_audio} />
                  ))}
                </div>
              </>
            ) : (
              <div className="mt-4 text-sm text-emerald-400">No issues detected — everything lines up. ✓</div>
            )}
          </section>
        )}

        {/* 4 — loudness */}
        {result && result.loudness_flags.length > 0 && (
          <section className="card">
            <div className="section-title">4 · Loudness checks</div>
            <div className="section-sub">
              Lines on the dub tracks that are unusually quiet or hot (near clipping) — click to listen.
            </div>
            <div className="mt-3 max-h-[360px] overflow-y-auto divide-y divide-ink-900/60 border border-ink-800 rounded-lg">
              {result.loudness_flags.map((f, i) => (
                <LoudnessRow key={i} f={f} hasOriginal={!!result?.original_audio} />
              ))}
            </div>
          </section>
        )}
      </main>
    </div>
  );
}

function LoudnessRow({ f, hasOriginal }: { f: LoudnessFlag; hasOriginal?: boolean }) {
  const [open, setOpen] = useState(false);
  const hot = f.type === "LOUD";
  const tone = hot ? "text-amber" : "text-sky-300";
  const audioUrl = api.audioSliceUrl(f.channel, f.script_start_s, f.script_end_s, CONTEXT_PAD_S);
  const originalUrl = hasOriginal
    ? api.audioSliceUrl(null, f.script_start_s, f.script_end_s, CONTEXT_PAD_S, { source: "original" })
    : null;
  return (
    <div className="px-3 py-2 hover:bg-ink-800/40">
      <button className="flex items-start gap-3 w-full text-left" onClick={() => setOpen((o) => !o)}>
        <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${hot ? "bg-amber" : "bg-sky-400"}`} />
        <span className="font-mono text-xs text-ink-200 tabular-nums w-14 shrink-0 pt-0.5">{fmtTime(f.script_start_s)}</span>
        <span className="min-w-0 flex-1">
          <span className="text-sm block">
            <span className="font-medium text-ink-100">{f.character}</span>
            <span className={tone}> · {hot ? "Too hot" : "Too quiet"}</span>
            <span className="font-mono text-xs text-ink-300"> · {f.level_dbfs.toFixed(0)} dB{hot ? ` (peak ${f.peak_dbfs.toFixed(0)})` : ""}</span>
          </span>
          {!open && f.text && <span className="text-xs text-ink-300 block truncate">“{f.text}”</span>}
        </span>
        <span className="text-ink-300 text-xs pt-0.5 shrink-0">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="pl-[4.4rem] pr-2 pt-2 pb-1 space-y-2">
          <div className="text-xs text-ink-100">{f.message}</div>
          {f.text && (
            <div className="text-sm text-ink-100 bg-ink-800/60 border border-ink-700 rounded px-2 py-1">“{f.text}”</div>
          )}
          <div className="text-[10px] uppercase tracking-wide text-ink-400">
            Audio<span className="text-ink-200 font-mono normal-case"> · {f.channel}</span> (±{CONTEXT_PAD_S}s)
          </div>
          <audio key={audioUrl} controls preload="none" className="w-full h-8">
            <source src={audioUrl} type="audio/wav" />
          </audio>
          {originalUrl && (
            <>
              <div className="text-[10px] uppercase tracking-wide text-ink-400">
                Original <span className="text-ink-300 normal-case">— the source at this moment (level reference)</span>
              </div>
              <audio key={originalUrl} controls preload="none" className="w-full h-8">
                <source src={originalUrl} type="audio/wav" />
              </audio>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function PathRow({
  label,
  value,
  kind,
  onPick,
  onChange,
  onDropFile,
}: {
  label: string;
  value: string;
  kind: "file" | "folder";
  onPick?: () => void;
  onChange?: (v: string) => void;
  onDropFile?: (file: File) => void;
}) {
  const [over, setOver] = useState(false);
  return (
    <div className="flex items-start gap-2">
      <span className="text-xs text-ink-400 w-24 shrink-0 pt-3">{label}</span>
      <div
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
        className={`flex-1 flex items-center gap-2 rounded-lg border-2 border-dashed px-2 py-1.5 text-sm transition ${
          over ? "border-amber bg-amber/10" : "border-ink-700"
        }`}
      >
        {/* Editable path — works in the browser (paste the full path) and in the desktop app. */}
        <input
          className="flex-1 bg-transparent font-mono text-ink-200 text-xs outline-none placeholder:text-ink-500 min-w-0"
          value={value}
          onChange={(e) => onChange?.(e.target.value)}
          spellCheck={false}
          placeholder={`Paste the full ${kind} path, drag a ${kind} here${onPick ? ", or Browse…" : ""}`}
        />
        {onPick && (
          <button
            type="button"
            onClick={onPick}
            className="shrink-0 text-[11px] text-amber hover:text-amber/80 border border-amber/30 rounded px-2 py-1"
          >
            Browse…
          </button>
        )}
      </div>
    </div>
  );
}

// The missing lines, from the ORIGINAL audio, in two forms:
//   stitch   — clips back-to-back (short worklist to play through)
//   timeline — full episode-length track, silent except at the gaps (drop on the timeline)
function MissingCompilation({ nMissing, tolS }: { nMissing: number; tolS: number }) {
  const [busy, setBusy] = useState<null | "stitch" | "timeline">(null);
  const [built, setBuilt] = useState<null | { mode: "stitch" | "timeline"; url: string; name: string }>(null);
  const [err, setErr] = useState<string | null>(null);

  const build = async (mode: "stitch" | "timeline") => {
    setBusy(mode);
    setErr(null);
    try {
      const res = await fetch(api.missingCompilationUrl(mode, 1.0, tolS));
      if (!res.ok) {
        const d = await res.json().catch(() => null);
        throw new Error(d?.detail || `HTTP ${res.status}`);
      }
      const blob = await res.blob();
      setBuilt((prev) => {
        if (prev) URL.revokeObjectURL(prev.url);
        return {
          mode,
          url: URL.createObjectURL(blob),
          name: mode === "timeline" ? `missing-lines-timeline-${nMissing}.wav` : `missing-lines-original-${nMissing}.wav`,
        };
      });
    } catch (e) {
      setErr((e as Error).message);
      setBuilt(null);
    } finally {
      setBusy(null);
    }
  };

  const download = () => {
    if (!built) return;
    const a = document.createElement("a");
    a.href = built.url;
    a.download = built.name;
    a.click();
  };

  return (
    <div className="mt-3 border border-ink-700 rounded-lg px-3 py-2.5 bg-ink-800/40">
      <div className="text-sm text-ink-100 font-medium">Missing lines — from the original audio ({nMissing})</div>
      <div className="text-[11px] text-ink-400 mt-0.5">
        Every missing line taken from the original (±1s context). Build a short listen-through, or a
        full episode-length track that's silent except at the gaps — to lay over the dub timeline.
      </div>
      <div className="flex items-center gap-2 mt-2 flex-wrap">
        <button className="btn-ghost" onClick={() => build("stitch")} disabled={busy !== null}>
          {busy === "stitch" ? "Building…" : "🎧 Listen-through"}
        </button>
        <button className="btn-ghost" onClick={() => build("timeline")} disabled={busy !== null}>
          {busy === "timeline" ? "Building…" : "🎼 Full-length timeline"}
        </button>
        {built && (
          <>
            <span className="text-[10px] text-ink-500">
              {built.mode === "timeline" ? "full-length" : "listen-through"}
            </span>
            <audio key={built.url} controls preload="metadata" className="h-8 flex-1 min-w-[220px]">
              <source src={built.url} type="audio/wav" />
            </audio>
            <button className="btn-primary" onClick={download}>↓ Download WAV</button>
          </>
        )}
      </div>
      {err && <div className="text-xs text-err mt-2">{err}</div>}
    </div>
  );
}

const LANG_LABEL: Record<string, string> = {
  hi: "Hindi", ta: "Tamil", te: "Telugu", ml: "Malayalam",
  mr: "Marathi", bn: "Bengali", kn: "Kannada", "?": "—",
};
const LANG_ORDER = ["hi", "ta", "te", "ml", "kn", "bn", "mr", "?"];

// ElevenLabs voices per character across dub languages (from the production
// voice bank). Informational — helps the team grab the right voice ID fast.
function VoicesSection({ chars }: { chars: Character[] }) {
  const [open, setOpen] = useState(false);
  const withVoices = chars.filter((c) => c.voices && c.voices.length > 0);
  const nIds = withVoices.reduce((n, c) => n + (c.voices?.filter((v) => v.id).length ?? 0), 0);
  const copy = (id: string) => navigator.clipboard?.writeText(id).catch(() => {});
  const sortVoices = (vs: VoiceEntry[]) =>
    [...vs].sort(
      (a, b) =>
        (a.form === "granute" ? 1 : 0) - (b.form === "granute" ? 1 : 0) ||
        LANG_ORDER.indexOf(a.lang) - LANG_ORDER.indexOf(b.lang),
    );
  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="text-xs text-amber hover:text-amber/80"
      >
        {open ? "▾" : "▸"} ElevenLabs voices — {withVoices.length} characters, {nIds} voice IDs across languages
      </button>
      {open && (
        <div className="mt-2 overflow-x-auto border border-ink-800 rounded-lg">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wide text-ink-500 border-b border-ink-800">
                <th className="py-1.5 px-2">Character</th>
                <th className="py-1.5 px-2">Language</th>
                <th className="py-1.5 px-2">Voice name</th>
                <th className="py-1.5 px-2">Voice ID (click to copy)</th>
                <th className="py-1.5 px-2">Form</th>
              </tr>
            </thead>
            <tbody>
              {withVoices.flatMap((c) =>
                sortVoices(c.voices!).map((v, i) => (
                  <tr key={`${c.id}-${i}`} className="border-b border-ink-900/60">
                    <td className="py-1 px-2 font-medium text-ink-100">{i === 0 ? c.name : ""}</td>
                    <td className="py-1 px-2 text-ink-200">{LANG_LABEL[v.lang] ?? v.lang}</td>
                    <td className="py-1 px-2 text-ink-300">{v.name}</td>
                    <td className="py-1 px-2">
                      {v.id ? (
                        <button
                          type="button"
                          className="font-mono text-[11px] text-sky-300 hover:text-sky-200"
                          title="Copy voice ID"
                          onClick={() => copy(v.id!)}
                        >
                          {v.id}
                        </button>
                      ) : (
                        <span className="text-ink-500">—</span>
                      )}
                    </td>
                    <td className="py-1 px-2">
                      {v.form === "granute" ? (
                        <span className="text-[10px] text-amber bg-amber/10 border border-amber/30 rounded px-1">granute</span>
                      ) : (
                        <span className="text-ink-500">normal</span>
                      )}
                    </td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function NamingIssueRow({ iss, onAssign }: { iss: NamingIssue; onAssign?: () => void }) {
  const [open, setOpen] = useState(false);
  const tone =
    iss.kind === "name_mismatch" || iss.kind === "possible_match"
      ? "text-amber border-amber/30 bg-amber/5"
      : iss.kind === "verified_absent"
      ? "text-err border-err/30 bg-err/5"
      : "text-sky-300 border-sky-400/30 bg-sky-400/5";
  const tag =
    iss.kind === "name_mismatch"
      ? "NAME ≠ VOICE"
      : iss.kind === "possible_match"
      ? "POSSIBLE MATCH"
      : iss.kind === "verified_absent"
      ? "NO AUDIO (verified)"
      : "RECOVERED";
  const samples = iss.samples ?? [];
  const canOpen = samples.length > 0;

  return (
    <div className={`text-xs border rounded ${tone}`}>
      <button
        className="flex items-start gap-2 w-full text-left px-2 py-1.5"
        onClick={() => canOpen && setOpen((o) => !o)}
      >
        <span className="font-mono text-[10px] opacity-80 shrink-0 pt-0.5">{tag}</span>
        <span className="flex-1 text-ink-100">{iss.message}</span>
        {canOpen && <span className="shrink-0 pt-0.5 opacity-70">{open ? "▾" : "▸ listen"}</span>}
      </button>
      {open && (
        <div className="px-2 pb-2 pt-0.5 space-y-2 border-t border-ink-700/60">
          {samples.map((s, i) => (
            <div key={i}>
              <div className="text-[11px] text-ink-200 mb-0.5 font-mono">
                {s.label}
                <span className="text-ink-400"> · {fmtTime(s.start_s)}–{fmtTime(s.end_s)}</span>
              </div>
              <audio
                key={api.audioSliceUrl(s.channel, s.start_s, s.end_s, CONTEXT_PAD_S)}
                controls
                preload="none"
                className="w-full h-8"
              >
                <source src={api.audioSliceUrl(s.channel, s.start_s, s.end_s, CONTEXT_PAD_S)} type="audio/wav" />
              </audio>
            </div>
          ))}
          {iss.kind === "name_mismatch" && samples.length >= 2 && (
            <div className="text-[11px] text-ink-300">
              If the <span className="text-amber">voice-match</span> clip has the speaker and the{" "}
              <span className="text-ink-200">as-labelled</span> clip is silent, the track is mislabelled.
            </div>
          )}
          {onAssign && (
            <button
              type="button"
              onClick={onAssign}
              className="text-[11px] text-amber hover:text-amber/80 border border-amber/30 rounded px-2 py-1"
              title="Confirmed by ear? Assign this track to the character — errors and loudness re-score instantly."
            >
              ✓ It's them — assign '{iss.channel}' to {iss.character_name}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function ErrorRow({ e, hasOriginal }: { e: AlignError; hasOriginal?: boolean }) {
  const [open, setOpen] = useState(false);
  const st = TYPE_STYLE[e.type];
  const t = e.script_start_s ?? e.audio_start_s;
  const winStart = e.script_start_s ?? e.audio_start_s;
  const winEnd = e.script_end_s ?? e.audio_end_s;
  const audioUrl =
    e.channel != null && winStart != null && winEnd != null
      ? api.audioSliceUrl(e.channel, winStart, winEnd, CONTEXT_PAD_S)
      : null;
  // The original-language reference at the same moment (source-timed like the script).
  const originalUrl =
    hasOriginal && winStart != null && winEnd != null
      ? api.audioSliceUrl(null, winStart, winEnd, CONTEXT_PAD_S, { source: "original" })
      : null;

  // Layout of the flagged slot within the padded playback window (for the visual bar).
  const clipStart = winStart != null ? Math.max(0, winStart - CONTEXT_PAD_S) : 0;
  const clipEnd = winEnd != null ? winEnd + CONTEXT_PAD_S : 0;
  const clipLen = Math.max(0.001, clipEnd - clipStart);
  const flagLeft = winStart != null ? ((winStart - clipStart) / clipLen) * 100 : 0;
  const flagWidth = winStart != null && winEnd != null ? ((winEnd - winStart) / clipLen) * 100 : 0;

  return (
    <div className="px-3 py-2 hover:bg-ink-800/40">
      <button className="flex items-start gap-3 w-full text-left" onClick={() => setOpen((o) => !o)}>
        <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${st.dot}`} />
        <span className="font-mono text-xs text-ink-200 tabular-nums w-14 shrink-0 pt-0.5">{fmtTime(t)}</span>
        <span className="min-w-0 flex-1">
          <span className="text-sm block">
            <span className="font-medium text-ink-100">{e.character ?? "?"}</span>
            <span className={st.text}>
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
          {!open && e.text && <span className="text-xs text-ink-300 block truncate">“{e.text}”</span>}
        </span>
        <span className="text-ink-300 text-xs pt-0.5 shrink-0">{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <div className="pl-[4.4rem] pr-2 pt-2 pb-1 space-y-3">
          {/* What to listen for */}
          <div className={`text-xs rounded px-2 py-1.5 border ${st.soft} text-ink-100`}>
            {listenHint(e)}
          </div>

          {/* Script line */}
          <div>
            <div className="text-[10px] uppercase tracking-wide text-ink-400 mb-0.5">Script line</div>
            <div className="text-sm text-ink-100 bg-ink-800/60 border border-ink-700 rounded px-2 py-1">
              {e.text ? (
                `“${e.text}”`
              ) : e.type === "EXTRA" ? (
                <span className="text-ink-300">— no scripted line (extra speech in the track) —</span>
              ) : (
                <span className="text-ink-300">— line text unavailable —</span>
              )}
            </div>
          </div>

          {/* Audio with a timeline showing where the issue sits */}
          <div>
            <div className="text-[10px] uppercase tracking-wide text-ink-400 mb-1">
              Audio{e.channel ? <span className="text-ink-200 font-mono normal-case"> · {e.channel}</span> : ""}
            </div>
            {audioUrl ? (
              <>
                {/* Timeline: full bar = what plays (flagged slot + {CONTEXT_PAD_S}s each side); coloured band = the flagged part */}
                <div className="relative h-7 rounded bg-ink-800 border border-ink-700 overflow-hidden mb-1">
                  <div
                    className={`absolute inset-y-0 border-x ${st.fill}`}
                    style={{ left: `${flagLeft}%`, width: `${Math.max(flagWidth, 1.5)}%` }}
                    title={`Flagged region: ${fmtTime(winStart)}–${fmtTime(winEnd)}`}
                  />
                  <span className={`absolute left-1.5 top-1/2 -translate-y-1/2 text-[10px] font-medium ${st.text}`}>
                    ◀ {CONTEXT_PAD_S}s
                  </span>
                  <span
                    className="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 text-[10px] font-semibold text-ink-100"
                    style={{ left: `${flagLeft + Math.max(flagWidth, 1.5) / 2}%` }}
                  >
                    {st.label.toUpperCase()}
                  </span>
                  <span className={`absolute right-1.5 top-1/2 -translate-y-1/2 text-[10px] font-medium ${st.text}`}>
                    {CONTEXT_PAD_S}s ▶
                  </span>
                </div>
                <div className="flex justify-between text-[10px] font-mono text-ink-300 tabular-nums mb-1">
                  <span>{fmtTime(clipStart)}</span>
                  <span className={st.text}>
                    flagged {fmtTime(winStart)}–{fmtTime(winEnd)}
                    {winStart != null && winEnd != null ? ` (${(winEnd - winStart).toFixed(1)}s)` : ""}
                  </span>
                  <span>{fmtTime(clipEnd)}</span>
                </div>
                <audio key={audioUrl} controls preload="metadata" className="w-full h-8">
                  <source src={audioUrl} type="audio/wav" />
                </audio>
              </>
            ) : (
              <div className="text-xs text-ink-300">No audio track mapped for this line.</div>
            )}
          </div>

          {originalUrl && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-ink-400 mb-1">
                Original <span className="text-ink-300 normal-case">— what the source had at this moment</span>
              </div>
              <audio key={originalUrl} controls preload="none" className="w-full h-8">
                <source src={originalUrl} type="audio/wav" />
              </audio>
            </div>
          )}
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
