import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, isHosted, type AlignError, type AnalyzeResult, type BoxBrowse, type BoxLangSource, type BrowseResult, type Character, type CompareRequest, type EpisodeResult, type JobInfo, type LoudnessFlag, type NamingIssue, type Progress, type VoiceEntry } from "./api";
import { useAuth } from "./auth";

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

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
const listenHint = (e: AlignError, compare?: boolean): string => {
  if (e.type === "MISSING")
    return compare
      ? "The original speaks during the highlighted slot but the dub is silent. Play the original below to hear what should have been dubbed."
      : "This track should contain the line during the highlighted slot, but it's silent. Listen for the gap where the voice should be.";
  if (e.type === "MISALIGNED") {
    const dir = e.subtype === "late" ? "late" : e.subtype === "early" ? "early" : "off";
    const by = e.drift_s != null ? ` by ${Math.abs(e.drift_s).toFixed(2)}s` : "";
    return compare
      ? `The line IS in the dub but shifted ${dir}${by} — present, just mistimed. Compare the original and dub players.`
      : `The line is present but ${dir}${by}. Listen for the voice landing outside the highlighted slot.`;
  }
  return compare
    ? "The dub talks here but the original is silent. Listen to what was added."
    : "Unscripted speech here — the track talks during the highlighted slot but no script line covers it. Listen to what was said.";
};

// (frame-accurate HH:MM:SS:FF formatting lived here for the CSV export; the workbook
//  writes plain HH:MM:SS.s server-side, and the TXT already used its own formatter.)

const hasElectron = () => typeof window !== "undefined" && !!window.electronAPI;

export default function App() {
  const { user, signOut } = useAuth();
  const [scriptPath, setScriptPath] = useState("");
  const [audioDir, setAudioDir] = useState("");
  const [originalAudioPath, setOriginalAudioPath] = useState("");
  const [stripPrefix, setStripPrefix] = useState("");
  // script   — one language against a timecoded script (the classic flow)
  // compare  — scriptless: original episode audio vs the dub
  // episode  — one script x every dub language -> ONE .xlsx workbook (the studio deliverable)
  const [mode, setMode] = useState<"script" | "compare" | "episode">(isHosted ? "episode" : "script");
  // Episode mode: the six dub languages and where each one's tracks live. Editable —
  // a language with a blank folder is simply skipped, so you can run 2 or 6.
  const [langs, setLangs] = useState<{ name: string; dir: string }[]>([
    { name: "Malayalam", dir: "" }, { name: "Tamil", dir: "" }, { name: "Telugu", dir: "" },
    { name: "Kannada", dir: "" }, { name: "Bengali", dir: "" }, { name: "Marathi", dir: "" },
  ]);
  const [episodeName, setEpisodeName] = useState("");
  const [episodeOut, setEpisodeOut] = useState<EpisodeResult | null>(null);
  // Episode source: local folders on this machine/server, or straight from Box (the
  // server downloads + extracts + analyses; no bytes touch the browser).
  const [epSource, setEpSource] = useState<"local" | "box">(isHosted ? "box" : "local");
  const [boxReady, setBoxReady] = useState(false);        // server has its own Box connection
  const [boxDevToken, setBoxDevToken] = useState("");     // 60-min dev token (memory only)
  type BoxPick = { id: string; name: string };
  const [boxScript, setBoxScript] = useState<BoxPick | null>(null);
  const [boxOriginal, setBoxOriginal] = useState<BoxPick | null>(null);
  const [boxLangs, setBoxLangs] = useState<Record<string, (BoxPick & { kind: "zip" | "folder" }) | null>>({});
  // Which slot the Box picker is choosing for right now.
  const [boxPick, setBoxPick] = useState<null | { accept: "script" | "audio" | "tracks" | "folder"; set: (p: BoxPick & { kind: "zip" | "folder" }) => void }>(null);
  // Auto-detect: point at the episode's parent folder + give the number, and the server
  // finds the script / original / per-language zips. Manual pick stays available.
  const [boxFillMode, setBoxFillMode] = useState<"auto" | "manual">("auto");
  const [boxRoot, setBoxRoot] = useState<BoxPick | null>(null);
  const [scanEp, setScanEp] = useState("");
  const [scanNotes, setScanNotes] = useState<string[] | null>(null);
  const boxScan = useMutation({
    mutationFn: async () => {
      if (!boxRoot) throw new Error("Pick the folder that contains the episode first.");
      if (!scanEp.trim()) throw new Error("Enter the episode number to auto-detect.");
      return api.boxScan(boxRoot.id, scanEp.trim(), boxDevToken.trim() || null);
    },
    onSuccess: (r) => {
      setError(null);
      setScanNotes(r.notes);
      if (r.script) setBoxScript(r.script);
      if (r.original) setBoxOriginal(r.original);
      // match detected languages onto the existing rows by name (case-insensitive)
      setBoxLangs((prev) => {
        const next = { ...prev };
        for (const [lang, src] of Object.entries(r.languages)) {
          const row = langs.find((l) => l.name.trim().toLowerCase() === lang.toLowerCase());
          if (row) next[row.name] = { id: src.id, name: src.name, kind: src.kind };
        }
        return next;
      });
      if (!episodeName.trim()) setEpisodeName(`EP${scanEp.trim()}`);
    },
    onError: (e: Error) => { setError(e.message); setScanNotes(null); },
  });
  useEffect(() => {
    if (mode !== "episode") return;
    api.boxStatus().then((s) => setBoxReady(!!s.configured)).catch(() => setBoxReady(false));
  }, [mode]);
  const boxUsable = boxReady || boxDevToken.trim().length > 0;
  const [dubSource, setDubSource] = useState<"tracks" | "full">("tracks");
  const [dubAudioPath, setDubAudioPath] = useState("");
  const [tolS, setTolS] = useState(1.0);
  const [filter, setFilter] = useState<"ALL" | "MISSING" | "MISALIGNED" | "EXTRA">("ALL");
  const [result, setResult] = useState<AnalyzeResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dlOpen, setDlOpen] = useState(false);
  const [progress, setProgress] = useState<Progress | null>(null);

  // Hosted mode (served by the backend over a tunnel/LAN): no Electron dialogs; analyses
  // run as server-side jobs; input files are picked from the server's shared folder.
  const hosted = isHosted;
  const [srv, setSrv] = useState<{ browse: boolean } | null>(null);
  useEffect(() => {
    if (!hosted) return;
    // Retry the capability probe — one blip at page load must not hide Browse forever.
    let live = true;
    (async () => {
      for (let i = 0; i < 5 && live; i++) {
        try {
          const h = await api.healthz();
          if (live) setSrv({ browse: !!h.browse_enabled });
          return;
        } catch {
          await sleep(1500);
        }
      }
    })();
    return () => { live = false; };
  }, [hosted]);
  // Which input field the server-file browser is currently picking for.
  const [browse, setBrowse] = useState<null | { kind: "file" | "folder"; accept: "script" | "audio"; set: (p: string) => void }>(null);

  const analyze = useMutation({
    mutationFn: async () => {
      const req = {
        script_path: scriptPath.trim(),
        audio_dir: audioDir.trim(),
        strip_prefix: stripPrefix,
        tol_s: tolS,
        original_audio_path: originalAudioPath.trim() || null,
      };
      if (!hosted) return api.analyze(req);
      // Hosted: 202 + poll. Tunnels (ngrok/Cloudflare) cut long-silent requests, so the
      // run happens server-side and we poll its progress. Tunnels also blip, so we
      // tolerate a long run of transient failures (~1 min) before giving up — but a
      // DEFINITIVE answer (bad key, or the job id is gone) stops immediately.
      const job = await api.analyzeJob(req);
      let misses = 0;
      const MAX_MISSES = 40; // ~1 min of continuous failure before surrendering
      for (;;) {
        await sleep(misses === 0 ? 1200 : Math.min(5000, 1200 + misses * 400)); // back off on blips
        let j: JobInfo;
        try {
          j = await api.jobStatus(job.job_id);
          misses = 0;
        } catch (e) {
          const msg = (e as Error).message;
          if (/API key|Unknown or expired job/i.test(msg)) throw e; // definitive — don't retry
          if (++misses >= MAX_MISSES)
            throw new Error("Lost contact with the server for a while — the analysis may still be running on it; reload the page in a minute to check.");
          continue;
        }
        setProgress({ running: j.status === "running", ...j.progress });
        if (j.status === "done" && j.result) return j.result;
        if (j.status === "error") throw new Error(j.error || "Analysis failed");
      }
    },
    onSuccess: (r) => {
      setError(null);
      setResult(r);
    },
    onError: (e: Error) => setError(e.message),
  });

  // Snapshot of the last compare request, so "Re-run" (tolerance change) re-scores the
  // SAME inputs the on-screen result came from — not whatever the form says now.
  const lastCompareReq = useRef<CompareRequest | null>(null);
  const compare = useMutation({
    mutationFn: (override?: CompareRequest) => {
      const req: CompareRequest = override ?? {
        original_audio_path: originalAudioPath.trim(),
        audio_dir: dubSource === "tracks" ? audioDir.trim() : null,
        dub_audio_path: dubSource === "full" ? dubAudioPath.trim() : null,
        strip_prefix: stripPrefix,
        tol_s: tolS,
      };
      lastCompareReq.current = req;
      return api.compare(req);
    },
    onSuccess: (r) => {
      setError(null);
      setResult(r);
    },
    onError: (e: Error) => setError(e.message),
  });

  // One script x every language -> one workbook. ALWAYS a job: six languages x ~20 stems
  // is 10-20 minutes, far past any request timeout, so we submit and poll (same shape the
  // hosted analyze uses).
  const episode = useMutation({
    mutationFn: async () => {
      let job: JobInfo;
      if (epSource === "box") {
        const languages: Record<string, BoxLangSource> = {};
        for (const l of langs) {
          const pick = boxLangs[l.name];
          if (!l.name.trim() || !pick) continue;
          languages[l.name.trim()] = pick.kind === "zip"
            ? { zip_file_id: pick.id, name: pick.name }
            : { folder_id: pick.id, name: pick.name };
        }
        if (!boxScript) throw new Error("Pick the script from Box first.");
        if (!Object.keys(languages).length) throw new Error("Pick at least one language's zip/folder from Box.");
        job = await api.boxEpisodeJob({
          script_file_id: boxScript.id,
          original_file_id: boxOriginal?.id || null,
          languages,
          episode: episodeName.trim(),
          strip_prefix: stripPrefix,
          tol_s: tolS,
          box_token: boxDevToken.trim() || null,
        });
      } else {
        const languages: Record<string, string> = {};
        for (const l of langs) {
          if (l.name.trim() && l.dir.trim()) languages[l.name.trim()] = l.dir.trim();
        }
        if (!Object.keys(languages).length) throw new Error("Add at least one language and its tracks folder.");
        job = await api.episodeJob({
          script_path: scriptPath.trim(),
          languages,
          original_audio_path: originalAudioPath.trim() || null,
          episode: episodeName.trim(),
          strip_prefix: stripPrefix,
          tol_s: tolS,
        });
      }
      let misses = 0;
      for (;;) {
        await sleep(misses === 0 ? 2000 : Math.min(6000, 2000 + misses * 500));
        let j: JobInfo;
        try {
          j = await api.jobStatus(job.job_id);
          misses = 0;
        } catch (e) {
          const msg = (e as Error).message;
          if (/API key|Unknown or expired job/i.test(msg)) throw e;
          if (++misses >= 40)
            throw new Error("Lost contact with the server — the episode may still be running; reload in a minute.");
          continue;
        }
        setProgress({ running: j.status === "running", ...j.progress });
        if (j.status === "done") return j.result as unknown as EpisodeResult;
        if (j.status === "error") throw new Error(j.error || "Episode run failed");
      }
    },
    onSuccess: (r) => { setError(null); setEpisodeOut(r); },
    onError: (e: Error) => { setError(e.message); setEpisodeOut(null); },
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
  const pickDubAudio = async () => {
    const p = await window.electronAPI?.pickFile([
      { name: "Audio", extensions: ["wav", "flac", "ogg", "aiff", "aif", "mp3", "m4a"] },
    ]);
    if (p) setDubAudioPath(p);
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
  const onDropDubAudio = (file: File) => {
    const p = extractPath(file);
    if (p) { setError(null); setDubAudioPath(p); } else setError(NO_PATH_MSG);
  };

  // Poll real per-track progress while Analyse / Compare runs. In hosted mode the job
  // loop inside the analyze mutation drives setProgress instead (per-job progress).
  const running = analyze.isPending || compare.isPending || episode.isPending;
  useEffect(() => {
    if (!running) {
      setProgress(null);
      return;
    }
    if (hosted) return;
    const id = setInterval(() => {
      api.progress().then(setProgress).catch(() => {});
    }, 700);
    return () => clearInterval(id);
  }, [running, hosted]);

  const reset = () => {
    setScriptPath("");
    setAudioDir("");
    setOriginalAudioPath("");
    setStripPrefix("");
    setDubAudioPath("");
    setResult(null);
    setError(null);
    setFilter("ALL");
    setProgress(null);
    setDlOpen(false);
    lastCompareReq.current = null;
    // Episode state too, or "New analysis" leaves the previous episode's workbook panel
    // on screen next to a fresh run's results.
    setEpisodeOut(null);
    setEpisodeName("");
    setLangs((ls) => ls.map((l) => ({ ...l, dir: "" })));
    setBoxScript(null);
    setBoxOriginal(null);
    setBoxLangs({});
    setBoxRoot(null);
    setScanEp("");
    setScanNotes(null);
  };

  // ---- report helpers (used by the TXT report) ----
  const reportBase = () =>
    (((result?.mode === "compare" ? originalAudioPath : scriptPath).split(/[\\/]/).pop()) || "report")
      .replace(/\.[^.]+$/, "");
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
      // Grouped bit-parts (delivered inside a walla/crowd stem) are NOT "No audio".
      noAudio: r.characters.filter((c) => !c.channel && !c.grouped_in && c.line_count > 0).sort((a, b) => b.total_speech_s - a.total_speech_s),
      grouped: r.characters.filter((c) => c.grouped_in && c.line_count > 0).sort((a, b) => b.total_speech_s - a.total_speech_s),
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

    // ---- COMPARE (no-script) MODE: a plain timestamp list, built for opening the ----
    // ---- files in an editor (Audacity) and jumping straight to each finding.     ----
    if (r.mode === "compare") {
      const hmsd = (s: number | null | undefined) => {
        if (s == null) return "?";
        const neg = s < 0 ? "-" : "";
        const v = Math.abs(s);
        const p = (n: number) => String(Math.floor(n)).padStart(2, "0");
        return `${neg}${p(v / 3600)}:${p((v % 3600) / 60)}:${(v % 60).toFixed(1).padStart(4, "0")}`;
      };
      const span = (a?: number | null, b?: number | null) =>
        `${hmsd(a)} - ${hmsd(b)}   (${(a ?? 0).toFixed(2)}s - ${(b ?? 0).toFixed(2)}s)`;
      const dubLabel = r.channels.length === 1 ? r.channels[0] : `${r.channels.length} tracks (combined)`;

      L.push(bar, "DIALOGUE QC REPORT — ORIGINAL vs DUB (no script)", bar);
      L.push(`Original : ${reportBase()}`);
      L.push(`Dub      : ${dubLabel}`);
      L.push(`Tolerance: ${r.alignment.tol_s.toFixed(1)}s · speech regions found in original: ${r.n_segments}`);
      L.push("-".repeat(80), "SUMMARY");
      L.push(`  Missing    ......... ${pad3(S.n_missing)}   (dub genuinely SILENT where the original speaks)`);
      L.push(`  Misaligned ......... ${pad3(S.n_misaligned)}   (line present but mistimed, or dub audio VAD couldn't read)`);
      L.push(`  Extra      ......... ${pad3(S.n_extra)}   (dub speaks, original silent)`);
      L.push(bar);

      if (d.sync.length) {
        sec("SYNC", "whole-file shift between original and dub — times below already account for it");
        for (const w of d.sync) L.push(wrap(w.message, 2));
      }
      if (d.missing.length) {
        sec(`MISSING (${d.missing.length})`, "the dub is genuinely SILENT here — open the ORIGINAL at these times");
        for (const e of d.missing) L.push(`  ${span(e.script_start_s, e.script_end_s)}`);
      }
      if (d.misaligned.length) {
        sec(`MISALIGNED (${d.misaligned.length})`, "line is present but off — check what kind in the last column");
        for (const e of d.misaligned) {
          const tag =
            e.subtype === "early" || e.subtype === "late"
              ? `present but shifted ${e.drift_s != null ? Math.abs(e.drift_s).toFixed(1) + "s " : ""}${e.subtype}`
              : `partly covered (${Math.round((e.coverage ?? 0) * 100)}%${e.drift_s != null ? ", sits " + (e.drift_s > 0 ? "late" : "early") : ""})`;
          L.push(`  ${span(e.script_start_s, e.script_end_s)}   ${tag}`);
        }
      }
      // ALL extras the backend found (the on-screen list hides <1.5s ones; the plain
      // report is the full checklist, so nothing is silently dropped here).
      const allExtra = r.alignment.errors
        .filter((e) => e.type === "EXTRA" && e.audio_start_s != null)
        .sort((a, b) => (a.audio_start_s ?? 0) - (b.audio_start_s ?? 0));
      if (allExtra.length) {
        sec(`EXTRA (${allExtra.length})`, "dub speech where the original is silent — times are in the NAMED dub file");
        for (const e of allExtra) L.push(`  ${span(e.audio_start_s, e.audio_end_s)}   ${e.channel ?? ""}`);
      }
      L.push("", bar, "End of report.", bar);
      triggerDownload(L.join("\r\n"), `dialogue-qc_compare_${reportBase()}.txt`, "text/plain;charset=utf-8");
      return;
    }

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
        } else if (it.kind === "grouped") {
          block("GROUPED", "", it.character_name ?? "", it.channel ?? "", "bit-part in a walla/crowd stem");
          L.push(wrap(`No dedicated track; delivered inside the group stem '${it.channel}'. Normal for small parts — expected, not missing.`));
        } else if (it.kind === "reassigned") {
          block("REASSIGNED", "", it.character_name ?? "", it.channel ?? "", "track handed to the voice that owns it");
          L.push(wrap(it.message ?? `'${it.channel}' was reassigned to ${it.character_name} because its voice matches them, not the name it was labelled with.`));
        } else if (it.kind === "twin_merged") {
          block("TWIN MERGED", "", it.character_name ?? "", it.channel ?? "", "split delivery — stems checked together");
          L.push(wrap(it.message ?? `'${it.channel}' also carries ${it.character_name}'s dialogue; checked together with their main stem.`));
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
    if (d.grouped.length) {
      sec("GROUPED BIT-PARTS", "delivered inside a walla/crowd stem — expected, not missing");
      for (const c of d.grouped) L.push(`[GROUPED]     ${c.name}`.padEnd(40) + `(${c.line_count} lines) -> in '${c.grouped_in}'`);
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
  const canAnalyze =
    mode === "script"
      ? scriptPath.trim() && audioDir.trim() && !running
      : mode === "episode"
      ? (epSource === "box"
          ? !!boxScript && langs.some((l) => l.name.trim() && boxLangs[l.name]) && !running
          : scriptPath.trim() && langs.some((l) => l.name.trim() && l.dir.trim()) && !running)
      : originalAudioPath.trim() &&
        (dubSource === "tracks" ? audioDir.trim() : dubAudioPath.trim()) &&
        !running;

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-ink-800 bg-ink-950/75 backdrop-blur-xl sticky top-0 z-20">
        <div className="max-w-5xl mx-auto px-8 h-[58px] flex items-center justify-between">
          <h1 className="font-display text-[18px] font-semibold tracking-tight">
            Dialogue QC <span className="text-ink-500 font-normal text-sm">· missing-dialogue detection</span>
          </h1>
          <div className="flex items-center gap-3">
            {(scriptPath || audioDir || originalAudioPath || dubAudioPath || result) && (
              <button className="btn-ghost" onClick={reset} disabled={running}>
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
            <span className="font-mono text-[10.5px] text-ink-500">{hosted ? "hosted" : "offline · local"}</span>
          </div>
        </div>
      </header>

      <main className="flex-1 px-6 py-6 max-w-5xl w-full mx-auto space-y-5">
        {error && <div className="card text-sm text-err">{error}</div>}

        {/* 1 — inputs */}
        <section className="card space-y-3">
          <div>
            <div className="section-title">1 · Choose inputs</div>
            <div className="section-sub">
              {hosted
                ? "Files are read on the QC server — nothing uploads from your machine."
                : "Nothing is uploaded — it all stays on this machine."}
            </div>
          </div>

          {/* Mode switcher is DESKTOP-only. The hosted tool does exactly one thing —
              episode → Excel straight from Box (the default when hosted) — so the tabs
              (Script+tracks, Compare) are hidden there to keep it single-purpose. */}
          {!hosted && (
            <div className="flex gap-1 bg-ink-900 border border-ink-800 rounded-lg p-1 w-fit">
              <button
                className={`px-3 py-1 rounded text-xs ${mode === "script" ? "bg-ink-700 text-ink-100" : "text-ink-400 hover:text-ink-200"}`}
                onClick={() => setMode("script")}
                title="Check the dub tracks against a timecoded script (DOCX/SRT/CSV)."
              >
                Script + tracks
              </button>
              <button
                className={`px-3 py-1 rounded text-xs ${mode === "compare" ? "bg-ink-700 text-ink-100" : "text-ink-400 hover:text-ink-200"}`}
                onClick={() => setMode("compare")}
                title="No script? Compare the dub against the ORIGINAL episode audio: wherever the original has speech, the dub should too."
              >
                No script — compare vs original
              </button>
              <button
                className={`px-3 py-1 rounded text-xs ${mode === "episode" ? "bg-ink-700 text-ink-100" : "text-ink-400 hover:text-ink-200"}`}
                onClick={() => setMode("episode")}
                title="One script checked against EVERY dub language, producing one Excel workbook with a sheet per language."
              >
                Episode → Excel (all languages)
              </button>
            </div>
          )}

          {!hasElectron() && (
            <div className="text-[11px] text-amber/90 bg-amber/5 border border-amber/20 rounded px-2 py-1">
              {hosted ? (
                srv?.browse ? (
                  <>Pick the script and audio folder from the server's shared files with <b>Browse…</b>, then Analyse.</>
                ) : (
                  <>Paste server-side paths below (or ask the host to set <b>DQC_DATA_ROOT</b> to enable browsing).</>
                )
              ) : (
                <>Browser preview — choosing files needs the desktop app. Run <b>npm run dev</b> and
                use the app window (drag-and-drop / click-to-browse).</>
              )}
            </div>
          )}

          {mode === "script" ? (
            <>
              <PathRow
                label="Script file"
                value={scriptPath}
                kind="file"
                onPick={
                  hasElectron() ? pickScript
                  : hosted && srv?.browse ? () => setBrowse({ kind: "file", accept: "script", set: setScriptPath })
                  : undefined
                }
                onChange={setScriptPath}
                onDropFile={onDropScript}
              />
              <PathRow
                label="Audio folder"
                value={audioDir}
                kind="folder"
                onPick={
                  hasElectron() ? pickAudio
                  : hosted && srv?.browse ? () => setBrowse({ kind: "folder", accept: "audio", set: setAudioDir })
                  : undefined
                }
                onChange={setAudioDir}
                onDropFile={onDropAudio}
              />
              <PathRow
                label="Original audio (optional)"
                value={originalAudioPath}
                kind="file"
                onPick={
                  hasElectron() ? pickOriginalAudio
                  : hosted && srv?.browse ? () => setBrowse({ kind: "file", accept: "audio", set: setOriginalAudioPath })
                  : undefined
                }
                onChange={setOriginalAudioPath}
                onDropFile={onDropOriginalAudio}
              />
              <div className="text-[11px] text-ink-500 -mt-1">
                Original audio: one file with the source-language episode (e.g. the original mix).
                Flagged issues will show it side-by-side with the dub so you can hear what the
                original had at that moment.
              </div>
            </>
          ) : mode === "episode" ? (
            <>
              {/* Where the episode's files come from. Box = the server fetches everything. */}
              <div className="flex items-center gap-4 text-xs text-ink-400">
                <span>Files from:</span>
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input type="radio" className="accent-amber" checked={epSource === "local"}
                         onChange={() => setEpSource("local")} />
                  local folders
                </label>
                <label className={`flex items-center gap-1.5 ${boxUsable ? "cursor-pointer" : "opacity-50"}`}
                       title={boxUsable ? "Pick everything from Box — the server downloads and analyses."
                                        : "Needs the server's Box connection (OAuth) or a developer token below."}>
                  <input type="radio" className="accent-amber" checked={epSource === "box"}
                         disabled={!boxUsable}
                         onChange={() => setEpSource("box")} />
                  Box (server-to-server)
                </label>
                {!boxReady && (
                  <input
                    className="flex-1 min-w-[200px] bg-ink-800 border border-ink-700 rounded px-2 py-1 text-xs font-mono placeholder:text-ink-500"
                    value={boxDevToken}
                    onChange={(e) => setBoxDevToken(e.target.value)}
                    placeholder="Box developer token (60 min, for testing — kept in memory only)"
                    spellCheck={false}
                  />
                )}
              </div>

              {epSource === "box" ? (
                <>
                  {/* Auto-detect vs pick-each-file. Auto just PRE-FILLS the same fields, so
                      you can always fix what it guessed. */}
                  <div className="flex gap-1 bg-ink-900 border border-ink-800 rounded-lg p-1 w-fit text-xs">
                    <button className={`px-3 py-1 rounded ${boxFillMode === "auto" ? "bg-ink-700 text-ink-100" : "text-ink-400 hover:text-ink-200"}`}
                            onClick={() => setBoxFillMode("auto")}>🔍 Auto-detect episode</button>
                    <button className={`px-3 py-1 rounded ${boxFillMode === "manual" ? "bg-ink-700 text-ink-100" : "text-ink-400 hover:text-ink-200"}`}
                            onClick={() => setBoxFillMode("manual")}>✋ Pick each file</button>
                  </div>

                  {boxFillMode === "auto" && (
                    <div className="border border-amber/30 bg-amber/5 rounded-lg p-2.5 space-y-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        <div className="flex-1 min-w-[220px] text-xs font-mono px-2 py-1.5 border border-ink-700 rounded truncate">
                          {boxRoot ? <span className="text-emerald-400">📦 {boxRoot.name}</span>
                                   : <span className="text-ink-500">— folder that contains the episode —</span>}
                        </div>
                        <button type="button"
                                className="shrink-0 text-[11px] text-amber hover:text-amber/80 border border-amber/30 rounded px-2 py-1"
                                onClick={() => setBoxPick({ accept: "folder", set: (p) => setBoxRoot({ id: p.id, name: p.name }) })}>
                          Browse Box…
                        </button>
                        <input className="w-24 bg-ink-800 border border-ink-700 rounded px-2 py-1 text-xs"
                               value={scanEp} onChange={(e) => setScanEp(e.target.value)} placeholder="episode #" />
                        <button type="button" className="btn-primary text-xs py-1"
                                disabled={!boxRoot || !scanEp.trim() || boxScan.isPending}
                                onClick={() => boxScan.mutate()}>
                          {boxScan.isPending ? "Scanning…" : "Auto-detect"}
                        </button>
                      </div>
                      {scanNotes && scanNotes.length > 0 && (
                        <div className="text-[11px] text-amber/90 space-y-0.5">
                          {scanNotes.map((n, i) => <div key={i}>⚠ {n}</div>)}
                        </div>
                      )}
                      <div className="text-[11px] text-ink-500">
                        Fills the fields below from what it finds — <b>review and fix anything</b> before running.
                        (Detection is still being tuned to your Box's layout.)
                      </div>
                    </div>
                  )}

                  <BoxFieldRow label="Script file" pick={boxScript}
                    onBrowse={() => setBoxPick({ accept: "script", set: (p) => setBoxScript({ id: p.id, name: p.name }) })}
                    onClear={() => setBoxScript(null)} />
                  <BoxFieldRow label="Original audio (optional)" pick={boxOriginal}
                    onBrowse={() => setBoxPick({ accept: "audio", set: (p) => setBoxOriginal({ id: p.id, name: p.name }) })}
                    onClear={() => setBoxOriginal(null)} />
                </>
              ) : (
              <PathRow
                label="Script file"
                value={scriptPath}
                kind="file"
                onPick={
                  hasElectron() ? pickScript
                  : hosted && srv?.browse ? () => setBrowse({ kind: "file", accept: "script", set: setScriptPath })
                  : undefined
                }
                onChange={setScriptPath}
                onDropFile={onDropScript}
              />
              )}
              {epSource === "local" && (
              <PathRow
                label="Original audio (optional)"
                value={originalAudioPath}
                kind="file"
                onPick={
                  hasElectron() ? pickOriginalAudio
                  : hosted && srv?.browse ? () => setBrowse({ kind: "file", accept: "audio", set: setOriginalAudioPath })
                  : undefined
                }
                onChange={setOriginalAudioPath}
                onDropFile={onDropOriginalAudio}
              />
              )}
              <div className="flex items-start gap-2">
                <span className="text-xs text-ink-400 w-24 shrink-0 pt-2.5">Episode name</span>
                <input
                  className="flex-1 bg-ink-800 border border-ink-700 rounded px-2 py-1.5 text-xs font-mono"
                  value={episodeName}
                  onChange={(e) => setEpisodeName(e.target.value)}
                  placeholder="e.g. EP36  (used for the workbook name + Run info sheet)"
                />
              </div>

              <div className="border border-ink-700 rounded-lg p-2.5 space-y-1.5">
                <div className="text-[11px] uppercase tracking-wide text-ink-400">
                  {epSource === "box"
                    ? "Dub languages — pick each one's delivered ZIP (or stems folder) in Box. Unpicked = skipped."
                    : "Dub languages — one sheet each. Leave a folder blank to skip that language."}
                </div>
                {langs.map((l, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <input
                      className="w-28 shrink-0 bg-ink-800 border border-ink-700 rounded px-2 py-1 text-xs"
                      value={l.name}
                      onChange={(e) =>
                        setLangs((ls) => ls.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))
                      }
                    />
                    {epSource === "box" ? (
                      <>
                        <div className="flex-1 min-w-0 text-xs font-mono px-2 py-1 border border-ink-700 rounded bg-ink-800/60 truncate">
                          {boxLangs[l.name] ? (
                            <span className="text-emerald-400">
                              {boxLangs[l.name]!.kind === "zip" ? "🗜 " : "📁 "}{boxLangs[l.name]!.name}
                            </span>
                          ) : (
                            <span className="text-ink-500">— not picked (skipped) —</span>
                          )}
                        </div>
                        <button
                          type="button"
                          className="shrink-0 text-[11px] text-amber hover:text-amber/80 border border-amber/30 rounded px-2 py-1"
                          onClick={() =>
                            setBoxPick({
                              accept: "tracks",
                              set: (p) => setBoxLangs((m) => ({ ...m, [l.name]: p })),
                            })
                          }
                        >
                          Pick in Box…
                        </button>
                        {boxLangs[l.name] && (
                          <button
                            type="button"
                            className="shrink-0 text-[11px] text-ink-400 hover:text-ink-200"
                            onClick={() => setBoxLangs((m) => ({ ...m, [l.name]: null }))}
                          >
                            ✕
                          </button>
                        )}
                      </>
                    ) : (
                      <>
                    <input
                      className="flex-1 min-w-0 bg-ink-800 border border-ink-700 rounded px-2 py-1 text-xs font-mono text-ink-200 placeholder:text-ink-500"
                      value={l.dir}
                      onChange={(e) =>
                        setLangs((ls) => ls.map((x, j) => (j === i ? { ...x, dir: e.target.value } : x)))
                      }
                      placeholder={`tracks folder for ${l.name || "this language"} (blank = skip)`}
                      spellCheck={false}
                    />
                    {(hasElectron() || (hosted && srv?.browse)) && (
                      <button
                        type="button"
                        className="shrink-0 text-[11px] text-amber hover:text-amber/80 border border-amber/30 rounded px-2 py-1"
                        onClick={async () => {
                          if (hasElectron()) {
                            const p = await window.electronAPI?.pickFolder();
                            if (p) setLangs((ls) => ls.map((x, j) => (j === i ? { ...x, dir: p } : x)));
                          } else {
                            setBrowse({
                              kind: "folder", accept: "audio",
                              set: (p) => setLangs((ls) => ls.map((x, j) => (j === i ? { ...x, dir: p } : x))),
                            });
                          }
                        }}
                      >
                        Browse…
                      </button>
                    )}
                      </>
                    )}
                  </div>
                ))}
                <div className="text-[11px] text-ink-500 pt-0.5">
                  Every language is checked against the <b>same</b> script — that's what makes the sheets
                  comparable, and lets the workbook tell a script/mapping problem (missing in all
                  languages) from a real dub gap (missing in one). Runs sequentially: expect roughly
                  1–2 min per language.
                </div>
              </div>
            </>
          ) : (
            <>
              <PathRow
                label="Original episode audio"
                value={originalAudioPath}
                kind="file"
                onPick={hasElectron() ? pickOriginalAudio : undefined}
                onChange={setOriginalAudioPath}
                onDropFile={onDropOriginalAudio}
              />
              <div className="flex items-center gap-4 text-xs text-ink-400">
                <span>Dub side:</span>
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input type="radio" className="accent-amber" checked={dubSource === "tracks"}
                         onChange={() => setDubSource("tracks")} />
                  folder of speaker tracks (combined)
                </label>
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input type="radio" className="accent-amber" checked={dubSource === "full"}
                         onChange={() => setDubSource("full")} />
                  one full-episode dub file
                </label>
              </div>
              {dubSource === "tracks" ? (
                <PathRow
                  label="Dub tracks folder"
                  value={audioDir}
                  kind="folder"
                  onPick={hasElectron() ? pickAudio : undefined}
                  onChange={setAudioDir}
                  onDropFile={onDropAudio}
                />
              ) : (
                <PathRow
                  label="Full dub audio"
                  value={dubAudioPath}
                  kind="file"
                  onPick={hasElectron() ? pickDubAudio : undefined}
                  onChange={setDubAudioPath}
                  onDropFile={onDropDubAudio}
                />
              )}
              <div className="text-[11px] text-ink-500 -mt-1">
                Wherever the <b>original</b> has speech, the dub should too — silence there is flagged
                Missing (with timestamps you can verify in an editor). No character names or line text
                in this mode (that needs a script). Vocal efforts (shouts/grunts) in the original may
                flag even when a dub legitimately skips them — the audio players are the judge.
              </div>
            </>
          )}
          <div className="flex items-center gap-3 flex-wrap">
            {(mode === "script" || dubSource === "tracks") && (
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
            )}
            <button
              className="btn-primary ml-auto"
              disabled={!canAnalyze}
              onClick={() =>
                mode === "script" ? analyze.mutate()
                : mode === "episode" ? episode.mutate()
                : compare.mutate(undefined)
              }
            >
              {running ? "Analysing…" : mode === "script" ? "Analyse" : mode === "episode" ? "Run episode → Excel" : "Compare"}
            </button>
          </div>

          {running && (
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
                {progress?.stage || (mode === "compare" ? "Reading audio…" : "Parsing script…")}
              </div>
            </div>
          )}
        </section>

        {/* Episode run — the workbook + what actually made it in */}
        {episodeOut && (
          <section className="card space-y-3">
            <div>
              <div className="section-title">Episode workbook — {episodeOut.episode}</div>
              <div className="section-sub">
                One sheet per language, plus Run info and a cross-language Summary.
              </div>
            </div>

            {/* A workbook of 2 languages must never look like a workbook of 6. */}
            {Object.keys(episodeOut.failed).length > 0 && (
              <div className="text-xs text-err bg-err/5 border border-err/30 rounded px-2 py-1.5 space-y-0.5">
                <div className="font-medium">
                  ⚠ {Object.keys(episodeOut.failed).length} language(s) were NOT analysed — the
                  workbook does not cover them:
                </div>
                {Object.entries(episodeOut.failed).map(([l, why]) => (
                  <div key={l} className="font-mono text-[11px]">• {l}: {why}</div>
                ))}
              </div>
            )}

            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[11px] uppercase tracking-wide text-ink-500 border-b border-ink-800">
                    <th className="py-1.5 pr-3">Language</th>
                    <th className="py-1.5 pr-3">Missing</th>
                    <th className="py-1.5 pr-3">Misaligned</th>
                    <th className="py-1.5 pr-3">Extra</th>
                  </tr>
                </thead>
                <tbody>
                  {episodeOut.languages.map((l) => (
                    <tr key={l} className="border-b border-ink-900/60">
                      <td className="py-1.5 pr-3 font-medium">{l}</td>
                      <td className="py-1.5 pr-3 tabular-nums text-err">{episodeOut.summary[l]?.n_missing ?? "—"}</td>
                      <td className="py-1.5 pr-3 tabular-nums text-amber">{episodeOut.summary[l]?.n_misaligned ?? "—"}</td>
                      <td className="py-1.5 pr-3 tabular-nums text-sky-400">{episodeOut.summary[l]?.n_extra ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              <a className="btn-primary" href={api.reportXlsxUrl()} download>
                ↓ Download workbook (.xlsx)
              </a>
              {originalAudioPath.trim() && (
                <span className="text-[11px] text-ink-500">
                  Reference audio (original, silent except the missing parts) is under Detected
                  errors after a single-language run.
                </span>
              )}
            </div>
          </section>
        )}

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
                              c.channel ? "text-emerald-400" : c.grouped_in ? "text-ink-300" : "text-err"
                            }`}
                          >
                            <option value="">{c.grouped_in ? `↳ in ${c.grouped_in}` : "no audio ✗"}</option>
                            {(result?.channels ?? []).map((ch) => (
                              <option key={ch} value={ch}>{ch}</option>
                            ))}
                          </select>
                          {c.grouped_in && (
                            <span
                              className="text-[10px] text-ink-300 bg-ink-700/40 border border-ink-600/50 rounded px-1"
                              title={`Bit-part with no dedicated track — delivered inside the group stem "${c.grouped_in}" (walla/crowd), which is normal. Treated as delivered, not "No audio". Listen in the checks below to confirm.`}
                            >
                              grouped
                            </span>
                          )}
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
                          {c.roster_name && (
                            <span
                              className="text-[10px] text-emerald-300 bg-emerald-400/10 border border-emerald-400/30 rounded px-1"
                              title={`Character list: "${c.roster_name}"${c.roster_voice_name ? ` · voice name "${c.roster_voice_name}"` : ""}. Used to help match this character to the right track.`}
                            >
                              roster: {c.roster_name}
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
                  <button
                    className="btn-ghost"
                    disabled={running || realign.isPending}
                    onClick={() =>
                      result?.mode === "compare"
                        ? compare.mutate(
                            lastCompareReq.current ? { ...lastCompareReq.current, tol_s: tolS } : undefined,
                          )
                        : realign.mutate()
                    }
                    title={result?.mode === "compare" ? "Re-compare the same inputs at this tolerance (audio analysis is cached — fast)." : undefined}
                  >
                    {running || realign.isPending ? "…" : "Re-run"}
                  </button>
                </label>
                {result?.mode === "compare" && (result?.channels.length ?? 0) > 1 && (
                  <a
                    className="btn-ghost"
                    href={api.dubMixdownUrl()}
                    download
                    title="All dub speaker tracks summed into one full-length WAV — drop it into Audacity next to the original to compare. Takes ~a minute to build."
                  >
                    ⬇ Combined dub (.wav)
                  </a>
                )}
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
                      {/* The .xlsx is produced by an EPISODE run (all languages at once),
                          not by this single-language analysis — so it links to whatever the
                          last episode run built rather than pretending to export this view. */}
                      <a
                        className="block w-full text-left px-3 py-2 text-sm hover:bg-ink-700 border-t border-ink-700"
                        href={api.reportXlsxUrl()}
                        download
                        onClick={() => setDlOpen(false)}
                      >
                        Workbook (.xlsx)
                        <span className="block text-[11px] text-ink-400">
                          Per-episode, one sheet per language — from the last episode run
                        </span>
                      </a>
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
                    <ErrorRow key={i} e={e} hasOriginal={!!result?.original_audio} compare={result?.mode === "compare"} />
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

      {browse && (
        <BrowseModal
          kind={browse.kind}
          accept={browse.accept}
          onPick={(p) => {
            browse.set(p);
            setBrowse(null);
          }}
          onClose={() => setBrowse(null)}
        />
      )}
      {boxPick && (
        <BoxPickModal
          accept={boxPick.accept}
          devToken={boxDevToken.trim() || null}
          onPick={(p) => {
            boxPick.set(p);
            setBoxPick(null);
          }}
          onClose={() => setBoxPick(null)}
        />
      )}
    </div>
  );
}

// One picked-from-Box field (script / original) with pick + clear.
function BoxFieldRow({ label, pick, onBrowse, onClear }: {
  label: string;
  pick: { id: string; name: string } | null;
  onBrowse: () => void;
  onClear: () => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-ink-400 w-24 shrink-0">{label}</span>
      <div className="flex-1 min-w-0 text-xs font-mono px-2 py-1.5 border-2 border-dashed border-ink-700 rounded-lg truncate">
        {pick ? <span className="text-emerald-400">📦 {pick.name}</span>
              : <span className="text-ink-500">— pick from Box —</span>}
      </div>
      <button type="button" onClick={onBrowse}
              className="shrink-0 text-[11px] text-amber hover:text-amber/80 border border-amber/30 rounded px-2 py-1">
        Pick in Box…
      </button>
      {pick && (
        <button type="button" onClick={onClear} className="shrink-0 text-[11px] text-ink-400 hover:text-ink-200">✕</button>
      )}
    </div>
  );
}

// Box file/folder picker: navigates the server's Box connection (or a dev token) —
// only names and ids move through the browser, never file bytes.
function BoxPickModal({ accept, devToken, onPick, onClose }: {
  accept: "script" | "audio" | "tracks" | "folder";
  devToken: string | null;
  onPick: (p: { id: string; name: string; kind: "zip" | "folder" }) => void;
  onClose: () => void;
}) {
  // Breadcrumbs as a stack of visited folders; Box ids are opaque, root is "0".
  const [stack, setStack] = useState<{ id: string; name: string }[]>([{ id: "0", name: "All files" }]);
  const [data, setData] = useState<BoxBrowse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const cur = stack[stack.length - 1];

  useEffect(() => {
    let live = true;
    setLoading(true);
    api.boxBrowse(cur.id, devToken)
      .then((d) => { if (live) { setData(d); setErr(null); } })
      .catch((e) => { if (live) setErr((e as Error).message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [cur.id, devToken]);

  const match =
    accept === "script" ? /\.(docx|srt|csv|tsv)$/i
    : accept === "audio" ? /\.(wav|flac|ogg|aiff?|mp3|m4a)$/i
    : accept === "folder" ? /^$/  // folder mode: navigate only, don't list files
    : /\.zip$/i;
  const files = (data?.files ?? []).filter((f) => match.test(f.name));
  const fmtSize = (n: number) =>
    n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(0)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`;

  return (
    <div className="fixed inset-0 z-40 bg-black/60 flex items-center justify-center p-6" onClick={onClose}>
      <div className="bg-ink-900 border border-ink-700 rounded-xl w-full max-w-xl max-h-[70vh] flex flex-col overflow-hidden"
           onClick={(e) => e.stopPropagation()}>
        <div className="px-4 py-3 border-b border-ink-800 flex items-center gap-3">
          <div className="text-sm font-medium text-ink-100 flex-1 min-w-0">
            {accept === "script" ? "Pick the script from Box"
             : accept === "audio" ? "Pick the original audio from Box"
             : accept === "folder" ? "Open the folder that contains this episode"
             : "Pick this language's ZIP (or open its stems folder)"}
            <span className="block font-mono text-[11px] text-ink-400 truncate">
              📦 {stack.map((s) => s.name).join(" / ")}
            </span>
          </div>
          <button className="btn-ghost" onClick={onClose}>✕ Close</button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-2 text-sm">
          {err && <div className="text-xs text-err px-2 py-1">{err}</div>}
          {loading && !err && <div className="text-xs text-ink-400 px-2 py-1">Loading from Box…</div>}
          {!loading && !err && data && (
            <>
              {stack.length > 1 && (
                <button className="block w-full text-left px-2 py-1.5 rounded hover:bg-ink-800 text-ink-300"
                        onClick={() => setStack((s) => s.slice(0, -1))}>
                  ↑ ..
                </button>
              )}
              {data.folders.map((d) => (
                <button key={d.id}
                        className="block w-full text-left px-2 py-1.5 rounded hover:bg-ink-800 text-ink-100"
                        onClick={() => setStack((s) => [...s, { id: d.id, name: d.name }])}>
                  📁 {d.name}
                </button>
              ))}
              {files.map((f) => (
                <button key={f.id}
                        className="w-full flex items-center gap-2 text-left px-2 py-1.5 rounded hover:bg-ink-800"
                        onClick={() => onPick({ id: f.id, name: f.name, kind: accept === "tracks" ? "zip" : "folder" })}>
                  <span className="flex-1 min-w-0 truncate text-ink-100">
                    {accept === "tracks" ? "🗜" : "📄"} {f.name}
                  </span>
                  <span className="shrink-0 font-mono text-[10px] text-ink-500">{fmtSize(f.size)}</span>
                </button>
              ))}
              {data.folders.length === 0 && files.length === 0 && (
                <div className="text-xs text-ink-500 px-2 py-2">Nothing matching here.</div>
              )}
            </>
          )}
        </div>

        {((accept === "tracks" && stack.length > 1) || accept === "folder") && data && !err && (
          <div className="px-4 py-3 border-t border-ink-800 flex items-center justify-between gap-3">
            <span className="text-[11px] text-ink-400">
              {accept === "folder"
                ? `Use “${cur.name}” (${data.folders.length} subfolders, ${(data.files ?? []).length} files)`
                : `…or use this folder's loose stems (${(data.files ?? []).length} files listed)`}
            </span>
            <button className="btn-primary"
                    onClick={() => onPick({ id: cur.id, name: cur.name, kind: "folder" })}>
              Use this folder
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// Server-side file picker for HOSTED mode: navigates the folder the host shares via
// DQC_DATA_ROOT (the browser can't open the server's native file dialogs).
function BrowseModal({
  kind,
  accept,
  onPick,
  onClose,
}: {
  kind: "file" | "folder";
  accept: "script" | "audio";
  onPick: (absPath: string) => void;
  onClose: () => void;
}) {
  const [rel, setRel] = useState("");
  const [data, setData] = useState<BrowseResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .browse(rel)
      .then((d) => { if (live) { setData(d); setErr(null); } })
      .catch((e) => { if (live) setErr((e as Error).message); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [rel]);

  // Files the picker offers. For a SINGLE audio file (original) mp3/m4a are fine; but the
  // tracks-FOLDER analyzer only discovers wav/flac/ogg/aiff — so the folder count below
  // must use the stricter set or it would promise files the analysis then ignores.
  const match =
    accept === "script" ? /\.(docx|srt|csv|tsv)$/i
    : kind === "folder" ? /\.(wav|flac|ogg|aiff?)$/i
    : /\.(wav|flac|ogg|aiff?|mp3|m4a)$/i;
  const files = (data?.files ?? []).filter((f) => match.test(f.name));
  const sep = data?.abs.includes("\\") ? "\\" : "/";
  const fmtSize = (n: number) =>
    n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(0)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`;
  const upFrom = (r: string) => r.split("/").slice(0, -1).join("/");

  return (
    <div className="fixed inset-0 z-40 bg-black/60 flex items-center justify-center p-6" onClick={onClose}>
      <div
        className="bg-ink-900 border border-ink-700 rounded-xl w-full max-w-xl max-h-[70vh] flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-ink-800 flex items-center gap-3">
          <div className="text-sm font-medium text-ink-100 flex-1 min-w-0">
            {kind === "folder" ? "Pick a folder" : accept === "script" ? "Pick a script file" : "Pick an audio file"}
            <span className="block font-mono text-[11px] text-ink-400 truncate">/{data?.path || ""}</span>
          </div>
          <button className="btn-ghost" onClick={onClose}>✕ Close</button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-2 text-sm">
          {err && <div className="text-xs text-err px-2 py-1">{err}</div>}
          {loading && !err && <div className="text-xs text-ink-400 px-2 py-1">Loading…</div>}
          {!loading && !err && data && (
            <>
              {rel !== "" && (
                <button
                  className="block w-full text-left px-2 py-1.5 rounded hover:bg-ink-800 text-ink-300"
                  onClick={() => setRel(upFrom(rel))}
                >
                  ↑ ..
                </button>
              )}
              {data.dirs.map((d) => (
                <button
                  key={d}
                  className="block w-full text-left px-2 py-1.5 rounded hover:bg-ink-800 text-ink-100"
                  onClick={() => setRel(rel ? `${rel}/${d}` : d)}
                >
                  📁 {d}
                </button>
              ))}
              {kind === "file" &&
                files.map((f) => (
                  <button
                    key={f.name}
                    className="w-full flex items-center gap-2 text-left px-2 py-1.5 rounded hover:bg-ink-800"
                    onClick={() => onPick(`${data.abs}${sep}${f.name}`)}
                  >
                    <span className="flex-1 min-w-0 truncate text-ink-100">{accept === "script" ? "📄" : "🎵"} {f.name}</span>
                    <span className="shrink-0 font-mono text-[10px] text-ink-500">{fmtSize(f.size)}</span>
                  </button>
                ))}
              {data.dirs.length === 0 && (kind !== "file" || files.length === 0) && (
                <div className="text-xs text-ink-500 px-2 py-2">
                  {kind === "file" ? "No matching files in this folder." : "No subfolders here."}
                </div>
              )}
            </>
          )}
        </div>

        {kind === "folder" && data && !err && (
          <div className="px-4 py-3 border-t border-ink-800 flex items-center justify-between gap-3">
            <span className="text-[11px] text-ink-400">
              {files.length} audio file{files.length === 1 ? "" : "s"} in this folder
            </span>
            <button className="btn-primary" onClick={() => onPick(data.abs)}>
              Use this folder
            </button>
          </div>
        )}
      </div>
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
      const res = await fetch(api.missingCompilationUrl(mode, CONTEXT_PAD_S, tolS));
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
        Every missing line taken from the original (±{CONTEXT_PAD_S}s context, so cuts aren't abrupt). Build a short listen-through, or a
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
      : iss.kind === "grouped"
      ? "text-ink-300 border-ink-600/40 bg-ink-700/20"
      : "text-sky-300 border-sky-400/30 bg-sky-400/5";
  const tag =
    iss.kind === "name_mismatch"
      ? "NAME ≠ VOICE"
      : iss.kind === "possible_match"
      ? "POSSIBLE MATCH"
      : iss.kind === "verified_absent"
      ? "NO AUDIO (verified)"
      : iss.kind === "grouped"
      ? "GROUPED (walla)"
      : iss.kind === "reassigned"
      ? "REASSIGNED"
      : iss.kind === "twin_merged"
      ? "TWIN MERGED"
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

function ErrorRow({ e, hasOriginal, compare }: { e: AlignError; hasOriginal?: boolean; compare?: boolean }) {
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
            <span className="font-medium text-ink-100">{e.character ?? (compare ? (e.type === "EXTRA" ? "dub" : "original") : "?")}</span>
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
            {listenHint(e, compare)}
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
              <div className="text-xs text-ink-300">
                {compare
                  ? e.type === "MISSING"
                    ? "The dub (combined tracks) is silent here — the original player below is the evidence; verify at this timestamp in an editor."
                    : "Combined-tracks mode has no single dub file to slice — use the original player and check the dub at this timestamp in an editor."
                  : "No audio track mapped for this line."}
              </div>
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
